"""Stock footage sources: Pexels, Pixabay and a fake (component C6).

This is the only studio module that talks to the stock APIs over HTTP (ENGINEERING.md section 8).
Keys are passed in by the caller; they are never logged. Rate limits follow spec §7.4: a token bucket of
180 requests/hour for Pexels and 90/minute for Pixabay, with `429` honoured via `Retry-After`/reset headers.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from ns_common import ffmpeg as ff
from ns_common.errors import NsError

Orientation = Literal["landscape", "portrait"]
PEXELS_URL = "https://api.pexels.com"
PIXABAY_URL = "https://pixabay.com/api/videos/"
PIXABAY_CACHE_TTL_S = 24 * 3600


@dataclass(frozen=True)
class ClipCandidate:
    """One search hit: enough to rank it and to download it later."""

    source: str
    id: str
    page_url: str
    download_url: str
    width: int
    height: int
    duration: float
    tags: tuple[str, ...]
    title: str
    license: str

    @property
    def orientation(self) -> Orientation:
        """`portrait` when taller than wide, else `landscape`."""
        return "portrait" if self.height > self.width else "landscape"


class ClipSource(Protocol):
    """A searchable stock footage provider."""

    name: str

    def search(self, keyword: str, *, orientation: Orientation, per_page: int = 80) -> list[ClipCandidate]:
        """Candidates for `keyword` in `orientation`."""
        ...

    def download(self, cand: ClipCandidate, dest: Path) -> Path:
        """Download the candidate to `dest` (an .mp4 path) and return it."""
        ...


class TokenBucket:
    """Token bucket with injectable clock and sleep (no real waiting in tests)."""

    def __init__(
        self,
        capacity: float,
        refill_per_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.capacity = capacity
        self.refill_per_s = refill_per_s
        self.clock = clock
        self.sleep = sleep
        self.tokens = capacity
        self._last = clock()

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.refill_per_s)
        self._last = now

    def acquire(self, n: float = 1.0) -> float:
        """Take `n` tokens, sleeping until they are available. Returns the seconds slept."""
        self._refill()
        waited = 0.0
        if self.tokens < n:
            wait = (n - self.tokens) / self.refill_per_s
            self.sleep(wait)
            waited = wait
            self._refill()
        self.tokens -= n
        return waited


def retry_wait_s(resp: httpx.Response, wall_now: float) -> float:
    """Seconds to wait after a 429: Retry-After, else the reset header (epoch or seconds); 1..3600."""
    ra = resp.headers.get("Retry-After")
    reset = resp.headers.get("X-RateLimit-Reset") or resp.headers.get("X-Ratelimit-Reset")
    wait = 60.0
    try:
        if ra is not None:
            wait = float(ra)
        elif reset is not None:
            v = float(reset)
            wait = v - wall_now if v > 1e9 else v
    except ValueError:
        wait = 60.0
    return min(3600.0, max(1.0, wait))


class _HttpSource:
    """Shared HTTP plumbing: token bucket, 429 retry, coded errors, streamed download."""

    name = ""
    api_error = "NS-E501"

    def __init__(
        self,
        key: str,
        cache_dir: Path,
        *,
        client: httpx.Client | None,
        bucket: TokenBucket,
        sleep: Callable[[float], None],
        wall: Callable[[], float],
    ) -> None:
        self._key = key
        self.cache_dir = cache_dir
        self.client = client or httpx.Client(timeout=60, follow_redirects=True)
        self.bucket = bucket
        self._sleep = sleep
        self._wall = wall

    def _get(
        self, url: str, *, params: dict[str, Any], headers: dict[str, str] | None = None
    ) -> httpx.Response:
        if not self._key:
            raise NsError(self.api_error, f"{self.name} API key missing")
        for _attempt in range(4):
            self.bucket.acquire()
            try:
                r = self.client.get(url, params=params, headers=headers)
            except httpx.HTTPError as e:
                raise NsError("NS-E503", f"{self.name}: {type(e).__name__}") from e
            if r.status_code == 429:
                self._sleep(retry_wait_s(r, self._wall()))
                continue
            if r.status_code >= 400:
                raise NsError(self.api_error, f"HTTP {r.status_code}")
            return r
        raise NsError(self.api_error, "rate limited (429) after 4 attempts")

    def _fetch_file(self, url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        try:
            with self.client.stream("GET", url, follow_redirects=True) as r:
                if r.status_code >= 400:
                    raise NsError(self.api_error, f"download HTTP {r.status_code}")
                with part.open("wb") as fh:
                    for chunk in r.iter_bytes(1 << 16):
                        fh.write(chunk)
        except httpx.HTTPError as e:
            part.unlink(missing_ok=True)
            raise NsError("NS-E503", f"{self.name} download: {type(e).__name__}") from e
        except NsError:
            part.unlink(missing_ok=True)
            raise
        part.replace(dest)
        return dest


def _slug_words(page_url: str) -> tuple[str, ...]:
    """Title words from a Pexels page URL slug: `.../video/dark-forest-3571264/` -> ('dark', 'forest')."""
    slug = page_url.rstrip("/").rsplit("/", 1)[-1]
    words = [w for w in slug.split("-") if w and not w.isdigit()]
    return tuple(w.lower() for w in words)


def _usable_pexels_file(f: dict[str, Any]) -> bool:
    """An mp4 rendition with a link and known dimensions (HLS playlists are skipped)."""
    link = str(f.get("link") or "")
    is_mp4 = f.get("file_type") == "video/mp4" or link.split("?")[0].endswith(".mp4")
    return bool(link and is_mp4 and f.get("width") and f.get("height"))


def parse_pexels(data: dict[str, Any], orientation: Orientation) -> list[ClipCandidate]:
    """Candidates from a Pexels `/videos/search` response (best mp4 file per video)."""
    out: list[ClipCandidate] = []
    for v in data.get("videos", []):
        files = [f for f in v.get("video_files", []) if _usable_pexels_file(f)]
        if not files:
            continue
        hd = [f for f in files if min(int(f["width"]), int(f["height"])) >= 1080]
        pick = (
            min(hd, key=lambda f: int(f["width"]) * int(f["height"]))
            if hd
            else max(files, key=lambda f: int(f["width"]) * int(f["height"]))
        )
        words = _slug_words(str(v.get("url", "")))
        out.append(
            ClipCandidate(
                source="pexels",
                id=str(v["id"]),
                page_url=str(v.get("url", "")),
                download_url=str(pick["link"]),
                width=int(pick["width"]),
                height=int(pick["height"]),
                duration=float(v.get("duration", 0)),
                tags=words,
                title=" ".join(words),
                license="Pexels License",
            )
        )
    return out


class PexelsSource(_HttpSource):
    """Pexels video search (`Authorization: <key>`, `orientation`, page size up to 80)."""

    name = "pexels"
    api_error = "NS-E501"

    def __init__(
        self,
        key: str,
        cache_dir: Path,
        *,
        client: httpx.Client | None = None,
        bucket: TokenBucket | None = None,
        sleep: Callable[[float], None] = time.sleep,
        wall: Callable[[], float] = time.time,
        base_url: str = PEXELS_URL,
    ) -> None:
        super().__init__(
            key, cache_dir, client=client, bucket=bucket or TokenBucket(180, 180 / 3600.0, sleep=sleep),
            sleep=sleep, wall=wall,
        )
        self.base_url = base_url.rstrip("/")

    def search(self, keyword: str, *, orientation: Orientation, per_page: int = 80) -> list[ClipCandidate]:
        """Search Pexels; `size=medium` asks for Full HD or better."""
        r = self._get(
            f"{self.base_url}/videos/search",
            params={"query": keyword, "orientation": orientation, "size": "medium",
                    "per_page": min(80, per_page), "page": 1},
            headers={"Authorization": self._key},
        )
        return parse_pexels(r.json(), orientation)

    def download(self, cand: ClipCandidate, dest: Path) -> Path:
        """Stream the chosen mp4 to `dest` (the CDN link needs no Authorization header)."""
        return self._fetch_file(cand.download_url, dest)


MASTER_SHORT_SIDE = 1080


def pick_pixabay_rendition(renditions: dict[str, Any]) -> dict[str, Any] | None:
    """The smallest rendition that still covers a 1080p master, else the biggest one there is.

    Pixabay (2026) serves `large` at 3840x2160 (~35 MB for 17 s), `medium` at 2560x1440, `small` at
    1920x1080 and `tiny` at 1280x720; older videos are one step smaller each. A 1080p master gains nothing
    from 4K, so the first rendition (smallest first) whose short side is >= 1080 is taken, which keeps a
    clip download at about 10 MB instead of 35. An empty `large` (url "", size 0) is skipped.
    """
    usable: list[dict[str, Any]] = []
    for size in ("tiny", "small", "medium", "large"):
        r = renditions.get(size) or {}
        if r.get("url") and int(r.get("width") or 0) > 0 and int(r.get("height") or 0) > 0:
            usable.append(r)
    for r in usable:
        if min(int(r["width"]), int(r["height"])) >= MASTER_SHORT_SIDE:
            return r
    return usable[-1] if usable else None


def parse_pixabay(data: dict[str, Any], orientation: Orientation) -> list[ClipCandidate]:
    """Candidates from a Pixabay `/api/videos/` response, filtered to the wanted aspect ratio.

    Pixabay has no orientation parameter for videos, so portrait keeps height/width >= 1.2 and landscape
    keeps width/height >= 1.2 (using the best available rendition).
    """
    out: list[ClipCandidate] = []
    for h in data.get("hits", []):
        renditions = h.get("videos", {})
        pick = pick_pixabay_rendition(renditions)
        if pick is None:
            continue
        w, ht = int(pick["width"]), int(pick["height"])
        ratio = ht / w if orientation == "portrait" else w / ht
        if ratio < 1.2:
            continue
        tags = tuple(t.strip().lower() for t in str(h.get("tags", "")).split(",") if t.strip())
        out.append(
            ClipCandidate(
                source="pixabay",
                id=str(h["id"]),
                page_url=str(h.get("pageURL", "")),
                download_url=str(pick["url"]),
                width=w,
                height=ht,
                duration=float(h.get("duration", 0)),
                tags=tags,
                title=", ".join(tags),
                license="Pixabay Content License",
            )
        )
    return out


class PixabaySource(_HttpSource):
    """Pixabay video search with the required 24 h response cache and client-side ratio filtering."""

    name = "pixabay"
    api_error = "NS-E502"

    def __init__(
        self,
        key: str,
        cache_dir: Path,
        *,
        client: httpx.Client | None = None,
        bucket: TokenBucket | None = None,
        sleep: Callable[[float], None] = time.sleep,
        wall: Callable[[], float] = time.time,
        base_url: str = PIXABAY_URL,
        cache_ttl_s: float = PIXABAY_CACHE_TTL_S,
    ) -> None:
        super().__init__(
            key, cache_dir, client=client, bucket=bucket or TokenBucket(90, 90 / 60.0, sleep=sleep),
            sleep=sleep, wall=wall,
        )
        self.base_url = base_url
        self.cache_ttl_s = cache_ttl_s
        self.api_calls = 0

    def _cache_file(self, keyword: str, per_page: int) -> Path:
        digest = hashlib.sha256(f"{keyword.lower()}|{per_page}".encode()).hexdigest()[:24]
        return self.cache_dir / "pixabay" / f"{digest}.json"

    def search(self, keyword: str, *, orientation: Orientation, per_page: int = 80) -> list[ClipCandidate]:
        """Search Pixabay, serving responses younger than 24 h from `cache_dir/pixabay/`."""
        per_page = min(200, max(3, per_page))
        cache = self._cache_file(keyword, per_page)
        data: dict[str, Any] | None = None
        if cache.exists():
            try:
                stored = json.loads(cache.read_text(encoding="utf-8"))
                if self._wall() - float(stored["fetched_at"]) < self.cache_ttl_s:
                    data = stored["response"]
            except (ValueError, KeyError, OSError):
                data = None
        if data is None:
            r = self._get(
                self.base_url,
                params={"key": self._key, "q": keyword, "video_type": "film", "per_page": per_page,
                        "safesearch": "true", "page": 1},
            )
            self.api_calls += 1
            data = r.json()
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"fetched_at": self._wall(), "response": data}), encoding="utf-8")
        return parse_pixabay(data or {}, orientation)

    def download(self, cand: ClipCandidate, dest: Path) -> Path:
        """Stream the chosen rendition to `dest`."""
        return self._fetch_file(cand.download_url, dest)


_PALETTE = ("0x3a5f8a", "0x8a5a3a", "0x4a7a5a", "0x7a4a7a")


def _slug(keyword: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-") or "clip"


class FakeClipSource:
    """Synthetic clips: `search` invents deterministic candidates, `download` renders them with FFmpeg.

    Candidates advertise 1920x1080 (or 1080x1920) but the files are half size to keep CI fast.
    """

    name = "fake"

    def __init__(self, root: Path, *, per_keyword: int = 6) -> None:
        self.root = root
        self.per_keyword = per_keyword
        self.searches: list[tuple[str, str]] = []
        self.downloads: list[str] = []

    def search(self, keyword: str, *, orientation: Orientation, per_page: int = 80) -> list[ClipCandidate]:
        """`per_keyword` deterministic candidates tagged with the keyword."""
        self.searches.append((keyword, orientation))
        w, h = (1080, 1920) if orientation == "portrait" else (1920, 1080)
        slug = _slug(keyword)
        tag = hashlib.sha1(f"{keyword}|{orientation}".encode()).hexdigest()[:6]
        words = tuple(keyword.lower().split())
        return [
            ClipCandidate(
                source="fake",
                id=f"{slug}-{tag}-{i}",
                page_url=f"fake://{slug}/{i}",
                download_url=f"fake://{slug}-{tag}-{i}",
                width=w,
                height=h,
                duration=10.0 + (i % 3),
                tags=(*words, "fake"),
                title=f"fake {keyword} {i}",
                license="fake",
            )
            for i in range(self.per_keyword)
        ][:per_page]

    def download(self, cand: ClipCandidate, dest: Path) -> Path:
        """Render a testsrc2 or flat-colour clip of the candidate's duration."""
        self.downloads.append(cand.id)
        idx = int(cand.id.rsplit("-", 1)[-1])
        w, h = (cand.width // 2, cand.height // 2)
        kind: Literal["testsrc2", "color"] = "testsrc2" if idx % 2 == 0 else "color"
        return ff.make_test_clip(
            dest, width=w, height=h, seconds=cand.duration, kind=kind, color=_PALETTE[idx % len(_PALETTE)]
        )
