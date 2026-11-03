"""C1: wizard steps s00 to s05, plus the small helpers they use."""

import ast
import json
from pathlib import Path

import httpx
import pytest

from installer.stage2 import sysinfo, winenv
from installer.stage2.answers import AnswersError
from installer.stage2.downloader import is_verified
from installer.stage2.fallback import parse_pin
from installer.stage2.runner import RunResult
from installer.stage2.simulate import FakeRunner
from installer.stage2.steps.s00_welcome import S00Welcome
from installer.stage2.steps.s01_questions import S01Questions, validate_pexels, validate_pixabay
from installer.stage2.steps.s02_download_bins import (
    QWENTTS_RELEASE_URL,
    QWENTTS_RELEASE_URL_OLD,
    S02DownloadBins,
)
from installer.stage2.steps.s03_download_models import S03DownloadModels
from installer.stage2.steps.s04_venvs import S04Venvs, spacy_wheel_url
from installer.stage2.steps.s05_opencode_prewarm import S05OpencodePrewarm
from installer.stage2.testing import FakeUi, example_answers, make_test_context
from ns_common.config import load_runtime
from ns_common.errors import NsError

ROOT = Path(__file__).resolve().parents[1]
GOOD_PEXELS, GOOD_PIXABAY = "good-pexels-key-123", "good-pixabay-key-456"


def keyed_http():
    """Mock Pexels / Pixabay: only the two GOOD keys are accepted."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.pexels.com":
            ok = request.headers.get("authorization") == GOOD_PEXELS
            return httpx.Response(200 if ok else 401, json={"photos": []})
        if request.url.host == "pixabay.com":
            ok = request.url.params.get("key") == GOOD_PIXABAY
            return httpx.Response(200 if ok else 400, text="[]")
        return httpx.Response(404)

    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def all_log_text(ctx) -> str:
    return "\n".join(f.read_text(encoding="utf-8") for f in ctx.paths.logs.glob("*.jsonl"))


# ---------------------------------------------------------------------------------------------- hygiene
def test_installer_python_has_no_print_calls():
    offenders = []
    for py in (ROOT / "installer").rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
                offenders.append(f"{py.name}:{node.lineno}")
    assert offenders == []


def test_keyring_is_only_imported_inside_functions():
    for py in (ROOT / "installer").rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Import | ast.ImportFrom):
                names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
                assert "keyring" not in names, f"{py.name} imports keyring at module level"


# ---------------------------------------------------------------------------------------------- sysinfo
def test_choose_default_home_prefers_ssd_then_most_free():
    d = sysinfo.Drive
    assert sysinfo.choose_default_home([d("C", 100, "SSD"), d("D", 900, "HDD"), d("E", 300, "SSD")]) == Path(
        "E:\\Nightshift"
    )
    # an SSD that is too small loses to a big HDD
    assert sysinfo.choose_default_home([d("C", 30, "SSD"), d("D", 900, "HDD")]) == Path("D:\\Nightshift")
    # nothing is big enough: take the roomiest anyway
    small = [d("C", 10, "SSD"), d("D", 20, "HDD")]
    assert sysinfo.choose_default_home(small) == Path("C:\\Nightshift")
    assert sysinfo.choose_default_home([]) is None


def test_llama_pair_for_cuda_versions():
    assert sysinfo.llama_pair_for("13.0") == "cuda13"
    assert sysinfo.llama_pair_for("13.4") == "cuda13"
    assert sysinfo.llama_pair_for("12.8") == "cuda12"
    assert sysinfo.llama_pair_for("12.4") == "cuda12"
    assert sysinfo.llama_pair_for("12.2") is None
    assert sysinfo.llama_pair_for("") is None and sysinfo.llama_pair_for("x.y") is None


def test_required_free_gb_and_drive_type():
    assert [sysinfo.required_free_gb(t) for t in ("small", "balanced", "best")] == [60, 74, 81]
    pf = {"drives": [{"letter": "d", "free_gb": 5, "type": "HDD"}]}
    assert sysinfo.drive_type(pf, Path("D:\\Nightshift")) == "HDD"
    assert sysinfo.drive_type(pf, Path("E:\\x")) == "Unknown"
    assert sysinfo.drive_type(None, Path("E:\\x")) == "Unknown"


def test_load_preflight_reads_bom_and_ignores_garbage(tmp_path: Path):
    f = tmp_path / "pf.json"
    f.write_bytes(b"\xef\xbb\xbf" + json.dumps({"schema": 1}).encode())
    assert sysinfo.load_preflight(f) == {"schema": 1}
    f.write_text("nope", encoding="utf-8")
    assert sysinfo.load_preflight(f) is None and sysinfo.load_preflight(tmp_path / "missing") is None


def test_winenv_scripts_are_quoted_and_encoded(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desk")
    assert winenv.set_user_env(runner, winenv.HOME_VAR, "D:\\Night'shift")
    assert winenv.get_user_env(runner, winenv.HOME_VAR) == "D:\\Night'shift"
    lnk = tmp_path / "desk" / "X.lnk"
    assert winenv.create_shortcut(runner, lnk, "powershell.exe", "-File a.ps1", tmp_path, "d")
    assert lnk.exists()
    assert winenv.desktop_dir(runner) == tmp_path / "desk"
    assert winenv.ps_quote("a'b") == "a''b"
    argv = runner.calls[0].argv
    assert argv[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand"]


def test_parse_pin_splits_index_url():
    spec, index = parse_pin("torch==2.9.1+cu128 --index-url https://download.pytorch.org/whl/cu128")
    assert spec == "torch==2.9.1+cu128" and index == "https://download.pytorch.org/whl/cu128"
    assert parse_pin("qwen-tts==0.1.1") == ("qwen-tts==0.1.1", None)


# ---------------------------------------------------------------------------------------------- s00
def test_s00_shows_welcome_and_hardware_table(tmp_path: Path):
    ctx = make_test_context(tmp_path)
    step = S00Welcome()
    assert step.check(ctx) is False
    step.run(ctx)
    step.verify(ctx)
    assert "Welcome to Nightshift" in ctx.ui.markdowns[0]
    title, rows = ctx.ui.tables[0]
    assert title == "Your PC" and len(rows) == 10 and rows[0][1] == "ok"


def test_s00_red_row_raises_its_code(tmp_path: Path):
    pf = sysinfo.sample_preflight(tmp_path / "home", ROOT)
    pf["checks"][1] = {"id": "gpu", "label": "GPU", "status": "fail", "code": "NS-E102", "value": "none",
                       "hint": "x"}
    ctx = make_test_context(tmp_path, preflight=pf)
    with pytest.raises(NsError) as ei:
        S00Welcome().verify(ctx)
    assert ei.value.code == "NS-E102"


def test_s00_without_preflight_says_so_and_resume_skips(tmp_path: Path):
    ctx = make_test_context(tmp_path, preflight=None)
    S00Welcome().run(ctx)
    S00Welcome().verify(ctx)
    assert "No hardware summary" in ctx.ui.text
    ctx.resumed = True
    assert S00Welcome().check(ctx) is True


# ---------------------------------------------------------------------------------------------- s01
def provided(tmp_path: Path, **over):
    return example_answers(home_path=str(tmp_path / "home"), **over)


def test_s01_answers_from_file_write_blanked_answers(tmp_path: Path):
    ctx = make_test_context(tmp_path, raw_answers=provided(tmp_path), ui=FakeUi(interactive=False))
    step = S01Questions()
    assert step.check(ctx) is False
    step.run(ctx)
    step.verify(ctx)
    assert ctx.answers is not None and ctx.answers.channel.display_name == "After Dark Tales"
    saved = json.loads((ctx.paths.config / "answers.json").read_text(encoding="utf-8"))
    assert saved["pexels_api_key"] == "" and saved["pixabay_api_key"] == ""
    assert saved["home_path"] == str(ctx.paths.home) and saved["llm_tier"] == "small"
    assert ctx.runner.user_env["NIGHTSHIFT_HOME"] == str(ctx.paths.home)
    # a second context finds the saved answers: the step is satisfied
    ctx2 = make_test_context(tmp_path, ui=FakeUi(interactive=False))
    assert S01Questions().check(ctx2) is True and ctx2.answers is not None


def test_s01_asks_only_what_is_missing_and_plays_four_voice_samples(tmp_path: Path):
    ui = FakeUi({
        "niche": "cosy mystery",
        "Narrator voice": "preset",
        "Which voice": "Aiden",
        "Install Nightshift Code": "n",
    })
    raw = {"home_path": str(tmp_path / "home"), "channel": {"display_name": "Night Owl"}}
    ctx = make_test_context(tmp_path, raw_answers=raw, ui=ui)
    S01Questions().run(ctx)
    ans = ctx.answers
    assert ans is not None
    assert ans.channel.niche == "cosy mystery" and ans.channel.language == "en"
    assert ans.voice.speaker == "Aiden" and ans.install_code is False
    assert ans.masters_per_week == 1 and ans.run_time == "03:00" and ans.llm_tier == "balanced"
    assert len(ui.played) == 4
    assert not any("home" in p.lower() and "Install folder" in p for p in ui.asked)   # home was provided
    assert not any("Channel name" in p for p in ui.asked)                              # name was provided


def test_s01_balanced_is_the_default_and_small_the_fallback_under_32_gb(tmp_path: Path):
    raw = provided(tmp_path)
    del raw["llm_tier"]
    ctx = make_test_context(tmp_path, raw_answers=raw, ui=FakeUi(interactive=False))
    S01Questions().run(ctx)
    assert ctx.answers is not None and ctx.answers.llm_tier == "balanced"
    pf = sysinfo.sample_preflight(tmp_path / "b" / "home", ROOT)
    pf["ram_gb"] = 16
    raw2 = provided(tmp_path / "b")
    del raw2["llm_tier"]
    ui = FakeUi(interactive=False)
    ctx2 = make_test_context(tmp_path / "b", raw_answers=raw2, ui=ui, preflight=pf)
    S01Questions().run(ctx2)
    assert ctx2.answers is not None and ctx2.answers.llm_tier == "small"
    assert "16 GB of memory, so Small is selected" in ui.text


def test_s01_unattended_missing_required_answer_fails_clearly(tmp_path: Path):
    raw = {"home_path": str(tmp_path / "home"), "channel": {"display_name": "Only Name"}}
    ctx = make_test_context(tmp_path, raw_answers=raw, ui=FakeUi(interactive=False))
    with pytest.raises(AnswersError, match="niche"):
        S01Questions().run(ctx)


def test_s01_disk_space_check_is_e104(tmp_path: Path):
    ctx = make_test_context(tmp_path, raw_answers=provided(tmp_path), ui=FakeUi(interactive=False))
    ctx.free_gb_fn = lambda _p: 25.0
    with pytest.raises(NsError) as ei:
        S01Questions().run(ctx)
    assert ei.value.code == "NS-E104"
    assert "25 GB free" in ei.value.detail and "60 GB needed" in ei.value.detail


def test_s01_bigger_tier_needs_more_space_and_ram(tmp_path: Path):
    ctx = make_test_context(tmp_path, raw_answers=provided(tmp_path, llm_tier="best"),
                            ui=FakeUi(interactive=False))
    ctx.free_gb_fn = lambda _p: 70.0                      # enough for small (60), not for best (81)
    with pytest.raises(NsError) as ei:
        S01Questions().run(ctx)
    assert "81 GB needed" in ei.value.detail
    pf = sysinfo.sample_preflight(tmp_path / "home", ROOT)
    pf["ram_gb"] = 16
    ctx2 = make_test_context(tmp_path / "b", raw_answers=provided(tmp_path / "b", llm_tier="balanced"),
                             ui=FakeUi(interactive=False), preflight=pf)
    with pytest.raises(AnswersError, match="32 GB of RAM"):
        S01Questions().run(ctx2)


def test_s01_hdd_gives_w106_warning(tmp_path: Path):
    letter = (tmp_path / "home").drive[:1].upper()
    if not letter:
        pytest.skip("needs a drive letter")
    pf = sysinfo.sample_preflight(tmp_path / "home", ROOT)
    pf["drives"] = [{"letter": letter, "free_gb": 500, "type": "HDD"}]
    ui = FakeUi(interactive=False)
    ctx = make_test_context(tmp_path, raw_answers=provided(tmp_path), ui=ui, preflight=pf)
    S01Questions().run(ctx)
    assert [c for c, _ in ctx.warnings] == ["NS-W106"]


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"run_time": "25:99"}, "HH:MM"),
        ({"masters_per_week": 7}, "masters_per_week"),
        ({"models_source": "Z:\\no\\such\\folder"}, "models_source"),
        ({"voice": {"mode": "clone"}}, "cloned voice"),
        ({"llm_tier": "huge"}, "llm_tier"),
    ],
)
def test_s01_rejects_bad_answers(tmp_path: Path, over: dict, message: str):
    ctx = make_test_context(tmp_path, raw_answers=provided(tmp_path, **over), ui=FakeUi(interactive=False))
    with pytest.raises(AnswersError, match=message):
        S01Questions().run(ctx)


def test_s01_validates_keys_live_stores_them_and_never_logs_them(tmp_path: Path):
    raw = provided(tmp_path, pexels_api_key=GOOD_PEXELS, pixabay_api_key=GOOD_PIXABAY)
    ctx = make_test_context(tmp_path, raw_answers=raw, ui=FakeUi(interactive=False), simulate=False,
                            http=keyed_http())
    step = S01Questions()
    step.run(ctx)
    step.verify(ctx)
    assert ctx.store.get("pexels_api_key") == GOOD_PEXELS and ctx.store.get("pixabay_api_key") == GOOD_PIXABAY
    saved = (ctx.paths.config / "answers.json").read_text(encoding="utf-8")
    for secret in (GOOD_PEXELS, GOOD_PIXABAY):
        assert secret not in saved and secret not in all_log_text(ctx) and secret not in ctx.ui.text


def test_s01_rejected_key_is_e501_and_missing_key_unattended_too(tmp_path: Path):
    bad = provided(tmp_path, pexels_api_key="nope", pixabay_api_key=GOOD_PIXABAY)
    ctx = make_test_context(tmp_path, raw_answers=bad, ui=FakeUi(interactive=False), simulate=False,
                            http=keyed_http())
    with pytest.raises(NsError) as ei:
        S01Questions().run(ctx)
    assert ei.value.code == "NS-E501" and "401" in ei.value.detail

    missing = provided(tmp_path / "m", pexels_api_key=GOOD_PEXELS)
    ctx2 = make_test_context(tmp_path / "m", raw_answers=missing, ui=FakeUi(interactive=False),
                             simulate=False,
                             http=keyed_http())
    with pytest.raises(NsError) as ei2:
        S01Questions().run(ctx2)
    assert ei2.value.code == "NS-E502"


def test_s01_asks_for_missing_keys_and_retries_a_bad_paste(tmp_path: Path):
    ui = FakeUi({"Pexels API key": ["wrong", GOOD_PEXELS], "Pixabay API key": [GOOD_PIXABAY]})
    ctx = make_test_context(tmp_path, raw_answers=provided(tmp_path), ui=ui, simulate=False,
                            http=keyed_http())
    S01Questions().run(ctx)
    assert ctx.store.get("pexels_api_key") == GOOD_PEXELS
    assert "did not work" in ui.text and "pexels.com/api" in ui.text and "pixabay.com/api/docs" in ui.text


def test_key_validators_map_status_codes(tmp_path: Path):
    def client(status: int):
        return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status)))

    validate_pexels(client(200), "k")
    validate_pexels(client(429), "k")                 # rate limited: the key was recognised
    validate_pixabay(client(200), "k")
    with pytest.raises(NsError) as e1:
        validate_pexels(client(403), "k")
    with pytest.raises(NsError) as e2:
        validate_pixabay(client(400), "k")

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(NsError) as e3:
        validate_pexels(httpx.Client(transport=httpx.MockTransport(boom)), "k")
    assert (e1.value.code, e2.value.code, e3.value.code) == ("NS-E501", "NS-E502", "NS-E503")


def test_s01_simulate_keeps_keys_out_of_files_but_in_the_store(tmp_path: Path):
    raw = provided(tmp_path, pexels_api_key="sim-key-1", pixabay_api_key="sim-key-2")
    ctx = make_test_context(tmp_path, raw_answers=raw, ui=FakeUi(interactive=False))
    S01Questions().run(ctx)
    assert ctx.store.get("pexels_api_key") == "sim-key-1"
    assert "sim-key" not in (ctx.paths.config / "answers.json").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------------- s02
def ctx_resolved(tmp_path: Path, **kw):
    return make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, **kw)


def unpinned(ctx):
    """The manifest as it was before bins.qwentts was pinned (the null-pin fallback paths)."""
    ctx.manifest = ctx.manifest.model_copy(update={"bins": {**ctx.manifest.bins, "qwentts": None}})
    return ctx


def test_s02_cuda13_default_pair_and_qwentts_fallback(tmp_path: Path):
    ctx = unpinned(ctx_resolved(tmp_path))          # sample preflight reports CUDA 13.0, probe answers 404
    step = S02DownloadBins()
    assert step.check(ctx) is False
    step.run(ctx)
    step.verify(ctx)
    llama = ctx.paths.bin_llama
    assert (llama / "llama-server.exe").exists()
    assert (llama / ".ns-llama_cuda13.done").exists() and (llama / ".ns-cudart13.done").exists()
    assert ctx.flag("llama_pair") == "cuda13" and ctx.flag("qwentts") == "none"
    assert ctx.flag("tts_fallback") is True
    assert load_runtime(ctx.paths).tts.engine == "reference"
    assert [c for c, _ in ctx.warnings] == ["NS-W301"]
    assert (ctx.paths.bin_opencode / "opencode.exe").exists()          # install_code is true
    ytdlp = ctx.paths.home / "bin" / "yt-dlp" / "yt-dlp.exe"
    ytdlp_pin = ctx.manifest.bins["yt_dlp"]
    assert ytdlp_pin is not None
    assert ytdlp.is_file() and is_verified(ytdlp, ytdlp_pin.sha256)
    assert step.check(ctx) is True


def test_s02_cuda12_pair_for_older_drivers(tmp_path: Path):
    pf = sysinfo.sample_preflight(tmp_path / "home", ROOT, cuda="12.4")
    pf["gpu"]["name"] = "NVIDIA GeForce RTX 4070"   # the 12.4 build cannot run on the simulated RTX 50 card
    ctx = ctx_resolved(tmp_path, preflight=pf)
    S02DownloadBins().run(ctx)
    assert ctx.flag("llama_pair") == "cuda12"
    assert (ctx.paths.bin_llama / ".ns-llama_cuda.done").exists()
    assert not (ctx.paths.bin_llama / ".ns-llama_cuda13.done").exists()


def test_s02_driver_below_12_4_is_e103(tmp_path: Path):
    pf = sysinfo.sample_preflight(tmp_path / "home", ROOT, cuda="12.2")
    ctx = ctx_resolved(tmp_path, preflight=pf)
    with pytest.raises(NsError) as ei:
        S02DownloadBins().check(ctx)
    assert ei.value.code == "NS-E103" and "12.2" in ei.value.detail


def test_s02_uses_the_qwentts_release_when_it_exists(tmp_path: Path):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200)

    ctx = unpinned(ctx_resolved(tmp_path, http=lambda: httpx.Client(transport=httpx.MockTransport(handler))))
    ctx.head_checks = True
    S02DownloadBins().run(ctx)
    assert QWENTTS_RELEASE_URL in seen
    assert QWENTTS_RELEASE_URL.endswith("/releases/latest/download/Nightshift-VoiceEngine-win-cuda-x64.zip")
    assert ctx.flag("qwentts") == "binary" and ctx.flag("tts_fallback") is False
    assert ctx.warnings == []
    assert (ctx.paths.home / "bin" / "qwentts" / ".ns-qwentts.done").exists()


def test_s02_tries_the_old_asset_name_second_when_the_new_one_is_404(tmp_path: Path):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(404 if str(request.url) == QWENTTS_RELEASE_URL else 200)

    ctx = unpinned(ctx_resolved(tmp_path, http=lambda: httpx.Client(transport=httpx.MockTransport(handler))))
    ctx.head_checks = True
    S02DownloadBins().run(ctx)
    assert seen.index(QWENTTS_RELEASE_URL_OLD) > seen.index(QWENTTS_RELEASE_URL)
    assert QWENTTS_RELEASE_URL_OLD.endswith("/releases/latest/download/qwentts-win-cuda-x64.zip")
    assert ctx.flag("qwentts") == "binary" and ctx.flag("tts_fallback") is False


def test_s02_both_asset_names_missing_means_the_pytorch_fallback(tmp_path: Path):
    ctx = unpinned(ctx_resolved(tmp_path, http=lambda: httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(404)))))
    ctx.head_checks = True
    S02DownloadBins().run(ctx)
    assert ctx.flag("qwentts") == "none" and ctx.flag("tts_fallback") is True


def test_s02_network_error_on_the_probe_means_fallback(tmp_path: Path):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    ctx = unpinned(ctx_resolved(tmp_path, http=lambda: httpx.Client(transport=httpx.MockTransport(boom))))
    ctx.head_checks = True
    S02DownloadBins().run(ctx)
    assert ctx.flag("qwentts") == "none"


def test_s02_installs_the_pinned_qwentts_binary_without_probing(tmp_path: Path):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200)

    ctx = ctx_resolved(tmp_path, http=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    ctx.head_checks = True
    S02DownloadBins().run(ctx)
    pin = ctx.manifest.bins["qwentts"]
    assert pin is not None and pin.url in seen and QWENTTS_RELEASE_URL not in seen
    assert ctx.flag("qwentts") == "binary" and ctx.flag("tts_fallback") is False
    assert ctx.warnings == []


def test_s02_opencode_only_when_chosen_and_cpu_profile_uses_cpu_build(tmp_path: Path):
    ctx = make_test_context(tmp_path, raw_answers=example_answers(install_code=False), resolve=True)
    S02DownloadBins().run(ctx)
    assert not (ctx.paths.bin_opencode / "opencode.exe").exists()

    pf = sysinfo.sample_preflight(tmp_path / "c" / "home", ROOT)
    pf["profile"], pf["cuda"] = "cpu-dev", ""
    ctx2 = make_test_context(tmp_path / "c", raw_answers=example_answers(), resolve=True, preflight=pf)
    S02DownloadBins().run(ctx2)
    assert (ctx2.paths.bin_llama / ".ns-llama_cpu.done").exists()


def test_s02_verify_notices_a_missing_binary(tmp_path: Path):
    ctx = ctx_resolved(tmp_path)
    S02DownloadBins().run(ctx)
    (ctx.paths.bin_llama / "llama-server.exe").unlink()
    with pytest.raises(NsError) as ei:
        S02DownloadBins().verify(ctx)
    assert ei.value.code == "NS-E113"           # it was unpacked once (the flag says so): a scanner took it


# ---------------------------------------------------------------------------------------------- s03
def test_s03_simulate_writes_every_model_file_and_is_idempotent(tmp_path: Path):
    ctx = ctx_resolved(tmp_path)
    step = S03DownloadModels()
    assert step.check(ctx) is False
    step.run(ctx)
    step.verify(ctx)
    p = ctx.paths
    for rel in (
        p.models_llm / "Qwen3-14B-Q4_K_M.gguf",
        p.models_embed / "nomic-embed-text-v1.5.Q8_0.gguf",
        p.models_tts / "qwen-talker-1.7b-customvoice-Q8_0.gguf",
        p.models_tts / "qwen-talker-1.7b-base-Q8_0.gguf",
        p.models_tts / "qwen-tokenizer-12hz-Q8_0.gguf",
        p.models_whisper / "distil-large-v3" / "model.bin",
        p.models_whisper / "distil-large-v3" / "config.json",
        p.home / "models" / "cv" / "yunet.onnx",
    ):
        assert rel.is_file(), rel
    assert "INSTALL.cmd" in ctx.ui.text and "files to fetch" in ctx.ui.text
    assert not (p.models_tts / "hf").exists()                          # no fallback unless flagged
    ctx2 = ctx_resolved(tmp_path)
    assert S03DownloadModels().check(ctx2) is True


def test_s03_adds_pytorch_safetensors_when_the_fallback_is_flagged(tmp_path: Path):
    ctx = ctx_resolved(tmp_path)
    ctx.set_flag("tts_fallback", True)
    step = S03DownloadModels()
    step.run(ctx)
    step.verify(ctx)
    hf = ctx.paths.models_tts / "hf"
    assert (hf / "Qwen3-TTS-12Hz-1.7B-CustomVoice" / "model.safetensors").is_file()
    assert (hf / "Qwen3-TTS-12Hz-1.7B-Base" / "model.safetensors").is_file()


def test_s03_optional_404_files_are_recorded_and_not_required(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(("vocabulary.json", "vocabulary.txt")):
            return httpx.Response(404)
        return httpx.Response(200)

    ctx = ctx_resolved(tmp_path, http=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    ctx.head_checks = True
    step = S03DownloadModels()
    step.run(ctx)
    step.verify(ctx)
    assert ctx.flag("missing_optional") == ["whisper/vocabulary.json", "whisper/vocabulary.txt"]
    wdir = ctx.paths.models_whisper / "distil-large-v3"
    assert (wdir / "preprocessor_config.json").exists() and not (wdir / "vocabulary.txt").exists()
    assert step.check(ctx) is True


def test_s03_required_404_fails_with_e105(tmp_path: Path):
    ctx = ctx_resolved(tmp_path, http=lambda: httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    ctx.head_checks = True
    with pytest.raises(NsError) as ei:
        S03DownloadModels().run(ctx)
    assert ei.value.code == "NS-E105"


def _tiny_manifest():
    """The real manifest with every model replaced by tiny files whose SHA256 the test knows."""
    import hashlib

    from ns_common.config import load_manifest

    m = load_manifest()
    gpu = m.profiles["gpu"]
    contents: dict[str, bytes] = {}

    def pin(f):
        body = f"content of {f.file}".encode()
        contents[Path(f.file).name] = body
        f.sha256, f.bytes = hashlib.sha256(body).hexdigest(), len(body)

    for f in (gpu.llm, gpu.embed, *gpu.tts):
        pin(f)
    body = b"whisper weights"
    gpu.whisper_repo.files_sha256["model.bin"] = hashlib.sha256(body).hexdigest()
    contents["model.bin"] = body
    yunet = m.bins["yunet"]
    assert yunet is not None
    contents["yunet.onnx"] = b"onnx"
    yunet.sha256 = hashlib.sha256(b"onnx").hexdigest()
    return m, contents


def test_s03_models_source_copies_matching_files_and_downloads_the_rest(tmp_path: Path):
    manifest, contents = _tiny_manifest()
    usb = tmp_path / "usb"
    (usb / "models").mkdir(parents=True)
    for name, body in contents.items():
        (usb / "models" / name).write_bytes(body)
    (usb / "models" / "qwen-tokenizer-12hz-Q8_0.gguf").write_bytes(b"corrupt copy")   # wrong hash: ignored
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(f"{request.method} {request.url.path}")
        name = request.url.path.rsplit("/", 1)[-1]
        if name in ("vocabulary.json", "vocabulary.txt", "preprocessor_config.json"):
            return httpx.Response(404)
        return httpx.Response(200, content=contents.get(name, b"{}"))

    raw = example_answers(models_source=str(usb))
    ctx = make_test_context(tmp_path, raw_answers=raw, resolve=True, manifest=manifest, simulate=False,
                            http=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    step = S03DownloadModels()
    step.run(ctx)
    step.verify(ctx)
    gets = [r for r in requested if r.startswith("GET")]
    assert sorted(g.rsplit("/", 1)[-1] for g in gets) == [
        "config.json", "qwen-tokenizer-12hz-Q8_0.gguf", "tokenizer.json",
    ]
    assert (ctx.paths.models_tts / "qwen-tokenizer-12hz-Q8_0.gguf").read_bytes() == contents[
        "qwen-tokenizer-12hz-Q8_0.gguf"]
    assert (ctx.paths.models_llm / "Qwen3-14B-Q4_K_M.gguf").read_bytes() == contents["Qwen3-14B-Q4_K_M.gguf"]
    assert "Looking for matching files" in ctx.ui.text
    assert S03DownloadModels().check(ctx) is True


# ---------------------------------------------------------------------------------------------- s04
def uv_calls(runner: FakeRunner):
    return [c for c in runner.calls if c.tag.startswith("uv ")]


def test_s04_creates_studio_and_supervisor_venvs_without_torch(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop")
    ctx = ctx_resolved(tmp_path, runner=runner)
    step = S04Venvs()
    assert step.check(ctx) is False
    step.run(ctx)
    step.verify(ctx)
    tags = [c.tag for c in uv_calls(runner)]
    assert tags == [
        "uv venv studio", "uv pip install studio app", "uv pip install studio gpu dlls",
        "uv pip install studio spacy model", "uv venv supervisor", "uv pip install supervisor app",
    ]
    app = next(c for c in runner.calls if c.tag == "uv pip install studio app")
    assert "-e" in app.argv and f"{ROOT}[studio,supervisor]" in app.argv
    assert app.argv[:3] == [ctx.uv(), "pip", "install"] and "--python" in app.argv
    dlls = next(c for c in runner.calls if c.tag == "uv pip install studio gpu dlls")
    assert "nvidia-cublas-cu12>=12.8,<13" in dlls.argv and "nvidia-cudnn-cu12>=9,<10" in dlls.argv
    spacy = next(c for c in runner.calls if c.tag == "uv pip install studio spacy model")
    assert spacy.argv[-1] == spacy_wheel_url("en_core_web_md")
    assert spacy.argv[-1].endswith("en_core_web_md-3.8.0-py3-none-any.whl")
    sup = next(c for c in runner.calls if c.tag == "uv pip install supervisor app")
    assert f"{ROOT}[supervisor,tts,dashboard]" in sup.argv
    assert not any("torch" in a for c in runner.calls for a in c.argv)
    assert not ctx.venv_python("tts").exists()
    assert ctx.venv_python("studio").exists() and step.check(ctx) is True
    assert runner.called("studio import check")


def test_s04_cpu_profile_skips_gpu_dll_wheels(tmp_path: Path):
    pf = sysinfo.sample_preflight(tmp_path / "home", ROOT)
    pf["profile"] = "cpu-dev"
    runner = FakeRunner(tmp_path / "desktop")
    ctx = ctx_resolved(tmp_path, runner=runner, preflight=pf)
    S04Venvs().run(ctx)
    assert not runner.called("uv pip install studio gpu dlls")


def test_s04_builds_the_tts_fallback_venv_only_when_flagged(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop")
    ctx = ctx_resolved(tmp_path, runner=runner)
    ctx.set_flag("tts_fallback", True)
    step = S04Venvs()
    step.run(ctx)
    step.verify(ctx)
    assert ctx.venv_python("tts").exists() and ctx.flag("tts_venv_installed") is True
    torch = next(c for c in runner.calls if c.tag == "uv pip install tts torch")
    assert "torch==2.9.1+cu128" in torch.argv
    assert torch.argv[-2:] == ["--index-url", "https://download.pytorch.org/whl/cu128"]
    app = next(c for c in runner.calls if c.tag == "uv pip install tts app")
    assert "--no-deps" in app.argv and f"{ROOT}[tts]" in app.argv
    assert runner.called("tts torch check")
    assert step.check(ctx) is True


def test_s04_torch_that_cannot_see_the_gpu_is_e108(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop", results={"tts torch check": RunResult(1)})
    ctx = ctx_resolved(tmp_path, runner=runner)
    ctx.set_flag("tts_fallback", True)
    with pytest.raises(NsError) as ei:
        S04Venvs().run(ctx)
    assert ei.value.code == "NS-E108" and "cu128" in ei.value.definition.hint


def test_s04_uv_failure_is_reported(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop", results={"uv venv studio": RunResult(2)})
    ctx = ctx_resolved(tmp_path, runner=runner)
    with pytest.raises(NsError) as ei:
        S04Venvs().run(ctx)
    assert ei.value.code == "NS-E105" and "exit 2" in ei.value.detail


# ---------------------------------------------------------------------------------------------- s05
def test_s05_skipped_without_install_code(tmp_path: Path):
    ctx = make_test_context(tmp_path, raw_answers=example_answers(install_code=False), resolve=True)
    assert S05OpencodePrewarm().skip_reason(ctx) is not None


def test_s05_runs_the_binary_once(tmp_path: Path):
    ctx = ctx_resolved(tmp_path)
    step = S05OpencodePrewarm()
    assert step.skip_reason(ctx) is None and step.check(ctx) is False
    with pytest.raises(NsError):
        step.verify(ctx)                                   # no binary yet
    (ctx.paths.bin_opencode / "opencode.exe").write_bytes(b"")
    step.run(ctx)
    step.verify(ctx)
    assert step.check(ctx) is True and "1.19.5" in ctx.ui.text
    assert step.optional is True
