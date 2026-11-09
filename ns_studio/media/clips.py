"""Clip cache, keyword dedupe, clip selection with reuse rules, and the clips stage (component C6).

Spec §7.4: one search per unique keyword per source per orientation; clips cached 90 days; no clip twice in
a video, 14 days on a channel, 30 days across channels; faces skipped for dark moods; NS-E402 when a beat
cannot be filled, after a mood-only fallback search.
"""

from __future__ import annotations

import json
import random
import re
import zlib
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ns_common.db import Db
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_studio.context import JobContext
from ns_studio.media.clip_sources import ClipCandidate, ClipSource, Orientation, give_log
from ns_studio.media.presets import visual_treatment
from ns_studio.media.shotplan import Shot, ShotSlot, beat_spans, plan_slots
from ns_studio.media.transcribe import Word
from ns_studio.stages import Tenant

CACHE_DAYS = 90
SAME_CHANNEL_DAYS = 14
OTHER_CHANNEL_DAYS = 30
DARK_MOODS = frozenset({"intimate", "horror", "eerie", "creepy", "dread", "haunting"})
FACE_WORDS = frozenset({"person", "people", "portrait", "face", "faces", "selfie"})
_API_ERRORS = {"NS-E501", "NS-E502", "NS-E503"}


def utcnow() -> datetime:
    """Current UTC time (patched in tests through the `now` parameters)."""
    return datetime.now(UTC)


def _singular(w: str) -> str:
    """Strip a plural s (not ss, us, is) from words longer than 3 letters."""
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def normalize_keyword(kw: str) -> str:
    """Lowercase, trim, collapse whitespace and crudely singularize ("windows" -> "window")."""
    words = re.sub(r"[^a-z0-9 ]+", " ", kw.lower()).split()
    out = [_singular(w) for w in words]
    return " ".join(out)


def dedupe_keywords(keywords: Sequence[str]) -> list[str]:
    """Normalized keywords, first occurrence order, duplicates removed."""
    seen: dict[str, None] = {}
    for k in keywords:
        n = normalize_keyword(k)
        if n:
            seen.setdefault(n, None)
    return list(seen)


class ClipCache:
    """Downloaded clips under `cache/clips/<source>/<id>.mp4` with a row in the `clips` table."""

    def __init__(
        self,
        db: Db,
        paths: Paths,
        sources: Sequence[ClipSource],
        *,
        now: Callable[[], datetime] = utcnow,
        log: NsLogger | None = None,
    ) -> None:
        self.db = db
        self.paths = paths
        self.sources = {s.name: s for s in sources}
        self.now = now
        self.log = log
        give_log(sources, log)

    def path_for(self, source: str, clip_id: str) -> Path:
        """Cache location of a clip."""
        return self.paths.cache_clips / source / f"{clip_id}.mp4"

    def ensure(self, cand: ClipCandidate, keywords: Sequence[str]) -> Path:
        """Return the cached file, downloading it when missing or older than 90 days (no API call on hits)."""
        dest = self.path_for(cand.source, cand.id)
        row = self.db.one(
            "select local_path, fetched_at, keywords from clips where source=? and clip_id=?",
            (cand.source, cand.id),
        )
        kws = list(keywords)
        if row is not None:
            kws = list(dict.fromkeys([*json.loads(row["keywords"] or "[]"), *kws]))
            fresh = datetime.fromisoformat(row["fetched_at"]) > self.now() - timedelta(days=CACHE_DAYS)
            if fresh and row["local_path"] and Path(row["local_path"]).exists():
                self.db.execute(
                    "update clips set keywords=? where source=? and clip_id=?",
                    (json.dumps(kws), cand.source, cand.id),
                )
                return Path(row["local_path"])
        src = self.sources.get(cand.source)
        if src is None:
            raise NsError("NS-E402", f"no clip source named {cand.source!r} to download {cand.id}")
        src.download(cand, dest)
        self.record(cand, dest, kws)
        return dest

    def record(self, cand: ClipCandidate, dest: Path, keywords: Sequence[str]) -> None:
        """Write (or replace) the `clips` row of a downloaded candidate."""
        self.db.execute(
            "insert or replace into clips(source, clip_id, url, license, width, height, duration, local_path,"
            " fetched_at, keywords) values (?,?,?,?,?,?,?,?,?,?)",
            (cand.source, cand.id, cand.page_url, cand.license, cand.width, cand.height, cand.duration,
             str(dest), self.now().isoformat(), json.dumps(list(keywords))),
        )
        if self.log:
            self.log.info("clip_cached", source=cand.source, clip_id=cand.id)

    def purge_expired(self) -> int:
        """Delete cached files older than 90 days (rows stay, `local_path` cleared, for usage history)."""
        cutoff = (self.now() - timedelta(days=CACHE_DAYS)).isoformat()
        rows = self.db.query(
            "select source, clip_id, local_path from clips where fetched_at < ? and local_path is not null",
            (cutoff,),
        )
        for r in rows:
            Path(r["local_path"]).unlink(missing_ok=True)
            self.db.execute(
                "update clips set local_path=NULL where source=? and clip_id=?", (r["source"], r["clip_id"])
            )
        return len(rows)


def blocked_clips(db: Db, channel: str, job_id: str, now: datetime) -> set[tuple[str, str]]:
    """Clips that may not be used now: same channel within 14 days, another channel within 30 days.

    Usage rows of the job itself are ignored so a re-run is not blocked by its own earlier attempt.
    """
    same = (now - timedelta(days=SAME_CHANNEL_DAYS)).isoformat()
    other = (now - timedelta(days=OTHER_CHANNEL_DAYS)).isoformat()
    rows = db.query(
        "select distinct source, clip_id from clip_usage where job_id != ?"
        " and ((channel = ? and used_at >= ?) or (channel != ? and used_at >= ?))",
        (job_id, channel, same, channel, other),
    )
    return {(r["source"], r["clip_id"]) for r in rows}


def clip_seed(ctx: JobContext, label: str) -> int:
    """Seed of a clip draw: fixed per job and `label`, different on every regeneration attempt.

    A regenerated job (policy NS-E611/E613) must not pick the clips the gate just rejected.
    """
    key = f"{ctx.job_id}|{label}" + (f"|a{ctx.attempt}" if ctx.attempt else "")
    return zlib.crc32(key.encode()) & 0x7FFFFFFF


def blocked_for_job(ctx: JobContext, previous: Sequence[str]) -> set[tuple[str, str]]:
    """`blocked_clips` plus, on a regeneration, the clips listed in the earlier attempt's `previous` files."""
    blocked = blocked_clips(ctx.db, ctx.channel.slug, ctx.job_id, utcnow())
    if not ctx.attempt:
        return blocked
    for name in previous:
        if not ctx.file(name).exists():
            continue
        for entry in ctx.read_json(name).values():
            for shot in entry.get("shots", []):
                blocked.add((str(shot["source"]), str(shot["clip_id"])))
    return blocked


class Searcher:
    """Search all sources with a per-job memo: one call per (source, orientation, keyword)."""

    def __init__(
        self, sources: Sequence[ClipSource], *, per_page: int = 80, log: NsLogger | None = None
    ) -> None:
        self.sources = list(sources)
        self.per_page = per_page
        self.log = log
        give_log(self.sources, log)
        self._memo: dict[tuple[str, str, str], list[ClipCandidate]] = {}
        self._down: set[str] = set()  # sources that gave NS-E503 once: not asked again this job
        self.errors: list[NsError] = []

    def search(self, keyword: str, orientation: Orientation) -> list[ClipCandidate]:
        """Results of every source, interleaved round-robin; a failing source is skipped and recorded."""
        per_source: list[list[ClipCandidate]] = []
        for src in self.sources:
            key = (src.name, orientation, keyword)
            if key not in self._memo and src.name in self._down:
                self._memo[key] = []
            if key not in self._memo:
                try:
                    self._memo[key] = src.search(keyword, orientation=orientation, per_page=self.per_page)
                except NsError as e:
                    if e.code not in _API_ERRORS:
                        raise
                    self.errors.append(e)
                    if e.code == "NS-E503":  # an outage already cost its backoff; do not pay it per keyword
                        self._down.add(src.name)
                    if self.log:
                        self.log.warn("clip_search_failed", source=src.name, keyword=keyword, code=e.code)
                    self._memo[key] = []
            per_source.append(self._memo[key])
        merged: list[ClipCandidate] = []
        for i in range(max((len(p) for p in per_source), default=0)):
            merged += [p[i] for p in per_source if i < len(p)]
        return merged

    def first_error(self) -> NsError | None:
        """The error to report when no source found anything: Pixabay's before Pexels'.

        Pexels gives out no keys any more, so "check the Pexels key" sends the owner after something she
        cannot get; the Pixabay error ("add a Pixabay key, it is free") names the key she can.
        """
        for e in self.errors:
            if e.code == "NS-E502":
                return e
        return self.errors[0] if self.errors else None


def has_face_hint(c: ClipCandidate) -> bool:
    """True when the title or tags suggest a person (Pexels/Pixabay expose no face flag)."""
    words = {w for t in (*c.tags, *c.title.replace(",", " ").split()) for w in t.lower().split()}
    return bool(words & FACE_WORDS)


def pick_for_slots(
    pool: Sequence[ClipCandidate],
    slots: Sequence[ShotSlot],
    *,
    used: set[tuple[str, str]],
    blocked: set[tuple[str, str]],
    dark: bool,
    wanted: Orientation = "portrait",
) -> list[ClipCandidate] | None:
    """One distinct candidate per slot, or None when the pool cannot fill them all.

    Order of preference: >= 1080p, wanted orientation, pool order (keyword priority). A clip must outlast its
    slot by 1 s; if that leaves a slot empty the margin is dropped to 0.
    """
    ranked = sorted(
        enumerate(pool),
        key=lambda ic: (
            min(ic[1].width, ic[1].height) < 1080,
            ic[1].orientation != wanted,
            ic[0],
        ),
    )
    for margin in (1.0, 0.0):
        taken: set[tuple[str, str]] = set()
        chosen: list[ClipCandidate] = []
        for slot in slots:
            hit = next(
                (
                    c
                    for _i, c in ranked
                    if (c.source, c.id) not in used | blocked | taken
                    and c.duration >= slot.length + margin
                    and not (dark and has_face_hint(c))
                ),
                None,
            )
            if hit is None:
                break
            taken.add((hit.source, hit.id))
            chosen.append(hit)
        else:
            return chosen
    return None


def select_for_beat(
    searcher: Searcher,
    keywords: Sequence[str],
    mood: str,
    slots: Sequence[ShotSlot],
    *,
    used: set[tuple[str, str]],
    blocked: set[tuple[str, str]],
    wanted: Orientation = "portrait",
    allow_other: bool = True,
) -> list[tuple[ClipCandidate, str]]:
    """Fill a beat's slots; widen the search in steps and finally fall back to the mood (spec §7.4).

    Steps: keywords in the wanted orientation, then the other orientation, then the mood word in both.
    Raises NsError NS-E402 when nothing suffices, or the first API error when every source failed.
    """
    other: Orientation = "landscape" if wanted == "portrait" else "portrait"
    dark = mood.lower() in DARK_MOODS
    kws = dedupe_keywords(keywords)
    mood_kws = dedupe_keywords([mood]) if mood else []
    steps: list[tuple[Orientation, list[str]]] = [
        (wanted, kws), (other, kws), (wanted, mood_kws), (other, mood_kws)
    ]
    if not allow_other:  # landscape edits never use portrait clips
        steps = [s for s in steps if s[0] == wanted]
    pool: list[tuple[ClipCandidate, str]] = []
    seen: set[tuple[str, str]] = set()
    for orient, step_kws in steps:
        for kw in step_kws:
            for c in searcher.search(kw, orient):
                if (c.source, c.id) not in seen:
                    seen.add((c.source, c.id))
                    pool.append((c, kw))
        picks = pick_for_slots(
            [c for c, _k in pool], slots, used=used, blocked=blocked, dark=dark, wanted=wanted
        )
        if picks is not None:
            kw_of = {(c.source, c.id): k for c, k in pool}
            return [(c, kw_of[(c.source, c.id)]) for c in picks]
    if not pool and searcher.errors:
        raise searcher.first_error() or searcher.errors[0]
    if pool:
        # Every candidate was ruled out by the reuse rules (14 days on this channel, 30 across channels) or a
        # regeneration's previous picks: repeat footage with a warning rather than fail the whole video after
        # the writing and the voice (the first dev-mode nightly run died here: NS-E402 "24 candidates for 2
        # slots"). Clips are still never repeated inside this video (`used`).
        candidates = [c for c, _k in pool]
        relaxed = pick_for_slots(candidates, slots, used=used, blocked=set(), dark=dark, wanted=wanted)
        how = "earlier footage is reused"
        if relaxed is None:
            # the pool is smaller than the video (a master plus its Shorts on a niche with few clips): a clip
            # may appear twice in the video rather than lose it
            relaxed = pick_for_slots(candidates, slots, used=set(), blocked=set(), dark=dark, wanted=wanted)
            how = "footage from this video is repeated"
        if relaxed is not None:
            if searcher.log:
                searcher.log.warn("footage_reused", code="NS-W405", keywords=list(kws), mood=mood,
                                  msg=f"no fresh stock clip for this beat; {how}")
            kw_of = {(c.source, c.id): k for c, k in pool}
            return [(c, kw_of[(c.source, c.id)]) for c in relaxed]
    raise NsError("NS-E402", f"keywords {kws} mood {mood!r}: {len(pool)} candidates for {len(slots)} slots")


def assign_clips(
    *,
    beats: Sequence[dict[str, Any]],
    slots: Sequence[ShotSlot],
    searcher: Searcher,
    cache: ClipCache,
    blocked: set[tuple[str, str]],
    seed: int,
    wanted: Orientation = "portrait",
    used: set[tuple[str, str]] | None = None,
    allow_other: bool = True,
) -> list[Shot]:
    """Choose, download and time-place one clip per slot; clips are never repeated within the video."""
    rng = random.Random(seed)
    used = used if used is not None else set()
    shots: list[Shot] = []
    for bi, beat in enumerate(beats):
        beat_slots = [s for s in slots if s.beat == bi]
        if not beat_slots:
            continue
        picks = select_for_beat(
            searcher, beat["keywords"], str(beat.get("mood", "")), beat_slots,
            used=used, blocked=blocked, wanted=wanted, allow_other=allow_other,
        )
        for slot, (cand, kw) in zip(beat_slots, picks, strict=True):
            used.add((cand.source, cand.id))
            path = cache.ensure(cand, [kw])
            ss = round(rng.uniform(0.0, max(0.0, cand.duration - slot.length - 0.2)), 2)
            shots.append(
                Shot(bi, slot.start, slot.end, cand.source, cand.id, str(path), ss, cand.width, cand.height,
                     cand.page_url, cand.license, kw)
            )
    return shots


def fallback_beat(ctx: JobContext) -> dict[str, Any]:
    """A single beat built from the channel keywords, used when a part has no visual plan."""
    kws = ctx.channel.seed_keywords[:3] or ctx.channel.niche.split()[:3] or ["night"]
    return {"sentence_start": 0, "sentence_end": 0, "keywords": kws, "mood": "eerie"}


class ClipsStage:
    """Stage `clips`: for each Short (part) plan slots from the caption timeline and choose clips.

    Writes `clips/shots.json` = {"s<k>": {"total_s", "treatment", "shots": [...]}}.
    """

    name: str = "clips"
    tenant: Tenant = None
    inputs: tuple[str, ...] = ("visual_plan.json", "captions/report.json", "story.json")
    outputs: tuple[str, ...] = ("clips/shots.json",)

    def run(self, ctx: JobContext) -> None:
        """Select clips for every part of the job (landscape clips for the master in master-first jobs)."""
        if ctx.fmt == "master_first":
            from ns_studio.media.longform import run_clips_master

            run_clips_master(ctx)
            return
        parts = ctx.read_json("parts.json")
        plan = ctx.read_json("visual_plan.json")
        treatment = visual_treatment(ctx.combo.visual_treatment)
        cache = ClipCache(ctx.db, ctx.paths, ctx.services.clip_sources, log=ctx.log)
        cache.purge_expired()
        searcher = Searcher(ctx.services.clip_sources, log=ctx.log)
        blocked = blocked_for_job(ctx, ("clips/shots.json",))
        out: dict[str, Any] = {}
        for p in parts:
            n = p["n"]
            timeline = ctx.read_json(f"captions/s{n}.json")
            words = [Word.from_json(w) for w in timeline["words"]]
            beats = [b for b in plan if b.get("part") in (n, None)] or [fallback_beat(ctx)]
            total_s = float(timeline["audio_s"])
            spans = beat_spans(beats, words, total_s)
            slots = plan_slots(spans, treatment.cut_range_s)
            seed = clip_seed(ctx, f"s{n}")
            shots = assign_clips(
                beats=beats, slots=slots, searcher=searcher, cache=cache, blocked=blocked, seed=seed
            )
            out[f"s{n}"] = {
                "total_s": total_s,
                "treatment": treatment.name,
                "shots": [s.to_json() for s in shots],
            }
            ctx.log.info("clips_selected", job_id=ctx.job_id, item=f"s{n}", shots=len(shots))
        ctx.write_json("clips/shots.json", out)
