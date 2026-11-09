"""C2 GitHub releases: `ns update --check` and `ns update --github` (spec J8, gap 9/16).

The latest release of the public repository is read from the GitHub API. The update package is the release
asset `Nightshift-Update-<version>.zip` (releases up to v0.1.1 call it `nightshift-app-<version>.zip`,
still accepted); it is applied only after its SHA256 matches the digest GitHub publishes
for that asset (`digest: "sha256:..."`) or, failing that, the `.sha256` file attached to the release (the same
`<hash>  <name>` format as `manifest.json` pins and `make_bundle.ps1`). A package with no digest to compare
is refused (`NS-E701`). Only https URLs on GitHub hosts are followed. No other network use, no credentials.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ns_common.atomic import write_chunks_atomic, write_text_atomic
from ns_common.errors import NsError

REPO = "AbdulrahmanAmer/nightshift"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
TRUSTED_HOSTS = ("github.com", "githubusercontent.com")
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+]([0-9A-Za-z.-]+))?$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


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


def trusted(url: str) -> bool:
    """https on github.com or githubusercontent.com (any subdomain)."""
    u = urlparse(url)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in TRUSTED_HOSTS)


def _unreachable(e: Exception) -> NsError:
    """`NS-E105` for a connection that failed; `NS-E118` when GitHub answered with an error status.

    "Check the internet connection" is wrong advice for a reply: a 403 or 429 is GitHub's hourly limit for
    update checks from one address, a 404 means no release is published.
    """
    if isinstance(e, httpx.HTTPStatusError):
        return NsError("NS-E118", f"github.com: {status_words(e.response)}")
    return NsError("NS-E105", f"github.com: {e}")


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


def parse_release(data: dict[str, Any]) -> Release:
    """A `Release` from the API's JSON. `NS-E701` when it has no usable tag or is a prerelease or a draft.

    GitHub's `/releases/latest` never returns either, but the updater does not rely on that alone (H-12): a
    release candidate (`v0.1.15-rc1`) must never be offered as the update, whatever the endpoint answers."""
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
        if low in zip_names and trusted(url) and zip_names.index(low) < best:
            best = zip_names.index(low)
            zip_name, zip_url, digest = name, url, None
            raw = str(asset.get("digest") or "")
            if raw.lower().startswith("sha256:") and HEX64.match(raw[7:].lower()):
                digest = raw[7:].lower()
        elif low in want_sums and trusted(url):
            sidecars[low] = url
    if zip_name is not None:
        sidecar = sidecars.get(f"{zip_name.lower()}.sha256") or next(iter(sidecars.values()), None)
    return Release(version, tag, str(data.get("html_url") or ""), zip_name, zip_url, digest, sidecar)


def fetch_latest(client: httpx.Client | None = None) -> Release:
    """The newest published (non-draft, non-prerelease) release. `NS-E105` when GitHub is unreachable."""
    http = client or httpx.Client(timeout=30, follow_redirects=True)
    try:
        r = http.get(LATEST_URL, headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise _unreachable(e) from e
    finally:
        if client is None:
            http.close()
    if not isinstance(data, dict):
        raise NsError("NS-E701", "unexpected answer from the GitHub releases API")
    return parse_release(data)


def check(current: str, client: httpx.Client | None = None) -> dict[str, Any]:
    """`{update_available, current, latest, notes}`: the dict `ns update --check` prints as JSON."""
    rel = fetch_latest(client)
    return {
        "update_available": is_newer(rel.version, current),
        "current": current,
        "latest": rel.version,
        "notes": rel.page,
    }


def expected_digest(rel: Release, client: httpx.Client) -> str:
    """The SHA256 the package must have: the API digest, else the attached `.sha256`. `NS-E701` if neither."""
    if rel.digest:
        return rel.digest
    if rel.sidecar_url:
        try:
            r = client.get(rel.sidecar_url)
            r.raise_for_status()
            token = r.text.split()[0].lower()
        except (httpx.HTTPError, IndexError) as e:
            raise NsError("NS-E701", f"cannot read the release checksum: {e}") from e
        if HEX64.match(token):
            return token
    raise NsError("NS-E701", f"release {rel.tag} publishes no SHA256 for its update package")


def download(rel: Release, dest_dir: Path, client: httpx.Client | None = None) -> Path:
    """Download the update zip into `dest_dir`, verify its SHA256 and write `<zip>.sha256` next to it.

    The result is what `ns_supervisor.update.update_app` takes. A mismatch deletes the file (`NS-E701`).
    """
    if rel.zip_url is None or rel.zip_name is None:
        raise NsError("NS-E701", f"release {rel.tag} has no Nightshift-Update-{rel.version}.zip")
    http = client or httpx.Client(timeout=httpx.Timeout(60, read=300), follow_redirects=True)
    try:
        want = expected_digest(rel, http)
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / rel.zip_name
        h = hashlib.sha256()
        try:
            with http.stream("GET", rel.zip_url) as r:
                r.raise_for_status()
                if not trusted(str(r.url)):
                    raise NsError("NS-E701", f"the download was redirected away from GitHub ({r.url.host})")

                def blocks() -> Iterator[bytes]:
                    for block in r.iter_bytes(1024 * 1024):
                        h.update(block)
                        yield block

                # The zip appears under its name only when all of it arrived: a crash leaves no torn zip.
                write_chunks_atomic(target, blocks())
        except httpx.HTTPError as e:
            target.unlink(missing_ok=True)
            raise _unreachable(e) from e
        except NsError:
            target.unlink(missing_ok=True)
            raise
        got = h.hexdigest()
        if got != want:
            target.unlink(missing_ok=True)
            raise NsError(
                "NS-E701", f"SHA256 mismatch for {rel.zip_name}: expected {want[:12]}, got {got[:12]}"
            )
        write_text_atomic(target.with_name(target.name + ".sha256"), f"{got}  {rel.zip_name}\n", newline=None)
        return target
    finally:
        if client is None:
            http.close()
