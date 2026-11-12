"""Stock footage sources: Pexels, Pixabay and a fake (component C6).

This is the only studio module that talks to the stock APIs over HTTP (ENGINEERING.md section 8).
Keys are passed in by the caller; they are never logged. Rate limits follow spec §7.4: a token bucket of
180 requests/hour for Pexels and 90/minute for Pixabay, with `429` honoured via `Retry-After`/reset headers.
Every answer (search, download, key check) is read by the one classifier, `ns_common.stock_answer`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from ns_common import ffmpeg as ff
from ns_common import stock_answer as sa
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_studio.media.diskfull import classify_ffmpeg_error, disk_full_error, is_disk_full, remove_partial

Orientation = Literal["landscape", "portrait"]
#: The documented bases (`stock_answer`): Pexels' search is `<PEXELS_URL>/videos/search`. Read when a source
#: is made (a stand-in server in a test replaces them).
PEXELS_URL = sa.PEXELS_BASE
PIXABAY_URL = sa.PIXABAY_BASE
#: The one bounded request of the check before a job (`footage_keys`): no retry, at most this long.
KEY_CHECK_TIMEOUT_S = sa.TIMEOUT_S
PIXABAY_CACHE_TTL_S = 24 * 3600
OUTAGE_BACKOFF_S = (2.0, 5.0, 15.0)  # waits before the retries after a 5xx, a timeout or a reset connection


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


def give_log(sources: Sequence[ClipSource], log: NsLogger | None) -> None:
    """Let the stock-API sources log their waits and retries to the job's log (they are built without one)."""
    if log is None:
        return
    for src in sources:
        if isinstance(src, _HttpSource) and src.log is None:
            src.log = log


def retry_wait_s(resp: httpx.Response, wall_now: float) -> float:
    """Seconds to wait after a 429: Retry-After, else the reset header (epoch or seconds); 1..3600."""
    return sa.wait_s(resp.headers, wall_now)


def httpx_fetch(client: httpx.Client, timeout: float = KEY_CHECK_TIMEOUT_S) -> sa.Fetch:
    """`stock_answer.check_key`'s one request, sent through a source's own client (no answer: OSError with the
    error's type only, as an httpx message can carry the address and a Pixabay address carries the key)."""

    def fetch(req: urllib.request.Request) -> tuple[int, Mapping[str, str], bytes]:
        try:
            r = client.get(req.full_url, headers=dict(req.header_items()), timeout=timeout)
        except httpx.HTTPError as e:
            raise OSError(type(e).__name__) from None
        return r.status_code, r.headers, r.content

    return fetch


def keyed(sources: Sequence[ClipSource]) -> list[_HttpSource]:
    """The real stock sources among `sources` that have a key (the fake one has none)."""
    return [s for s in sources if isinstance(s, _HttpSource) and s.has_key]


class _HttpSource:
    """Shared HTTP plumbing: token bucket, 429 retry, coded errors, streamed download."""

    name = ""
    api_error = "NS-E501"
    base_url = ""

    def __init__(
        self,
        key: str,
        cache_dir: Path,
        *,
        client: httpx.Client | None,
        bucket: TokenBucket,
        sleep: Callable[[float], None],
        wall: Callable[[], float],
        log: NsLogger | None = None,
    ) -> None:
        self.log = log
        #: Called with the size of every chunk a download writes (the clips stage's MB count, F5); None: off.
        self.progress: Callable[[int], None] | None = None
        self._key = key
        self.cache_dir = cache_dir
        self.client = client or httpx.Client(timeout=60, follow_redirects=True)
        self.bucket = bucket
        self._sleep = sleep
        self._wall = wall

    @property
    def host(self) -> str:
        """The name the owner knows the host by ("Pexels", "Pixabay")."""
        return self.name.capitalize()

    @property
    def has_key(self) -> bool:
        """True when a key was given (the key itself is never handed out by name)."""
        return bool(self._key)

    @property
    def key_id(self) -> str:
        """A short hash that tells two keys apart (the check's cache); the key cannot be read back from it."""
        from ns_studio.footage_keys import stock_key_id

        return stock_key_id(self.name, self._key)

    def check_key(self) -> sa.Answer:
        """The check before a job (`footage_keys`): ONE request at this source's search endpoint, at most
        `KEY_CHECK_TIMEOUT_S`, no retry, classified by `stock_answer`."""
        return sa.check_key(self.name, self._key, fetch=httpx_fetch(self.client), base=self.base_url)

    def _classify(self, r: httpx.Response | None = None, *, failed: Exception | None = None,
                  expect: str | None = None, key_sent: bool = True) -> sa.Answer:
        if isinstance(failed, httpx.TransportError):  # timeouts, refused or reset connections, DNS
            return sa.classify(self.name, no_answer=type(failed).__name__)
        if failed is not None or r is None:  # an answer that could not be read (decoding, redirects)
            return sa.classify(self.name, unreadable=type(failed).__name__ if failed else "no answer")
        body = r.content if r.status_code != 429 else b""
        return sa.classify(self.name, status=r.status_code, headers=r.headers, body=body, expect=expect,
                           key_sent=key_sent, wall_now=self._wall())

    def _get(
        self, url: str, *, params: dict[str, Any], headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """The parsed answer of one search; every other answer is classified (`stock_answer`): a wrong key or
        an answer not understood raise at once, a 429 is waited out (four times), a 5xx or no answer is tried
        again after `OUTAGE_BACKOFF_S`."""
        if not self._key:
            raise NsError(self.api_error, f"{self.name} API key missing")
        rate_limited = 0
        outage_retries = 0
        while True:
            self.bucket.acquire()
            r: httpx.Response | None = None
            try:
                r = self.client.get(url, params=params, headers=headers)
                answer = self._classify(r, expect=sa.EXPECT[self.name])
            except httpx.HTTPError as e:
                answer = self._classify(failed=e)
            if answer.ok:
                return dict(answer.data)
            if answer.kind == sa.KEY_WRONG:  # an "ok" kept by the check before the job is wrong now: fix it
                from ns_studio.footage_keys import remember_refused

                if remember_refused(self.cache_dir, self.name, self.key_id, answer.why) and self.log:
                    self.log.warn("stock_key_refused_by_search", source=self.name, code=answer.code)
            if answer.kind in (sa.KEY_WRONG, sa.UNEXPECTED):
                raise answer.error()
            if answer.kind == sa.RATE_LIMITED:
                rate_limited += 1
                wait = retry_wait_s(r, self._wall()) if r is not None else sa.DEFAULT_WAIT_S
                if self.log:
                    self.log.warn("footage_rate_limited", source=self.name, wait_s=wait, attempt=rate_limited)
                self._sleep(wait)
                if rate_limited >= 4:
                    raise answer.error(f"Asked {rate_limited} times.", gave_up=True)
                continue
            if outage_retries >= len(OUTAGE_BACKOFF_S):  # FAILING or NO_CONNECTION
                if self.log:
                    self.log.error("footage_host_down", source=self.name, reason=answer.why)
                raise answer.error(f"Tried {outage_retries + 1} times.", gave_up=True)
            wait = OUTAGE_BACKOFF_S[outage_retries]
            outage_retries += 1
            if self.log:
                self.log.warn(
                    "footage_host_retry", source=self.name, reason=answer.why, wait_s=wait,
                    retry=outage_retries,
                )
            self._sleep(wait)

    def _fetch_file(self, url: str, dest: Path) -> Path:
        """Stream `url` to `dest` through a `.part` file, with the search's retry rules (footage-robust F1).

        A timeout, a refused or reset connection and a 5xx are tried again after `OUTAGE_BACKOFF_S` (2, 5,
        15 s); a try cut mid-way is continued from the bytes already on disk with a `Range` request when the
        server said `Accept-Ranges: bytes` (the owner's data is paid: a retry fetches no byte twice), else it
        starts again from byte 0; a 429 is waited out (four times), as a search is.
        Anything else (a 4xx, an answer that cannot be read) stops at once: asking again gets the same answer.
        Every retry and the giving up are logged. A file link carries no key, so no status of it says the key
        is wrong (`key_sent=False`).
        """
        part = dest.with_name(dest.name + ".part")
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)  # a part of a stopped process: its validator is lost
        try:
            return self._fetch_loop(url, dest, _Resume())
        finally:
            with contextlib.suppress(OSError):
                part.unlink(missing_ok=True)  # whatever went wrong, no half file stays in the cache

    def _fetch_loop(self, url: str, dest: Path, state: _Resume) -> Path:
        """The tries of `_fetch_file`; `state` carries what the first answer said about resuming."""
        outage_retries = 0
        rate_limited = 0
        while True:
            answer = self._fetch_once(url, dest, state)
            if answer.ok:
                return dest
            if answer.kind == sa.RATE_LIMITED:
                rate_limited += 1
                wait = answer.wait or sa.DEFAULT_WAIT_S
                if rate_limited >= 4:
                    if self.log:
                        self.log.error("footage_download_failed", source=self.name, file=dest.name,
                                       reason=answer.why, tries=rate_limited)
                    raise answer.error(f"Asked {rate_limited} times. The download stopped.", gave_up=True)
                if self.log:
                    self.log.warn("footage_rate_limited", source=self.name, wait_s=wait, attempt=rate_limited)
                self._sleep(wait)
                continue
            if answer.kind not in (sa.FAILING, sa.NO_CONNECTION):
                if self.log:
                    self.log.warn("footage_download_failed", source=self.name, file=dest.name,
                                  reason=answer.why, tries=outage_retries + rate_limited + 1)
                raise answer.error("The download stopped.", gave_up=True)
            if outage_retries >= len(OUTAGE_BACKOFF_S):
                if self.log:
                    self.log.error("footage_download_failed", source=self.name, file=dest.name,
                                   reason=answer.why, tries=outage_retries + 1)
                raise answer.error(f"Tried {outage_retries + 1} times. The download stopped.", gave_up=True)
            wait = OUTAGE_BACKOFF_S[outage_retries]
            outage_retries += 1
            if self.log:
                self.log.warn("footage_download_retry", source=self.name, file=dest.name, reason=answer.why,
                              wait_s=wait, retry=outage_retries)
            self._sleep(wait)

    def _fetch_once(self, url: str, dest: Path, state: _Resume | None = None) -> sa.Answer:
        """One try of a file download: the answer's kind (ok when `dest` is complete).

        A part file left by an earlier try of the same download (`state` says the server takes ranges) is
        continued: `Range: bytes=<size>-` (with `If-Range` when the server gave a validator). A server that
        answers 200 anyway, a 416 or a `Content-Range` that does not start where the part ends makes this try
        start from byte 0 (the part is dropped). A body shorter than the size the server announced is a cut
        connection, not a clip (kept as a part, the next try continues it). The part file is the caller's
        (`_fetch_file` removes it at the end); a full disk raises NS-E104 at once, any other local error is
        raised as it is.
        """
        state = state if state is not None else _Resume()
        part = dest.with_name(dest.name + ".part")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            for _pass in range(2):  # the second pass is the restart from byte 0 after a refused resume
                have = part.stat().st_size if part.exists() else 0
                if have and not state.ranges:
                    part.unlink(missing_ok=True)
                    have = 0
                headers: dict[str, str] = {}
                if have:
                    headers["Range"] = f"bytes={have}-"
                    if state.validator:
                        headers["If-Range"] = state.validator
                with self.client.stream("GET", url, headers=headers, follow_redirects=True) as r:
                    if have and r.status_code == 416:
                        r.read()
                        part.unlink(missing_ok=True)
                        continue
                    if r.status_code >= 300:
                        r.read()
                        return self._classify(r, key_sent=False)
                    total: int | None = None
                    if have and r.status_code == 206:
                        start, total = _content_range(r.headers.get("content-range"))
                        if start != have:
                            part.unlink(missing_ok=True)
                            continue
                        mode = "ab"
                        if self.log:
                            self.log.info("footage_download_resume", source=self.name, file=dest.name,
                                          from_byte=have)
                    else:
                        mode, have = "wb", 0  # the whole body: a first try, or a server that ignored Range
                        state.learn(r.headers)
                        if r.status_code == 206:
                            _start, total = _content_range(r.headers.get("content-range"))
                        elif not r.headers.get("content-encoding"):
                            total = _int_or_none(r.headers.get("content-length"))
                    with part.open(mode) as fh:
                        for chunk in r.iter_bytes(1 << 16):
                            fh.write(chunk)
                            if self.progress is not None:
                                self.progress(len(chunk))
                if total is not None and part.stat().st_size != total:
                    return self._classify(failed=httpx.ReadError("IncompleteBody"))
                part.replace(dest)
                return sa.Answer(self.name, sa.OK)
            return self._classify(failed=httpx.ReadError("ResumeRefused"))
        except httpx.HTTPError as e:
            return self._classify(failed=e)  # the part stays: the next try continues it
        except OSError as e:
            if is_disk_full(e):
                raise disk_full_error(dest.parent, self.log) from e
            raise


class _Resume:
    """What the first answer of one download said about continuing it (`Accept-Ranges`, `ETag`)."""

    def __init__(self) -> None:
        self.ranges = False
        self.validator: str | None = None

    def learn(self, headers: Mapping[str, str]) -> None:
        """Read the headers of a full (200) answer."""
        self.ranges = str(headers.get("accept-ranges", "")).strip().lower() == "bytes"
        etag = str(headers.get("etag", "")).strip()
        modified = headers.get("last-modified") or None
        self.validator = etag if etag and not etag.startswith("W/") else modified


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(str(value)) if value is not None else None
    except ValueError:
        return None


_CONTENT_RANGE = re.compile(r"^\s*bytes\s+(\d+)-(\d+)/(\d+|\*)\s*$", re.IGNORECASE)


def _content_range(value: str | None) -> tuple[int | None, int | None]:
    """`(first byte, total size)` of a `Content-Range: bytes 100-999/1000` header, None where not given."""
    m = _CONTENT_RANGE.match(value or "")
    if not m:
        return None, None
    return int(m.group(1)), (int(m.group(3)) if m.group(3) != "*" else None)


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
        base_url: str | None = None,
        log: NsLogger | None = None,
    ) -> None:
        super().__init__(
            key, cache_dir, client=client, bucket=bucket or TokenBucket(180, 180 / 3600.0, sleep=sleep),
            sleep=sleep, wall=wall, log=log,
        )
        self.base_url = (base_url or PEXELS_URL).rstrip("/")

    def search(self, keyword: str, *, orientation: Orientation, per_page: int = 80) -> list[ClipCandidate]:
        """Search Pexels at the documented `/v1/videos/search`; `size=medium` asks for Full HD or better."""
        data = self._get(
            sa.search_url(self.name, self.base_url),
            params={"query": keyword, "orientation": orientation, "size": "medium",
                    "per_page": min(80, per_page), "page": 1},
            headers={"Authorization": self._key},
        )
        return parse_pexels(data, orientation)

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
        base_url: str | None = None,
        cache_ttl_s: float = PIXABAY_CACHE_TTL_S,
        log: NsLogger | None = None,
    ) -> None:
        super().__init__(
            key, cache_dir, client=client, bucket=bucket or TokenBucket(90, 90 / 60.0, sleep=sleep),
            sleep=sleep, wall=wall, log=log,
        )
        self.base_url = base_url or PIXABAY_URL
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
            data = self._get(
                sa.search_url(self.name, self.base_url),
                params={"key": self._key, "q": keyword, "video_type": "film", "per_page": per_page,
                        "safesearch": "true", "page": 1},
            )
            self.api_calls += 1
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

    def __init__(
        self, root: Path, *, per_keyword: int = 6, fail: Sequence[str] = (), black: Sequence[str] = ()
    ) -> None:
        self.root = root
        self.per_keyword = per_keyword
        self.searches: list[tuple[str, str]] = []
        self.downloads: list[str] = []
        #: Clip ids whose download gives up as a stock site's would after its retries (NS-E503), and clip ids
        #: rendered pure black: the footage-robust tests' stand-ins for a dead link and for black footage.
        self.fail = set(fail)
        self.black = set(black)

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
        if cand.id in self.fail:
            raise sa.Answer(self.name, sa.NO_CONNECTION, "ReadTimeout").error(
                "Tried 4 times. The download stopped.", gave_up=True
            )
        idx = int(cand.id.rsplit("-", 1)[-1])
        w, h = (cand.width // 2, cand.height // 2)
        kind: Literal["testsrc2", "color"] = "testsrc2" if idx % 2 == 0 else "color"
        colour = _PALETTE[idx % len(_PALETTE)]
        if cand.id in self.black:
            kind, colour = "color", "0x000000"
        try:
            return ff.make_test_clip(dest, width=w, height=h, seconds=cand.duration, kind=kind, color=colour)
        except NsError as e:
            remove_partial(dest)  # no truncated clip stays in the cache
            mapped = classify_ffmpeg_error(e, dest.parent)
            if mapped is e:
                raise
            raise mapped from e
        except OSError as e:
            remove_partial(dest)
            if is_disk_full(e):
                raise disk_full_error(dest.parent) from e
            raise
