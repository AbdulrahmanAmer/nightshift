"""Final-gate checks (spec section 18.8): run on the rendered files after QC."""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from ns_common import ffmpeg
from ns_common.atomic import write_text_atomic
from ns_common.errors import describe
from ns_common.models import PolicyAdvice, PolicyCheck
from ns_studio.context import JobContext
from ns_studio.policy import phash
from ns_studio.policy.base import Check, fail, ok
from ns_studio.policy.judge import JudgeFn, default_judge, record_notes, verdict_failed
from ns_studio.policy.meta import (
    collect_items,
    policy_list,
    short_moments,
    story_text,
    unsafe_categories,
)
from ns_studio.policy.text_checks import NerFn, place_entities
from ns_studio.text.story_profile import duration_profile

MIN_MASTER_S = 510.0  # 8:30
MIDROLL_MARGIN_S = 120.0
PHASH_MEAN_MAX = 8.0  # mean Hamming bits (of 64) at or under which two renders count as the same
SAME_CHANNEL_DAYS = 14
OTHER_CHANNEL_DAYS = 30
BED_FLOOR_DBFS = -55.0
# Spectral flatness (geometric over arithmetic mean of the power spectrum) of the quiet frames: noise and
# room tone sit around 0.3 to 0.6, a held note or a pad is far below 0.1. UNCALIBRATED: 0.1 and
# QUIET_MIN_DB were checked against synthetic noise and tones only, not against real qwen-tts or AAC-encoded
# narration (a decaying voiced tail or low hum in the quietest frames may read as tonal), so a value under
# this limit is a WARNING with the measured value recorded, never a failure. The one place the limit lives;
# make the check a failure only with a threshold measured on real qwen-tts output (docs/FINALIZATION.md F).
TONAL_FLATNESS_MAX = 0.1
QC_FLATNESS_FILE = "qc/audio_flatness.json"
QUIET_SHARE = 0.10  # the quietest tenth of the 50 ms frames
QUIET_MIN_DB = -85.0  # below this a frame is digital silence: no bed, nothing to measure
QUIET_MIN_FRAMES = 5
FFT_SIZE = 2048
FRAME_S = 0.05
SR = 16000
INDEX_NAME = "phash_index.jsonl"

_FACTUAL_CLAIMS = ("this really happened", "this actually happened", "this is real", "i swear this is true")


def find_renders(ctx: JobContext) -> list[tuple[str, Path]]:
    """Rendered videos of the job as `(item, path)`: the master and every Short."""
    out: list[tuple[str, Path]] = []
    master = ctx.file("long/master.mp4")
    if master.exists():
        out.append(("long", master))
    shorts = ctx.file("shorts")
    if shorts.is_dir():
        for p in sorted(shorts.glob("s*/short.mp4")):
            out.append((p.parent.name, p))
        for p in sorted(shorts.glob("s*.mp4")):
            out.append((p.stem, p))
    if not out:
        for p in sorted(ctx.job_dir.glob("*.mp4")):
            out.append(("s1", p))
    return out


def _is_fake_footage(ctx: JobContext) -> bool:
    return bool(ctx.services.clip_sources) and all(s.name == "fake" for s in ctx.services.clip_sources)


# ---------------------------------------------------------------- render uniqueness
def read_index(path: Path) -> list[dict]:
    """Rows of the pHash index file (one JSON object per line)."""
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


class RenderUniqueness(Check):
    """pHash of 10 frames against every earlier render on any channel (E611)."""

    name = "render_uniqueness"
    gate = "final"

    def __init__(self, *, threshold: float = PHASH_MEAN_MAX, skip_when_fake: bool = True) -> None:
        self.threshold = threshold
        self.skip_when_fake = skip_when_fake

    def run(self, ctx: JobContext) -> PolicyCheck:
        """Fail when a render is within `threshold` bits of an earlier render or of a sibling render."""
        renders = find_renders(ctx)
        if not renders:
            return ok("no renders found")
        if self.skip_when_fake and _is_fake_footage(ctx):
            return ok("skipped: fake footage is the same for every job")
        hashes = {item: phash.frame_hashes(path) for item, path in renders}
        index_file = ctx.paths.data / INDEX_NAME
        prior = [r for r in read_index(index_file) if r.get("job_id") != ctx.job_id]
        for r in ctx.db.query("select output_id, job_id, phash from outputs where phash is not null"):
            if r["job_id"] != ctx.job_id:
                prior.append({"job_id": r["job_id"], "item": r["output_id"], "hash": r["phash"]})
        problems: list[str] = []
        items = list(hashes)
        for i, a in enumerate(items):
            for b in items[i + 1 :]:
                d = phash.distance(hashes[a], hashes[b])
                if d <= self.threshold:
                    problems.append(f"{a} and {b} of this job look alike (distance {d:.1f})")
            for row in prior:
                d = phash.distance(hashes[a], phash.decode(str(row.get("hash", ""))))
                if d <= self.threshold:
                    problems.append(f"{a} matches {row.get('job_id')}/{row.get('item')} (distance {d:.1f})")
        if problems:
            return fail("; ".join(problems))
        self._record(ctx, index_file, hashes)
        return ok(f"{len(hashes)} renders unique against {len(prior)} earlier")

    @staticmethod
    def _record(ctx: JobContext, index_file: Path, hashes: dict[str, list[int]]) -> None:
        rows = [r for r in read_index(index_file) if r.get("job_id") != ctx.job_id]
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        for item, hs in hashes.items():
            rows.append(
                {
                    "job_id": ctx.job_id,
                    "channel": ctx.channel.slug,
                    "item": item,
                    "hash": phash.encode(hs),
                    "ts": stamp,
                }
            )
            ctx.db.execute(
                "update outputs set phash=? where output_id=?", (phash.encode(hs), f"{ctx.job_id}/{item}")
            )
        write_text_atomic(index_file, "\n".join(json.dumps(r) for r in rows) + "\n")


# ---------------------------------------------------------------- runtime and mid-rolls
def _midrolls(ctx: JobContext) -> list[float]:
    for name in ("long/chapters.json", "seo_long.json", "long/metadata.json"):
        if ctx.file(name).exists():
            data = ctx.read_json(name)
            if isinstance(data, dict) and isinstance(data.get("midroll_suggestions_s"), list):
                return [float(x) for x in data["midroll_suggestions_s"]]
    return []


class LongRuntime(Check):
    """Master at least 8:30 and mid-roll suggestions outside the first and last 2 minutes (E612).

    The 8:30 is a length, like QC's: an engine-test install (`tiny_story`) takes QC's minimum of its profile
    instead. The mid-roll margin is not a length of the story and stays."""

    name = "long_runtime"
    gate = "final"

    def __init__(self, duration_fn: Callable[[Path], float] | None = None) -> None:
        self.duration_fn = duration_fn or ffmpeg.duration_s

    def run(self, ctx: JobContext) -> PolicyCheck:
        """Measure the master with ffprobe and validate the mid-roll suggestions."""
        master = ctx.file("long/master.mp4")
        if not master.exists():
            return ok("no master in this job")
        dur = self.duration_fn(master)
        problems: list[str] = []
        window = duration_profile(ctx).master_qc_s
        minimum = MIN_MASTER_S if window is None else window[0]
        if dur < minimum:
            problems.append(f"master is {dur:.0f} s (minimum {minimum:.0f} s)")
        for m in _midrolls(ctx):
            if m < MIDROLL_MARGIN_S or m > dur - MIDROLL_MARGIN_S:
                problems.append(f"mid-roll at {m:.0f} s is inside the first or last 2 minutes")
        return fail("; ".join(problems)) if problems else ok(f"master {dur:.0f} s")


# ---------------------------------------------------------------- clip reuse
def _parse(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


SHOT_FILES = ("clips/shots.json", "clips/shorts_shots.json")
# A (source, clip id, item, relaxed) use: `relaxed` marks a pick made under the NS-W405 relaxation.
ClipUse = tuple[str, str, str, bool]


def picked_clip_uses(ctx: JobContext) -> list[ClipUse] | None:
    """The clips this job picked, read from `clips/shots.json` and `clips/shorts_shots.json`.

    `clip_usage` rows are only written by the outbox stage, after this gate, so the picks are the truth at
    gate time (`blocked_for_job` reads the same files). None when the job has neither file.
    """
    found = [name for name in SHOT_FILES if ctx.file(name).exists()]
    if not found:
        return None
    uses: list[ClipUse] = []
    for name in found:
        for item, entry in ctx.read_json(name).items():
            for shot in entry.get("shots", []):
                uses.append((str(shot["source"]), str(shot["clip_id"]), str(item), bool(shot.get("relaxed"))))
    return uses


class ClipReuse(Check):
    """Spec 7.4: no clip twice in a video, 14 days per channel, 30 days across channels (E613).

    The job's own picks come from the shot files (the `clip_usage` rows of a job do not exist yet when this
    gate runs; they are the fallback for a job without shot files). A clip picked by the NS-W405 relaxation,
    the documented last resort when every fresh clip is used up, is reported as a warning; any other breach
    of the windows fails.
    """

    name = "clip_reuse"
    gate = "final"

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(UTC))

    def run(self, ctx: JobContext) -> PolicyCheck:
        """Check this job's clip picks against each other and against earlier usage."""
        now = self.clock()
        uses = picked_clip_uses(ctx)
        if uses is None:
            rows = ctx.db.query("select source, clip_id, item from clip_usage where job_id=?", (ctx.job_id,))
            uses = [(r["source"], r["clip_id"], r["item"], False) for r in rows]
        problems: set[str] = set()
        warnings: set[str] = set()
        seen: set[tuple[str, str, str]] = set()
        for source, clip_id, item, _relaxed in uses:
            key = (source, clip_id, item)
            if key in seen:
                (warnings if self._has_relaxed(uses, key) else problems).add(
                    f"{source}/{clip_id} used twice in {item}"
                )
            seen.add(key)
        for source, clip_id in {(s, c) for s, c, _i, _r in uses}:
            all_relaxed = all(rl for s, c, _i, rl in uses if (s, c) == (source, clip_id))
            others = ctx.db.query(
                "select channel, job_id, used_at from clip_usage "
                "where source=? and clip_id=? and job_id != ?",
                (source, clip_id, ctx.job_id),
            )
            for o in others:
                when = _parse(o["used_at"])
                if when is None:
                    continue
                same = o["channel"] == ctx.channel.slug
                limit = timedelta(days=SAME_CHANNEL_DAYS if same else OTHER_CHANNEL_DAYS)
                if now - when < limit:
                    where = "this channel" if same else f"channel {o['channel']}"
                    (warnings if all_relaxed else problems).add(
                        f"{source}/{clip_id} already used on {where} by {o['job_id']}"
                    )
        if problems:
            return fail("; ".join(sorted(problems)))
        # a relaxed pick always tells the owner, even when no usage row matches it (a regeneration relaxes
        # against the earlier attempt's picks, which have no row yet)
        for source, clip_id, item, relaxed in uses:
            if relaxed and not any(w.startswith(f"{source}/{clip_id} ") for w in warnings):
                warnings.add(f"{source}/{clip_id} reused for {item}")
        if warnings:
            known = describe("NS-W405")
            detail = (
                f"{known.message} {known.hint} (NS-W405 warning, relaxed pick: "
                + "; ".join(sorted(warnings)) + ")"
            )
            ctx.log.warn("clip_reuse_relaxed", code="NS-W405", job_id=ctx.job_id, detail=detail)
            return ok(f"{len(uses)} clip uses checked; {detail}")
        return ok(f"{len(uses)} clip uses checked")

    @staticmethod
    def _has_relaxed(uses: list[ClipUse], key: tuple[str, str, str]) -> bool:
        """True when some use of `key` was a relaxed pick (the repeat inside a video is the relaxation)."""
        return any(rl for s, c, i, rl in uses if (s, c, i) == key)


# ---------------------------------------------------------------- audio
def quiet_floor_db(samples: np.ndarray) -> float:
    """Mean level (dBFS) of the quietest 2 % of 50 ms frames of mono float samples in [-1, 1]."""
    n = int(SR * FRAME_S)
    count = len(samples) // n
    if count == 0:
        return -120.0
    frames = samples[: count * n].reshape(count, n)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    db = 20 * np.log10(np.maximum(rms, 1e-6))
    k = max(1, int(count * 0.02))
    return float(np.mean(np.sort(db)[:k]))


def quiet_flatness(samples: np.ndarray) -> float | None:
    """Median spectral flatness of the quiet 50 ms frames (the quietest tenth, above digital silence).

    Narration has room tone or silence between words; a music bed under it stays tonal there even when it is
    quieter than `BED_FLOOR_DBFS`. None when there are too few non-silent quiet frames to say anything.
    """
    n = int(SR * FRAME_S)
    count = len(samples) // n
    if count == 0:
        return None
    frames = samples[: count * n].reshape(count, n).astype(np.float64)
    db = 20 * np.log10(np.maximum(np.sqrt(np.mean(frames**2, axis=1)), 1e-6))
    order = np.argsort(db)[: max(1, int(count * QUIET_SHARE))]
    picked = [i for i in order if db[i] > QUIET_MIN_DB]
    if len(picked) < QUIET_MIN_FRAMES:
        return None
    window = np.hanning(n)
    flat: list[float] = []
    for i in picked:
        power = np.abs(np.fft.rfft(frames[i] * window, FFT_SIZE)) ** 2 + 1e-20
        flat.append(float(np.exp(np.mean(np.log(power))) / np.mean(power)))
    return float(np.median(flat))


def _decode_audio(path: Path) -> np.ndarray:
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "a.raw"
        ffmpeg.run(["-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", str(raw)])
        data = raw.read_bytes()
    return np.frombuffer(data, dtype=np.int16).astype(np.float64) / 32768.0


def _round4(value: float | bool | None) -> float | None:
    """A measured flatness as stored in the QC file and the log (4 decimals); None stays None."""
    return None if value is None else round(float(value), 4)


class AudioOnlyVoice(Check):
    """Exactly one audio stream and real silence between words, so no music bed (E614).

    Two halves. The floor half (`BED_FLOOR_DBFS`: no quiet frame anywhere) fails. The flatness half
    (`quiet_flatness` against `TONAL_FLATNESS_MAX`) is "measured, warning only" until the value is calibrated
    on real qwen-tts narration (F-H116): it records the measured value in `QC_FLATNESS_FILE` and in the job
    log every time and only warns, because a false NS-E614 costs a full render, a regeneration and the night.
    """

    name = "audio_only_voice"
    gate = "final"

    def run(self, ctx: JobContext) -> PolicyCheck:
        """Count audio streams with ffprobe; measure the level and the tonality of the quietest frames."""
        renders = find_renders(ctx)
        if not renders:
            return ok("no renders found")
        problems: list[str] = []
        measured: dict[str, dict[str, float | bool | None]] = {}
        for item, path in renders:
            streams = ffmpeg.probe(path)["streams"]
            audio = [s for s in streams if s.get("codec_type") == "audio"]
            if len(audio) != 1:
                problems.append(f"{item}: {len(audio)} audio streams (expected 1)")
                continue
            samples = _decode_audio(path)
            flatness = quiet_flatness(samples)
            warned = flatness is not None and flatness < TONAL_FLATNESS_MAX
            measured[item] = {"quiet_flatness": flatness, "warned": warned}
            floor = quiet_floor_db(samples)
            if floor > BED_FLOOR_DBFS:
                problems.append(f"{item}: no silence anywhere (floor {floor:.0f} dBFS), possible music bed")
        flat_note = self._record_flatness(ctx, measured)
        if problems:
            return fail("; ".join(problems))
        return ok(f"{len(renders)} renders carry narration only{flat_note}")

    @staticmethod
    def _record_flatness(ctx: JobContext, measured: dict[str, dict[str, float | bool | None]]) -> str:
        """Write the measured values to the QC report and the log; return the text for the check's detail.

        `qc/audio_flatness.json` has no `passed` key on purpose: the channel pass rate counts the `qc/*.json`
        files that have one.
        """
        if not measured:
            # every render failed the stream count: an earlier pass's values would disagree with these renders
            ctx.file(QC_FLATNESS_FILE).unlink(missing_ok=True)
            return ""
        rounded = {
            item: {
                "quiet_flatness": _round4(row["quiet_flatness"]),
                "warned": row["warned"],
            }
            for item, row in measured.items()
        }
        ctx.write_json(
            QC_FLATNESS_FILE,
            {"warn_below": TONAL_FLATNESS_MAX, "mode": "warning only", "items": rounded},
        )
        shown: list[str] = []
        warnings: list[str] = []
        for item, row in measured.items():
            value = row["quiet_flatness"]
            text = "n/a" if value is None else f"{float(value):.3f}"
            shown.append(f"{item}={text}")
            if row["warned"]:
                warnings.append(f"{item}={text}")
            event = "audio_flatness_warning" if row["warned"] else "audio_flatness"
            log = ctx.log.warn if row["warned"] else ctx.log.info
            # no NS-W code in contracts/errors.json fits a tonal quiet passage: logged without one
            log(
                event,
                job_id=ctx.job_id,
                item=item,
                quiet_flatness=_round4(value),
                warn_below=TONAL_FLATNESS_MAX,
            )
        note = f"; quiet_flatness {', '.join(shown)} (warns under {TONAL_FLATNESS_MAX})"
        if warnings:
            note += (
                f"; WARNING quiet_flatness under {TONAL_FLATNESS_MAX} for {', '.join(warnings)}: tonal "
                "energy between the words, possible music bed (warning only, not calibrated on qwen-tts)"
            )
        return note


# ---------------------------------------------------------------- unsafe topics, second look
class FinalUnsafeTopics(Check):
    """Spec 18.3 "both gates": the unsafe-topic classifier over the final script and metadata (E601).

    The text gate judged the story as it was then; this reads what is about to be published: the script as
    it stands after any expansion, plus titles, descriptions, tags and hooks. It also keeps the intensity
    and real-setting notes the disclosure advice reads (never lowering them).
    """

    name = "unsafe_topics"
    gate = "final"

    def __init__(self, judge: JudgeFn | None = None) -> None:
        self.judge = judge or default_judge

    def run(self, ctx: JobContext) -> PolicyCheck:
        """Fail when the judge flags any section 18.3 category in the script or the metadata."""
        meta = [f"[{it.name}] " + " | ".join(t for t in it.texts() if t) for it in collect_items(ctx)]
        metadata = "Metadata:\n" + "\n".join(meta) if meta else ""
        text = "\n\n".join(p for p in (story_text(ctx), metadata) if p)
        if not text.strip():
            return ok("no script or metadata to re-check")
        answer = self.judge(
            ctx,
            "unsafe_topics",
            story=text,
            categories=list(unsafe_categories()),
            audience=ctx.channel.audience,
        )
        notes = record_notes(ctx, answer, merge=True)
        if verdict_failed(answer):
            cats = ", ".join(str(f.get("category", "?")) for f in answer.get("flags", [])) or "unspecified"
            return fail(f"flagged: {cats}; {answer.get('reason', '')}".strip())
        return ok(f"script and metadata re-checked, intensity={notes['intensity']}")


# ---------------------------------------------------------------- hook in the derived Short
_WORD = re.compile(r"[a-z0-9']+")
HOOK_MATCH_MIN = 0.75  # share of the hook's words found, in order, among the Short's opening words


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def hook_match(hook: str, spoken: list[str]) -> float:
    """Share of the hook's words that open the Short (case, punctuation and plural endings are ignored)."""
    want = _tokens(hook)
    if not want:
        return 1.0
    got = [t for w in spoken for t in _tokens(w)][: len(want) + 2]
    stem = [t.rstrip("s") for t in got]
    hits, at = 0, 0
    for t in (w.rstrip("s") for w in want):
        if t in stem[at:]:
            at += stem[at:].index(t) + 1
            hits += 1
    return hits / len(want)


class ShortHookPresent(Check):
    """Spec 18.8 render uniqueness, the second half: a derived Short opens with its own new hook (E611).

    A Short may share its narration with the master but must differ from it by the hook it was given. The
    hook is read from `shorts_plan.json` and compared with the first caption words of `captions/s<k>.json`
    (aligned from the Short's own audio). This needs no footage, so dev runs are checked too.
    """

    name = "short_hook"
    gate = "final"

    def run(self, ctx: JobContext) -> PolicyCheck:
        """Fail when a planned hook is missing from the opening words of its Short."""
        moments = [m for m in short_moments(ctx) if str(m["hook"]).strip()]
        if not moments:
            return ok("no derived Shorts with a hook")
        problems: list[str] = []
        for m in moments:
            item = f"s{m['k']}"
            f = ctx.file(f"captions/{item}.json")
            if not f.exists():
                problems.append(f"{item}: no captions to find the hook in")
                continue
            words = [str(w.get("w", "")) for w in ctx.read_json(f"captions/{item}.json").get("words", [])]
            share = hook_match(str(m["hook"]), words)
            if share < HOOK_MATCH_MIN:
                problems.append(f"{item}: opens with {' '.join(words[:8])!r}, not its hook {m['hook']!r}")
        return fail("; ".join(problems)) if problems else ok(f"{len(moments)} Shorts open with their hook")

    def hint(self, ctx: JobContext) -> str | None:
        """NS-E611 is shared with render uniqueness: say what this failure is, and what is redone."""
        return (
            "A Short does not open with the hook that was written for it. "
            "The narration is being voiced again so the hook leads the Short."
        )


# ---------------------------------------------------------------- disclosure
def compute_advice(ctx: JobContext, place_ner: NerFn | None = None) -> PolicyAdvice:
    """Spec 18.7: altered-content answer, made-for-kids and the age-restriction suggestion.

    Altered or synthetic content is "yes" when anything could pass as real footage and when in doubt: a
    place spaCy recognises (GPE, LOC, FAC) in the script or metadata, a claim that the story is real, the
    classifier's `real_setting` answer being yes, or that answer being missing or unclear.
    """
    text = story_text(ctx)
    blob = "\n".join([text, *(t for it in collect_items(ctx) for t in it.texts())])
    lowered = blob.lower()
    claims_real = any(p in lowered for p in (*_FACTUAL_CLAIMS, *policy_list("misleading_claims.txt")))
    notes = ctx.read_json("policy_notes.json") if ctx.file("policy_notes.json").exists() else {}
    setting = notes.get("real_setting")
    unclear_or_real = setting is not False  # True (a real place) or missing/None (the classifier did not say)
    places = (place_ner or (lambda t: place_entities(ctx.paths, t)))(blob)
    intense = ctx.channel.audience == "mature" and notes.get("intensity") in ("medium", "high")
    return PolicyAdvice(
        ai_disclosure="yes" if (unclear_or_real or places or claims_real) else "no",
        made_for_kids=ctx.channel.made_for_kids,
        age_restrict_suggested=bool(intense),
    )


class DisclosureAdvice(Check):
    """Always passes; the advice itself goes into `policy_final.json` and the upload sheet."""

    name = "disclosure_advice"
    gate = "final"

    def __init__(self, place_ner: NerFn | None = None) -> None:
        self.place_ner = place_ner

    def run(self, ctx: JobContext) -> PolicyCheck:
        """Summarise the section 18.7 answers."""
        a = compute_advice(ctx, self.place_ner)
        return ok(
            f"altered_content={a.ai_disclosure} made_for_kids={a.made_for_kids} "
            f"age_restrict={a.age_restrict_suggested}"
        )


def final_checks() -> list[Check]:
    """The ordered final-gate check list."""
    return [
        FinalUnsafeTopics(),
        RenderUniqueness(),
        LongRuntime(),
        ClipReuse(),
        AudioOnlyVoice(),
        ShortHookPresent(),
        DisclosureAdvice(),
    ]
