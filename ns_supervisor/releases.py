"""C2 GitHub releases: `ns update --check` and `ns update --github` (spec J8, gap 9/16).

The latest release of the public repository is read from the GitHub API. The update package is the release
asset `Nightshift-Update-<version>.zip` (releases up to v0.1.1 call it `nightshift-app-<version>.zip`,
still accepted); it is applied only after its SHA256 matches the digest GitHub publishes
for that asset (`digest: "sha256:..."`) or, failing that, the `.sha256` file attached to the release (the same
`<hash>  <name>` format as `manifest.json` pins and `make_bundle.ps1`). A package with no digest to compare
is refused (`NS-E701`). Only https URLs on GitHub hosts are followed. No other network use, no credentials.

The one exception is for tests (H-15.9, docs/DEV_MODE.md "A fake GitHub for the updater"): on a dev or
engine-test install the variable `NS_RELEASE_SOURCE` names a releases address on the loopback interface, and
then that address stands in for GitHub: the release, its package and its checksum are taken from that host
and port only. `release_source` is the only reader of the variable and the only place that decides; every
other install refuses the variable with `NS-E704` before any request is made, and never falls back to GitHub.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ns_common import devmode
from ns_common.atomic import FileInUseError, read_text_retry, write_chunks_atomic, write_text_atomic
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.paths import Paths

REPO = "AbdulrahmanAmer/nightshift"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
TRUSTED_HOSTS = ("github.com", "githubusercontent.com")
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+]([0-9A-Za-z.-]+))?$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
#: The variable that aims a dev or engine-test install at a fake GitHub. Read by `release_source` only.
SOURCE_ENV = "NS_RELEASE_SOURCE"
#: The hosts a test release source may be on: this PC, nothing else.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class Source:
    """Where releases are read from: GitHub, or the test source of a dev or engine-test install."""

    latest_url: str
    origin: tuple[str, str, int] | None = None  # (scheme, host, port) of a test source; None is GitHub

    @property
    def test(self) -> bool:
        return self.origin is not None

    @property
    def name(self) -> str:
        """What an error message calls it."""
        if self.origin is None:
            return "github.com"
        return f"test release source {self.origin[1]}:{self.origin[2]}"


def _origin(url: str | httpx.URL) -> tuple[str, str, int] | None:
    """(scheme, host, port) of an http(s) URL without a user name, as httpx (which sends the request) reads
    it; None for anything else."""
    try:
        u = httpx.URL(url)
        port = u.port
    except (httpx.InvalidURL, TypeError, ValueError):
        return None
    if u.scheme not in _DEFAULT_PORTS or not u.host or u.userinfo:
        return None
    port = _DEFAULT_PORTS[u.scheme] if port is None else port
    return (u.scheme, u.host.lower(), port) if 0 < port < 65536 else None


def _test_install(paths: Paths) -> bool:
    """True for a dev install and for an engine-test install: `dev_mode`, `fake_footage` or `tiny_story` is
    `true` in its `runtime.json`, or its voice engine is the fake one. An unreadable file is neither."""
    if devmode.fake_footage(paths):  # dev_mode or fake_footage
        return True
    try:
        data = json.loads(read_text_retry(paths.runtime_file, "utf-8-sig"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    tts = data.get("tts")
    return data.get("tiny_story") is True or (isinstance(tts, dict) and tts.get("engine") == "fake")


def release_source(env: Mapping[str, str] | None = None, paths: Paths | None = None) -> Source:
    """Where this install reads its releases from. The ONLY reader of `NS_RELEASE_SOURCE`.

    Not set (or blank): GitHub, and nothing else is looked at. Set: the install (`paths`, else the one `env`
    names) must be a dev or engine-test install and the value an http(s) address on this PC; then that
    address is the source and GitHub is not asked. Anything else is `NS-E704`: a real install is never aimed
    at a fake by a variable somebody left behind, and never quietly goes to GitHub instead either. Nothing is
    written and no request is made here.
    """
    e = os.environ if env is None else env
    raw = (e.get(SOURCE_ENV) or "").strip()
    if not raw:
        return Source(LATEST_URL)
    home = paths or Paths.from_env(e)
    if not _test_install(home):
        raise NsError(
            "NS-E704",
            f"{SOURCE_ENV} is set, and the install at {home.home} is not a dev or engine-test install; "
            "no update server was asked",
        )
    origin = _origin(raw)
    if origin is None or origin[1] not in LOOPBACK_HOSTS:
        raise NsError(
            "NS-E704",
            f"{SOURCE_ENV} must be an http or https address on 127.0.0.1, localhost or ::1, not {raw!r}; "
            "no update server was asked",
        )
    return Source(raw, origin)


@dataclass(frozen=True)
class Release:
    """What `ns update` needs to know about one GitHub release."""

    version: str
    tag: str
    page: str
    zip_name: str | None
    zip_url: str | None
    digest: str | None  # sha256 hex from the API, when GitHub provides it
    sidecar_url: str | None  # the attached .sha256 file, when there is one


def parse_version(text: str) -> tuple[int, int, int, int, str]:
    """Sortable form of `0.1.1` / `v0.2.0-rc1`: (major, minor, patch, is_final, prerelease).

    A final release sorts above its own prereleases. Raises ValueError for anything else."""
    m = VERSION_RE.match(text.strip())
    if not m:
        raise ValueError(f"not a version: {text!r}")
    pre = m.group(4) or ""
    # numbers inside the prerelease part sort as numbers: rc2 is before rc10 (as text "rc10" < "rc2")
    pre = re.sub(r"\d+", lambda d: d.group().zfill(9), pre)
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), 0 if pre else 1, pre


def is_newer(latest: str, current: str) -> bool:
    """True when `latest` is a higher version than `current` (unreadable versions never count as newer)."""
    try:
        return parse_version(latest) > parse_version(current)
    except ValueError:
        return False


def trusted(url: str, source: Source | None = None) -> bool:
    """https on github.com or githubusercontent.com (any subdomain).

    With a test source in force (`release_source`), the scheme, host and port of that source instead: GitHub
    is not trusted then, and neither is another port of this PC."""
    src = release_source() if source is None else source
    if src.origin is not None:
        return _origin(url) == src.origin
    u = urlparse(url)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in TRUSTED_HOSTS)


def _unreachable(e: Exception, source: Source | None = None) -> NsError:
    """`NS-E105` for a connection that failed; `NS-E118` when GitHub answered with an error status.

    "Check the internet connection" is wrong advice for a reply: a 403 or 429 is GitHub's hourly limit for
    update checks from one address, a 404 means no release is published. The message names who was asked
    (a test source by its host and port).
    """
    who = source.name if source is not None else "github.com"
    if isinstance(e, httpx.HTTPStatusError):
        return NsError("NS-E118", f"{who}: {status_words(e.response)}")
    return NsError("NS-E105", f"{who}: {e}")


def _client(source: Source, timeout: Any) -> httpx.Client:
    """The client for a source. One for a test source ignores the PC's proxy variables: a proxy would carry a
    request for 127.0.0.1 to another machine."""
    if source.test:
        return httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False)
    return httpx.Client(timeout=timeout, follow_redirects=True)


def _stayed(r: httpx.Response, source: Source) -> None:
    """`NS-E701` when the answer did not come from the source. GitHub: the final address, as before. A test
    source: every hop, so its answer never comes by way of another host."""
    for hop in (*r.history, r) if source.test else (r,):
        if not trusted(str(hop.url), source):
            where = "the test release source" if source.test else "GitHub"
            raise NsError("NS-E701", f"the download was redirected away from {where} ({hop.url.host})")


def status_words(r: httpx.Response) -> str:
    """What a GitHub error answer means, in a sentence that starts with the status."""
    code = r.status_code
    try:
        body = r.text.lower()
    except httpx.ResponseNotRead:  # an answer to a streamed download: the body was not read
        body = ""
    limited = r.headers.get("x-ratelimit-remaining") == "0" or "rate limit" in body
    if code in (403, 429) and (code == 429 or limited):
        return f"HTTP {code}: GitHub's limit of update checks per hour was reached; try again in an hour"
    if code == 404:
        return "HTTP 404: no release is published there yet"
    if code in (500, 502, 503, 504):
        return f"HTTP {code}: GitHub has a problem right now; try again later"
    return f"HTTP {code}: GitHub refused the request"


def parse_release(data: dict[str, Any], source: Source | None = None) -> Release:
    """A `Release` from the API's JSON. `NS-E701` when it has no usable tag or is a prerelease or a draft.

    GitHub's `/releases/latest` never returns either, but the updater does not rely on that alone (H-12): a
    release candidate (`v0.1.15-rc1`) must never be offered as the update, whatever the endpoint answers.
    The same holds for a test source. Only assets on the source (`trusted`) are picked."""
    src = release_source() if source is None else source
    tag = str(data.get("tag_name") or "")
    if data.get("prerelease") or data.get("draft"):
        raise NsError("NS-E701", f"the latest release {tag!r} is a prerelease or a draft, not an update")
    try:
        parse_version(tag)
    except ValueError as e:
        raise NsError("NS-E701", f"the latest release has no version tag ({tag!r})") from e
    version = tag.lstrip("v")
    # new name first, then the v0.1.0/v0.1.1 name; the sidecar is the picked zip's .sha256, else any known one
    zip_names = [f"Nightshift-Update-{version}.zip".lower(), f"nightshift-app-{version}.zip".lower()]
    want_sums = {f"{n}.sha256" for n in zip_names} | {
        f"nightshift-setup-{version}.zip.sha256",
        f"nightshift-{version}.zip.sha256",
    }
    zip_name = zip_url = digest = sidecar = None
    best = len(zip_names)
    sidecars: dict[str, str] = {}
    for asset in data.get("assets") or []:
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        low = name.lower()
        if low in zip_names and trusted(url, src) and zip_names.index(low) < best:
            best = zip_names.index(low)
            zip_name, zip_url, digest = name, url, None
            raw = str(asset.get("digest") or "")
            if raw.lower().startswith("sha256:") and HEX64.match(raw[7:].lower()):
                digest = raw[7:].lower()
        elif low in want_sums and trusted(url, src):
            sidecars[low] = url
    if zip_name is not None:
        sidecar = sidecars.get(f"{zip_name.lower()}.sha256") or next(iter(sidecars.values()), None)
    return Release(version, tag, str(data.get("html_url") or ""), zip_name, zip_url, digest, sidecar)


def fetch_latest(client: httpx.Client | None = None) -> Release:
    """The newest published (non-draft, non-prerelease) release. `NS-E105` when GitHub is unreachable.

    Read from `release_source()`: GitHub, or the test source of a dev or engine-test install (`NS-E704` on any
    other install that has the variable set, before a client exists). An unreachable test source is an error
    too; GitHub is not asked in its place."""
    paths = Paths.from_env()
    src = release_source(paths=paths)
    http = client or _client(src, 30)
    try:
        r = http.get(src.latest_url, headers={"Accept": "application/vnd.github+json"})
        if src.test:
            _stayed(r, src)  # before the answer is read: one that came by another host is not looked at
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise _unreachable(e, src) from e
    finally:
        if client is None:
            http.close()
    if src.test:
        NsLogger("update", paths.logs, echo=False).warn(
            "test_release_source", f"releases are read from {src.latest_url} ({SOURCE_ENV}), not from GitHub"
        )
    if not isinstance(data, dict):
        raise NsError("NS-E701", "unexpected answer from the GitHub releases API")
    return parse_release(data, src)


def check(current: str, client: httpx.Client | None = None) -> dict[str, Any]:
    """`{update_available, current, latest, notes}`: the dict `ns update --check` prints as JSON."""
    rel = fetch_latest(client)
    return {
        "update_available": is_newer(rel.version, current),
        "current": current,
        "latest": rel.version,
        "notes": rel.page,
    }


def expected_digest(rel: Release, client: httpx.Client, source: Source | None = None) -> str:
    """The SHA256 the package must have: the API digest, else the attached `.sha256`. `NS-E701` if neither."""
    if rel.digest:
        return rel.digest
    if rel.sidecar_url:
        src = release_source() if source is None else source
        try:
            r = client.get(rel.sidecar_url)
            if src.test:
                _stayed(r, src)
            r.raise_for_status()
            token = r.text.split()[0].lower()
        except (httpx.HTTPError, IndexError) as e:
            raise NsError("NS-E701", f"cannot read the release checksum: {e}") from e
        if HEX64.match(token):
            return token
    raise NsError("NS-E701", f"release {rel.tag} publishes no SHA256 for its update package")


def _forget(target: Path) -> None:
    """Delete an older zip of the name and its `.sha256`; one that cannot be deleted (open elsewhere) hides no
    error. The sidecar goes too: it vouches for the zip that was just refused, not for whatever comes next."""
    for f in (target, target.with_name(target.name + ".sha256")):
        with contextlib.suppress(OSError):
            f.unlink(missing_ok=True)


def download(rel: Release, dest_dir: Path, client: httpx.Client | None = None) -> Path:
    """Download the update zip into `dest_dir`, verify its SHA256 and write `<zip>.sha256` next to it.

    The result is what `ns_supervisor.update.update_app` takes. The download goes to a temporary file beside
    the final name; its SHA256 is checked on that file and only a match is moved under the final name. A
    mismatch (`NS-E701`), a cut connection (`NS-E105`/`NS-E118`) or a write that fails (disk full) leaves
    nothing under the final name, and an older zip of the same name is deleted. The one exception is a program
    that holds the name open (`NS-E119`): Windows will not delete or replace it, so an older zip stays and the
    error says why. An older zip that cannot be deleted for that reason after a mismatch also stays; the
    mismatch is still the error raised.

    The source is read again here (`release_source`), and the release must be one of that source: a release
    read from a test source is refused once the variable is gone or names another address, and a GitHub
    release is refused while a test source is in force (`NS-E701`, before any request and any folder).
    """
    src = release_source()
    if rel.zip_url is None or rel.zip_name is None:
        raise NsError("NS-E701", f"release {rel.tag} has no Nightshift-Update-{rel.version}.zip")
    for url in (rel.zip_url, rel.sidecar_url):
        if url is not None and not trusted(url, src):
            raise NsError(
                "NS-E701",
                f"release {rel.tag} is not a release of {src.name} (its files are at "
                f"{urlparse(url).netloc}); check for the update again",
            )
    http = client or _client(src, httpx.Timeout(60, read=300))
    try:
        want = expected_digest(rel, http, src)
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / rel.zip_name

        def check(tmp: Path) -> None:
            """The digest of the finished temporary file (what is on disk, not what the socket delivered)."""
            with tmp.open("rb") as fh:
                got = hashlib.file_digest(fh, "sha256").hexdigest()
            if got != want:
                raise NsError(
                    "NS-E701",
                    f"SHA256 mismatch for {rel.zip_name}: expected {want[:12]}, got {got[:12]}; "
                    "the download was discarded and nothing was saved",
                )

        try:
            with http.stream("GET", rel.zip_url) as r:
                r.raise_for_status()
                _stayed(r, src)
                # The zip appears under its name only when all of it arrived AND its digest matched: a crash
                # leaves no torn zip, and a zip nobody checked is never there for a later step to pick up.
                write_chunks_atomic(target, r.iter_bytes(1024 * 1024), verify=check)
        except httpx.HTTPError as e:
            _forget(target)
            raise _unreachable(e, src) from e
        except FileInUseError:
            raise  # a program holds the name open: it cannot be deleted either; the swap left it as it was
        except (NsError, OSError):
            _forget(target)
            raise
        sidecar = target.with_name(target.name + ".sha256")
        write_text_atomic(sidecar, f"{want}  {rel.zip_name}\n", newline=None)
        return target
    finally:
        if client is None:
            http.close()
