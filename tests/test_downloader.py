"""C1: resumable verified downloader, manifest plans and models_source copy mode."""

import hashlib
import http.server
import io
import json
import threading
import time
import zipfile
from pathlib import Path

import httpx
import pytest

from installer.stage2 import downloader as dl
from ns_common.config import load_manifest
from ns_common.errors import NsError

DATA = bytes(range(256)) * 4096          # 1 MiB
SHA = hashlib.sha256(DATA).hexdigest()


def make_client(data: bytes = DATA, *, ranges: bool = True, calls: list | None = None, fail_first: int = 0):
    state = {"gets": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append((request.method, request.headers.get("range")))
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(data))})
        state["gets"] += 1
        if state["gets"] <= fail_first:
            return httpx.Response(500)
        rng = request.headers.get("range")
        if rng and ranges:
            start = int(rng.split("=")[1].split("-")[0])
            hdr = {"content-range": f"bytes {start}-{len(data) - 1}/{len(data)}"}
            return httpx.Response(206, content=data[start:], headers=hdr)
        return httpx.Response(200, content=data)

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def no_sleep(_: float) -> None:
    return None


# ---------------------------------------------------------------------------------------------- single file
def test_full_download_verifies_and_renames(tmp_path: Path):
    dest = tmp_path / "m" / "a.bin"
    seen: list[tuple[int, int | None]] = []
    out = dl.download("https://x.test/a.bin", dest, SHA, client=make_client(),
                      progress=lambda d, t: seen.append((d, t)))
    assert out == dest and dest.read_bytes() == DATA
    assert not dest.with_name("a.bin.part").exists()
    assert seen[-1] == (len(DATA), len(DATA))
    assert dl.is_verified(dest, SHA)


def test_resume_uses_range_from_part_size(tmp_path: Path):
    dest = tmp_path / "a.bin"
    dest.with_name("a.bin.part").write_bytes(DATA[:300_000])
    calls: list = []
    dl.download("https://x.test/a.bin", dest, SHA, client=make_client(calls=calls))
    assert ("GET", "bytes=300000-") in calls
    assert dest.read_bytes() == DATA


def test_server_that_ignores_range_restarts_the_file(tmp_path: Path):
    dest = tmp_path / "a.bin"
    dest.with_name("a.bin.part").write_bytes(DATA[:1000])
    dl.download("https://x.test/a.bin", dest, SHA, client=make_client(ranges=False))
    assert dest.read_bytes() == DATA


def test_complete_part_needs_no_get(tmp_path: Path):
    dest = tmp_path / "a.bin"
    dest.with_name("a.bin.part").write_bytes(DATA)
    calls: list = []
    dl.download("https://x.test/a.bin", dest, SHA, client=make_client(calls=calls))
    assert [m for m, _ in calls] == ["HEAD"] and dest.read_bytes() == DATA


def test_resume_false_discards_part(tmp_path: Path):
    dest = tmp_path / "a.bin"
    dest.with_name("a.bin.part").write_bytes(b"garbage")
    calls: list = []
    dl.download("https://x.test/a.bin", dest, SHA, client=make_client(calls=calls), resume=False)
    assert ("GET", None) in calls and dest.read_bytes() == DATA


def test_wrong_sha_removes_the_file_retries_once_then_raises_e109(tmp_path: Path):
    dest = tmp_path / "a.bin"
    sleeps: list[float] = []
    calls: list = []
    with pytest.raises(NsError) as ei:
        dl.download("https://x.test/a.bin", dest, "0" * 64, client=make_client(calls=calls),
                    sleep=sleeps.append)
    assert ei.value.code == "NS-E109"
    assert [m for m, _ in calls].count("GET") == 2  # the first wrong file is dropped and fetched once more
    assert [r for m, r in calls if m == "GET"] == [None, None]  # ... from zero, never resumed from a bad part
    assert sleeps == [2]
    assert not dest.exists() and not dest.with_name("a.bin.part").exists()


def test_retry_then_success(tmp_path: Path):
    sleeps: list[float] = []
    dest = tmp_path / "a.bin"
    dl.download("https://x.test/a.bin", dest, SHA, client=make_client(fail_first=1), sleep=sleeps.append)
    assert dest.read_bytes() == DATA and sleeps == [2]


def test_unreachable_is_e105(tmp_path: Path):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    client = httpx.Client(transport=httpx.MockTransport(boom))
    with pytest.raises(NsError) as ei:
        dl.download("https://x.test/a.bin", tmp_path / "a.bin", SHA, client=client, sleep=no_sleep)
    assert ei.value.code == "NS-E105"


def test_404_is_e105_without_retry(tmp_path: Path):
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(NsError) as ei:
        dl.download("https://x.test/a.bin", tmp_path / "a.bin", SHA, client=client, sleep=no_sleep)
    assert ei.value.code == "NS-E105" and ei.value.data["status"] == 404
    assert calls == ["HEAD"]


def test_verified_marker_skips_second_download(tmp_path: Path):
    dest = tmp_path / "a.bin"
    dl.download("https://x.test/a.bin", dest, SHA, client=make_client())
    calls: list = []
    dl.download("https://x.test/a.bin", dest, SHA, client=make_client(calls=calls))
    assert calls == []


def test_tampered_file_is_downloaded_again(tmp_path: Path):
    dest = tmp_path / "a.bin"
    dl.download("https://x.test/a.bin", dest, SHA, client=make_client())
    dest.write_bytes(b"changed")
    assert not dl.is_verified(dest, SHA)
    dl.download("https://x.test/a.bin", dest, SHA, client=make_client())
    assert dest.read_bytes() == DATA


def test_no_pinned_hash_still_downloads(tmp_path: Path):
    dest = tmp_path / "a.bin"
    dl.download("https://x.test/a.bin", dest, None, client=make_client())
    assert dest.read_bytes() == DATA


# ---------------------------------------------------------------------------------------------- real http
class _RangeHandler(http.server.BaseHTTPRequestHandler):
    data = DATA
    seen: list = []

    def log_message(self, *args) -> None:
        return None

    def _send(self, with_body: bool) -> None:
        rng = self.headers.get("Range")
        start = int(rng.split("=")[1].split("-")[0]) if rng else 0
        if rng:
            type(self).seen.append(rng)
        body = self.data[start:]
        self.send_response(206 if rng else 200)
        self.send_header("Content-Length", str(len(body)))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{len(self.data) - 1}/{len(self.data)}")
        self.end_headers()
        if with_body:
            self.wfile.write(body)

    def do_GET(self) -> None:
        self._send(True)

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.data)))
        self.end_headers()


@pytest.fixture
def range_server():
    _RangeHandler.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/file.bin"
    server.shutdown()
    server.server_close()


def test_resume_against_real_http_server(tmp_path: Path, range_server: str):
    dest = tmp_path / "file.bin"
    dest.with_name("file.bin.part").write_bytes(DATA[:123_456])
    dl.download(range_server, dest, SHA)
    assert dest.read_bytes() == DATA
    assert _RangeHandler.seen == ["bytes=123456-"]


# ---------------------------------------------------------------------------------------------- source
def test_copy_from_source_matches_name_and_sha(tmp_path: Path):
    src = tmp_path / "src"
    (src / "models" / "llm").mkdir(parents=True)
    (src / "models" / "llm" / "a.bin").write_bytes(DATA)
    dest = tmp_path / "home" / "a.bin"
    assert dl.copy_from_source(src, dest, SHA) is True
    assert dest.read_bytes() == DATA and dl.is_verified(dest, SHA)
    assert not dest.with_name("a.bin.part").exists()


def test_copy_from_source_ignores_wrong_hash_and_missing(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.bin").write_bytes(b"not the right file")
    assert dl.copy_from_source(src, tmp_path / "o" / "a.bin", SHA) is False
    assert not (tmp_path / "o" / "a.bin").exists()
    assert dl.copy_from_source(src, tmp_path / "o" / "missing.bin", SHA) is False
    assert dl.copy_from_source(tmp_path / "nope", tmp_path / "o" / "a.bin", SHA) is False


# ---------------------------------------------------------------------------------------------- zips
def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return buf.getvalue()


def test_extract_zip_rejects_zip_slip(tmp_path: Path):
    z = tmp_path / "evil.zip"
    z.write_bytes(_zip_bytes({"../escape.txt": b"x"}))
    with pytest.raises(NsError) as ei:
        dl.extract_zip(z, tmp_path / "out")
    assert ei.value.code == "NS-E109"
    assert not (tmp_path / "escape.txt").exists()


def test_run_plan_zip_extracts_verifies_expect_and_is_idempotent(tmp_path: Path):
    body = _zip_bytes({"llama-server.exe": b"MZ", "ggml.dll": b"x"})
    sha = hashlib.sha256(body).hexdigest()
    item = dl.DownloadItem(
        name="llama", url="https://x.test/l.zip", dest=tmp_path / "dl" / "l.zip", sha256=sha, kind="zip",
        extract_to=tmp_path / "bin" / "llama", expect=("llama-server.exe",),
    )
    res = dl.run_plan([item], client=make_client(body))
    assert res.downloaded == ["llama"]
    assert (tmp_path / "bin" / "llama" / "llama-server.exe").read_bytes() == b"MZ"
    assert not item.dest.exists()
    calls: list = []
    res2 = dl.run_plan([item], client=make_client(body, calls=calls))
    assert res2.skipped == ["llama"] and calls == []


def test_run_plan_zip_missing_expected_file_fails(tmp_path: Path):
    body = _zip_bytes({"other.txt": b"x"})
    item = dl.DownloadItem(
        name="llama", url="https://x.test/l.zip", dest=tmp_path / "l.zip", sha256=None, kind="zip",
        extract_to=tmp_path / "bin", expect=("llama-server.exe",),
    )
    with pytest.raises(NsError) as ei:
        dl.run_plan([item], client=make_client(body))
    assert ei.value.code == "NS-E109"


# ---------------------------------------------------------------------------------------------- plans
class _Rec:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def start(self, name, total):
        self.events.append(("start", name))

    def update(self, name, done, total):
        return None

    def finish(self, name, outcome):
        self.events.append((outcome, name))


def test_run_plan_concurrency_is_two(tmp_path: Path):
    lock = threading.Lock()
    state = {"in_flight": 0, "peak": 0, "gets": 0}
    barrier = threading.Barrier(2, timeout=10)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(DATA))})
        with lock:
            state["gets"] += 1
            first_two = state["gets"] <= 2
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
        try:
            if first_two:
                barrier.wait()          # both first downloads must be running at the same time
            return httpx.Response(200, content=DATA)
        finally:
            with lock:
                state["in_flight"] -= 1

    client = httpx.Client(transport=httpx.MockTransport(handler))
    items = [dl.DownloadItem(name=f"f{i}", url=f"https://x.test/{i}", dest=tmp_path / f"f{i}", sha256=SHA)
             for i in range(5)]
    rec = _Rec()
    res = dl.run_plan(items, client=client, reporter=rec)
    assert sorted(res.downloaded) == [f"f{i}" for i in range(5)]
    assert state["peak"] == 2
    assert ("downloaded", "f0") in rec.events


def test_run_plan_optional_404_is_reported_not_raised(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("missing.json"):
            return httpx.Response(404)
        return httpx.Response(200, content=DATA, headers={"content-length": str(len(DATA))})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    items = [
        dl.DownloadItem(name="ok", url="https://x.test/ok.bin", dest=tmp_path / "ok.bin", sha256=SHA),
        dl.DownloadItem(
            name="opt", url="https://x.test/missing.json", dest=tmp_path / "m.json", optional=True
        ),
    ]
    res = dl.run_plan(items, client=client)
    assert res.missing_optional == ["opt"] and res.downloaded == ["ok"]


def test_run_plan_failure_still_finishes_other_files_then_raises(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if "bad" in request.url.path:
            return httpx.Response(200, content=b"wrong", headers={"content-length": "5"})
        return httpx.Response(200, content=DATA, headers={"content-length": str(len(DATA))})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    items = [
        dl.DownloadItem(name="bad", url="https://x.test/bad", dest=tmp_path / "bad", sha256=SHA),
        dl.DownloadItem(name="good", url="https://x.test/good", dest=tmp_path / "good", sha256=SHA),
    ]
    with pytest.raises(NsError) as ei:
        dl.run_plan(items, client=client, sleep=no_sleep)
    assert ei.value.code == "NS-E109"
    assert (tmp_path / "good").read_bytes() == DATA


def test_run_plan_uses_models_source_and_downloads_the_rest(tmp_path: Path):
    src = tmp_path / "usb"
    src.mkdir()
    (src / "have.bin").write_bytes(DATA)
    calls: list = []
    items = [
        dl.DownloadItem(
            name="have", url="https://x.test/have.bin", dest=tmp_path / "h" / "have.bin", sha256=SHA
        ),
        dl.DownloadItem(
            name="need", url="https://x.test/need.bin", dest=tmp_path / "h" / "need.bin", sha256=SHA
        ),
    ]
    res = dl.run_plan(items, client=make_client(calls=calls), models_source=src)
    assert res.copied == ["have"] and res.downloaded == ["need"]
    assert all(call[0] in ("HEAD", "GET") for call in calls)
    assert (tmp_path / "h" / "have.bin").read_bytes() == DATA


def test_pending_and_total_bytes(tmp_path: Path):
    a = dl.DownloadItem(name="a", url="u", dest=tmp_path / "a", sha256=SHA, size=10)
    b = dl.DownloadItem(name="b", url="u", dest=tmp_path / "b", sha256=SHA, size=20)
    assert dl.total_bytes([a, b]) == 30
    dl.download("https://x.test/a", a.dest, SHA, client=make_client())
    assert dl.pending([a, b]) == [b]


def test_human_bytes_and_eta():
    assert dl.human_bytes(512) == "512 B"
    assert dl.human_bytes(9 * 1024**3) == "9.0 GB"
    assert dl.eta_text(15 * 10**9, 50).endswith("minutes")


# ---------------------------------------------------------------------------------------------- manifest
@pytest.fixture(scope="module")
def manifest():
    return load_manifest()


def _names(items):
    return [i.name for i in items]


def test_plan_small_tier_gpu(manifest, tmp_path: Path):
    items = dl.plan_from_manifest(manifest, "gpu", "small", tmp_path, llama_pair="cuda12")
    by = {i.name: i for i in items}
    llm = by["Qwen3-14B-Q4_K_M.gguf"]
    rev = manifest.profiles["gpu"].llm.revision
    assert rev != "main"
    assert llm.url == f"https://huggingface.co/Qwen/Qwen3-14B-GGUF/resolve/{rev}/Qwen3-14B-Q4_K_M.gguf"
    assert llm.dest == tmp_path / "models" / "llm" / "Qwen3-14B-Q4_K_M.gguf"
    assert llm.sha256 == manifest.profiles["gpu"].llm.sha256 and llm.size == 9001752960
    assert "nomic-embed-text-v1.5.Q8_0.gguf" in by
    assert {"qwen-talker-1.7b-customvoice-Q8_0.gguf", "qwen-talker-1.7b-base-Q8_0.gguf",
            "qwen-tokenizer-12hz-Q8_0.gguf"} <= set(by)
    assert "qwen-talker-1.7b-voicedesign-Q8_0.gguf" not in by          # on demand only
    assert by["whisper/model.bin"].dest == tmp_path / "models" / "whisper" / "distil-large-v3" / "model.bin"
    assert by["whisper/model.bin"].sha256 and by["whisper/config.json"].sha256 is None
    assert by["whisper/vocabulary.txt"].optional and not by["whisper/config.json"].optional
    assert by["yunet"].dest == tmp_path / "models" / "cv" / "yunet.onnx"
    assert by["llama_cuda"].kind == "zip" and by["llama_cuda"].expect == ("llama-server.exe",)
    assert by["llama_cuda"].extract_to == tmp_path / "bin" / "llama"
    assert "cudart" in by and "llama_cuda13" not in by and "opencode" not in by
    assert dl.total_bytes(items) > 13 * 10**9


def test_plan_tiers_pick_the_right_llm(manifest, tmp_path: Path):
    bal = dl.plan_models(manifest, "gpu", "balanced", tmp_path)
    best = dl.plan_models(manifest, "gpu", "best", tmp_path)
    assert "Qwen3.6-35B-A3B-APEX-MTP-I-Mini.gguf" in _names(bal)
    assert "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf" in _names(best)
    assert "Qwen3-14B-Q4_K_M.gguf" not in _names(bal) + _names(best)


def test_plan_cuda13_pair_and_code(manifest, tmp_path: Path):
    items = dl.plan_bins(manifest, "gpu", tmp_path, include_code=True, llama_pair="cuda13")
    assert _names(items)[:4] == ["llama_cuda13", "cudart13", "opencode", "yt_dlp"]
    assert "cuda-13.4" in items[0].url
    assert items[2].extract_to == tmp_path / "bin" / "opencode"


def test_plan_includes_the_pinned_ytdlp_for_every_profile(manifest, tmp_path: Path):
    pin = manifest.bins["yt_dlp"]
    assert pin is not None and pin.url.endswith("/yt-dlp.exe") and pin.sha256 and len(pin.sha256) == 64
    assert pin.version in pin.url and "github.com/yt-dlp/yt-dlp/releases/download/" in pin.url
    for profile, pair in (("gpu", "cuda13"), ("gpu", "cuda12"), ("cpu-dev", None)):
        by = {i.name: i for i in dl.plan_bins(manifest, profile, tmp_path, llama_pair=pair)}
        item = by["yt_dlp"]
        assert item.kind == "file" and item.sha256 == pin.sha256 and item.url == pin.url
        assert item.dest == tmp_path / "bin" / "yt-dlp" / "yt-dlp.exe"


def test_plan_cpu_dev(manifest, tmp_path: Path):
    items = dl.plan_from_manifest(manifest, "cpu-dev", "best", tmp_path)
    names = _names(items)
    assert "llama_cpu" in names and "Qwen3-1.7B-Q8_0.gguf" in names
    assert "qwen-talker-0.6b-customvoice-Q8_0.gguf" in names
    assert "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf" not in names
    assert "whisper/model.bin" in names


def test_qwentts_item_is_the_pinned_zip_and_none_for_a_null_pin(manifest, tmp_path: Path):
    pin = manifest.bins["qwentts"]
    assert pin is not None                          # pinned to the v0.1.0 release asset since PR #36
    item = dl.qwentts_item(manifest, tmp_path)
    assert item is not None and item.url == pin.url and item.sha256 == pin.sha256
    unpinned = manifest.model_copy(update={"bins": {**manifest.bins, "qwentts": None}})
    assert dl.qwentts_item(unpinned, tmp_path) is None
    url = "https://github.com/o/r/releases/latest/download/q.zip"
    item = dl.release_zip_item("qwentts", url, None, tmp_path)
    assert item.kind == "zip" and item.extract_to == tmp_path / "bin" / "qwentts"


def test_plan_from_hf_tree(tmp_path: Path):
    tree = [
        {"type": "file", "path": "config.json", "size": 100},
        {"type": "file", "path": ".gitattributes", "size": 5},
        {"type": "directory", "path": "sub"},
        {"type": "file", "path": "sub/model.safetensors", "size": 5,
         "lfs": {"oid": "ab" * 32, "size": 4_000_000_000}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/models/Qwen/Qwen3-TTS-12Hz-1.7B-Base/tree/main"
        assert request.url.params["recursive"] == "true"
        return httpx.Response(200, content=json.dumps(tree))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    items = dl.plan_from_hf_tree("Qwen/Qwen3-TTS-12Hz-1.7B-Base", "main", tmp_path / "hf", client)
    assert [i.name.split("/", 2)[2] for i in items] == ["config.json", "sub/model.safetensors"]
    big = items[1]
    assert big.sha256 == "ab" * 32 and big.size == 4_000_000_000
    assert big.dest == tmp_path / "hf" / "sub" / "model.safetensors"
    assert big.url.endswith("/Qwen/Qwen3-TTS-12Hz-1.7B-Base/resolve/main/sub/model.safetensors")
    assert items[0].sha256 is None


def test_plan_from_hf_tree_error_is_e105(tmp_path: Path):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(NsError) as ei:
        dl.plan_from_hf_tree("a/b", "main", tmp_path, client)
    assert ei.value.code == "NS-E105"


# ---------------------------------------------------------------------------------------------- pause
def wait_until(pred, what: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


class ChunkedBody(httpx.SyncByteStream):
    """A response body served in fixed chunks. It can clear the pause gate just before serving chunk N."""

    def __init__(self, data: bytes, chunk: int, log: dict, *, gate=None, clear_before: int | None = None):
        self.data, self.chunk, self.log = data, chunk, log
        self.gate, self.clear_before = gate, clear_before

    def __iter__(self):
        for index, start in enumerate(range(0, len(self.data), self.chunk)):
            if self.gate is not None and index == self.clear_before:
                self.gate.clear()              # the operator presses Pause while this chunk is in flight
            self.log["served"] += 1
            yield self.data[start:start + self.chunk]

    def close(self) -> None:
        self.log["closed"] += 1


def ranged_handler(data: bytes, chunk: int, log: dict, ranges: list, *, gate=None, clear_before=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(data))})
        rng = request.headers.get("range")
        ranges.append(rng)
        start = int(rng.split("=")[1].split("-")[0]) if rng else 0
        body = data[start:]
        headers = {"content-length": str(len(body))}
        if rng:
            headers["content-range"] = f"bytes {start}-{len(data) - 1}/{len(data)}"
        stream = ChunkedBody(body, chunk, log, gate=gate if not rng else None, clear_before=clear_before)
        return httpx.Response(206 if rng else 200, headers=headers, stream=stream)

    return handler


def test_pause_stops_the_stream_within_one_chunk_and_resume_continues_from_the_right_byte(tmp_path: Path):
    chunk = 64 * 1024
    data = bytes(range(256)) * 4096                     # 1 MiB = 16 chunks
    sha = hashlib.sha256(data).hexdigest()
    gate = threading.Event()
    gate.set()
    log = {"served": 0, "closed": 0}
    ranges: list = []
    handler = ranged_handler(data, chunk, log, ranges, gate=gate, clear_before=3)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    dest = tmp_path / "a.bin"
    out: dict = {}
    worker = threading.Thread(
        target=lambda: out.update(path=dl.download("https://x.test/a.bin", dest, sha, client=client,
                                                   gate=gate, chunk=chunk, sleep=no_sleep)))
    worker.start()
    wait_until(lambda: log["closed"] >= 1, "the paused stream to be closed")
    assert worker.is_alive()                             # parked at the gate, not finished and not failed
    part = dest.with_name("a.bin.part")
    assert log["served"] == 4                            # chunk 3 came after the clear; chunk 4 not read
    assert part.stat().st_size == 4 * chunk              # the byte offset the Range request must carry
    assert ranges == [None]
    gate.set()                                           # Resume
    worker.join(timeout=10)
    assert not worker.is_alive() and out["path"] == dest
    assert ranges == [None, f"bytes={4 * chunk}-"]       # reconnected from the right byte
    assert dest.read_bytes() == data and dl.is_verified(dest, sha)
    assert log["closed"] == 2                            # one connection per stream, none left open


def test_a_cleared_gate_before_the_first_byte_sends_no_request_until_resume(tmp_path: Path):
    data = DATA
    gate = threading.Event()                             # starts paused
    log = {"served": 0, "closed": 0}
    ranges: list = []
    client = httpx.Client(transport=httpx.MockTransport(ranged_handler(data, 1 << 16, log, ranges)))
    dest = tmp_path / "a.bin"
    worker = threading.Thread(target=lambda: dl.download("https://x.test/a.bin", dest, SHA, client=client,
                                                         gate=gate, chunk=1 << 16, sleep=no_sleep))
    worker.start()
    time.sleep(0.05)
    assert worker.is_alive() and ranges == [] and not dest.exists()
    gate.set()
    worker.join(timeout=10)
    assert not worker.is_alive() and dest.read_bytes() == data


def test_a_pause_that_drops_the_connection_does_not_use_up_a_retry(tmp_path: Path):
    chunk = 64 * 1024
    data = bytes(range(256)) * 4096
    gate = threading.Event()
    gate.set()
    state = {"gets": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(data))})
        state["gets"] += 1
        if state["gets"] == 1:
            gate.clear()                                 # paused, and the connection breaks with it
            raise httpx.ReadError("connection reset", request=request)
        rng = request.headers.get("range")
        body = data if not rng else data[int(rng.split("=")[1].split("-")[0]):]
        return httpx.Response(200, content=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps: list[float] = []
    dest = tmp_path / "a.bin"
    worker = threading.Thread(target=lambda: dl.download(
        "https://x.test/a.bin", dest, None, client=client, gate=gate, chunk=chunk, retries=1,
        sleep=sleeps.append))
    worker.start()
    wait_until(lambda: state["gets"] >= 1, "the first request")
    time.sleep(0.05)
    assert worker.is_alive()                             # that one failure was a pause, so no retry was used
    gate.set()
    worker.join(timeout=10)
    assert not worker.is_alive() and dest.read_bytes() == data and sleeps == []


def test_head_info_reads_the_hugging_face_linked_size_without_following_the_redirect():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.host))
        if request.url.host == "huggingface.co":
            return httpx.Response(302, headers={"location": "https://cdn.test/f", "x-linked-size": "1234"})
        return httpx.Response(200, headers={"content-length": "9"})

    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    assert dl.head_info(http, "https://huggingface.co/o/r/resolve/main/f.gguf") == (200, 1234)
    assert seen == [("HEAD", "huggingface.co")]          # the CDN was not asked


def test_head_info_follows_a_redirect_without_a_linked_size_and_reports_a_missing_length():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a.test":
            return httpx.Response(302, headers={"location": "https://b.test/f"})
        if request.url.path == "/nolen":
            return httpx.Response(200)
        if request.url.path == "/gone":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-length": "77"})

    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    assert dl.head_info(http, "https://a.test/x") == (200, 77)
    assert dl.head_info(http, "https://b.test/nolen") == (200, None)
    assert dl.head_info(http, "https://b.test/gone") == (404, None)


# ---------------------------------------------------------------------------------------------- stall
def start_stalling_server(data: bytes, *, stall_after: int, stall_first: int = 1):
    """A loopback server. Its first ``stall_first`` GETs send ``stall_after`` bytes, then go silent."""
    state: dict = {"ranges": [], "release": threading.Event()}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            return None

        def do_HEAD(self) -> None:  # noqa: N802 - http.server API
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            rng = self.headers.get("Range")
            state["ranges"].append(rng)
            start = int(rng.split("=")[1].split("-")[0]) if rng else 0
            body = data[start:]
            self.send_response(206 if rng else 200)
            self.send_header("Content-Length", str(len(body)))
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
            self.end_headers()
            try:
                if len(state["ranges"]) <= stall_first:
                    self.wfile.write(body[:stall_after])
                    self.wfile.flush()
                    state["release"].wait(15)             # the server stops sending; the client must give up
                else:
                    self.wfile.write(body)
            except OSError:
                return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


def stop_server(server, state) -> None:
    state["release"].set()
    server.shutdown()
    server.server_close()


def test_a_stalled_transfer_is_aborted_and_retried_with_range_as_a_network_error(tmp_path: Path):
    chunk = 64 * 1024
    data = bytes(range(256)) * 4096
    sha = hashlib.sha256(data).hexdigest()
    server, state = start_stalling_server(data, stall_after=3 * chunk)
    sleeps: list[float] = []
    retries: list[tuple[int, str, float]] = []
    dest = tmp_path / "a.bin"
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/a.bin"
        with httpx.Client(trust_env=False) as client:
            dl.download(url, dest, sha, client=client, sleep=sleeps.append, stall_timeout=0.3, chunk=chunk,
                        on_retry=lambda attempt, reason, wait: retries.append((attempt, reason, wait)))
    finally:
        stop_server(server, state)
    assert dest.read_bytes() == data and dl.is_verified(dest, sha)
    assert state["ranges"] == [None, f"bytes={3 * chunk}-"]  # resumed where the stall began
    assert sleeps == [2] and retries == [(1, "stall", 2.0)]       # it counted as an attempt and waited


def test_a_server_that_always_stalls_ends_in_e105_after_the_retries(tmp_path: Path):
    data = bytes(range(256)) * 1024
    server, state = start_stalling_server(data, stall_after=1000, stall_first=99)
    sleeps: list[float] = []
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/a.bin"
        with httpx.Client(trust_env=False) as client, pytest.raises(NsError) as ei:
            dl.download(url, tmp_path / "a.bin", None, client=client, sleep=sleeps.append,
                        stall_timeout=0.2, retries=2)
    finally:
        stop_server(server, state)
    assert ei.value.code == "NS-E105" and "no data for 0.2 s" in ei.value.detail
    assert len(state["ranges"]) == 2 and sleeps == [2]


def test_the_default_stall_timeout_is_sixty_seconds():
    assert dl.STALL_TIMEOUT_S == 60.0
    client = dl._new_client()
    try:
        assert client.timeout.read == 60.0
    finally:
        client.close()


def test_run_plan_tells_the_reporter_about_a_retry(tmp_path: Path):
    class Rec(_Rec):
        def __init__(self) -> None:
            super().__init__()
            self.retries: list[tuple] = []

        def retrying(self, name, attempt, reason, wait_s):
            self.retries.append((name, attempt, reason, wait_s))

    rec = Rec()
    item = dl.DownloadItem(name="f0", url="https://x.test/f0", dest=tmp_path / "f0", sha256=SHA)
    res = dl.run_plan([item], client=make_client(fail_first=1), reporter=rec, sleep=no_sleep)
    assert res.downloaded == ["f0"] and rec.retries == [("f0", 1, "network", 2.0)]
    plain = _Rec()                                       # a reporter without ``retrying`` is simply not told
    item2 = dl.DownloadItem(name="f1", url="https://x.test/f1", dest=tmp_path / "f1", sha256=SHA)
    res2 = dl.run_plan([item2], client=make_client(fail_first=1), reporter=plain, sleep=no_sleep)
    assert res2.downloaded == ["f1"]


def test_plan_without_voice_leaves_out_every_voice_model(manifest, tmp_path: Path):
    """The `fake_voice` engine test (docs/DEV_MODE.md) downloads the real models, no talker or tokenizer."""
    items = dl.plan_models(
        manifest, "cpu-dev", "small", tmp_path, include_voice=False, include_voicedesign=True
    )
    names = _names(items)
    assert not any(n.startswith("qwen-") for n in names), names        # talker and tokenizer GGUFs
    assert "Qwen3-1.7B-Q8_0.gguf" in names and "whisper/model.bin" in names and "yunet" in names
    with_voice = _names(dl.plan_models(manifest, "cpu-dev", "small", tmp_path))
    assert "qwen-talker-0.6b-customvoice-Q8_0.gguf" in with_voice
