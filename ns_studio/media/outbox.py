"""Outbox: upload metadata, human-readable upload sheets and DB rows (component C6)."""

from __future__ import annotations

import random
import re
import shutil
import zlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ns_common.models import Channel, ClipRef, Metadata, PolicyAdvice, PolicyResult, Seo
from ns_studio.context import JobContext
from ns_studio.media.shotplan import Shot
from ns_studio.stages import Tenant

JITTER_MIN = 45
_JOB_DATE = re.compile(r"-(\d{8})-[0-9a-f]{4}$")


def job_date(job_id: str) -> datetime:
    """The date embedded in a job id (`<slug>-YYYYMMDD-<4hex>`) as a midnight datetime."""
    m = _JOB_DATE.search(job_id)
    if not m:
        raise ValueError(f"job id without date: {job_id!r}")
    return datetime.strptime(m.group(1), "%Y%m%d")


def publish_after(channel: Channel, job_id: str, key: str, *, day_offset: int = 1) -> str:
    """Local ISO timestamp: `day_offset` days after the job date at the publish window start, +-45 min.

    The jitter is seeded by job id and `key` (e.g. `s1`, `long`), so re-runs give the same value.
    """
    start = channel.publish_window.split("-")[0]
    hh, mm = (int(x) for x in start.split(":"))
    rng = random.Random(zlib.crc32(f"{job_id}|{key}|publish".encode()))
    jitter = rng.randint(-JITTER_MIN, JITTER_MIN)
    when = job_date(job_id) + timedelta(days=day_offset, hours=hh, minutes=mm + jitter)
    return when.isoformat(timespec="minutes")


def short_day_offset(channel: Channel, k: int) -> int:
    """Day offset for the k-th Short (1-based): `shorts_per_day_max` Shorts per day, starting tomorrow."""
    return (k - 1) // max(1, channel.shorts_per_day_max) + 1


def clip_refs(shots: Sequence[Shot]) -> list[ClipRef]:
    """Distinct clips used by `shots` in first-use order."""
    seen: dict[tuple[str, str], ClipRef] = {}
    for s in shots:
        seen.setdefault(
            (s.source, s.clip_id),
            ClipRef.model_validate(
                {"source": s.source, "id": s.clip_id, "url": s.url, "license": s.license}
            ),
        )
    return list(seen.values())


def load_advice(ctx: JobContext) -> PolicyAdvice:
    """Disclosure advice from the final (else text) policy result; defaults when no gate has run."""
    for name in ("policy_final.json", "policy_text.json"):
        if ctx.file(name).exists():
            return PolicyResult.model_validate(ctx.read_json(name)).advice
    return PolicyAdvice(made_for_kids=ctx.channel.made_for_kids)


def build_metadata(
    *,
    channel: Channel,
    job_id: str,
    kind: str,
    k: int | None,
    seo: Seo,
    advice: PolicyAdvice,
    shots: Sequence[Shot],
    combo_id: str,
    prompt_versions: dict[str, int],
    publish_at: str,
    extra: dict[str, Any] | None = None,
) -> Metadata:
    """Assemble and validate the upload metadata record (schema `metadata`)."""
    data: dict[str, Any] = {
        "channel": channel.slug,
        "job_id": job_id,
        "kind": kind,
        "k": k,
        "title": seo.title,
        "description": seo.description,
        "tags": seo.tags,
        "hashtags": seo.hashtags,
        "publish_after": publish_at,
        "ai_disclosure": advice.ai_disclosure,
        "made_for_kids": advice.made_for_kids or channel.made_for_kids,
        "age_restrict_suggested": advice.age_restrict_suggested,
        "clips": [c.model_dump() for c in clip_refs(shots)],
        "combo_id": combo_id,
        "prompt_versions": prompt_versions,
    }
    data.update(extra or {})
    return Metadata.model_validate(data)


def _yes_no(flag: bool) -> str:
    return "Yes" if flag else "No"


def sheet_text(meta: Metadata, video_name: str, *, related_title: str | None = None) -> str:
    """The plain-text upload sheet: everything to paste into YouTube Studio, in order."""
    lines = [
        f"FILE: {video_name}",
        f"TITLE: {meta.title}",
        "",
        "DESCRIPTION:",
        meta.description.rstrip(),
        "",
        " ".join(meta.hashtags),
        "",
        "TAGS: " + ", ".join(meta.tags),
        "",
        f"PUBLISH AFTER (local time): {meta.publish_after.replace('T', ' ')}",
        "",
        "SETTINGS:",
        f"  Altered or synthetic content: {meta.ai_disclosure.capitalize()}",
        f"  Made for kids: {_yes_no(meta.made_for_kids)}",
        f"  Age restriction suggested: {_yes_no(meta.age_restrict_suggested)}",
    ]
    if related_title:
        lines += ["", f"Related video: set Related video -> {related_title}"]
    if meta.midroll_suggestions_s:
        marks = ", ".join(f"{int(t // 60)}:{int(t % 60):02d}" for t in meta.midroll_suggestions_s)
        lines += ["", f"MID-ROLL SUGGESTIONS (chapter breaks): {marks}"]
    return "\n".join(lines) + "\n"


def write_sheet_txt(
    meta: Metadata, video_name: str, path: Path, *, related_title: str | None = None
) -> Path:
    """Write the upload sheet next to the video."""
    path.write_text(sheet_text(meta, video_name, related_title=related_title), encoding="utf-8")
    return path


def copy_if_changed(src: Path, dest: Path) -> Path:
    """Copy `src` to `dest` unless an identical-size file is already there."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists() or dest.stat().st_size != src.stat().st_size:
        shutil.copy2(src, dest)
    return dest


def record_output(
    ctx: JobContext,
    *,
    kind: str,
    k: int | None,
    item: str,
    meta: Metadata,
    path: Path,
    duration_s: float,
    shots: Sequence[Shot],
) -> None:
    """Upsert the `outputs` row (status preserved on re-run) and replace this item's `clip_usage` rows."""
    now = datetime.now(UTC).isoformat()
    ctx.db.execute(
        "insert into outputs(output_id, job_id, channel, kind, k, title, path, duration_s, combo_id, status,"
        " created_at) values (?,?,?,?,?,?,?,?,?,'pending',?) on conflict(output_id) do update set"
        " title=excluded.title, path=excluded.path, duration_s=excluded.duration_s,"
        " combo_id=excluded.combo_id",
        (f"{ctx.job_id}/{item}", ctx.job_id, ctx.channel.slug, kind, k, meta.title, str(path), duration_s,
         meta.combo_id, now),
    )
    ctx.db.execute("delete from clip_usage where job_id=? and item=?", (ctx.job_id, item))
    for c in meta.clips:
        ctx.db.execute(
            "insert into clip_usage(source, clip_id, channel, job_id, item, used_at) values (?,?,?,?,?,?)",
            (c.source, c.id, ctx.channel.slug, ctx.job_id, item, now),
        )


class OutboxStage:
    """Stage `outbox`: copy each QC-passed Short to the Outbox with its `.json` metadata and `.txt` sheet.

    Layout: `outbox/<slug>/<YYYY-MM-DD>/<job_id>/shorts/s<k>.{mp4,json,txt}`. Also writes the `outputs` and
    `clip_usage` rows and `outbox/manifest.json` in the job directory.
    """

    name: str = "outbox"
    tenant: Tenant = None
    inputs: tuple[str, ...] = ("qc/report.json", "seo.json", "seo_long.json", "seo_shorts.json")
    outputs: tuple[str, ...] = ("outbox/manifest.json",)

    def run(self, ctx: JobContext) -> None:
        """Publish every item listed in `render/report.json` (master and Shorts in master-first jobs)."""
        if ctx.fmt == "master_first":
            run_outbox_master(ctx)
            return
        rendered: dict[str, Any] = ctx.read_json("render/report.json")
        shots_all: dict[str, Any] = ctx.read_json("clips/shots.json")
        seos = [Seo.model_validate(s) for s in ctx.read_json("seo.json")]
        advice = load_advice(ctx)
        date = job_date(ctx.job_id).strftime("%Y-%m-%d")
        dest_dir = ctx.paths.outbox_dir(ctx.channel.slug, date, ctx.job_id) / "shorts"
        manifest: dict[str, Any] = {}
        for item, info in rendered.items():
            k = int(item[1:])
            shots = [Shot.from_json(s) for s in shots_all[item]["shots"]]
            meta = build_metadata(
                channel=ctx.channel,
                job_id=ctx.job_id,
                kind="short",
                k=k,
                seo=seos[min(k, len(seos)) - 1],
                advice=advice,
                shots=shots,
                combo_id=ctx.combo.combo_id,
                prompt_versions=ctx.prompt_versions,
                publish_at=publish_after(
                    ctx.channel, ctx.job_id, item, day_offset=short_day_offset(ctx.channel, k)
                ),
            )
            mp4 = copy_if_changed(Path(info["path"]), dest_dir / f"{item}.mp4")
            (dest_dir / f"{item}.json").write_text(
                meta.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            write_sheet_txt(meta, mp4.name, dest_dir / f"{item}.txt")
            record_output(
                ctx, kind="short", k=k, item=item, meta=meta, path=mp4,
                duration_s=float(info["duration_s"]), shots=shots,
            )
            manifest[item] = {"mp4": str(mp4), "json": str(mp4.with_suffix(".json")),
                              "txt": str(mp4.with_suffix(".txt"))}
        ctx.write_json("outbox/manifest.json", manifest)
        ctx.log.info("outbox_done", job_id=ctx.job_id, items=list(manifest))


# ---------------------------------------------------------------------------------------------------------
# Master-first Outbox (M2): long/ + shorts/
# ---------------------------------------------------------------------------------------------------------

CHAPTERS_PLACEHOLDER = "[[CHAPTERS]]"


def seo_from(d: dict[str, Any]) -> Seo:
    """A `Seo` from a SEO document that may carry extra fields (chapter titles, thumbnail text)."""
    return Seo(
        title=str(d["title"])[:100],
        description=str(d["description"]),
        tags=list(d["tags"]),
        hashtags=list(d.get("hashtags", []))[:5],
        keywords=list(d.get("keywords", [])),
    )


def chapter_list(chapters: Sequence[dict[str, Any]]) -> str:
    """`MM:SS Title` lines; the first entry is `00:00` (spec §9.1)."""
    return "\n".join(f"{c['timestamp']} {c['title']}" for c in chapters)


def long_description(description: str, chapters: Sequence[dict[str, Any]]) -> str:
    """Master description (spec §9.1): summary lines, blank, chapter list, then the rest.

    The `[[CHAPTERS]]` placeholder is replaced; without one the list goes after the first paragraph.
    """
    block = chapter_list(chapters)
    if CHAPTERS_PLACEHOLDER in description:
        return description.replace(CHAPTERS_PLACEHOLDER, block)
    head, sep, rest = description.partition("\n\n")
    return f"{head}\n\n{block}" + (f"\n\n{rest}" if sep else "")


def short_description(description: str, master_title: str) -> str:
    """Short description (spec §9.2): adds "From the full story: <master title>" before the fiction line."""
    line = f"From the full story: {master_title}"
    if line in description:
        return description
    lines = description.rstrip().split("\n")
    at = next((i for i, ln in enumerate(lines) if ln.strip() == "Original fiction."), len(lines))
    lines.insert(at, line)
    return "\n".join(lines)


def run_outbox_master(ctx: JobContext) -> None:
    """Publish the master (`long/`) and its derived Shorts (`shorts/`) with metadata and upload sheets."""
    advice = load_advice(ctx)
    date = job_date(ctx.job_id).strftime("%Y-%m-%d")
    root = ctx.paths.outbox_dir(ctx.channel.slug, date, ctx.job_id)
    chapters_doc: dict[str, Any] = ctx.read_json("long/chapters.json")
    chapters = chapters_doc["chapters"]
    long_info = ctx.read_json("render/long.json")
    long_shots = [Shot.from_json(s) for s in ctx.read_json("clips/shots.json")["long"]["shots"]]
    long_seo = seo_from(ctx.read_json("seo_long.json"))
    long_seo = long_seo.model_copy(update={
        "description": long_description(long_seo.description, chapters),
        "hashtags": [h for h in long_seo.hashtags if h.lower() != "#shorts"][:5],
    })
    thumbs = [f"thumb_{i}.jpg" for i in (1, 2, 3) if ctx.file(f"long/thumb_{i}.jpg").exists()]
    manifest: dict[str, Any] = {}

    long_dir = root / "long"
    meta = build_metadata(
        channel=ctx.channel, job_id=ctx.job_id, kind="long", k=None, seo=long_seo, advice=advice,
        shots=long_shots, combo_id=ctx.combo.combo_id, prompt_versions=ctx.prompt_versions,
        publish_at=publish_after(ctx.channel, ctx.job_id, "long", day_offset=1),
        extra={
            "chapters": [{"timestamp": c["timestamp"], "title": c["title"], "start_s": c["start_s"]}
                         for c in chapters],
            "midroll_suggestions_s": chapters_doc.get("midroll_suggestions_s", []),
            "thumbnails": thumbs,
        },
    )
    mp4 = copy_if_changed(Path(long_info["path"]), long_dir / "master.mp4")
    for t in thumbs:
        copy_if_changed(ctx.file(f"long/{t}"), long_dir / t)
    (long_dir / "master.json").write_text(meta.model_dump_json(indent=2) + "\n", encoding="utf-8")
    write_sheet_txt(meta, mp4.name, long_dir / "master.txt")
    record_output(ctx, kind="long", k=None, item="long", meta=meta, path=mp4,
                  duration_s=float(long_info["duration_s"]), shots=long_shots)
    manifest["long"] = {
        "mp4": str(mp4), "json": str(long_dir / "master.json"), "txt": str(long_dir / "master.txt"),
    }

    shorts_dir = root / "shorts"
    has_report = ctx.file("render/report.json").exists()
    rendered: dict[str, Any] = ctx.read_json("render/report.json") if has_report else {}
    shorts_shots: dict[str, Any] = (
        ctx.read_json("clips/shorts_shots.json") if ctx.file("clips/shorts_shots.json").exists() else {}
    )
    seos = ctx.read_json("seo_shorts.json") if ctx.file("seo_shorts.json").exists() else []
    for item, info in rendered.items():
        k = int(item[1:])
        shots = [Shot.from_json(s) for s in shorts_shots[item]["shots"]]
        base = seo_from(seos[min(k, len(seos)) - 1])
        seo = base.model_copy(update={"description": short_description(base.description, long_seo.title)})
        smeta = build_metadata(
            channel=ctx.channel, job_id=ctx.job_id, kind="short", k=k, seo=seo, advice=advice, shots=shots,
            combo_id=ctx.combo.combo_id, prompt_versions=ctx.prompt_versions,
            publish_at=publish_after(
                ctx.channel, ctx.job_id, item, day_offset=1 + short_day_offset(ctx.channel, k)
            ),
            extra={"related_video_instruction": f"Set Related video -> {long_seo.title}",
                   "parent_job_id": ctx.job_id},
        )
        smp4 = copy_if_changed(Path(info["path"]), shorts_dir / f"{item}.mp4")
        (shorts_dir / f"{item}.json").write_text(smeta.model_dump_json(indent=2) + "\n", encoding="utf-8")
        write_sheet_txt(smeta, smp4.name, shorts_dir / f"{item}.txt", related_title=long_seo.title)
        record_output(ctx, kind="short", k=k, item=item, meta=smeta, path=smp4,
                      duration_s=float(info["duration_s"]), shots=shots)
        manifest[item] = {"mp4": str(smp4), "json": str(smp4.with_suffix(".json")),
                          "txt": str(smp4.with_suffix(".txt"))}
    ctx.write_json("outbox/manifest.json", manifest)
    ctx.log.info("outbox_done", job_id=ctx.job_id, items=list(manifest))
