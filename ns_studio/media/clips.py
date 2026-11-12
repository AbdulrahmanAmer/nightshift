"""Clip cache, keyword dedupe, clip selection with reuse rules, and the clips stage (component C6).

Spec §7.4: one search per unique keyword per source per orientation; clips cached 90 days; no clip twice in
a video, 14 days on a channel, 30 days across channels; faces skipped for dark moods; NS-E402 when a beat
cannot be filled, after a mood-only fallback search.
"""

from __future__ import annotations

import json
import random
import re
import time
import zlib
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ns_common import ffmpeg as ff
from ns_common.db import Db
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_studio.context import JobContext
from ns_studio.media.clip_sources import ClipCandidate, ClipSource, Orientation, give_log
from ns_studio.media.formats import footage_orientation, shot_range
from ns_studio.media.job_formats import job_format
from ns_studio.media.presets import VisualTreatment, visual_treatment
from ns_studio.media.shotplan import Shot, ShotSlot, beat_spans, plan_slots
from ns_studio.media.transcribe import Word
from ns_studio.stages import Tenant

CACHE_DAYS = 90
#: The clips stage logs its progress at least this often while it downloads (F5: at least once a minute).
PROGRESS_EVERY_S = 30.0
SAME_CHANNEL_DAYS = 14
OTHER_CHANNEL_DAYS = 30
DARK_MOODS = frozenset({"intimate", "horror", "eerie", "creepy", "dread", "haunting"})
FACE_WORDS = frozenset({"person", "people", "portrait", "face", "faces", "selfie"})
#: The codes of `ns_common.stock_answer`: a wrong key (E501, E502), no answer or a failing site (E503), the
#: site's limit (E504), an answer not understood (E505). The key codes say nothing about the site's state.
_API_ERRORS = {"NS-E501", "NS-E502", "NS-E503", "NS-E504", "NS-E505"}
_SITE_DOWN = {"NS-E503", "NS-E504", "NS-E505"}
# Titles and tags that mark stock footage with a burnt-in mark (spec 7.4 bullet 6); a word match on either.
WATERMARK_WORDS = frozenset({"watermark", "watermarked", "watermarks", "logo", "logos"})
WATERMARK_PHRASES = ("text overlay", "text overlays", "overlay text")
# Words that carry no picture when a multi-word keyword is split up to widen a dry search.
_FILLER_WORDS = frozenset({"the", "and", "with", "from", "into", "over", "under", "for", "that", "this"})
# Moods that read the same on screen: tried (as search words) when the beat's own keywords ran dry.
MOOD_ALTERNATES: dict[str, tuple[str, ...]] = {
    "eerie": ("creepy", "fog", "dark"),
    "creepy": ("eerie", "dark", "shadow"),
    "horror": ("dark", "creepy", "night"),
    "dread": ("dark", "storm", "night"),
    "haunting": ("eerie", "fog", "empty"),
    "intimate": ("candle", "quiet", "window"),
    "calm": ("nature", "water", "sky"),
    "tense": ("dark", "night", "storm"),
    "sad": ("rain", "window", "empty"),
    "lonely": ("empty", "night", "window"),
}

LemmaFn = Callable[[str], list[str]]


def utcnow() -> datetime:
    """Current UTC time (patched in tests through the `now` parameters)."""
    return datetime.now(UTC)


def _singular(w: str) -> str:
    """Strip a plural s (not ss, us, is) from words longer than 3 letters."""
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def make_lemmatizer(paths: Paths) -> LemmaFn | None:
    """A phrase lemmatizer over the spaCy pipeline the policy NER already loads, or None without spaCy.

    The returned function maps a lowercase phrase to its lemmas ("running mice" -> ["run", "mouse"]); an empty
    list tells the caller to use the trailing-s rule instead.
    """
    from ns_studio.policy.text_checks import _spacy_nlp

    nlp = _spacy_nlp(paths)
    if nlp is None:
        return None

    def lemmatize(phrase: str) -> list[str]:
        out: list[str] = []
        for tok in nlp(phrase):
            lemma = re.sub(r"[^a-z0-9]+", "", str(tok.lemma_).lower())
            if lemma:
                out.append(lemma)
        return out

    return lemmatize


def normalize_keyword(kw: str, lemma: LemmaFn | None = None) -> str:
    """Lowercase, trim, collapse whitespace and lemmatize ("mice" -> "mouse", "windows" -> "window").

    `lemma` is the spaCy lemmatizer (`make_lemmatizer`); without it, or when it gives nothing usable for the
    phrase, the old trailing-s rule applies.
    """
    words = re.sub(r"[^a-z0-9 ]+", " ", kw.lower()).split()
    if lemma is not None and words:
        try:
            lemmas = lemma(" ".join(words))
        except Exception:  # a broken pipeline must not stop a video: fall back to the plain rule
            lemmas = []
        if len(lemmas) == len(words):
            return " ".join(lemmas)
    return " ".join(_singular(w) for w in words)


def dedupe_keywords(keywords: Sequence[str], lemma: LemmaFn | None = None) -> list[str]:
    """Normalized keywords, first occurrence order, duplicates removed."""
    seen: dict[str, None] = {}
    for k in keywords:
        n = normalize_keyword(k, lemma)
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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db = db
        self.paths = paths
        self.sources = {s.name: s for s in sources}
        self.now = now
        self.log = log
        give_log(sources, log)
        #: Progress and cost (F5): files downloaded, bytes received (failed tries included: they cost data
        #: too), shots placed of `total`; `give_ups_in_a_row` is `assign_clips`' outage count (F1).
        self.fetched = 0
        self.bytes = 0
        self.placed = 0
        self.total = 0
        self.give_ups_in_a_row = 0
        #: Clips that could not be fetched or are black or unreadable in every window, over all parts of the
        #: stage (f5): `assign_clips` never picks them again, so a dead link costs its waits once.
        self.excluded: set[tuple[str, str]] = set()
        self._clock = clock
        self._said = clock()
        self._hooked: set[str] = set()
        for src in sources:
            if hasattr(src, "progress"):  # the stock sites report every chunk; the fake is counted per file
                src.progress = self._got_bytes  # type: ignore[attr-defined]
                self._hooked.add(src.name)

    @property
    def mb(self) -> float:
        """Megabytes received so far (Windows units, 1 MB = 1024 * 1024 bytes)."""
        return self.bytes / (1 << 20)

    def _got_bytes(self, n: int) -> None:
        self.bytes += n
        self.report()

    def shot_placed(self) -> None:
        """Count one shot done; the last one always reports."""
        self.placed += 1
        self.report(force=self.total > 0 and self.placed >= self.total)

    def report(self, *, force: bool = False) -> None:
        """Log `clips_progress` (files done of all, MB downloaded) every `PROGRESS_EVERY_S`, or now."""
        now = self._clock()
        if not force and now - self._said < PROGRESS_EVERY_S:
            return
        self._said = now
        if self.log:
            self.log.info(
                "clips_progress", f"{self.placed} of {self.total} clips, {self.mb:.0f} MB downloaded",
                done=self.placed, total=self.total, downloaded=self.fetched, mb=round(self.mb, 1),
            )

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
        self.fetched += 1
        if cand.source not in self._hooked:
            self.bytes += dest.stat().st_size if dest.exists() else 0
        self.record(cand, dest, kws)
        self.report()
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


#: Regeneration codes whose cause is the footage itself (the policy gate rejected what the clips show): the
#: redo must not pick the same clips. Every other cause (QC's black frames, loudness, a voice redo) keeps
#: them, so the redo takes them from the cache and downloads nothing it already has (footage-robust F2, C1).
FOOTAGE_REJECTED = frozenset({"NS-E611", "NS-E613"})


def _last_regen(ctx: JobContext) -> dict[str, Any] | None:
    """The history entry of the last time the job was sent back (`policy_attempts.json`), None if unknown."""
    try:
        data = json.loads(ctx.file("policy_attempts.json").read_text(encoding="utf-8"))
        last = data["history"][-1]
        return last if isinstance(last, dict) and last.get("code") else None
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None


def _black_spans(video: Path) -> list[tuple[float, float]] | None:
    """QC's own black check (`qc.black_check`, same filter) on a rendered video: its black intervals, None
    when the video is missing or cannot be read."""
    from ns_studio.media.qc import parse_blackdetect

    if not video.is_file() or not ff.available():
        return None
    qc_filter = "blackdetect=d=0.3:pic_th=0.98:pix_th=0.10"
    try:
        proc = ff.run(["-i", str(video), "-an", "-vf", qc_filter, "-f", "null", "-"])
    except Exception:  # noqa: BLE001 - FFmpeg's error, a timeout: the render counts as unreadable
        return None
    return [(a, b) for a, b, _d in parse_blackdetect(proc.stderr)]


def black_shot_clips(ctx: JobContext, previous: Sequence[str], last: dict[str, Any]) -> set[tuple[str, str]]:
    """On a QC redo for black frames (NS-E404 naming `black_frames`): the clips of the earlier attempt's
    shots under the black intervals of its rejected render, so the redo never puts the black clip back in its
    shot (every clip of that item when the render cannot be read). Every other clip stays usable (cache)."""
    detail = str(last.get("detail") or "")
    if last.get("code") != "NS-E404" or "black_frames" not in detail:
        return set()
    items = {part.split(":", 1)[0].strip() for part in detail.split(";") if "black_frames" in part}
    rejected = last.get("rejected_to")
    out: set[tuple[str, str]] = set()
    for name in previous:
        if not ctx.file(name).exists():
            continue
        for item, entry in ctx.read_json(name).items():
            if item not in items:
                continue
            render = ("long/master.mp4" if item == "long" else f"shorts/{item}/short.mp4")
            spans = _black_spans(Path(rejected) / render) if rejected else None
            for shot in entry.get("shots", []):
                start, end = float(shot["start"]), float(shot["end"])
                if spans is None or any(start < b + 0.05 and a - 0.05 < end for a, b in spans):
                    out.add((str(shot["source"]), str(shot["clip_id"])))
    if out:
        ctx.log.info("clips_blocked_black", job_id=ctx.job_id, clips=len(out))
    return out


def blocked_for_job(ctx: JobContext, previous: Sequence[str]) -> set[tuple[str, str]]:
    """`blocked_clips` plus, on a regeneration whose cause was the footage (`FOOTAGE_REJECTED`) or is not
    known, the clips listed in the earlier attempt's `previous` files; on a QC redo for black frames, the
    clips under the black (`black_shot_clips`)."""
    blocked = blocked_clips(ctx.db, ctx.channel.slug, ctx.job_id, utcnow())
    if not ctx.attempt:
        return blocked
    last = _last_regen(ctx)
    # `short_hook` shares NS-E611 but is a hook not voiced at the start (`regen.REGEN_FROM_CHECK`, redone from
    # the voice): the footage is fine and is kept (the detail starts with the check's name, `gate.run_gate`)
    hook_only = last is not None and str(last.get("detail") or "").startswith("short_hook")
    if last is not None and (str(last["code"]) not in FOOTAGE_REJECTED or hook_only):
        return blocked | black_shot_clips(ctx, previous, last)
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
        self,
        sources: Sequence[ClipSource],
        *,
        per_page: int = 80,
        log: NsLogger | None = None,
        lemma: LemmaFn | None = None,
    ) -> None:
        self.sources = list(sources)
        self.per_page = per_page
        self.log = log
        give_log(self.sources, log)
        self.lemma = lemma  # spaCy lemmatizer for keyword dedupe; None = the trailing-s rule
        self._memo: dict[tuple[str, str, str], list[ClipCandidate]] = {}
        self._down: set[str] = set()  # sources that gave NS-E503/E504/E505 once: not asked again this job
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
                    if e.code in _SITE_DOWN:  # its retries and waits are spent; do not pay them per keyword
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


def has_watermark_hint(c: ClipCandidate) -> bool:
    """True when the title or tags say the footage carries a watermark, a logo or overlaid text (spec 7.4)."""
    text = " ".join([*c.tags, c.title]).lower().replace(",", " ")
    return bool(set(text.split()) & WATERMARK_WORDS) or any(p in text for p in WATERMARK_PHRASES)


def expansion_keywords(
    keywords: Sequence[str], mood: str, extra: Sequence[str], lemma: LemmaFn | None = None
) -> list[str]:
    """More search words for a beat whose own keywords ran dry (spec 7.4: widen before reusing footage).

    Order: the single words of the beat's multi-word keywords, then `extra` (other beats of the job and the
    channel's seed keywords), then the moods that read alike. Words the beat already searched are left out.
    """
    own = set(dedupe_keywords(keywords, lemma))
    words = [
        w for k in keywords for w in re.sub(r"[^a-z0-9 ]+", " ", k.lower()).split()
        if len(k.split()) > 1 and len(w) >= 4 and w not in _FILLER_WORDS
    ]
    alternates = MOOD_ALTERNATES.get(mood.lower(), ()) if mood else ()
    return [k for k in dedupe_keywords([*words, *extra, *alternates], lemma) if k not in own]


def pick_for_slots(
    pool: Sequence[ClipCandidate],
    slots: Sequence[ShotSlot],
    *,
    used: set[tuple[str, str]],
    blocked: set[tuple[str, str]],
    dark: bool,
    wanted: Orientation = "portrait",
    exclude: set[tuple[str, str]] | frozenset[tuple[str, str]] = frozenset(),
) -> list[ClipCandidate] | None:
    """One distinct candidate per slot, or None when the pool cannot fill them all.

    Order of preference: >= 1080p, wanted orientation, pool order (keyword priority). Footage with a
    watermark, logo or text overlay in its title or tags is never picked. A clip should outlast its slot by
    1 s; a slot no such clip can fill takes the best clip that covers it with no margin, and the other slots
    keep their margin. `exclude` (clips that could not be fetched or are black) is never picked, whatever
    reuse rule the caller relaxes.
    """
    if exclude:
        pool = [c for c in pool if (c.source, c.id) not in exclude]
    ranked = sorted(
        enumerate(pool),
        key=lambda ic: (
            min(ic[1].width, ic[1].height) < 1080,
            ic[1].orientation != wanted,
            ic[0],
        ),
    )
    taken: set[tuple[str, str]] = set()
    chosen: list[ClipCandidate] = []
    for slot in slots:
        hit: ClipCandidate | None = None
        for margin in (1.0, 0.0):
            hit = next(
                (
                    c
                    for _i, c in ranked
                    if (c.source, c.id) not in used | blocked | taken
                    and c.duration >= slot.length + margin
                    and not (dark and has_face_hint(c))
                    and not has_watermark_hint(c)
                ),
                None,
            )
            if hit is not None:
                break
        if hit is None:
            return None
        taken.add((hit.source, hit.id))
        chosen.append(hit)
    return chosen


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
    extra_keywords: Sequence[str] = (),
    relaxed: set[tuple[str, str]] | None = None,
    exclude: set[tuple[str, str]] | frozenset[tuple[str, str]] = frozenset(),
) -> list[tuple[ClipCandidate, str]]:
    """Fill a beat's slots; widen the search in steps and finally fall back to the mood (spec §7.4).

    Steps: keywords in the wanted orientation, then the other orientation, then the mood word in both, then
    more words (the beat's keyword parts, `extra_keywords`, moods that read alike) in both. Only when all of
    that finds nothing fresh are the reuse windows dropped (NS-W405); the clips picked that way are added to
    `relaxed` so the final gate can tell them from a violation. Raises NsError NS-E402 when nothing suffices,
    or the first API error when every source failed.
    """
    other: Orientation = "landscape" if wanted == "portrait" else "portrait"
    dark = mood.lower() in DARK_MOODS
    lemma = searcher.lemma
    kws = dedupe_keywords(keywords, lemma)
    mood_kws = dedupe_keywords([mood], lemma) if mood else []
    more_kws = [k for k in expansion_keywords(keywords, mood, extra_keywords, lemma) if k not in mood_kws]
    steps: list[tuple[Orientation, list[str]]] = [
        (wanted, kws), (other, kws), (wanted, mood_kws), (other, mood_kws), (wanted, more_kws),
        (other, more_kws),
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
            [c for c, _k in pool], slots, used=used, blocked=blocked, dark=dark, wanted=wanted,
            exclude=exclude,
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
        # slots"). Clips are still never repeated inside this video (`used`) unless even that cannot be met.
        candidates = [c for c, _k in pool if (c.source, c.id) not in exclude]
        relaxed_picks = pick_for_slots(candidates, slots, used=used, blocked=set(), dark=dark, wanted=wanted)
        how = "earlier footage is reused"
        if relaxed_picks is None:
            # the pool is smaller than the video (a master plus its Shorts on a niche with few clips): a clip
            # may appear twice in the video rather than lose it
            relaxed_picks = pick_for_slots(
                candidates, slots, used=set(), blocked=set(), dark=dark, wanted=wanted
            )
            how = "footage from this video is repeated"
        if relaxed_picks is not None:
            if searcher.log:
                searcher.log.warn("footage_reused", code="NS-W405", keywords=list(kws), mood=mood,
                                  msg=f"no fresh stock clip for this beat; {how}")
            if relaxed is not None:
                relaxed.update((c.source, c.id) for c in relaxed_picks)
            kw_of = {(c.source, c.id): k for c, k in pool}
            return [(c, kw_of[(c.source, c.id)]) for c in relaxed_picks]
    raise NsError("NS-E402", f"keywords {kws} mood {mood!r}: {len(pool)} candidates for {len(slots)} slots")


#: A whole clip that still could not be fetched after its own retries (F1) of these codes says the site or the
#: line is down: after this many in a row the stage stops (each one costs up to 22 s of waits plus the read
#: timeouts), instead of walking every candidate of every beat through the same outage.
MAX_GIVE_UPS_IN_A_ROW = 3
_OUTAGE_CODES = {"NS-E503", "NS-E504"}
#: Download failures that cost one clip, not the job: the next candidate fills the slot (F1).
_SKIPPABLE = _OUTAGE_CODES | {"NS-E505"}
#: The black probe (F3): QC's thresholds (`qc.black_check`, pix_th 0.10, pic_th 0.98) at a third of QC's
#: shortest interval, so two black edges that meet at a cut stay under QC's 0.3 s.
PROBE_MIN_BLACK_S = 0.1
PROBE_GUARD_S = 0.1
_FRAMES_RE = re.compile(r"frame=\s*(\d+)")
#: What FFmpeg says when the filter chain itself (not the clip) is wrong: the same on every clip.
_FILTER_FAILED = (
    "error initializing filter", "error parsing filter", "no such filter", "error reinitializing filters",
    "unrecognized option", "failed to configure",
)


def _filter_failed(detail: str) -> bool:
    """True when FFmpeg's error text says the black check's own filter chain failed."""
    low = detail.lower()
    return any(s in low for s in _FILTER_FAILED)


def spare_bound(n_slots: int) -> int:
    """Files a part may download beyond its slots (F2): clips found black or unreadable after the download.

    2 + 10 % of the slots: a stock clip that is black in every window of its slot is rare (most black is a
    fade at one end, fixed by another in-point at no data cost), and each spare costs about 16 MB (the rc5
    average), so a Short of 77 shots may spend 9 spares (about 140 MB) and no more.
    """
    return 2 + n_slots // 10


#: (clip file, in-point, length) -> black intervals (start, end) in the clip's own time, or None when the clip
#: cannot be decoded. `length` None: the whole clip.
BlackProbe = Callable[[Path, float, float | None], "list[tuple[float, float]] | None"]


def treatment_probe(treatment: VisualTreatment, size: tuple[int, int]) -> BlackProbe:
    """A `BlackProbe` that sees a clip as the render does: cover-cropped to the canvas's shape and coloured by
    the treatment's eq, curves, colour balance and vignette (`render.post_chain`, grain left out: grain only
    lifts pixels, so leaving it out can only call more frames black), then FFmpeg's blackdetect at QC's
    thresholds."""
    from ns_studio.media.qc import parse_blackdetect
    from ns_studio.media.render import post_chain

    w, h = size
    pw, ph = max(2, w // 8 // 2 * 2), max(2, h // 8 // 2 * 2)
    colour = post_chain(treatment.model_copy(update={"grain": 0}), None, None)
    chain = (
        f"scale={pw}:{ph}:force_original_aspect_ratio=increase,crop={pw}:{ph},{colour},"
        f"blackdetect=d={PROBE_MIN_BLACK_S}:pic_th=0.98:pix_th=0.10"
    )

    def probe(path: Path, ss: float, length: float | None) -> list[tuple[float, float]] | None:
        window = ["-ss", f"{ss:.3f}", "-t", f"{length:.3f}"] if length is not None else []
        try:
            proc = ff.run([*window, "-i", str(path), "-an", "-vf", chain, "-f", "null", "-"], timeout_s=600)
        except NsError as e:
            # FFmpeg ran and refused the file: the clip is unreadable (replace it). FFmpeg that did not
            # start, or a check of ours it cannot build, says nothing about the clip: stop (f4).
            if "cmd" not in e.data or _filter_failed(e.detail):
                raise NsError("NS-E401", f"The black check could not run: {e.detail}") from e
            return None
        except Exception as e:  # noqa: BLE001 - FFmpeg would not start, or hung past the limit: not the clip
            raise NsError("NS-E401", f"The black check could not run: {type(e).__name__} {e}"[:400]) from e
        counts = _FRAMES_RE.findall(proc.stderr)
        if not counts or int(counts[-1]) == 0:  # no frame decoded: the render would get an empty shot
            return None
        offset = ss if length is not None else 0.0
        return [(offset + a, offset + b) for a, b, _d in parse_blackdetect(proc.stderr)]

    return probe


def clear_in_point(
    probe: BlackProbe, path: Path, length: float, ss: float, rng: random.Random
) -> float | None:
    """An in-point whose window of `length` s shows no black (F3): `ss` when its window is clear, else one
    drawn from the clip's clear stretches (one probe of the whole clip), else None (black or unreadable
    throughout).
    """
    first = probe(path, ss, length + 0.2)
    if first == []:
        return ss
    spans = probe(path, 0.0, None)
    if spans is None:
        return None
    try:
        end = ff.duration_s(path)
    except NsError as e:
        if "path" not in e.data:  # ffprobe is not there: not a fact about this clip (f4)
            raise
        return None
    except (OSError, KeyError, ValueError):
        return None
    fits: list[tuple[float, float]] = []
    cursor = 0.0
    for a, b in sorted(spans):
        if a - PROBE_GUARD_S - cursor >= length + 0.2:
            fits.append((cursor, a - PROBE_GUARD_S - length - 0.2))
        cursor = max(cursor, b + PROBE_GUARD_S)
    if end - cursor >= length + 0.2:
        fits.append((cursor, end - length - 0.2))
    if not fits:
        return None
    lo, hi = fits[rng.randrange(len(fits))]
    return round(rng.uniform(lo, max(lo, hi)), 2)


def _slot_name(label: str, slots: Sequence[ShotSlot], slot: ShotSlot) -> str:
    where = f"{label} " if label else ""
    return f"{where}shot {list(slots).index(slot) + 1} ({slot.start:.1f}-{slot.end:.1f} s)"


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
    extra_keywords: Sequence[str] = (),
    probe: BlackProbe | None = None,
    label: str = "",
) -> list[Shot]:
    """Choose, download and time-place one clip per slot; clips are never repeated within the video.

    `extra_keywords` (the channel's seed keywords) join the other beats' keywords as search words for a beat
    whose own pool runs dry. A shot picked by the NS-W405 relaxation carries `relaxed=True`.

    A clip that could not be fetched after its retries (NS-E503/E504/E505) is left out and the slot is filled
    from the next candidate (F1); the stage fails only when a slot has no candidate left, or after
    `MAX_GIVE_UPS_IN_A_ROW` outages in a row, with the slot named. With `probe` every shot's window is checked
    for black: another in-point of the same clip first, else the next candidate, at most `spare_bound` extra
    downloads (F3, F2). `label` names the part in an error ("s1").
    """
    rng = random.Random(seed)
    used = used if used is not None else set()
    shots: list[Shot] = []
    spares_left = spare_bound(len(slots))
    excluded = cache.excluded  # the stage's: a dead or black clip is not tried again
    for bi, beat in enumerate(beats):
        pending = [s for s in slots if s.beat == bi]
        elsewhere = [k for j, b in enumerate(beats) if j != bi for k in b["keywords"]]
        relaxed: set[tuple[str, str]] = set()
        last_failure: NsError | None = None
        while pending:
            try:
                picks = select_for_beat(
                    searcher, beat["keywords"], str(beat.get("mood", "")), pending,
                    used=used, blocked=blocked, wanted=wanted, allow_other=allow_other,
                    extra_keywords=[*elsewhere, *extra_keywords], relaxed=relaxed, exclude=excluded,
                )
            except NsError as e:
                if last_failure is None or e.code != "NS-E402":
                    raise
                # a shortage (NS-E402), not a network error: the dead clips are why the pool ran out, and the
                # owner's fix is the footage (keywords, keys), not the line (f5)
                raise NsError(
                    "NS-E402",
                    f"{e.detail} Clips that could not be fetched ({last_failure.code}): "
                    f"{last_failure.detail} "
                    f"No other clip was left for {_slot_name(label, slots, pending[0])}.",
                ) from e
            for slot, (cand, kw) in zip(list(pending), picks, strict=True):
                key = (cand.source, cand.id)
                fetched_before = cache.fetched
                try:
                    path = cache.ensure(cand, [kw])
                    cache.give_ups_in_a_row = 0
                except NsError as e:
                    if e.code not in _SKIPPABLE:
                        raise
                    if e.code in _OUTAGE_CODES:
                        cache.give_ups_in_a_row += 1
                    if cache.log:
                        cache.log.warn("clip_skipped", source=cand.source, clip_id=cand.id, code=e.code,
                                       slot=_slot_name(label, slots, slot), msg="the next candidate is tried")
                    if cache.give_ups_in_a_row >= MAX_GIVE_UPS_IN_A_ROW:
                        raise NsError(
                            e.code,
                            f"{e.detail} {cache.give_ups_in_a_row} clips in a row could not be fetched; "
                            f"stopped at {_slot_name(label, slots, slot)}.",
                        ) from e
                    excluded.add(key)
                    last_failure = e
                    break  # the rest of the beat's slots are chosen again, without this clip
                ss = round(rng.uniform(0.0, max(0.0, cand.duration - slot.length - 0.2)), 2)
                if probe is not None:
                    clear = clear_in_point(probe, path, slot.length, ss, rng)
                    if clear is None:
                        excluded.add(key)
                        downloaded = cache.fetched > fetched_before
                        if cache.log:
                            cache.log.warn("clip_black", source=cand.source, clip_id=cand.id,
                                           slot=_slot_name(label, slots, slot), downloaded=downloaded,
                                           msg="black or unreadable in every window; next candidate")
                        if downloaded:
                            spares_left -= 1
                            if spares_left < 0:
                                raise NsError(
                                    "NS-E402",
                                    f"the footage found for {_slot_name(label, slots, slot)} is black; "
                                    f"{spare_bound(len(slots))} spare downloads are spent",
                                )
                        break
                    ss = clear
                used.add(key)
                pending.remove(slot)
                shots.append(
                    Shot(bi, slot.start, slot.end, cand.source, cand.id, str(path), ss, cand.width,
                         cand.height, cand.page_url, cand.license, kw, key in relaxed)
                )
                cache.shot_placed()
    shots.sort(key=lambda s: s.start)
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
        fmt = job_format(ctx, "short")
        cache = ClipCache(ctx.db, ctx.paths, ctx.services.clip_sources, log=ctx.log)
        cache.purge_expired()
        searcher = Searcher(ctx.services.clip_sources, log=ctx.log, lemma=make_lemmatizer(ctx.paths))
        blocked = blocked_for_job(ctx, ("clips/shots.json",))
        probe = treatment_probe(treatment, (fmt.canvas.w, fmt.canvas.h)) if ff.available() else None
        planned: list[tuple[int, list[dict[str, Any]], list[ShotSlot], float]] = []
        for p in parts:
            n = p["n"]
            timeline = ctx.read_json(f"captions/s{n}.json")
            words = [Word.from_json(w) for w in timeline["words"]]
            beats = [b for b in plan if b.get("part") in (n, None)] or [fallback_beat(ctx)]
            total_s = float(timeline["audio_s"])
            spans = beat_spans(beats, words, total_s)
            planned.append((n, beats, plan_slots(spans, shot_range(treatment.cut_range_s, fmt)), total_s))
        cache.total = sum(len(slots) for _n, _b, slots, _t in planned)
        cache.report(force=True)  # "0 of N clips": the owner sees the size of the job before the first file
        out: dict[str, Any] = {}
        try:
            for n, beats, slots, total_s in planned:
                shots = assign_clips(
                    beats=beats, slots=slots, searcher=searcher, cache=cache, blocked=blocked,
                    seed=clip_seed(ctx, f"s{n}"), wanted=footage_orientation(fmt),
                    extra_keywords=ctx.channel.seed_keywords, probe=probe, label=f"s{n}",
                )
                out[f"s{n}"] = {
                    "total_s": total_s,
                    "treatment": treatment.name,
                    "shots": [s.to_json() for s in shots],
                }
                ctx.log.info("clips_selected", job_id=ctx.job_id, item=f"s{n}", shots=len(shots))
        finally:  # what the footage cost, also when the stage failed half way (F5)
            record_footage_cost(ctx, cache)
        ctx.write_json("clips/shots.json", out)


def record_footage_cost(ctx: JobContext, cache: ClipCache) -> None:
    """Add this run of the clips stage to `clips/footage.json` (the job's record of the data its footage
    cost: every run, a redo included, and the total) and log it. Never raises: a record must not end a job."""
    try:
        f = ctx.file("clips/footage.json")
        try:
            runs = list(json.loads(f.read_text(encoding="utf-8")).get("runs", []))
        except (OSError, ValueError, AttributeError):
            runs = []
        runs.append({"attempt": ctx.attempt, "files_downloaded": cache.fetched, "bytes": cache.bytes,
                     "mb": round(cache.mb, 1), "shots": cache.placed})
        total = sum(int(r.get("bytes", 0)) for r in runs if isinstance(r, dict))
        job_mb = round(total / (1 << 20), 1)
        ctx.write_json("clips/footage.json", {"runs": runs, "bytes": total, "mb": job_mb})
        ctx.log.info("footage_cost", f"{cache.fetched} files, {cache.mb:.0f} MB downloaded",
                     job_id=ctx.job_id, files=cache.fetched, mb=round(cache.mb, 1),
                     job_mb=job_mb)
    except Exception as e:  # noqa: BLE001 - the record never stops a job
        ctx.log.warn("footage_cost_not_recorded", str(e)[:200])
