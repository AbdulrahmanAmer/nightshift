"""C1: the downloader against the real internet (GitHub release assets and the Hugging Face CDN).

Everything else in the suite proves the resume logic against a fake server. These tests prove it against the
two sites the installer really uses: an interrupted download continues with ONE ``Range`` request from the
``.part`` size, through the redirects, and the finished file has the pinned hash.

They run only where ``NS_NETWORK_TESTS`` is set (the CI ``test`` job sets it; a laptop does not) and carry the
``network`` marker, so ``pytest -m "not network"`` leaves them out. Each test prints its evidence (bytes
before the pause, the Range header sent, the final hash) and appends it to the job summary when CI has one.
"""

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from installer.stage2 import downloader as dl
from ns_common.config import load_manifest

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        not os.environ.get("NS_NETWORK_TESTS"),
        reason="real-network download tests: set NS_NETWORK_TESTS=1 (the CI test job does)",
    ),
]

REDIRECTS = (301, 302, 303, 307, 308)
MIB = 1024 * 1024


def evidence(*lines: str) -> None:
    """Print the proof lines, and add them to the GitHub job summary when there is one."""
    text = "\n".join(f"[network] {line}" for line in lines)
    print(text, flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("\n".join(f"    {line}" for line in text.splitlines()) + "\n")


@dataclass
class Wire:
    """An httpx client like the installer's that logs every request and response, redirect hops included."""

    requests: list[tuple[str, str, str | None]] = field(default_factory=list)   # method, url, Range header
    # method, url, status, x-linked-size
    responses: list[tuple[str, str, int, str | None]] = field(default_factory=list)
    client: httpx.Client = field(init=False)

    def __post_init__(self) -> None:
        self.client = httpx.Client(
            follow_redirects=True, timeout=httpx.Timeout(30.0, read=dl.STALL_TIMEOUT_S),
            event_hooks={"request": [self._on_request], "response": [self._on_response]},
        )

    def _on_request(self, request: httpx.Request) -> None:
        self.requests.append((request.method, str(request.url), request.headers.get("range")))

    def _on_response(self, response: httpx.Response) -> None:
        self.responses.append((response.request.method, str(response.request.url), response.status_code,
                               response.headers.get("x-linked-size")))

    def gets(self) -> list[tuple[str, str, str | None]]:
        return [r for r in self.requests if r[0] == "GET"]

    def ranged(self) -> list[tuple[str, str, str | None]]:
        return [r for r in self.gets() if r[2]]

    def close(self) -> None:
        self.client.close()


@pytest.fixture
def wire():
    w = Wire()
    yield w
    w.close()


def rename_back_and_forth(path: Path) -> None:
    """Rename ``path`` away and back. Windows refuses while any handle is open on it; a virus scanner can hold
    a freshly written file for a moment, so a refusal is retried for a few seconds before it counts."""
    moved = path.with_name(path.name + ".moved")
    for attempt in range(25):
        try:
            path.rename(moved)
            break
        except PermissionError:
            if attempt == 24:
                raise
            threading.Event().wait(0.2)
    moved.rename(path)


def interrupted_download(url: str, dest: Path, sha256: str | None, wire: Wire, pause_after: int) -> dict:
    """Download with the real ``download()``; pause through the gate after ``pause_after`` bytes, look at the
    disk while it is parked, then resume. Returns what was seen while paused."""
    gate = threading.Event()
    gate.set()
    parked = threading.Event()
    outcome: dict = {}

    def progress(done: int, total: int | None) -> None:
        outcome["total"] = total
        if not gate.is_set():
            parked.set()                  # the report made after the file and the connection were closed
        elif done >= pause_after:
            gate.clear()                  # "Pause" pressed: the downloader stops after the next block

    def work() -> None:
        try:
            outcome["path"] = dl.download(url, dest, sha256, client=wire.client, gate=gate, progress=progress,
                                          sleep=lambda s: None)
        except BaseException as e:        # re-raised in the test thread
            outcome["error"] = e

    worker = threading.Thread(target=work, name="download", daemon=True)
    worker.start()
    try:
        for _ in range(180):                                 # up to 3 minutes, but fail at once if it died
            if parked.wait(1):
                break
            if not worker.is_alive():
                raise AssertionError(f"the download ended before the pause: {outcome}") from outcome.get("error")
        assert parked.is_set(), f"the download never reached the pause: {outcome}"
        part = dest.with_name(dest.name + ".part")
        outcome["paused_at"] = part.stat().st_size
        outcome["requests_before_resume"] = list(wire.requests)
        assert not dest.exists() and not dl._marker(dest).exists()      # nothing published while paused
        rename_back_and_forth(part)                                      # Windows refuses if a handle is open
        assert part.stat().st_size == outcome["paused_at"]
        threading.Event().wait(0.5)
        assert part.stat().st_size == outcome["paused_at"]               # the connection really is closed
    finally:
        gate.set()                                                       # Resume, and never leave it parked
    worker.join(300)
    assert not worker.is_alive(), "the download did not finish after Resume"
    if "error" in outcome:
        raise outcome["error"]
    return outcome


# ---------------------------------------------------------------------------------------------- (a) GitHub
def test_github_release_asset_pause_resume_one_range_request_and_the_pinned_hash(tmp_path: Path, wire: Wire,
                                                                                monkeypatch):
    pin = load_manifest().bins["yt_dlp"]
    assert pin.sha256 and len(pin.sha256) == 64
    url = pin.url
    assert url == f"https://github.com/yt-dlp/yt-dlp/releases/download/{pin.version}/yt-dlp.exe"
    dest = tmp_path / "yt-dlp" / "yt-dlp.exe"

    order: list[str] = []
    real_hash, real_marker = dl.sha256_file, dl.write_marker

    def hashing(path: Path, chunk: int = dl.CHUNK) -> str:
        order.append("hash")
        assert not dest.exists() and not dl._marker(dest).exists()
        return real_hash(path, chunk)

    def marking(path: Path, digest: str | None) -> None:
        order.append("marker")
        assert digest == pin.sha256.lower()  # the marker records the verified hash
        real_marker(path, digest)

    monkeypatch.setattr(dl, "sha256_file", hashing)
    monkeypatch.setattr(dl, "write_marker", marking)

    seen = interrupted_download(url, dest, pin.sha256, wire, pause_after=2 * MIB)
    paused_at, total = seen["paused_at"], seen["total"]
    assert paused_at >= 2 * MIB and total and paused_at < total
    assert not any(r[2] for r in seen["requests_before_resume"] if r[0] == "GET")  # no Range first

    want = f"bytes={paused_at}-"
    ranged = wire.ranged()
    on_original = [r for r in ranged if r[1] == url]
    assert len(on_original) == 1 and on_original[0][2] == want  # exactly one Range request, from the .part
    assert {r[2] for r in ranged} == {want}  # and the redirect hops repeated it as is
    assert len([r for r in wire.gets() if r[1] == url]) == 2  # the whole story: first GET and one resume
    assert order == ["hash", "marker"]                                       # .ok only after the hash
    final = real_hash(dest)
    assert final == pin.sha256.lower() and dl.is_verified(dest, pin.sha256)
    assert not dest.with_name("yt-dlp.exe.part").exists()
    hops = sorted({urlparse_host(r[1]) for r in ranged})
    evidence(
        f"GitHub release asset {url}",
        f"bytes on disk when paused: {paused_at} of {total} ({paused_at / MIB:.1f} of {total / MIB:.1f} MiB)",
        f"Range header sent on resume: {want}  (one request on the pinned URL; hops {', '.join(hops)})",
        f"final sha256 {final} == manifest pin {pin.sha256}: {final == pin.sha256.lower()}",
        f".ok marker written after the hash: {order}",
    )


def urlparse_host(url: str) -> str:
    return httpx.URL(url).host


# ------------------------------------------------------------------------------------------- (b) Hugging Face
def pick_small_hf_file(manifest) -> tuple[str, str, str, int]:
    """HEAD the whisper side files of every profile in the manifest and take the largest one of 1 to 10 MB.

    Returns ``(repo, revision, file, size)``. The size comes from ``x-linked-size`` on the redirect, exactly
    as the installer reads it."""
    client = httpx.Client(follow_redirects=True, timeout=30.0)
    best: tuple[str, str, str, int] | None = None
    try:
        for pm in manifest.profiles.values():
            repo = pm.whisper_repo
            for fname in (*dl.WHISPER_REQUIRED, *dl.WHISPER_OPTIONAL):
                status, size = dl.head_info(client, dl.hf_url(repo.repo, repo.revision, fname))
                if status == 200 and size and MIB <= size <= 10 * MIB and (best is None or size > best[3]):
                    best = (repo.repo, repo.revision, fname, size)
    finally:
        client.close()
    assert best is not None, "no whisper side file of 1 to 10 MB found in the manifest's repos"
    return best


def test_hugging_face_cdn_pause_resume_through_the_redirect_matches_a_straight_download(tmp_path: Path,
                                                                                         wire: Wire):
    repo, revision, fname, size = pick_small_hf_file(load_manifest())
    url = dl.hf_url(repo, revision, fname)
    straight_wire = Wire()
    try:
        straight = dl.download(url, tmp_path / "straight" / fname, None, client=straight_wire.client,
                               sleep=lambda s: None)
    finally:
        straight_wire.close()
    straight_hash = dl.sha256_file(straight)
    assert straight.stat().st_size == size

    dest = tmp_path / "resumed" / fname
    pause_after = max(64 * 1024, min(MIB, size // 3))
    seen = interrupted_download(url, dest, None, wire, pause_after=pause_after)
    paused_at = seen["paused_at"]
    assert pause_after <= paused_at < size
    resumed_hash = dl.sha256_file(dest)
    assert resumed_hash == straight_hash and dest.stat().st_size == size
    assert dl.is_verified(dest, None)

    redirects = [r for r in wire.responses if r[0] == "GET" and r[2] in REDIRECTS]
    assert redirects, "the Hugging Face resolve URL did not redirect to its CDN"
    assert any(r[3] and r[3].isdigit() and int(r[3]) == size for r in wire.responses), "no x-linked-size seen"
    want = f"bytes={paused_at}-"
    ranged = wire.ranged()
    assert {r[2] for r in ranged} == {want}
    assert len([r for r in ranged if r[1] == url]) == 1  # one resume request on the resolve URL
    assert any(r[1] != url for r in ranged), "Range was not sent on the redirected URL"
    hosts = sorted({urlparse_host(r[1]) for r in ranged})
    evidence(
        f"Hugging Face {repo}/{fname} ({size} bytes, HEAD x-linked-size = {size})",
        f"redirects seen: {[(r[2], urlparse_host(r[1])) for r in redirects]}",
        f"bytes on disk when paused: {paused_at} of {size}",
        f"Range header sent on resume: {want}  (hosts {', '.join(hosts)})",
        f"resumed sha256 {resumed_hash} == straight download sha256 {straight_hash}: "
        f"{resumed_hash == straight_hash}",
    )


# ---------------------------------------------------------------------------------------------- (c) crash
class Crash(BaseException):
    """A killed installer: not an Exception, so no handler in the downloader may swallow it."""


def test_hugging_face_part_truncated_by_hand_resumes_from_the_truncated_size(tmp_path: Path, wire: Wire):
    repo, revision, fname, size = pick_small_hf_file(load_manifest())
    url = dl.hf_url(repo, revision, fname)
    reference = dl.download(url, tmp_path / "ref" / fname, None, client=wire.client, sleep=lambda s: None)
    reference_hash = dl.sha256_file(reference)
    wire.requests.clear()

    dest = tmp_path / "crashed" / fname
    stop_at = max(64 * 1024, size // 2)

    def kill(done: int, total: int | None) -> None:
        if done >= stop_at:
            raise Crash()

    with pytest.raises(Crash):
        dl.download(url, dest, None, client=wire.client, progress=kill, sleep=lambda s: None)
    part = dest.with_name(dest.name + ".part")
    cut = part.stat().st_size - 4097                         # the last written bytes were lost in the crash
    with part.open("r+b") as fh:
        fh.truncate(cut)
    wire.requests.clear()
    dl.download(url, dest, None, client=wire.client, sleep=lambda s: None)
    want = f"bytes={cut}-"
    assert {r[2] for r in wire.ranged()} == {want}
    assert len([r for r in wire.ranged() if r[1] == url]) == 1
    final = dl.sha256_file(dest)
    assert final == reference_hash and dl.is_verified(dest, None)
    evidence(f"{fname}: crash left {cut + 4097} bytes, hand-truncated to {cut}; resumed with {want}; "
             f"sha256 {final} == full download {reference_hash}: {final == reference_hash}")


def test_the_real_servers_honour_the_head_size_the_downloader_trusts(wire: Wire):
    """HEAD on both sites gives the size the resume maths relies on (HF: the x-linked-size header)."""
    pin = load_manifest().bins["yt_dlp"]
    status, size = dl.head_info(wire.client, pin.url)
    assert status == 200 and size and size > 5 * MIB
    repo, revision, fname, hf_size = pick_small_hf_file(load_manifest())
    assert dl.head_info(wire.client, dl.hf_url(repo, revision, fname)) == (200, hf_size)
