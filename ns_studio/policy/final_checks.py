"""Final-gate checks (spec section 18.8): run on the rendered files after QC."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from ns_common import ffmpeg
from ns_common.models import PolicyAdvice, PolicyCheck
from ns_studio.context import JobContext
from ns_studio.policy import phash
from ns_studio.policy.base import Check, fail, ok
from ns_studio.policy.meta import collect_items, policy_list, story_text

MIN_MASTER_S = 510.0  # 8:30
MIDROLL_MARGIN_S = 120.0
PHASH_MEAN_MAX = 8.0  # mean Hamming bits (of 64) at or under which two renders count as the same
SAME_CHANNEL_DAYS = 14
OTHER_CHANNEL_DAYS = 30
BED_FLOOR_DBFS = -55.0
FRAME_S = 0.05
SR = 16000
INDEX_NAME = "phash_index.jsonl"

# Larger countries, US states and cities that a story must not use as a real setting (disclosure advice).
REAL_PLACES = frozenset(
    "america canada mexico england london paris berlin tokyo china india russia australia ireland scotland "
    "brazil texas california florida ohio nevada oregon alaska hawaii arizona colorado georgia michigan "
    "virginia chicago boston seattle denver detroit houston dallas miami atlanta portland".split()
)
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
        index_file.parent.mkdir(parents=True, exist_ok=True)
        index_file.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- runtime and mid-rolls
def _midrolls(ctx: JobContext) -> list[float]:
    for name in ("long/chapters.json", "seo_long.json", "long/metadata.json"):
        if ctx.file(name).exists():
            data = ctx.read_json(name)
            if isinstance(data, dict) and isinstance(data.get("midroll_suggestions_s"), list):
                return [float(x) for x in data["midroll_suggestions_s"]]
    return []


class LongRuntime(Check):
    """Master at least 8:30 and mid-roll suggestions outside the first and last 2 minutes (E612)."""

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
        if dur < MIN_MASTER_S:
            problems.append(f"master is {dur:.0f} s (minimum {MIN_MASTER_S:.0f} s)")
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


class ClipReuse(Check):
    """Spec 7.4: no clip twice in a video, 14 days per channel, 30 days across channels (E613)."""

    name = "clip_reuse"
    gate = "final"

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(UTC))

    def run(self, ctx: JobContext) -> PolicyCheck:
        """Check this job's `clip_usage` rows against earlier usage."""
        now = self.clock()
        mine = ctx.db.query("select source, clip_id, item from clip_usage where job_id=?", (ctx.job_id,))
        problems: list[str] = []
        seen: set[tuple[str, str, str]] = set()
        for r in mine:
            key = (r["source"], r["clip_id"], r["item"])
            if key in seen:
                problems.append(f"{r['source']}/{r['clip_id']} used twice in {r['item']}")
            seen.add(key)
        for source, clip_id in {(r["source"], r["clip_id"]) for r in mine}:
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
                    problems.append(f"{source}/{clip_id} already used on {where} by {o['job_id']}")
        return fail("; ".join(sorted(set(problems)))) if problems else ok(f"{len(mine)} clip uses checked")


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


def _decode_audio(path: Path) -> np.ndarray:
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "a.raw"
        ffmpeg.run(["-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", str(raw)])
        data = raw.read_bytes()
    return np.frombuffer(data, dtype=np.int16).astype(np.float64) / 32768.0


class AudioOnlyVoice(Check):
    """Exactly one audio stream and real silence between words, so no music bed (E614)."""

    name = "audio_only_voice"
    gate = "final"

    def run(self, ctx: JobContext) -> PolicyCheck:
        """Count audio streams with ffprobe and measure the noise floor of the quietest frames."""
        renders = find_renders(ctx)
        if not renders:
            return ok("no renders found")
        problems: list[str] = []
        for item, path in renders:
            streams = ffmpeg.probe(path)["streams"]
            audio = [s for s in streams if s.get("codec_type") == "audio"]
            if len(audio) != 1:
                problems.append(f"{item}: {len(audio)} audio streams (expected 1)")
                continue
            floor = quiet_floor_db(_decode_audio(path))
            if floor > BED_FLOOR_DBFS:
                problems.append(f"{item}: no silence anywhere (floor {floor:.0f} dBFS), possible music bed")
        return fail("; ".join(problems)) if problems else ok(f"{len(renders)} renders carry narration only")


# ---------------------------------------------------------------- disclosure
def compute_advice(ctx: JobContext) -> PolicyAdvice:
    """Spec 18.7: altered-content answer, made-for-kids and the age-restriction suggestion."""
    text = story_text(ctx)
    blob = "\n".join([text, *(t for it in collect_items(ctx) for t in it.texts())]).lower()
    words = set(blob.replace(",", " ").replace(".", " ").split())
    real_place = bool(words & REAL_PLACES) or "new york" in blob
    claims_real = any(p in blob for p in (*_FACTUAL_CLAIMS, *policy_list("misleading_claims.txt")))
    notes = ctx.read_json("policy_notes.json") if ctx.file("policy_notes.json").exists() else {}
    intense = ctx.channel.audience == "mature" and notes.get("intensity") in ("medium", "high")
    return PolicyAdvice(
        ai_disclosure="yes" if (real_place or claims_real) else "no",
        made_for_kids=ctx.channel.made_for_kids,
        age_restrict_suggested=bool(intense),
    )


class DisclosureAdvice(Check):
    """Always passes; the advice itself goes into `policy_final.json` and the upload sheet."""

    name = "disclosure_advice"
    gate = "final"

    def run(self, ctx: JobContext) -> PolicyCheck:
        """Summarise the section 18.7 answers."""
        a = compute_advice(ctx)
        return ok(
            f"altered_content={a.ai_disclosure} made_for_kids={a.made_for_kids} "
            f"age_restrict={a.age_restrict_suggested}"
        )


def final_checks() -> list[Check]:
    """The ordered final-gate check list."""
    return [
        RenderUniqueness(),
        LongRuntime(),
        ClipReuse(),
        AudioOnlyVoice(),
        DisclosureAdvice(),
    ]
