import hashlib
import json
import re

import httpx

from tests.online import give_up, online_client, retry_call, skip_on_outage, split_outage
from tools import resolve_manifest as rm

GGUF = "Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL.gguf"
LLAMA_ZIP = "llama-b9999-bin-win-cuda-12.4-x64.zip"
BASE = "https://github.com/ggml-org/llama.cpp/releases/download/b9999"
TREE = [
    {"type": "file", "path": GGUF, "size": 21000000000, "lfs": {"oid": "ab" * 32, "size": 21000000000}},
    {"type": "file", "path": "README.md", "size": 100},
]
RELEASE = {
    "assets": [
        {
            "name": LLAMA_ZIP,
            "browser_download_url": f"{BASE}/{LLAMA_ZIP}",
            "digest": "sha256:" + "cd" * 32,
        }
    ]
}


def _client():
    def handler(req: httpx.Request) -> httpx.Response:
        if "huggingface.co/api/models" in str(req.url):
            return httpx.Response(200, json=TREE)
        if "api.github.com" in str(req.url):
            return httpx.Response(200, json=RELEASE)
        return httpx.Response(200)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _sample_manifest() -> dict:
    return {
        "version": "0.1.0", "python_version": "3.12", "spacy_model": "en_core_web_md", "tts_packages": [],
        "profiles": {
            "gpu": {
                "llm": {"repo": "unsloth/Qwen3.6-35B-A3B-MTP-GGUF", "file": GGUF},
                "embed": {"repo": "x/y", "file": GGUF},
                "tts": [{"repo": "Serveurperso/Qwen3-TTS-GGUF", "file": GGUF}],
                "tts_optional": [{"repo": "Serveurperso/Qwen3-TTS-GGUF", "file": GGUF}],
                "whisper_model": "distil-large-v3",
                "whisper_repo": {"repo": "Systran/faster-distil-whisper-large-v3"},
            }
        },
        "bins": {
            "llama_cuda": {
                "version": "b9999",
                "url": f"{BASE}/{LLAMA_ZIP}",
            },
            "qwentts": None,
        },
    }


def test_hf_tree_maps_sha():
    t = rm.hf_tree("unsloth/Qwen3.6-35B-A3B-MTP-GGUF", "main", _client())
    assert t[GGUF] == ("ab" * 32, 21000000000)
    assert "README.md" not in t


def test_fill_sets_sha_and_bytes():
    out = rm.fill(_sample_manifest(), _client())
    gpu = out["profiles"]["gpu"]
    assert gpu["llm"]["sha256"] == "ab" * 32
    assert gpu["llm"]["bytes"] == 21000000000
    assert gpu["tts"][0]["sha256"] == "ab" * 32
    assert gpu["tts_optional"][0]["bytes"] == 21000000000
    assert gpu["whisper_repo"]["files_sha256"][GGUF] == "ab" * 32
    assert out["bins"]["llama_cuda"]["sha256"] == "cd" * 32
    assert out["bins"]["qwentts"] is None
    assert rm.check(out, _client()) == []


def test_check_reports_missing_sha():
    mf = {
        "profiles": {
            "gpu": {
                "llm": {"repo": "a/b", "file": "f.gguf", "sha256": None},
                "embed": {"repo": "a/b", "file": "e", "sha256": "0" * 64},
                "tts": [],
                "whisper_model": "x",
            }
        },
        "bins": {},
    }
    problems = rm.check(mf, _client())
    assert any("llm" in p and "sha256" in p for p in problems)


def test_check_flags_a_python_pin_without_a_hash_or_an_exact_version():
    mf = {
        "profiles": {}, "bins": {},
        "python": {
            "whisper_cuda": [
                "nvidia-cublas-cu12>=12.8", "nvidia-cudnn-cu12==9.10.2.21",
                "ok==1.0 --hash=sha256:" + "a" * 64,
            ],
            "spacy_wheel": {"version": "3.8.0", "url": "https://example.test/x.whl", "sha256": None},
        },
    }
    problems = rm.check(mf, _client())
    assert [p.split(":")[0] for p in problems if p.startswith("python.whisper_cuda")] == [
        "python.whisper_cuda[0]", "python.whisper_cuda[1]"]
    assert any(p.startswith("python.spacy_wheel") and "sha256" in p for p in problems)


def _all_sha256(mf) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    for pname, prof in mf.profiles.items():
        for k in ("llm", "llm_balanced", "llm_best", "story_llm", "embed"):
            f = getattr(prof, k)
            if f is not None:
                out.append((f"{pname}.{k}", f.sha256))
        for k in ("tts", "tts_optional"):
            for i, f in enumerate(getattr(prof, k)):
                out.append((f"{pname}.{k}[{i}]", f.sha256))
        for fname, sha in prof.whisper_repo.files_sha256.items():
            out.append((f"{pname}.whisper_repo.{fname}", sha))
    for name, b in mf.bins.items():
        if b is not None and not (b.optional and b.sha256 == "TBD-release"):
            out.append((f"bins.{name}", b.sha256))
    if mf.python.spacy_wheel is not None:
        out.append(("python.spacy_wheel", mf.python.spacy_wheel.sha256))
    return out


def test_repo_manifest_validates():
    from ns_common.config import load_manifest

    mf = load_manifest()
    assert set(mf.profiles) == {"gpu", "cpu-dev"}
    gpu = mf.profiles["gpu"]
    assert gpu.llm.repo == "Qwen/Qwen3-14B-GGUF" and "Q4_K_M" in gpu.llm.file
    assert gpu.llm_balanced is not None and gpu.llm_balanced.repo.startswith("mudler/")
    assert gpu.llm_best is not None and gpu.llm_best.repo.startswith("unsloth/")
    assert [f.file for f in gpu.tts] == [
        "qwen-talker-1.7b-customvoice-Q8_0.gguf",
        "qwen-talker-1.7b-base-Q8_0.gguf",
        "qwen-tokenizer-12hz-Q8_0.gguf",
    ]
    assert [f.file for f in gpu.tts_optional] == ["qwen-talker-1.7b-voicedesign-Q8_0.gguf"]
    assert gpu.whisper_model == "distil-large-v3"
    assert gpu.whisper_repo.repo == "Systran/faster-distil-whisper-large-v3"
    assert mf.profiles["cpu-dev"].llm.repo == "Qwen/Qwen3-1.7B-GGUF"
    for key in ("llama_cuda", "cudart", "llama_cpu", "qwentts", "opencode", "uv", "yt_dlp", "yunet"):
        assert key in mf.bins
    assert any(p.startswith("torch==") for p in mf.tts_packages)


def test_python_pins_in_the_repo_manifest(repo_root):
    """CR-C1-4 a, b, c: hash-locked cuBLAS/cuDNN and spaCy wheels, fallback repos named by the manifest."""
    from ns_common.config import load_manifest

    mf = load_manifest()
    lines = mf.python.whisper_cuda
    assert [line.split("==")[0] for line in lines] == ["nvidia-cublas-cu12", "nvidia-cudnn-cu12"]
    assert all(rm.PIN_LINE_RE.match(line) for line in lines), lines
    assert "==12.8." in lines[0] and "==9." in lines[1]     # CTranslate2 4.8.5: CUDA 12.8, cuDNN 9
    wheel = mf.python.spacy_wheel
    assert wheel is not None and wheel.url.endswith(f"{mf.spacy_model}-{wheel.version}-py3-none-any.whl")
    assert mf.tts_fallback_repos == ["Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", "Qwen/Qwen3-TTS-12Hz-1.7B-Base"]
    raw = json.loads((repo_root / "manifest.json").read_text(encoding="utf-8"))
    assert "python" in raw and "tts_fallback_repos" in raw     # written in the file, not only defaulted


def test_every_sha256_is_64_hex():
    from ns_common.config import load_manifest

    entries = _all_sha256(load_manifest())
    assert entries
    for where, sha in entries:
        assert sha is not None and re.fullmatch(r"[0-9a-f]{64}", sha), where


@skip_on_outage
def test_repo_manifest_check_online(repo_root):
    """Same as `tools/resolve_manifest.py --check`: every URL answers HEAD, every sha256 is set.

    A 5xx, a timeout or a reset connection is retried (tests/online.py); if it lasts it is an outage and the
    test skips. A 404 or a missing hash is a wrong pin and fails."""
    import json

    mf = json.loads((repo_root / "manifest.json").read_text(encoding="utf-8"))
    with online_client(timeout=60, headers={"User-Agent": "nightshift-resolve"}) as client:
        client.head("https://huggingface.co", follow_redirects=True)     # an outage here skips the test
        problems = rm.check(mf, client)
    real, outage = split_outage(problems)
    assert real == []
    if outage:
        give_up(outage[0])


def _online_client() -> httpx.Client:
    client = online_client(timeout=300, headers={"User-Agent": "nightshift-resolve"}, follow_redirects=True)
    try:
        client.head("https://pypi.org")                                  # an outage here skips the test
    except BaseException:
        client.close()
        raise
    return client


@skip_on_outage
def test_whisper_cuda_pins_match_the_pypi_hashes_online(repo_root):
    """The sha256 of each pinned cuBLAS/cuDNN wheel is the one PyPI lists for its win_amd64 file."""
    lines = json.loads((repo_root / "manifest.json").read_text(encoding="utf-8"))["python"]["whisper_cuda"]
    client = _online_client()
    try:
        for line in lines:
            spec, sha = line.split(" --hash=sha256:")
            name, version = spec.split("==")
            r = client.get(f"https://pypi.org/pypi/{name}/{version}/json")
            r.raise_for_status()
            win = [u for u in r.json()["urls"] if u["filename"].endswith("win_amd64.whl")]
            assert win, f"{name} {version} has no win_amd64 wheel"
            assert sha in {u["digests"]["sha256"] for u in win}, line
    finally:
        client.close()


def _sha256_of_download(client: httpx.Client, url: str) -> str:
    digest = hashlib.sha256()
    with client.stream("GET", url) as r:
        r.raise_for_status()
        for chunk in r.iter_bytes(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


@skip_on_outage
def test_spacy_wheel_pin_matches_the_download_online(repo_root):
    """Streams the pinned spaCy model wheel (about 33 MB) and compares its sha256 with the manifest."""
    pin = json.loads((repo_root / "manifest.json").read_text(encoding="utf-8"))["python"]["spacy_wheel"]
    client = _online_client()
    try:
        # the transport retries the connection and a 5xx; this also retries a download cut off half way
        got = retry_call(lambda: _sha256_of_download(client, pin["url"]), host=httpx.URL(pin["url"]).host)
    finally:
        client.close()
    assert got == pin["sha256"]
