"""Outbox: upload metadata, human-readable upload sheets and DB rows (component C6)."""

from __future__ import annotations

import random
import re
import zlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ns_common.atomic import copy_atomic, read_text_retry, write_text_atomic
from ns_common.models import Channel, ClipRef, Metadata, PolicyAdvice, PolicyResult, Seo
from ns_common.paths import Paths
from ns_studio.context import JobContext
from ns_studio.media.master import load_shorts
from ns_studio.media.shotplan import Shot
from ns_studio.stages import Tenant

JITTER_MIN = 45
_JOB_DATE = re.compile(r"-(\d{8})-[0-9a-f]{4}$")
MARKER = ".nightshift-job"
_RESERVED = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                       *(f"lpt{i}" for i in range(1, 10))})
_BAD_CHARS = re.compile(r'[<>:"/\|?*\x00-\x1f]')


def safe_slug(text: str, max_len: int = 80) -> str:
    """Windows-safe, readable file or folder name of at most `max_len` characters (spec 15.2a).

    Reserved characters become spaces, whitespace is collapsed, leading and trailing dots and spaces are
    dropped, and a reserved device name (`con`, `nul`, `com1`, ...) gets a `_` suffix. Case is kept. Empty
    input gives an empty string, so callers can fall back to an id. C8 and C11 import this function.
    """
    s = _BAD_CHARS.sub(" ", text)
    s = re.sub(r"\s+", " ", s).strip(" .")
    if s.lower() in _RESERVED:
        s = f"{s}_"
    return s[:max_len].rstrip(" .")


def unique_dir(parent: Path, slug: str, job_id: str) -> Path:
    """`parent/slug` for this job, or `slug-2`, `slug-3`... when another job already owns the folder.

    Ownership is recorded in a marker file, so re-running a job reuses its own folder.
    """
    n = 1
    while True:
        suffix = "" if n == 1 else f"-{n}"
        d = parent / f"{slug[: 80 - len(suffix)]}{suffix}"
        marker = d / MARKER
        if not d.exists():
            d.mkdir(parents=True)
            marker.write_text(job_id, encoding="utf-8")
            return d
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == job_id:
            return d
        n += 1


def story_title(ctx: JobContext) -> str:
    """The story's title: master SEO title, else the first Short's SEO title, else the working title."""
    for name in ("seo_long.json", "seo.json"):
        if ctx.file(name).exists():
            data = ctx.read_json(name)
            first = data[0] if isinstance(data, list) else data
            if first.get("title"):
                return str(first["title"])
    if ctx.file("story.json").exists():
        story = ctx.read_json("story.json")
        if isinstance(story, dict) and story.get("working_title"):
            return str(story["working_title"])
    return ctx.job_id


MAX_PATH = 200  # longest Outbox path we create; Windows stops at 260 without LongPathsEnabled (NS-W112)
MIN_STEM = 16


def fit_stem(base: Path, fixed: int, wanted: int) -> int:
    """Longest name stem (at most `wanted`) that keeps the longest sheet path under `MAX_PATH`.

    The stem occurs twice in a path (folder and file name); `fixed` counts every other character below
    `base`. Never shorter than `MIN_STEM`, so a very deep home yields short but still readable names.
    """
    room = (MAX_PATH - len(str(base)) - fixed) // 2
    return max(MIN_STEM, min(wanted, room))


def outbox_target(ctx: JobContext, kind: str, k: int | None = None) -> Path:
    """The Outbox path of a video (spec §15.2a); creates its folder (and the story folder marker).

    - `long` -> `outbox/<channel>/stories/<title-slug>/full/<title>.mp4`
    - `short` -> `.../stories/<title-slug>/shorts/<title> - Short 01.mp4`
    - with `series_context.json` {series_slug, part_n, series_title}:
      `outbox/<channel>/series/<series-slug>/part-NN/{full|shorts}/<series-title> - Part NN[ - Short KK].mp4`

    The `.json` sheet, `.txt` sheet and thumbnails sit next to the mp4. Story folders get a `-2` suffix when
    another job already used the slug.
    """
    base = ctx.paths.outbox / ctx.channel.slug
    if ctx.file("series_context.json").exists():
        sc = ctx.read_json("series_context.json")
        part = int(sc["part_n"])
        fit = fit_stem(base, 50, 80)  # <base>/series/<slug>/part-NN/shorts/<title> - Part NN - Short KK.json
        series_title = safe_slug(str(sc["series_title"]), fit)
        stem = f"{series_title} - Part {part:02d}"
        root = base / "series" / safe_slug(str(sc["series_slug"]), fit) / f"part-{part:02d}"
    else:
        title = story_title(ctx)
        # <base>/stories/<title>/shorts/<title> - Short KK.json: the title is in the path twice
        stem = safe_slug(title, fit_stem(base, 33, 80)) or ctx.job_id
        root = unique_dir(base / "stories", stem, ctx.job_id)
    folder = root / ("full" if kind == "long" else "shorts")
    folder.mkdir(parents=True, exist_ok=True)
    name = stem if kind == "long" else f"{stem} - Short {int(k or 1):02d}"
    return folder / f"{name}.mp4"


def rejected_path(paths: Paths, outbox_file: Path) -> Path:
    """Where a rejected Outbox file goes: `rejected/` mirrors the Outbox tree (spec §15.2a)."""
    return paths.rejected / outbox_file.relative_to(paths.outbox)


def job_date(job_id: str) -> datetime:
    """The date embedded in a job id (`<slug>-YYYYMMDD-<4hex>`) as a midnight datetime."""
    m = _JOB_DATE.search(job_id)
    if not m:
        raise ValueError(f"job id without date: {job_id!r}")
    return datetime.strptime(m.group(1), "%Y%m%d")


def series_delay_days(ctx: JobContext) -> int:
    """Days a series part waits before its first publish (the design's cadence times the part's place)."""
    return max(0, int(ctx.series.get("publish_not_before_days", 0) or 0))


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


def zone_label(publish_after: str) -> str:
    """Time zone of a local `publish_after` stamp, like `Egypt Standard Time, UTC+02:00` (DST-aware).

    The sheets carry local wall-clock times; a reviewer in another zone needs to know which zone they are in.
    Empty when the stamp cannot be read.
    """
    try:
        when = datetime.fromisoformat(publish_after)
    except ValueError:
        return ""
    local = when.astimezone() if when.tzinfo is None else when
    offset = local.utcoffset()
    if offset is None:
        return ""
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    utc = f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
    name = local.tzname()
    return f"{name}, {utc}" if name and not name.startswith("UTC") else utc


def publish_line(publish_after: str) -> str:
    """`PUBLISH AFTER (local time): 2026-11-02 18:05 (<zone>)` as written in every upload sheet."""
    zone = zone_label(publish_after)
    suffix = f" ({zone})" if zone else ""
    return f"PUBLISH AFTER (local time): {publish_after.replace('T', ' ')}{suffix}"


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
        publish_line(meta.publish_after),
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
    write_text_atomic(path, sheet_text(meta, video_name, related_title=related_title), newline=None)
    return path


def copy_if_changed(src: Path, dest: Path) -> Path:
    """Copy `src` to `dest` unless an identical-size file is already there."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists() or dest.stat().st_size != src.stat().st_size:
        copy_atomic(src, dest)  # the finished video appears whole: a watcher never meets half of it
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


RELATED_PREFIX = "Set Related video -> "


def retime_sheet(entry: Mapping[str, str], publish_at: str) -> Metadata | None:
    """Set `publish_after` in the `.json` sheet of one outbox manifest entry and rewrite its `.txt` sheet.

    `entry` is a value of the job's `outbox/manifest.json` (`mp4`, `json`, `txt` paths). Returns the new
    metadata, or None when the sheet is missing or unreadable (nothing is changed then).
    """
    sheet = Path(entry["json"])
    try:
        meta = Metadata.model_validate_json(read_text_retry(sheet))
    except (OSError, ValueError):
        return None
    meta = meta.model_copy(update={"publish_after": publish_at})
    related = (meta.related_video_instruction or "").removeprefix(RELATED_PREFIX) or None
    write_text_atomic(sheet, meta.model_dump_json(indent=2) + "\n", newline=None)
    write_sheet_txt(meta, Path(entry["mp4"]).name, Path(entry["txt"]), related_title=related)
    return meta


class OutboxStage:
    """Stage `outbox`: copy each QC-passed Short to the Outbox with its `.json` metadata and `.txt` sheet.

    Layout: see `outbox_target` (`outbox/<channel>/stories/<title>/shorts/...`). Also writes the `outputs` and
    `clip_usage` rows and `outbox/manifest.json` in the job directory.
    """

    name: str = "outbox"
    tenant: Tenant = None
    inputs: tuple[str, ...] = (
        "qc/report.json", "seo.json", "seo_long.json", "seo_shorts.json", "short_combos.json",
    )
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
                prompt_versions=ctx.versions(),
                publish_at=publish_after(
                    ctx.channel, ctx.job_id, item, day_offset=short_day_offset(ctx.channel, k)
                ),
            )
            mp4 = copy_if_changed(Path(info["path"]), outbox_target(ctx, "short", k))
            write_text_atomic(mp4.with_suffix(".json"), meta.model_dump_json(indent=2) + "\n", newline=None)
            write_sheet_txt(meta, mp4.name, mp4.with_suffix(".txt"))
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

    meta = build_metadata(
        channel=ctx.channel, job_id=ctx.job_id, kind="long", k=None, seo=long_seo, advice=advice,
        shots=long_shots, combo_id=ctx.combo.combo_id, prompt_versions=ctx.versions(),
        publish_at=publish_after(ctx.channel, ctx.job_id, "long", day_offset=1 + series_delay_days(ctx)),
        extra={
            "chapters": [{"timestamp": c["timestamp"], "title": c["title"], "start_s": c["start_s"]}
                         for c in chapters],
            "midroll_suggestions_s": chapters_doc.get("midroll_suggestions_s", []),
            "thumbnails": thumbs,
        },
    )
    mp4 = copy_if_changed(Path(long_info["path"]), outbox_target(ctx, "long"))
    for t in thumbs:
        copy_if_changed(ctx.file(f"long/{t}"), mp4.parent / t)
    write_text_atomic(mp4.with_suffix(".json"), meta.model_dump_json(indent=2) + "\n", newline=None)
    write_sheet_txt(meta, mp4.name, mp4.with_suffix(".txt"))
    record_output(ctx, kind="long", k=None, item="long", meta=meta, path=mp4,
                  duration_s=float(long_info["duration_s"]), shots=long_shots)
    manifest["long"] = {"mp4": str(mp4), "json": str(mp4.with_suffix(".json")),
                        "txt": str(mp4.with_suffix(".txt"))}

    has_report = ctx.file("render/report.json").exists()
    rendered: dict[str, Any] = ctx.read_json("render/report.json") if has_report else {}
    shorts_shots: dict[str, Any] = (
        ctx.read_json("clips/shorts_shots.json") if ctx.file("clips/shorts_shots.json").exists() else {}
    )
    seos = ctx.read_json("seo_shorts.json") if ctx.file("seo_shorts.json").exists() else []
    specs = {s.k: s for s in load_shorts(ctx)}
    for item, info in rendered.items():
        k = int(item[1:])
        shots = [Shot.from_json(s) for s in shorts_shots[item]["shots"]]
        spec = specs.get(k)
        base = seo_from(seos[min(k, len(seos)) - 1])
        seo = base.model_copy(update={"description": short_description(base.description, long_seo.title)})
        smeta = build_metadata(
            channel=ctx.channel, job_id=ctx.job_id, kind="short", k=k, seo=seo, advice=advice, shots=shots,
            combo_id=(spec.combo_id if spec else None) or ctx.combo.combo_id,
            prompt_versions=ctx.versions(),
            publish_at=publish_after(
                ctx.channel, ctx.job_id, item,
                day_offset=1 + short_day_offset(ctx.channel, k) + series_delay_days(ctx),
            ),
            extra={"related_video_instruction": f"Set Related video -> {long_seo.title}",
                   "parent_job_id": ctx.job_id},
        )
        smp4 = copy_if_changed(Path(info["path"]), outbox_target(ctx, "short", k))
        write_text_atomic(smp4.with_suffix(".json"), smeta.model_dump_json(indent=2) + "\n", newline=None)
        write_sheet_txt(smeta, smp4.name, smp4.with_suffix(".txt"), related_title=long_seo.title)
        record_output(ctx, kind="short", k=k, item=item, meta=smeta, path=smp4,
                      duration_s=float(info["duration_s"]), shots=shots)
        manifest[item] = {"mp4": str(smp4), "json": str(smp4.with_suffix(".json")),
                          "txt": str(smp4.with_suffix(".txt"))}
    ctx.write_json("outbox/manifest.json", manifest)
    ctx.log.info("outbox_done", job_id=ctx.job_id, items=list(manifest))
