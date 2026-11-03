"""C1: the downloader after a crash, a Stop, a full disk or a bad server (fakes only; the real-network twins
are in ``test_downloader_network.py``).

The owner's rule: downloads are smooth, every file is verified, an interrupted one continues where it stopped
and nothing is fetched twice. Each test below is one way the installer gets killed or the network misbehaves.
"""

import errno
import hashlib
import io
import json
import os
import threading
import time
from collections import namedtuple
from pathlib import Path

import httpx
import pytest

from installer.stage2 import downloader as dl
from installer.stage2.bridge import BridgeReporter, BridgeUi, SetupStopped
from ns_common.errors import NsError
from tests.test_downloader import DATA, SHA, make_client, no_sleep, ranged_handler

URL = "https://x.test/a.bin"


class Boom(BaseException):
    """Stands for Stop (``SetupStopped``) or a killed process: not an Exception, so nothing may swallow it."""


def served(data: bytes = DATA, chunk: int = 64 * 1024):
    """A client for a server that supports Range, plus its connection log and the Range headers it saw."""
    log = {"served": 0, "closed": 0}
    ranges: list = []
    client = httpx.Client(transport=httpx.MockTransport(ranged_handler(data, chunk, log, ranges)))
    return client, log, ranges


def stop_after(n: int):
    """A progress callback that raises Boom once ``n`` bytes are on disk."""

    def progress(done: int, total: int | None) -> None:
        if done >= n:
            raise Boom()

    return progress


# ---------------------------------------------------------------------------------------------- crash, stop
def test_stop_keeps_the_part_file_and_the_next_run_resumes_from_its_size(tmp_path: Path):
    client, log, ranges = served()
    dest = tmp_path / "a.bin"
    with pytest.raises(Boom):
        dl.download(URL, dest, SHA, client=client, progress=stop_after(300_000), sleep=no_sleep)
    part = dest.with_name("a.bin.part")
    size = part.stat().st_size
    assert 300_000 <= size < len(DATA)
    assert not dest.exists() and not dl._marker(dest).exists()
    assert log["closed"] == 1                            # the connection was closed on the way out
    dl.download(URL, dest, SHA, client=client, sleep=no_sleep)
    assert ranges == [None, f"bytes={size}-"]            # the second run asked only for the rest
    assert dest.read_bytes() == DATA and dl.is_verified(dest, SHA)


def test_a_part_cut_short_by_hand_resumes_from_the_cut(tmp_path: Path):
    client, _log, ranges = served()
    dest = tmp_path / "a.bin"
    with pytest.raises(Boom):
        dl.download(URL, dest, SHA, client=client, progress=stop_after(400_000), sleep=no_sleep)
    part = dest.with_name("a.bin.part")
    cut = part.stat().st_size - 1234                     # a crash that lost the last written bytes
    with part.open("r+b") as fh:
        fh.truncate(cut)
    dl.download(URL, dest, SHA, client=client, sleep=no_sleep)
    assert ranges[-1] == f"bytes={cut}-" and ranges.count(None) == 1
    assert dest.read_bytes() == DATA


def test_a_part_longer_than_the_file_on_the_server_starts_again(tmp_path: Path):
    dest = tmp_path / "a.bin"
    dest.with_name("a.bin.part").write_bytes(DATA + b"junk")
    calls: list = []
    dl.download(URL, dest, SHA, client=make_client(calls=calls), sleep=no_sleep)
    assert calls == [("HEAD", None), ("GET", None)]      # no Range was sent: the junk was dropped first
    assert dest.read_bytes() == DATA


def test_a_server_that_ignores_range_is_read_from_the_start_in_one_request(tmp_path: Path):
    dest = tmp_path / "a.bin"
    dest.with_name("a.bin.part").write_bytes(DATA[:1000])
    calls: list = []
    seen: list[tuple[int, int | None]] = []
    dl.download(URL, dest, SHA, client=make_client(ranges=False, calls=calls), sleep=no_sleep,
                progress=lambda d, t: seen.append((d, t)))
    assert [c for c in calls if c[0] == "GET"] == [("GET", "bytes=1000-")]   # one request, answered in full
    assert seen[0] == (1000, len(DATA)) and (0, len(DATA)) in seen            # the bar went back to zero
    assert dest.read_bytes() == DATA


def test_a_416_restarts_from_zero_and_does_not_use_an_attempt(tmp_path: Path):
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.headers.get("range")))
        if request.method == "HEAD":
            return httpx.Response(200)  # no length, so the junk part cannot be judged by size
        if request.headers.get("range"):
            return httpx.Response(416, headers={"content-range": f"bytes */{len(DATA)}"})
        return httpx.Response(200, content=DATA)

    dest = tmp_path / "a.bin"
    dest.with_name("a.bin.part").write_bytes(b"x" * 5000)
    sleeps: list[float] = []
    dl.download(URL, dest, SHA, client=httpx.Client(transport=httpx.MockTransport(handler)),
                sleep=sleeps.append)
    assert [c for c in calls if c[0] == "GET"] == [("GET", "bytes=5000-"), ("GET", None)]
    assert sleeps == []                                  # restarting is not a failed attempt: no backoff
    assert dest.read_bytes() == DATA and not dest.with_name("a.bin.part").exists()


def test_a_416_that_says_the_part_is_the_whole_file_goes_straight_to_the_hash(tmp_path: Path):
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.headers.get("range")))
        if request.method == "HEAD":
            return httpx.Response(200)
        return httpx.Response(416, headers={"content-range": f"bytes */{len(DATA)}"})

    dest = tmp_path / "a.bin"
    dest.with_name("a.bin.part").write_bytes(DATA)
    dl.download(URL, dest, SHA, client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=no_sleep)
    assert calls == [("HEAD", None), ("GET", f"bytes={len(DATA)}-")]
    assert dest.read_bytes() == DATA


def test_a_corrupt_first_download_is_dropped_and_fetched_again_from_zero(tmp_path: Path):
    state = {"gets": 0}
    ranges: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(DATA))})
        state["gets"] += 1
        ranges.append(request.headers.get("range"))
        return httpx.Response(200, content=bytes(len(DATA)) if state["gets"] == 1 else DATA)

    retries: list = []
    dest = tmp_path / "a.bin"
    dl.download(URL, dest, SHA, client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=no_sleep,
                on_retry=lambda *a: retries.append(a))
    assert retries == [(1, "hash", 2.0)] and ranges == [None, None]
    assert dest.read_bytes() == DATA


def test_the_marker_is_written_only_after_the_hash_and_the_rename(tmp_path: Path, monkeypatch):
    dest = tmp_path / "a.bin"
    order: list[str] = []
    real_hash, real_marker = dl.sha256_file, dl.write_marker

    def hashing(path: Path, chunk: int = dl.CHUNK) -> str:
        order.append("hash")
        assert not dest.exists() and not dl._marker(dest).exists()      # still the .part, nothing published
        return real_hash(path, chunk)

    def marking(path: Path, digest: str | None) -> None:
        order.append("marker")
        assert path.read_bytes() == DATA and digest == SHA            # the final file, after the hash
        real_marker(path, digest)

    monkeypatch.setattr(dl, "sha256_file", hashing)
    monkeypatch.setattr(dl, "write_marker", marking)
    dl.download(URL, dest, SHA, client=make_client(), sleep=no_sleep)
    assert order == ["hash", "marker"]


# ---------------------------------------------------------------------------------------------- pause
def test_a_paused_download_holds_no_open_file_and_no_connection(tmp_path: Path):
    chunk = 64 * 1024
    gate = threading.Event()
    gate.set()
    log = {"served": 0, "closed": 0}
    ranges: list = []
    handler = ranged_handler(DATA, chunk, log, ranges, gate=gate, clear_before=5)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    dest = tmp_path / "a.bin"
    parked = threading.Event()

    def progress(done: int, total: int | None) -> None:
        if not gate.is_set():
            parked.set()          # the one report made after the file and the connection were closed

    worker = threading.Thread(target=lambda: dl.download(
        URL, dest, SHA, client=client, gate=gate, chunk=chunk, sleep=no_sleep, progress=progress))
    worker.start()
    assert parked.wait(10)
    assert log["closed"] == 1
    part = dest.with_name("a.bin.part")
    moved = part.with_name("moved")
    part.rename(moved)                                   # Windows refuses this while any handle is open
    moved.rename(part)
    assert part.stat().st_size == 6 * chunk and worker.is_alive()
    gate.set()
    worker.join(timeout=10)
    assert not worker.is_alive() and dest.read_bytes() == DATA and ranges[-1] == f"bytes={6 * chunk}-"


class _Rec:
    def __init__(self) -> None:
        self.blocking = 0
        self.nowait = 0

    def start(self, name, total):
        return None

    def update(self, name, done, total):
        self.blocking += 1

    def update_nowait(self, name, done, total):
        self.nowait += 1

    def finish(self, name, outcome):
        return None


def test_run_plan_reports_without_blocking_when_the_downloader_owns_the_pause_gate(tmp_path: Path):
    gate = threading.Event()
    gate.set()
    rec = _Rec()
    item = dl.DownloadItem(name="f", url=URL, dest=tmp_path / "f", sha256=SHA)
    dl.run_plan([item], client=make_client(), reporter=rec, gate=gate)
    assert rec.nowait > 0 and rec.blocking == 0
    rec2 = _Rec()                                        # no gate (console mode): the plain update is used
    item2 = dl.DownloadItem(name="g", url=URL, dest=tmp_path / "g", sha256=SHA)
    dl.run_plan([item2], client=make_client(), reporter=rec2)
    assert rec2.blocking > 0 and rec2.nowait == 0


def test_the_bridge_reporter_never_waits_at_the_gate_for_a_download_and_obeys_stop():
    out = io.StringIO()
    ui = BridgeUi(out=out, inp=io.StringIO())
    rep = BridgeReporter(ui)
    ui.gate.clear()                                      # the window is paused
    worker = threading.Thread(target=lambda: rep.update_nowait("a", 10, 100))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()                         # update() would sit here until Resume
    assert json.loads(out.getvalue().splitlines()[0])["done"] == 10
    ui.stop.set()
    with pytest.raises(SetupStopped):
        rep.update_nowait("a", 20, 100)
    rep.retrying("a", 1, "network", 2.0)
    assert json.loads(out.getvalue().splitlines()[-1]) == {
        "type": "file", "key": "a", "event": "retry", "attempt": 1, "reason": "network", "waitS": 2.0,
        "of": 3}


# ---------------------------------------------------------------------------------------------- progress
def test_progress_comes_at_least_every_64_kib_when_bytes_are_fast(tmp_path: Path):
    client, _log, _ranges = served(DATA, 64 * 1024)
    seen: list[int] = []
    dl.download(URL, tmp_path / "a.bin", SHA, client=client, progress=lambda d, t: seen.append(d),
                clock=lambda: 0.0)                       # time never passes: only the byte rule can fire
    gaps = [b - a for a, b in zip(seen, seen[1:], strict=False)]
    assert max(gaps) <= 64 * 1024 and seen[-1] == len(DATA)
    assert len(seen) >= len(DATA) // (64 * 1024)


def test_progress_comes_at_least_every_250_ms_when_bytes_are_slow(tmp_path: Path):
    data = bytes(range(256)) * 256                       # 64 KiB served in 1 KiB blocks
    client, _log, _ranges = served(data, 1024)
    ticks = {"t": 0.0}

    def clock() -> float:
        ticks["t"] += 0.1                                # every block "takes" 100 ms
        return ticks["t"]

    stamps: list[tuple[float, int]] = []
    dl.download(URL, tmp_path / "a.bin", None, client=client, clock=clock,
                progress=lambda d, t: stamps.append((ticks["t"], d)))
    middle = stamps[1:-1]                                # the first and last reports are the bookends
    assert len(middle) >= 10
    assert max(b[0] - a[0] for a, b in zip(middle, middle[1:], strict=False)) <= 0.35
    assert max(b[1] - a[1] for a, b in zip(middle, middle[1:], strict=False)) <= 4 * 1024   # not 64 KiB apart


# ---------------------------------------------------------------------------------------------- disk, retries
def test_a_full_disk_is_ns_e104_with_the_free_space_and_is_not_retried(tmp_path: Path, monkeypatch):
    class FullDisk:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def write(self, block):
            raise OSError(errno.ENOSPC, "No space left on device")

    real_open = Path.open

    def fake_open(self, mode="r", *args, **kwargs):
        if self.name.endswith(".part") and ("w" in mode or "a" in mode):
            return FullDisk()
        return real_open(self, mode, *args, **kwargs)

    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(Path, "open", fake_open)
    monkeypatch.setattr(dl.shutil, "disk_usage", lambda p: Usage(10**12, 10**12 - 123_456_789, 123_456_789))
    calls: list = []
    sleeps: list[float] = []
    with pytest.raises(NsError) as ei:
        dl.download(URL, tmp_path / "m" / "a.bin", SHA, client=make_client(calls=calls), sleep=sleeps.append)
    assert ei.value.code == "NS-E104"
    assert "117.7 MB free" in ei.value.detail and ei.value.data["free_bytes"] == 123_456_789
    assert [c[0] for c in calls].count("GET") == 1 and sleeps == []      # no retry loop on a full disk


def test_the_windows_disk_full_codes_are_recognised():
    class DiskFull(OSError):
        winerror = 112                                   # what Windows reports for ERROR_DISK_FULL

    err = DiskFull(0, "There is not enough space on the disk")
    assert dl.is_disk_full(err) and dl.is_disk_full(OSError(errno.ENOSPC, "full"))
    assert not dl.is_disk_full(OSError(errno.EACCES, "denied"))


def test_retries_and_the_backoff_are_capped(tmp_path: Path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": "5"})
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps: list[float] = []
    with pytest.raises(NsError) as ei:
        dl.download(URL, tmp_path / "a.bin", None, client=client, retries=99, sleep=sleeps.append)
    assert ei.value.code == "NS-E105" and sleeps == [2, 4, 8, 16]        # five attempts, never more
    monkeypatch.setattr(dl, "MAX_RETRIES", 8)
    sleeps.clear()
    with pytest.raises(NsError):
        dl.download(URL, tmp_path / "a.bin", None, client=client, retries=8, sleep=sleeps.append)
    assert sleeps == [2, 4, 8, 16, 30, 30, 30]                           # the wait stops growing at 30 s


# ---------------------------------------------------------------------------------------------- rerun
def test_a_stale_marker_is_ignored_when_the_file_was_deleted_resized_or_touched(tmp_path: Path):
    dest = tmp_path / "a.bin"
    dl.download(URL, dest, SHA, client=make_client(), sleep=no_sleep)
    assert dl.is_verified(dest, SHA) and dl.marker_matches(dest)
    st = dest.stat()
    os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert not dl.is_verified(dest, SHA)                                 # changed mtime
    os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert dl.is_verified(dest, SHA)
    with dest.open("ab") as fh:
        fh.write(b"x")
    os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert not dl.is_verified(dest, SHA)                                 # changed size
    dest.unlink()
    assert not dl.is_verified(dest, SHA) and dl._marker(dest).exists()   # deleted, marker left behind
    (tmp_path / "bad.ok").write_text("[1, 2]", encoding="utf-8")         # nonsense in a marker is no marker
    (tmp_path / "bad").write_bytes(b"x")
    assert not dl.is_verified(tmp_path / "bad", None)


def test_rerun_after_a_crash_skips_verified_files_fast_and_resumes_every_part(tmp_path: Path):
    n = 40
    items = [dl.DownloadItem(name=f"f{i}", url=f"https://x.test/f{i}", dest=tmp_path / "m" / f"f{i}.bin",
                             sha256=SHA) for i in range(n)]
    first = dl.run_plan(items, client=make_client(), sleep=no_sleep)
    assert len(first.downloaded) == n
    d = [i.dest for i in items]
    dl._marker(d[0]).unlink()                                            # killed between rename and marker
    d[1].unlink()                                                        # file gone, a half .part left
    d[1].with_name("f1.bin.part").write_bytes(DATA[:300_000])
    with d[2].open("ab") as fh:                                          # file changed size under its marker
        fh.write(b"x")
    d[3].unlink()                                                        # file gone, marker left, no part
    st = d[4].stat()  # file touched: mtime no longer matches
    os.utime(d[4], ns=(st.st_atime_ns, st.st_mtime_ns + 7_000_000_000))

    seen: dict[str, list] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.setdefault(request.url.path, []).append((request.method, request.headers.get("range")))
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(DATA))})
        rng = request.headers.get("range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            return httpx.Response(206, content=DATA[start:],
                                  headers={"content-range": f"bytes {start}-{len(DATA) - 1}/{len(DATA)}"})
        return httpx.Response(200, content=DATA)

    t0 = time.perf_counter()
    todo = dl.pending(items)
    elapsed = time.perf_counter() - t0
    assert [i.name for i in todo] == ["f0", "f1", "f2", "f3", "f4"]
    assert elapsed / n < 1.0 and elapsed < 1.0                           # stats and tiny reads, no hashing
    second = dl.run_plan(items, client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=no_sleep)
    assert sorted(second.skipped) == sorted(f"f{i}" for i in range(5, n))
    assert sorted(second.downloaded) == ["f0", "f1", "f2", "f3", "f4"]
    assert not any(f"/f{i}" in seen for i in range(5, n))  # a verified file costs no request at all
    assert seen["/f0"] == [("HEAD", None)] and seen["/f4"] == [("HEAD", None)]   # hashed locally, adopted
    assert seen["/f1"] == [("HEAD", None), ("GET", "bytes=300000-")]     # the part continued
    assert seen["/f2"] == [("HEAD", None), ("GET", None)]
    assert seen["/f3"] == [("HEAD", None), ("GET", None)]
    assert all(dl.is_verified(p, SHA) for p in d) and all(p.read_bytes() == DATA for p in d)


def test_a_finished_file_without_a_pin_is_adopted_when_its_size_matches(tmp_path: Path):
    dest = tmp_path / "a.bin"
    dl.download(URL, dest, None, client=make_client(), sleep=no_sleep)
    dl._marker(dest).unlink()
    calls: list = []
    dl.download(URL, dest, None, client=make_client(calls=calls), sleep=no_sleep)
    assert calls == [("HEAD", None)] and dl.is_verified(dest, None)
    dl._marker(dest).unlink()
    dest.write_bytes(DATA[:-1])                                          # same name, wrong size: not adopted
    calls.clear()
    dl.download(URL, dest, None, client=make_client(calls=calls), sleep=no_sleep)
    assert ("GET", None) in calls and dest.read_bytes() == DATA


def test_a_download_hash_failure_never_leaves_a_marker(tmp_path: Path):
    dest = tmp_path / "a.bin"
    with pytest.raises(NsError):
        dl.download(URL, dest, hashlib.sha256(b"other").hexdigest(), client=make_client(), sleep=no_sleep)
    assert not dest.exists() and not dl._marker(dest).exists()


# ---------------------------------------------------------------------------------------------- models_source
def test_a_copy_from_models_source_never_touches_a_download_in_progress(tmp_path: Path):
    src = tmp_path / "usb"
    src.mkdir()
    (src / "a.bin").write_bytes(b"not the right file")
    dest = tmp_path / "home" / "a.bin"
    dest.parent.mkdir()
    part = dest.with_name("a.bin.part")
    part.write_bytes(DATA[:100])
    assert dl.copy_from_source(src, dest, SHA) is False
    assert part.read_bytes() == DATA[:100]                               # the resumable part is still there
    assert not list(dest.parent.glob("*.copy"))


def test_stop_in_the_middle_of_a_copy_leaves_no_half_file(tmp_path: Path):
    src = tmp_path / "usb"
    src.mkdir()
    (src / "a.bin").write_bytes(DATA)
    dest = tmp_path / "home" / "a.bin"
    with pytest.raises(Boom):
        dl.copy_from_source(src, dest, SHA, progress=stop_after(1))
    assert not list(dest.parent.iterdir())
