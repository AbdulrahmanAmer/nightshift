"""C1: resumable verified downloader, manifest plans and models_source copy mode."""

import hashlib
import http.server
import io
import json
import threading
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


def test_wrong_sha_raises_e109_after_three_attempts(tmp_path: Path):
    dest = tmp_path / "a.bin"
    sleeps: list[float] = []
    calls: list = []
    with pytest.raises(NsError) as ei:
        dl.download("https://x.test/a.bin", dest, "0" * 64, client=make_client(calls=calls),
                    sleep=sleeps.append)
    assert ei.value.code == "NS-E109"
    assert [m for m, _ in calls].count("GET") == 3
    assert sleeps == [2, 4]
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
    assert llm.url == "https://huggingface.co/Qwen/Qwen3-14B-GGUF/resolve/main/Qwen3-14B-Q4_K_M.gguf"
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
    assert _names(items) == ["llama_cuda13", "cudart13", "opencode", "yt_dlp"]
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


def test_qwentts_item_is_none_while_manifest_pin_is_null(manifest, tmp_path: Path):
    assert manifest.bins["qwentts"] is None
    assert dl.qwentts_item(manifest, tmp_path) is None
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
