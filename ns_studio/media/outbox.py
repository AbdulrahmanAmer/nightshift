"""Outbox: upload metadata, human-readable upload sheets and DB rows (component C6)."""

from __future__ import annotations

import json
import random
import re
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ns_common.atomic import (
    copy_atomic,
    read_text_retry,
    sweep_stale_tmp,
    write_bytes_atomic,
    write_text_atomic,
)
from ns_common.logging import NsLogger
from ns_common.models import Channel, ClipRef, Metadata, PolicyAdvice, PolicyResult, Seo
from ns_common.paths import Paths
from ns_studio.context import JobContext
from ns_studio.media import sheet_read as sr
from ns_studio.media import youtube_rules as yr
from ns_studio.media.master import load_shorts
from ns_studio.media.sheet_read import read_sheet_file
from ns_studio.media.shotplan import Shot
from ns_studio.media.youtube_rules import Notes
from ns_studio.stages import Tenant
from ns_studio.text.story_profile import job_mark

JITTER_MIN = 45
_JOB_DATE = re.compile(r"-(\d{8})-[0-9a-f]{4}$")
MARKER = ".nightshift-job"
#: The Outbox layout in words, as `outbox_target` and the clip outbox write it (spec 15.2a). The Assistant's
#: folder map (`ns_dashboard.assistant.orientation`) shows this text, so it cannot drift from the code (G-40).
OUTBOX_LAYOUT = (
    "<channel>/stories/<story title>/full/ (the full video) and shorts/ (the Shorts), each with its upload "
    "sheet (.json and .txt); a series part: <channel>/series/<series>/part-NN/; clips from your own video: "
    "<channel>/clips/<source name>/"
)
#: What `rejected` holds: a rejected video's files at the same path as in the Outbox.
REJECTED_LAYOUT = "videos you rejected, at the same path as in the Outbox (rejected/<channel>/stories/...)"
#: What `removed` holds: what you removed, with a record of each.
REMOVED_LAYOUT = "what you removed (voices, ideas, stories, footage), each with a record.json"
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
    """Disclosure advice from the final (else text) policy result; defaults when no gate has run.

    The altered-content answer is Yes for a job narrated by a cloned voice whatever the gate wrote (R8,
    `youtube_rules.disclosure`): a gate file of an earlier release, or no gate at all (policy off)."""
    advice = PolicyAdvice(made_for_kids=ctx.channel.made_for_kids)
    for name in ("policy_final.json", "policy_text.json"):
        if ctx.file(name).exists():
            advice = PolicyResult.model_validate(ctx.read_json(name)).advice
            break
    answer = yr.disclosure(advice.ai_disclosure, yr.job_voice_mode(ctx)).stored
    return advice.model_copy(update={"ai_disclosure": answer})


def working_title(ctx: JobContext) -> str:
    """The story's working title (used when the SEO title is empty once cleaned), else the job id."""
    return working_title_of(ctx.job_dir, ctx.job_id)


def working_title_of(job_dir: Path, job_id: str) -> str:
    """`working_title` from a job folder: `story.json`, else the owner's script title, else the job id."""
    for name, key in (("story.json", "working_title"), ("script_source.json", "title")):
        try:
            data = json.loads(read_text_retry(job_dir / name))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and str(data.get(key) or "").strip():
            return str(data[key])
    return job_id


@dataclass
class Sheet:
    """An upload sheet: its validated `.json` record and the notes its `.txt` tells the owner."""

    meta: Metadata
    notes: Notes


def build_sheet(
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
    fallback_title: str = "",
    duration_s: float | None = None,
    voice_mode: str | None = None,
    changes: Sequence[str] = (),
) -> Sheet:
    """Assemble the upload record with every field fitted to YouTube's rules (`youtube_rules.fit`).

    `duration_s` of a Short is checked against R7; `voice_mode` gives the altered-content answer its reason
    (R8); `changes` are lines the caller already made for YouTube (a chapter list left out)."""
    f = yr.fit(
        title=seo.title, description=seo.description, tags=seo.tags, hashtags=seo.hashtags,
        fallback_title=fallback_title or job_id, short_seconds=duration_s if kind != "long" else None,
    )
    told = yr.disclosure(advice.ai_disclosure, voice_mode)
    data: dict[str, Any] = {
        "channel": channel.slug,
        "job_id": job_id,
        "kind": kind,
        "k": k,
        "title": f.title,
        "description": f.description,
        "tags": f.tags,
        "hashtags": f.hashtags,
        "publish_after": publish_at,
        "ai_disclosure": told.stored,
        "made_for_kids": advice.made_for_kids or channel.made_for_kids,
        "age_restrict_suggested": advice.age_restrict_suggested,
        "clips": [c.model_dump() for c in clip_refs(shots)],
        "combo_id": combo_id,
        "prompt_versions": prompt_versions,
    }
    data.update(extra or {})
    notes = yr.notes_for(told, f.warnings, [*changes, *f.changes])
    return Sheet(Metadata.model_validate(data), notes)


def build_metadata(**kw: Any) -> Metadata:
    """The upload metadata record (schema `metadata`), fitted to YouTube's rules; see `build_sheet`."""
    return build_sheet(**kw).meta


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


TEST_BANNER = (
    "TEST OUTPUT ({mark}): made on an engine-test install with a tiny model, a short length and "
    "placeholder footage. Do not upload."
)


def sheet_text(
    meta: Metadata, video_name: str, *, related_title: str | None = None, notes: Notes | None = None
) -> str:
    """The plain-text upload sheet: everything to paste into YouTube Studio, in order.

    The sheet of a video made on an engine-test install (`meta.profile`) opens with a line that says so.
    Then come the `notes`: a warning per problem YouTube would not accept that has no safe repair, and one
    "Changed for YouTube" line naming every change made to fit its rules. The altered-content line gives its
    reason, and the note below it says what is left to the owner's judgement. A video with Pexels or Pixabay
    clips gets the optional credit line (`youtube_rules.credit_line`: asked for, not required)."""
    notes = notes or Notes()
    lines = [TEST_BANNER.format(mark=meta.profile), ""] if meta.profile else []
    lines += notes.head_lines()
    lines += [
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
        *yr.disclosure_lines(meta.ai_disclosure, notes),
        f"  Made for kids: {_yes_no(meta.made_for_kids)}",
        f"  Age restriction suggested: {_yes_no(meta.age_restrict_suggested)}",
    ]
    # an offer (card stock-truth), never a field, counted against R3 with the description and hashtags
    credit = yr.credit_line([c.source for c in meta.clips], meta.description, meta.hashtags)
    if credit:
        lines += ["", credit]
    if related_title:
        lines += ["", f"Related video: set Related video -> {related_title}"]
    if meta.midroll_suggestions_s:
        marks = ", ".join(f"{int(t // 60)}:{int(t % 60):02d}" for t in meta.midroll_suggestions_s)
        lines += ["", f"MID-ROLL SUGGESTIONS (chapter breaks): {marks}"]
    return "\n".join(lines) + "\n"


def write_sheet_txt(
    meta: Metadata, video_name: str, path: Path, *, related_title: str | None = None,
    notes: Notes | None = None,
) -> Path:
    """Write the upload sheet next to the video."""
    text = sheet_text(meta, video_name, related_title=related_title, notes=notes)
    write_text_atomic(path, text, newline=None)
    return path


def copy_if_changed(src: Path, dest: Path) -> Path:
    """Copy `src` to `dest` unless an identical-size file is already there."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    sweep_stale_tmp(dest.parent)  # a power cut during an earlier copy left a video-sized temporary file
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
    profile: str | None = None,
) -> None:
    """Upsert the `outputs` row (status preserved on re-run) and replace this item's `clip_usage` rows.

    `profile` is `tiny_story` for a video made on an engine-test install and NULL for every other video: the
    publish helpers and the Queue read it from the row."""
    now = datetime.now(UTC).isoformat()
    ctx.db.execute(
        "insert into outputs(output_id, job_id, channel, kind, k, title, path, duration_s, combo_id, status,"
        " profile, created_at) values (?,?,?,?,?,?,?,?,?,'pending',?,?) on conflict(output_id) do update set"
        " title=excluded.title, path=excluded.path, duration_s=excluded.duration_s,"
        " combo_id=excluded.combo_id, profile=excluded.profile",
        (f"{ctx.job_id}/{item}", ctx.job_id, ctx.channel.slug, kind, k, meta.title, str(path), duration_s,
         meta.combo_id, profile, now),
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
    j = read_sheet_file(sheet, "json")
    if not j.usable:  # the start sweep and the Queue deal with a sheet that is not whole
        return None
    t = read_sheet_file(Path(entry["txt"]), "txt")
    if t.state in (sr.HELD, sr.OS_ERROR):  # its notes cannot be read now: retimed at the next night
        return None
    before: Any = j.value
    name = Path(entry["mp4"]).name
    notes: Notes
    moved = before.model_copy(update={"publish_after": publish_at})
    meta, notes = refit(moved, yr.read_notes(t.text), fallback_title=before.job_id)
    if t.state != sr.MISSING:  # the owner's own text (an edit, another layout or encoding) is kept first
        ours = {yr.bare_sheet(render_sheet(m, name)) for m in (before, meta)}
        if t.state != sr.OK or yr.bare_sheet(t.text) not in ours:
            notes.kept = keep_copy(Path(entry["txt"]), t.raw)
    related = (meta.related_video_instruction or "").removeprefix(RELATED_PREFIX) or None
    write_text_atomic(sheet, meta.model_dump_json(indent=2) + "\n", newline=None)
    write_sheet_txt(meta, name, Path(entry["txt"]), related_title=related, notes=notes)
    return meta


def refit(
    meta: Any, old: Notes, *, fallback_title: str, duration_s: float | None = None,
    told: yr.Disclosure | None = None,
) -> tuple[Any, Notes]:
    """A sheet already written, fitted to YouTube's rules again (`youtube_rules.fit`), with its notes.

    The notes it already carried (`old`, read from its `.txt`) are kept and the new changes added, so a
    rewrite never drops a line that told the owner what was changed. A chapter list that breaks R6 is taken
    out of the description. `told` replaces the altered-content answer and its reason (the start sweep)."""
    description, changes = meta.description, []
    chapters = getattr(meta, "chapters", None)
    why = yr.chapter_problem(chapters, duration_s) if chapters else None
    if why:
        description = yr.drop_chapter_lines(description)
        changes.append(f"left the chapter list out of the description: {why}")
    f = yr.fit(
        title=meta.title, description=description, tags=meta.tags, hashtags=meta.hashtags,
        fallback_title=fallback_title, short_seconds=duration_s if meta.kind != "long" else None,
    )
    update: dict[str, Any] = {
        "title": f.title, "description": f.description, "tags": f.tags, "hashtags": f.hashtags,
    }
    if why:
        update["chapters"] = None
    new = Notes(f.warnings, [*changes, *f.changes])
    if told is not None:
        update["ai_disclosure"] = told.stored
        new = yr.notes_for(told, f.warnings, [*changes, *f.changes])
    fitted = type(meta).model_validate({**meta.model_dump(), **update})
    return fitted, old.merged(new)


def disclosure_for(paths: Paths, job_id: str, kind: str, answer: str) -> yr.Disclosure:
    """The altered-content answer of a written output and its reason, from the job's own voice record
    (`youtube_rules.voice_mode_of`; never the channel's current voice): what the start sweep writes and the
    Queue shows."""
    return yr.disclosure(answer, yr.voice_mode_of(paths.job_dir(job_id)), kind=kind)


SHEET_RULES_FILE = "sheet-rules.json"
#: Starts in a row at which the sweep tries a sheet it cannot read, then stops and says so.
SWEEP_TRIES = 5


def sweep_sheets(paths: Paths, db: Any, log: NsLogger) -> int:
    """Dashboard start: the sheets of videos waiting to be posted, brought up to YouTube's rules once.

    Pending and approved outputs only (an uploaded video is on YouTube already, a rejected one is not going).
    Each sheet's two files are read by `sheet_read.read_sheet_file` and `sheet_read.plan_sheet` decides what
    happens (the table is there): fitted from its `.json`, rebuilt from its `.txt`, or left as it is with the
    owner's sentence. A sheet left as it is is NOT marked done: `data/sheet-rules.json` lists it, and the next
    start (at most `SWEEP_TRIES` starts in a row) and every opening of the video in the Queue try it again,
    with a WARN at each try. Each file is written atomically in the order kept copy, `.txt`, `.json`,
    `outputs.title`, and `data/sheet-rules.json` last, so a sweep killed half-way redoes only what still
    differs; a second run with nothing left to retry does nothing. Returns the outputs rewritten."""
    marker = paths.data / SHEET_RULES_FILE
    try:
        state = json.loads(read_text_retry(marker))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    same = state.get("rules_version") == yr.RULES_VERSION
    raw = state.get("retry")
    retry: dict[str, Any] = raw if same and isinstance(raw, dict) else {}
    if same and not retry:
        return 0
    rows = db.query(
        "SELECT output_id, job_id, channel, kind, k, path, title, duration_s, combo_id, profile FROM outputs"
        " WHERE status IN ('pending', 'approved') ORDER BY created_at, output_id"
    )
    if same:  # an earlier run of this version did the rest: only the sheets it left as they were
        rows = [r for r in rows if r["output_id"] in retry]
    rewritten = 0
    left: dict[str, int] = {}
    for row in rows:
        oid = str(row["output_id"])
        try:
            changed, sentence = refit_output(paths, db, row)
        except (OSError, ValueError) as e:  # a write that failed (a full disk): the same retry, its text said
            changed, sentence = 0, f"the sheet could not be written ({e})"
        rewritten += changed
        if sentence is None:
            continue
        tries = int(retry.get(oid, 0)) + 1
        if tries < SWEEP_TRIES:
            left[oid] = tries
            then = (f"Nightshift tries again at the next start (try {tries} of {SWEEP_TRIES}) and whenever "
                    "the video is opened in the Queue")
        else:
            then = (f"Nightshift stopped trying at its starts after {SWEEP_TRIES}; opening the video in the "
                    "Queue tries once more")
        log.warn("sheet_unreadable", f"the upload sheet of {oid} was left as it is: {sentence}. {then}",
                 output_id=oid, path=str(row["path"]), tries=tries)
    write_text_atomic(
        marker,
        json.dumps({"rules_version": yr.RULES_VERSION, "retry": left, "rewritten": rewritten,
                    "at": datetime.now(UTC).isoformat(timespec="seconds")}) + "\n",
    )
    log.info("sheets_fitted", f"upload sheets brought up to YouTube's rules: {rewritten}", count=rewritten)
    return rewritten


def render_sheet(meta: Any, video_name: str, notes: Notes | None = None) -> str:
    """The `.txt` text of a story or clip sheet (LF line ends)."""
    if meta.kind == "clip":
        from ns_studio.clipper.stages_clip import clip_sheet_text

        return clip_sheet_text(meta, video_name, notes=notes)
    related = (meta.related_video_instruction or "").removeprefix(RELATED_PREFIX) or None
    return sheet_text(meta, video_name, related_title=related, notes=notes)


def keep_copy(txt: Path, raw: bytes) -> str:
    """Keep the owner's `.txt` (its bytes as they are, whatever their encoding) as
    `<stem>.before-<version>.txt` next to it; returns that file's name. A copy already there with the same
    bytes is reused (a sweep killed after keeping it); one with other bytes is never overwritten: `-2`,
    `-3` is added."""
    from ns_common import __version__

    n = 1
    while True:
        dest = txt.with_name(f"{txt.stem}.before-{__version__}{'' if n == 1 else f'-{n}'}.txt")
        there = read_sheet_file(dest, "raw")
        if there.state == sr.MISSING:
            write_bytes_atomic(dest, raw)
            return dest.name
        if there.raw == raw:
            return dest.name
        n += 1


_SEO_FILES = {
    "long": ("seo_long.json",), "short": ("seo_shorts.json", "seo.json"), "clip": ("seo_clips.json",),
}


def seo_record(paths: Paths, row: Any) -> Path | None:
    """The job's own SEO file for this output's kind (what the job wrote), when the job folder has one."""
    job_dir = paths.job_dir(str(row["job_id"]))
    return next((job_dir / n for n in _SEO_FILES.get(str(row["kind"]), ()) if (job_dir / n).is_file()), None)


def rebuild_sheet(paths: Paths, db: Any, row: Any, f: dict[str, Any]) -> Any:
    """A `.json` sheet rebuilt from the job's own records: `f`, the fields the owner pastes, parsed from its
    `.txt` (`sheet_read.parse_sheet_text`: title, description, hashtags, tags, publish time, settings,
    related video, mid-roll marks, clip source), the footage it used from the database (`clip_usage` joined
    with `clips`), the combo and the test mark from its `outputs` row, the prompt versions from the job
    folder's `prompt_versions.json`, the chapters from `long/chapters.json` (a master that lists them) and the
    thumbnails next to the video."""
    from ns_studio.context import PROMPT_VERSIONS_FILE

    video = Path(str(row["path"]))
    job_id, kind = str(row["job_id"]), str(row["kind"])
    job_dir = paths.job_dir(job_id)
    try:
        saved = json.loads(read_text_retry(job_dir / PROMPT_VERSIONS_FILE))
        versions = {str(k): int(v) for k, v in saved.items()}
    except (OSError, ValueError, AttributeError):
        versions = {}
    data: dict[str, Any] = {
        "channel": row["channel"], "job_id": job_id, "k": row["k"], "title": f["title"],
        "description": f["description"], "tags": f["tags"], "hashtags": f["hashtags"],
        "publish_after": f["publish_after"], "ai_disclosure": f["ai_disclosure"],
        "made_for_kids": f["made_for_kids"], "age_restrict_suggested": f["age_restrict_suggested"],
        "combo_id": row["combo_id"] or "", "prompt_versions": versions,
    }
    if kind == "clip":
        from ns_studio.clipper.models import ClipMetadata

        source_video, template = f["source"] or (video.stem, "")
        return ClipMetadata.model_validate({**data, "source_video": source_video, "template": template})
    item = str(row["output_id"]).rsplit("/", 1)[-1]
    clips = db.query(
        "SELECT DISTINCT c.source AS source, c.clip_id AS id, c.url AS url, c.license AS license"
        " FROM clip_usage u JOIN clips c ON c.source = u.source AND c.clip_id = u.clip_id"
        " WHERE u.job_id = ? AND u.item = ?",
        (job_id, item),
    )
    chapters = None
    if kind == "long" and re.search(r"^\d{1,2}:\d{2}(?::\d{2})? ", f["description"], re.M):
        try:
            doc = json.loads(read_text_retry(job_dir / "long" / "chapters.json"))
            chapters = [{"timestamp": c["timestamp"], "title": c["title"], "start_s": c["start_s"]}
                        for c in doc["chapters"]]
        except (OSError, ValueError, KeyError, TypeError):
            chapters = None
    thumbs = sorted(p.name for p in video.parent.glob("thumb_*.jpg")) if kind == "long" else []
    data.update({
        "kind": kind, "clips": [dict(c) for c in clips], "chapters": chapters,
        "midroll_suggestions_s": f["midroll_suggestions_s"], "thumbnails": thumbs or None,
        "related_video_instruction": f"{RELATED_PREFIX}{f['related']}" if f["related"] else None,
        "parent_job_id": job_id if f["related"] else None,
    })
    if row["profile"]:
        data["profile"] = row["profile"]
    return Metadata.model_validate(data)


REBUILT = "rebuilt the .json sheet from the .txt and the job's records, because the .json could not be read"
_ENCODINGS = {"utf-16-le": "UTF-16", "utf-16-be": "UTF-16 (big-endian)", "ansi": "ANSI"}


def read_sheet(row: Any) -> tuple[sr.SheetRead, sr.SheetRead]:
    """The two files of an output's sheet, each read by the one reader."""
    video = Path(str(row["path"]))
    json_read = read_sheet_file(video.with_suffix(".json"), "json")
    return json_read, read_sheet_file(video.with_suffix(".txt"), "txt")


def refit_output(paths: Paths, db: Any, row: Any) -> tuple[int, str | None]:
    """One output's sheet brought up to YouTube's rules (the start sweep; the Queue when it opens a sheet
    that is not whole): (1 when its files were rewritten else 0, the owner's sentence when it was left as it
    is). What happens is `sheet_read.plan_sheet`'s answer for the two files as `read_sheet_file` found
    them."""
    video = Path(str(row["path"]))
    jpath, tpath = video.with_suffix(".json"), video.with_suffix(".txt")
    j, t = read_sheet(row)
    job_id = str(row["job_id"])
    plan = sr.plan_sheet(j, t, seo_record(paths, row), paths.job_dir(job_id))
    if plan.action in (sr.WAIT, sr.GONE):
        return 0, plan.sentence
    rebuilt = plan.action == sr.REBUILD
    meta = rebuild_sheet(paths, db, row, t.value) if rebuilt else j.value
    old_text = t.text if t.state != sr.MISSING else None
    told = disclosure_for(paths, job_id, str(row["kind"]), meta.ai_disclosure)
    fitted, notes = refit(
        meta, yr.read_notes(old_text), told=told, duration_s=row["duration_s"],
        fallback_title=working_title_of(paths.job_dir(job_id), job_id),
    )
    if rebuilt:
        notes.changes = list(dict.fromkeys([REBUILT, *notes.changes]))
    if t.state == sr.NOT_UTF8 and t.usable:
        notes.changes = list(dict.fromkeys(
            [f"re-saved {tpath.name} as UTF-8 (it was saved as {_ENCODINGS.get(t.encoding, t.encoding)})",
             *notes.changes]))
    if j.state == sr.NOT_UTF8 and j.usable:
        notes.changes = list(dict.fromkeys([f"re-saved {jpath.name} as UTF-8", *notes.changes]))
    text = render_sheet(fitted, video.name, notes)
    if old_text is not None:
        new_bare, old_bare = yr.bare_sheet(text), yr.bare_sheet(old_text)
        # Nightshift's own rendering of the .json it fits: its repairs are said on the change line. Anything
        # else (an edit, another layout, another encoding, a rebuild whose repair changes the owner's text)
        # is the owner's text and is kept as .before- before it is replaced.
        ours = not rebuilt and not plan.keep_txt and old_bare == yr.bare_sheet(render_sheet(meta, video.name))
        if plan.keep_txt or (old_bare != new_bare and not ours):
            notes.kept = keep_copy(tpath, t.raw)
            text = render_sheet(fitted, video.name, notes)
    changed = 0
    if text != old_text or t.state != sr.OK:
        write_text_atomic(tpath, text, newline=None)
        changed = 1
    if rebuilt or j.state != sr.OK or fitted.model_dump(mode="json") != meta.model_dump(mode="json"):
        write_text_atomic(jpath, fitted.model_dump_json(indent=2) + "\n", newline=None)
        changed = 1
    if row["title"] != fitted.title:
        db.execute("UPDATE outputs SET title = ? WHERE output_id = ?", (fitted.title, row["output_id"]))
    return changed, None


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
        voice, fallback = yr.job_voice_mode(ctx), working_title(ctx)
        mark = job_mark(ctx)
        manifest: dict[str, Any] = {}
        for item, info in rendered.items():
            k = int(item[1:])
            shots = [Shot.from_json(s) for s in shots_all[item]["shots"]]
            sheet = build_sheet(
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
                extra={"profile": mark} if mark else None,
                fallback_title=fallback, duration_s=float(info["duration_s"]), voice_mode=voice,
            )
            meta = sheet.meta
            mp4 = copy_if_changed(Path(info["path"]), outbox_target(ctx, "short", k))
            write_text_atomic(mp4.with_suffix(".json"), meta.model_dump_json(indent=2) + "\n", newline=None)
            write_sheet_txt(meta, mp4.name, mp4.with_suffix(".txt"), notes=sheet.notes)
            record_output(
                ctx, kind="short", k=k, item=item, meta=meta, path=mp4,
                duration_s=float(info["duration_s"]), shots=shots, profile=mark,
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
    """A `Seo` from a SEO document that may carry extra fields (chapter titles, thumbnail text).

    The fields are taken as written: `build_sheet` fits them to YouTube's rules and says what it changed
    (a cut here was silent, and in the middle of a word)."""
    return Seo.model_construct(
        title=str(d["title"]),
        description=str(d["description"]),
        tags=[str(t) for t in d["tags"]],
        hashtags=[str(h) for h in d.get("hashtags", [])],
        keywords=list(d.get("keywords", [])),
    )


def chapter_check(
    chapters: Sequence[dict[str, Any]], duration_s: float
) -> tuple[list[dict[str, Any]], list[str]]:
    """The chapters to list in the description (none when they break R6) and the line that says why."""
    why = yr.chapter_problem(chapters, duration_s)
    return ([], [f"left the chapter list out of the description: {why}"]) if why else (list(chapters), [])


def chapter_list(chapters: Sequence[dict[str, Any]]) -> str:
    """`MM:SS Title` lines; the first entry is `00:00` (spec §9.1)."""
    return "\n".join(f"{c['timestamp']} {c['title']}" for c in chapters)


def long_description(description: str, chapters: Sequence[dict[str, Any]]) -> str:
    """Master description (spec §9.1): summary lines, blank, chapter list, then the rest.

    The `[[CHAPTERS]]` placeholder is replaced; without one the list goes after the first paragraph. With
    no chapters (they broke YouTube's chapter rules) the placeholder and the blank lines around it go.
    """
    if not chapters:
        return re.sub(r"\n{3,}", "\n\n", description.replace(CHAPTERS_PLACEHOLDER, "")).strip("\n")
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
    voice, fallback = yr.job_voice_mode(ctx), working_title(ctx)
    chapters_doc: dict[str, Any] = ctx.read_json("long/chapters.json")
    long_info = ctx.read_json("render/long.json")
    chapters, chapter_note = chapter_check(chapters_doc["chapters"], float(long_info["duration_s"]))
    long_shots = [Shot.from_json(s) for s in ctx.read_json("clips/shots.json")["long"]["shots"]]
    long_seo = seo_from(ctx.read_json("seo_long.json"))
    long_seo = long_seo.model_copy(update={
        "description": long_description(long_seo.description, chapters),
        "hashtags": [h for h in long_seo.hashtags if h.lower() != "#shorts"],
    })
    thumbs = [f"thumb_{i}.jpg" for i in (1, 2, 3) if ctx.file(f"long/thumb_{i}.jpg").exists()]
    mark = job_mark(ctx)
    marked: dict[str, Any] = {"profile": mark} if mark else {}
    manifest: dict[str, Any] = {}

    sheet = build_sheet(
        channel=ctx.channel, job_id=ctx.job_id, kind="long", k=None, seo=long_seo, advice=advice,
        shots=long_shots, combo_id=ctx.combo.combo_id, prompt_versions=ctx.versions(),
        publish_at=publish_after(ctx.channel, ctx.job_id, "long", day_offset=1 + series_delay_days(ctx)),
        extra={
            "chapters": [{"timestamp": c["timestamp"], "title": c["title"], "start_s": c["start_s"]}
                         for c in chapters] or None,
            "midroll_suggestions_s": chapters_doc.get("midroll_suggestions_s", []),
            "thumbnails": thumbs,
            **marked,
        },
        fallback_title=fallback, voice_mode=voice, changes=chapter_note,
    )
    meta = sheet.meta
    master_title = meta.title  # the Shorts name the master as it will be titled on YouTube
    mp4 = copy_if_changed(Path(long_info["path"]), outbox_target(ctx, "long"))
    for t in thumbs:
        copy_if_changed(ctx.file(f"long/{t}"), mp4.parent / t)
    write_text_atomic(mp4.with_suffix(".json"), meta.model_dump_json(indent=2) + "\n", newline=None)
    write_sheet_txt(meta, mp4.name, mp4.with_suffix(".txt"), notes=sheet.notes)
    record_output(ctx, kind="long", k=None, item="long", meta=meta, path=mp4,
                  duration_s=float(long_info["duration_s"]), shots=long_shots, profile=mark)
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
        seo = base.model_copy(update={"description": short_description(base.description, master_title)})
        ssheet = build_sheet(
            channel=ctx.channel, job_id=ctx.job_id, kind="short", k=k, seo=seo, advice=advice, shots=shots,
            combo_id=(spec.combo_id if spec else None) or ctx.combo.combo_id,
            prompt_versions=ctx.versions(),
            publish_at=publish_after(
                ctx.channel, ctx.job_id, item,
                day_offset=1 + short_day_offset(ctx.channel, k) + series_delay_days(ctx),
            ),
            extra={"related_video_instruction": f"{RELATED_PREFIX}{master_title}",
                   "parent_job_id": ctx.job_id, **marked},
            fallback_title=fallback, duration_s=float(info["duration_s"]), voice_mode=voice,
        )
        smeta = ssheet.meta
        smp4 = copy_if_changed(Path(info["path"]), outbox_target(ctx, "short", k))
        write_text_atomic(smp4.with_suffix(".json"), smeta.model_dump_json(indent=2) + "\n", newline=None)
        write_sheet_txt(smeta, smp4.name, smp4.with_suffix(".txt"), related_title=master_title,
                        notes=ssheet.notes)
        record_output(ctx, kind="short", k=k, item=item, meta=smeta, path=smp4,
                      duration_s=float(info["duration_s"]), shots=shots, profile=mark)
        manifest[item] = {"mp4": str(smp4), "json": str(smp4.with_suffix(".json")),
                          "txt": str(smp4.with_suffix(".txt"))}
    ctx.write_json("outbox/manifest.json", manifest)
    ctx.log.info("outbox_done", job_id=ctx.job_id, items=list(manifest))
