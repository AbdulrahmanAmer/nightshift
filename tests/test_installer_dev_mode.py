# ruff: noqa: E501
"""C1: `INSTALL.cmd --dev` in the wizard (docs/DEV_MODE.md): the real steps with stub models, no GPU packages,
fake engines and a disabled nightly task. Every `if ctx.dev_mode` branch has its test here; the matching real
mode is asserted next to it so a dev branch cannot leak into a real install."""

import json
from pathlib import Path

import pytest

from installer.stage2 import devmode, verify_downloads
from installer.stage2.answers import write_answers as write_answers_file
from installer.stage2.dev_stubs import stub_fetch
from installer.stage2.downloader import is_done, is_verified, plan_models
from installer.stage2.runner import RunResult
from installer.stage2.runtime_cfg import build_runtime, ensure_baseline
from installer.stage2.simulate import FakeRunner
from installer.stage2.steps import default_steps
from installer.stage2.steps.s01_questions import S01Questions
from installer.stage2.steps.s02_download_bins import S02DownloadBins
from installer.stage2.steps.s03_download_models import S03DownloadModels
from installer.stage2.steps.s04_venvs import S04Venvs
from installer.stage2.steps.s05_opencode_prewarm import S05OpencodePrewarm
from installer.stage2.steps.s06_tune import S06Tune
from installer.stage2.steps.s07_configs import S07Configs
from installer.stage2.steps.s08_smoke import S08Smoke
from installer.stage2.steps.s09_shortcuts_scheduler import S09ShortcutsScheduler
from installer.stage2.steps.s10_summary import S10Summary
from installer.stage2.testing import FakeUi, example_answers, make_test_context
from installer.stage2.wizard import main
from ns_common.config import load_install_state, load_manifest, load_runtime, save_runtime
from ns_common.models import Runtime
from ns_common.paths import Paths

ROOT = Path(__file__).resolve().parents[1]


def dev_ctx(tmp_path: Path, runner=None, *, dev: bool = True, **answers):
    return make_test_context(
        tmp_path, raw_answers=example_answers(**answers), resolve=True, runner=runner, dev_mode=dev
    )


def stub_files(ctx) -> list[Path]:
    return sorted(p for p in ctx.paths.home.rglob("*") if p.is_file() and devmode.is_stub(p))


# ---------------------------------------------------------------------------------------------- the context
def test_a_dev_install_is_always_the_cpu_dev_profile_and_the_small_stub_model(tmp_path: Path):
    ctx = dev_ctx(tmp_path, llm_tier="balanced", install_code=True, include_voicedesign=True)
    assert ctx.profile == "cpu-dev"
    ans = ctx.ensure_answers()
    assert (ans.llm_tier, ans.install_code, ans.include_voicedesign, ans.llm_custom, ans.models_source) == (
        "small", False, False, None, None)


def test_a_real_install_keeps_its_answers_and_its_gpu_profile(tmp_path: Path):
    ctx = dev_ctx(tmp_path / "r", dev=False, llm_tier="balanced", install_code=True)
    ans = ctx.ensure_answers()
    assert (ctx.profile, ans.llm_tier, ans.install_code) == ("gpu", "balanced", True)


# ---------------------------------------------------------------------------------------------- stubs
def test_a_stub_carries_a_recognisable_header_and_a_marker_a_real_install_rejects(tmp_path: Path):
    dest = tmp_path / "models" / "llm" / "Qwen3-1.7B-Q8_0.gguf"
    size = devmode.write_stub(dest)
    assert size == devmode.STUB_BYTES == dest.stat().st_size
    assert dest.read_bytes().startswith(devmode.HEADER) and b"INSTALL.cmd --dev" in dest.read_bytes()
    assert devmode.is_stub(dest) and dest.with_name(dest.name + ".dev-stub").is_file()
    assert is_verified(dest, None)                                  # a dev install accepts it
    assert not is_verified(dest, "ab" * 32)                         # a pinned hash never does
    assert json.loads(dest.with_name(dest.name + ".ok").read_text("utf-8"))["sha256"] == devmode.STUB_DIGEST


def test_json_stubs_stay_valid_json(tmp_path: Path):
    dest = tmp_path / "whisper" / "config.json"
    devmode.write_stub(dest)
    assert json.loads(dest.read_text(encoding="utf-8"))["nightshift_dev_stub"] is True
    assert dest.stat().st_size == devmode.STUB_BYTES


def test_remove_stubs_deletes_only_stubs(tmp_path: Path):
    stub = tmp_path / "models" / "a.gguf"
    devmode.write_stub(stub)
    keep = tmp_path / "models" / "real.gguf"
    keep.write_bytes(b"x" * 10)
    gone = devmode.remove_stubs(tmp_path / "models")
    assert gone == [stub] and not stub.exists() and not (tmp_path / "models" / "a.gguf.ok").exists()
    assert keep.is_file() and devmode.remove_stubs(tmp_path / "missing") == []


def test_the_dev_plan_stubs_every_model_but_the_face_finder(tmp_path: Path):
    manifest = load_manifest()
    items = plan_models(manifest, "cpu-dev", "small", tmp_path, dev=True)
    stubs = {i.name: i for i in items if i.stub}
    real = [i for i in items if not i.stub]
    assert [i.name for i in real] == ["yunet"] and real[0].sha256 is not None
    assert {"Qwen3-1.7B-Q8_0.gguf", "nomic-embed-text-v1.5.Q8_0.gguf", "qwen-talker-0.6b-customvoice-Q8_0.gguf",
            "qwen-tokenizer-12hz-Q8_0.gguf", "whisper/model.bin"} <= set(stubs)
    assert all(i.sha256 is None and i.size == devmode.STUB_BYTES and not i.optional for i in stubs.values())
    assert not any(i.optional for i in items)
    plain = plan_models(manifest, "cpu-dev", "small", tmp_path)
    assert not any(i.stub for i in plain) and any(i.optional for i in plain)      # the real plan is unchanged


def test_stub_fetch_leaves_a_real_model_alone(tmp_path: Path):
    manifest = load_manifest()
    item = next(i for i in plan_models(manifest, "cpu-dev", "small", tmp_path, dev=True) if i.name.endswith("gguf"))
    item.dest.parent.mkdir(parents=True, exist_ok=True)
    item.dest.write_bytes(b"R" * (devmode.STUB_BYTES + 100))                 # a real model from a real install
    stub_fetch([item])(item.url, item.dest, None, progress=None)
    assert item.dest.read_bytes() == b"R" * (devmode.STUB_BYTES + 100) and not devmode.is_stub(item.dest)
    assert is_done(item)


# ---------------------------------------------------------------------------------------------- s01
def test_s01_dev_needs_10_gb_and_ignores_the_tier_and_the_custom_model(tmp_path: Path):
    ctx = make_test_context(
        tmp_path, raw_answers=example_answers(llm_tier="best", install_code=True), dev_mode=True)
    ctx.free_gb_fn = lambda _p: 12.0                                  # 12 GB: enough for a dev install only
    step = S01Questions()
    step.run(ctx)
    ans = ctx.ensure_answers()
    assert (ans.llm_tier, ans.install_code) == ("small", False)
    saved = json.loads((ctx.paths.config / "answers.json").read_text(encoding="utf-8"))
    assert saved["llm_tier"] == "small" and saved["install_code"] is False
    low = make_test_context(tmp_path / "low", raw_answers=example_answers(), dev_mode=True)
    low.free_gb_fn = lambda _p: 9.0
    with pytest.raises(Exception) as ei:
        S01Questions().run(low)
    assert "NS-E104" in str(ei.value) or getattr(ei.value, "code", "") == "NS-E104"


def test_s01_real_mode_still_refuses_a_small_drive(tmp_path: Path):
    ctx = make_test_context(tmp_path, raw_answers=example_answers())
    ctx.free_gb_fn = lambda _p: 12.0
    with pytest.raises(Exception) as ei:
        S01Questions().run(ctx)
    assert getattr(ei.value, "code", "") == "NS-E104"


# ---------------------------------------------------------------------------------------------- s02
def test_s02_dev_downloads_the_small_programs_and_skips_the_rest(tmp_path: Path):
    ctx = dev_ctx(tmp_path)
    step = S02DownloadBins()
    assert step.check(ctx) is False
    step.run(ctx)
    step.verify(ctx)
    bins = ctx.paths.home / "bin"
    assert (bins / "llama" / "llama-server.exe").is_file()                     # the CPU build, for real
    assert (bins / "yt-dlp" / "yt-dlp.exe").is_file() and (bins / "webview2").is_dir()
    assert not (bins / "opencode" / "opencode.exe").exists() and not (bins / "qwentts" / ".ns-qwentts.done").exists()
    assert not any(p.is_file() for folder in ("opencode", "qwentts") for p in (bins / folder).rglob("*"))
    assert not list((bins / "llama").glob("cudart*")) and not list((bins / "_downloads").glob("cudart*"))
    assert (ctx.flag("qwentts"), ctx.flag("tts_fallback"), ctx.flag("llama_pair")) == ("none", False, None)
    assert ctx.warnings == [] and "Dev mode: not downloaded" in ctx.ui.text
    assert step.check(ctx) is True


def test_s02_real_mode_downloads_cuda_and_the_voice_engine(tmp_path: Path):
    ctx = dev_ctx(tmp_path / "r", dev=False)
    S02DownloadBins().run(ctx)
    assert (ctx.paths.home / "bin" / "qwentts" / ".ns-qwentts.done").exists()
    assert "Dev mode" not in ctx.ui.text and ctx.flag("qwentts") == "binary"


# ---------------------------------------------------------------------------------------------- s03
def test_s03_dev_writes_stubs_with_the_manifest_names_and_no_download(tmp_path: Path):
    ctx = dev_ctx(tmp_path)
    step = S03DownloadModels()
    assert step.check(ctx) is False
    step.run(ctx)
    step.verify(ctx)
    p = ctx.paths
    for rel in (p.models_llm / "Qwen3-1.7B-Q8_0.gguf", p.models_embed / "nomic-embed-text-v1.5.Q8_0.gguf",
                p.models_tts / "qwen-talker-0.6b-customvoice-Q8_0.gguf", p.models_tts / "qwen-tokenizer-12hz-Q8_0.gguf",
                p.models_whisper / "tiny" / "model.bin", p.models_whisper / "tiny" / "config.json"):
        assert devmode.is_stub(rel), rel
    assert (p.home / "models" / "cv" / "yunet.onnx").is_file() and not devmode.is_stub(p.home / "models" / "cv" / "yunet.onnx")
    assert not (p.models_llm / "Qwen3-14B-Q4_K_M.gguf").exists()
    assert not list(p.models_whisper.glob("distil*")) and not (p.models_tts / "hf").exists()
    assert "Dev mode: no model is downloaded" in ctx.ui.text
    assert ctx.flag("missing_optional") == [] and step.check(ctx) is True
    assert sum(f.stat().st_size for f in stub_files(ctx)) < 100_000          # a few KB each, never a model


def test_s03_real_mode_replaces_a_stub_an_earlier_dev_run_left(tmp_path: Path):
    ctx = dev_ctx(tmp_path, dev=False)
    old = ctx.paths.models_llm / "Qwen3-14B-Q4_K_M.gguf"
    devmode.write_stub(old)
    S03DownloadModels().run(ctx)
    assert old.is_file() and not devmode.is_stub(old)


# ---------------------------------------------------------------------------------------------- s04
def uv_tags(runner: FakeRunner) -> list[str]:
    return [c.tag for c in runner.calls if c.tag.startswith("uv ")]


def test_s04_dev_installs_the_dev_extra_without_gpu_speech_or_tts_packages(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop")
    ctx = dev_ctx(tmp_path, runner)
    step = S04Venvs()
    step.run(ctx)
    step.verify(ctx)
    assert uv_tags(runner) == [
        "uv venv studio", "uv pip install studio app", "uv pip install studio spacy model",
        "uv venv supervisor", "uv pip install supervisor app",
    ]
    studio = next(c for c in runner.calls if c.tag == "uv pip install studio app")
    assert f"{ROOT}[dev-install,supervisor]" in studio.argv
    sup = next(c for c in runner.calls if c.tag == "uv pip install supervisor app")
    assert f"{ROOT}[supervisor,tts,dashboard]" in sup.argv
    everything = " ".join(a for c in runner.calls for a in c.argv).lower()
    assert "torch" not in everything and "cublas" not in everything and "whisper" not in everything
    assert not (ctx.paths.logs / "pins-whisper-cuda.txt").exists() and not ctx.venv_python("tts").exists()
    assert step.check(ctx) is True


def test_s04_real_mode_keeps_the_studio_extra(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop")
    ctx = dev_ctx(tmp_path, runner, dev=False)
    S04Venvs().run(ctx)
    assert f"{ROOT}[studio,supervisor]" in next(c for c in runner.calls if c.tag == "uv pip install studio app").argv


# ---------------------------------------------------------------------------------------------- s05, s06, s07
def test_s05_dev_says_why_it_is_skipped(tmp_path: Path):
    ctx = dev_ctx(tmp_path, install_code=True)
    assert "dev mode" in (S05OpencodePrewarm().skip_reason(ctx) or "")
    assert S05OpencodePrewarm().skip_reason(dev_ctx(tmp_path / "r", dev=False, install_code=True)) is None


def test_s06_dev_keeps_the_baseline_and_never_runs_tune(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop")
    ctx = dev_ctx(tmp_path, runner)
    step = S06Tune()
    step.run(ctx)
    step.verify(ctx)
    assert runner.called("ns tune") == [] and ctx.flag("tuned") == "defaults" and step.check(ctx) is True
    assert "nothing to tune" in ctx.ui.text


def test_runtime_json_of_a_dev_install_says_dev_mode_cpu_dev_and_fake_voice_in_the_supervisor_venv(tmp_path: Path):
    ctx = dev_ctx(tmp_path)
    rt = ensure_baseline(ctx)
    on_disk = json.loads(ctx.paths.runtime_file.read_text(encoding="utf-8"))
    assert on_disk["dev_mode"] is True and on_disk["profile"] == "cpu-dev"
    assert rt.tts.engine == "qwentts" and rt.video.encoder == "libx264"
    assert rt.llama.model_path.endswith("Qwen3-1.7B-Q8_0.gguf") and rt.whisper.device == "cpu"
    assert load_runtime(ctx.paths).dev_mode is True
    real = build_runtime(dev_ctx(tmp_path / "r", dev=False))
    assert real.dev_mode is False and real.profile == "gpu"


def test_s07_dev_writes_no_opencode_config(tmp_path: Path):
    ctx = dev_ctx(tmp_path, install_code=True)
    S07Configs().run(ctx)
    assert not ctx.paths.opencode_file.exists() and load_runtime(ctx.paths).dev_mode is True


# ---------------------------------------------------------------------------------------------- s08
def test_s08_dev_runs_verify_but_never_probes_whisper(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop")
    ctx = dev_ctx(tmp_path, runner)
    ctx.set_flag("qwentts", "none")
    step = S08Smoke()
    step.run(ctx)
    step.verify(ctx)
    assert runner.called("ns verify --quick") and not runner.called("whisper probe")
    assert "Dev mode: speech recognition is a fake" in ctx.ui.text and ctx.warnings == []


def test_s08_real_cpu_profile_still_probes_whisper_on_the_processor(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop", results={"whisper probe cpu": RunResult(0, "{}")})
    ctx = dev_ctx(tmp_path, runner, dev=False)
    ctx.preflight = {**(ctx.preflight or {}), "profile": "cpu-dev"}
    ctx.set_flag("qwentts", "none")
    S08Smoke().run(ctx)
    assert runner.called("whisper probe cpu")


# ---------------------------------------------------------------------------------------------- s09, s10
def launcher_app(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    (app / "launchers").mkdir(parents=True)
    for name in ("nightshift-dashboard.ps1", "nightshift-code.ps1"):
        (app / "launchers" / name).write_text("# x\n", encoding="utf-8")
    return app


def test_s09_dev_registers_the_task_disabled_and_remembers_the_time(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop")
    ctx = make_test_context(tmp_path, raw_answers=example_answers(run_time="04:30", install_code=True),
                            resolve=True, app_path=launcher_app(tmp_path), runner=runner, dev_mode=True)
    step = S09ShortcutsScheduler()
    step.run(ctx)
    step.verify(ctx)
    assert runner.called("ns schedule install")[0].argv[-5:] == ["schedule", "install", "--time", "04:30", "--disabled"]
    assert (ctx.flag("schedule"), ctx.flag("schedule_mode")) == ("installed", "disabled")
    assert json.loads((ctx.paths.config / "dashboard.json").read_text("utf-8"))["run_time"] == "04:30"
    assert "DISABLED" in ctx.ui.text and ctx.warnings == []
    names = sorted(p.name for p in (tmp_path / "desktop").glob("*.lnk"))
    assert names == ["Nightshift Dashboard.lnk"]                             # no Nightshift Code shortcut


def test_s09_real_mode_registers_an_enabled_task(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop")
    ctx = make_test_context(tmp_path, raw_answers=example_answers(run_time="04:30", nightly=True),
                            resolve=True, app_path=launcher_app(tmp_path), runner=runner)
    ctx.set_flag("first_install", True)      # a first install that said yes to the nightly question
    S09ShortcutsScheduler().run(ctx)
    argv = runner.called("ns schedule install")[0].argv
    assert "--disabled" not in argv and argv[-4:] == ["--setup", "first", "--nightly", "yes"]
    assert ctx.flag("schedule_mode") == "s4u"


def test_s10_dev_names_the_mode_and_says_nothing_is_scheduled(tmp_path: Path):
    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, dev_mode=True, ui=FakeUi())
    for step in default_steps():
        if step.id != "s10_summary":
            ctx.state.mark(step.id, "done")
    ctx.set_flag("schedule", "installed")
    ctx.set_flag("schedule_mode", "disabled")
    S10Summary().run(ctx)
    assert devmode.BANNER in ctx.ui.text and "DISABLED (dev mode)" in ctx.ui.text
    assert "Create it now" in ctx.ui.text and "the PC wakes itself" not in ctx.ui.text


# ---------------------------------------------------------------------------------------------- state and verify --downloads
def test_install_state_records_dev_mode(tmp_path: Path):
    ctx = dev_ctx(tmp_path)
    ctx.state.begin(ctx.profile, ctx.dev_mode)
    st = load_install_state(ctx.paths)
    assert st.dev_mode is True and st.profile == "cpu-dev"
    ctx.state.begin("gpu")
    assert load_install_state(ctx.paths).dev_mode is False


def test_reset_for_mode_switch_redoes_the_later_steps_only(tmp_path: Path):
    home = tmp_path / "home"
    ctx = dev_ctx(tmp_path)
    for sid in ("b01_tools", "s01_questions", "s02_download_bins", "s04_venvs"):
        ctx.state.mark(sid, "done")
    ctx.state.begin("cpu-dev", True)
    ctx.set_flag("tuned", "defaults")
    ensure_baseline(ctx)
    assert devmode.reset_for_mode_switch(home, True) is False              # same mode: nothing to do
    assert devmode.reset_for_mode_switch(home, False) is True
    st = load_install_state(Paths(home))
    assert sorted(st.steps) == ["b01_tools", "s01_questions"] and st.dev_mode is False
    assert not (home / "config" / "wizard_flags.json").exists() and not (home / "config" / "runtime.json").exists()
    assert devmode.reset_for_mode_switch(home, True) is False             # nothing past the questions is done
    assert devmode.reset_for_mode_switch(tmp_path / "nowhere", True) is False


def test_verify_downloads_accepts_the_stubs_of_a_dev_install(tmp_path: Path):
    ctx = dev_ctx(tmp_path)
    write_answers_file(ctx.paths.config / "answers.json", ctx.ensure_answers())
    ctx.state.begin("cpu-dev", True)
    S03DownloadModels().run(ctx)
    items = verify_downloads.plan_for_install(ctx.paths, ROOT)
    pin = load_manifest().bins["yunet"].sha256 or ""
    rows = {r.name: r for r in verify_downloads.verify_items(items, hasher=lambda p: pin, say=None)}
    assert rows["Qwen3-1.7B-Q8_0.gguf"].status == verify_downloads.STUB
    assert rows["whisper/model.bin"].status == verify_downloads.STUB
    assert rows["yunet"].status == verify_downloads.OK
    assert not any(r.failing for n, r in rows.items() if n not in ("llama_cpu", "yt_dlp", "webview2"))


def test_verify_downloads_rejects_a_stub_in_a_real_install(tmp_path: Path):
    ctx = dev_ctx(tmp_path, dev=False)
    write_answers_file(ctx.paths.config / "answers.json", ctx.ensure_answers())
    ctx.state.begin("gpu", False)
    devmode.write_stub(ctx.paths.models_llm / "Qwen3-14B-Q4_K_M.gguf")
    rows = {r.name: r for r in verify_downloads.verify_items(
        verify_downloads.plan_for_install(ctx.paths, ROOT), hasher=lambda p: "00", say=None)}
    assert rows["Qwen3-14B-Q4_K_M.gguf"].status == verify_downloads.BAD


# ---------------------------------------------------------------------------------------------- the whole wizard
SECRET = "dev-e2e-key"
STEP_IDS = [
    "s00_welcome", "s01_questions", "s02_download_bins", "s03_download_models", "s04_venvs",
    "s05_opencode_prewarm", "s06_tune", "s07_configs", "s08_smoke", "s09_shortcuts_scheduler", "s10_summary",
]


def write_answers(tmp_path: Path, **over) -> Path:
    data = json.loads((ROOT / "installer" / "answers.example.json").read_text(encoding="utf-8"))
    data.update(home_path=str(tmp_path / "home"), llm_tier="small", pexels_api_key=SECRET, **over)
    f = tmp_path / "answers.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    return f


def wizard_args(tmp_path: Path, answers: Path, *extra: str) -> list[str]:
    return ["--simulate-downloads", "--non-interactive", "--no-head-checks", "--home", str(tmp_path / "home"),
            "--answers", str(answers), "--app-path", str(ROOT), "--preflight", str(tmp_path / "none.json"), *extra]


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("NIGHTSHIFT_HOME", raising=False)
    monkeypatch.delenv("NIGHTSHIFT_DEV_MODE", raising=False)


def test_the_whole_wizard_in_dev_mode_then_switching_to_a_real_install(tmp_path: Path):
    answers = write_answers(tmp_path)
    assert main(wizard_args(tmp_path, answers, "--dev")) == 0
    home = tmp_path / "home"
    st = load_install_state(Paths(home))
    assert [s for s in STEP_IDS if st.steps[s].status in ("done", "skipped")] == STEP_IDS
    assert st.steps["s05_opencode_prewarm"].status == "skipped"
    assert st.dev_mode is True and st.profile == "cpu-dev" and st.completed_at
    rt = json.loads((home / "config" / "runtime.json").read_text(encoding="utf-8"))
    assert rt["dev_mode"] is True and rt["profile"] == "cpu-dev" and rt["tts"]["engine"] == "qwentts"
    assert devmode.is_stub(home / "models" / "llm" / "Qwen3-1.7B-Q8_0.gguf")
    assert not (home / "models" / "llm" / "Qwen3-14B-Q4_K_M.gguf").exists()
    assert not any(p.is_file() for folder in ("opencode", "qwentts") for p in (home / "bin" / folder).rglob("*"))
    assert not (home / "venvs" / "tts").exists() and not (home / "config" / "opencode.json").exists()
    flags = json.loads((home / "config" / "wizard_flags.json").read_text(encoding="utf-8"))
    assert flags["schedule_mode"] == "disabled" and flags["tuned"] == "defaults" and flags["qwentts"] == "none"
    saved = json.loads((home / "config" / "answers.json").read_text(encoding="utf-8"))
    assert saved["install_code"] is False and SECRET not in json.dumps(saved)

    # the same folder, installed for real: the stubs go, the model comes, every later step runs again
    assert main(wizard_args(tmp_path, answers)) == 0
    st = load_install_state(Paths(home))
    assert st.dev_mode is False and st.profile == "gpu"
    assert not devmode.is_stub(home / "models" / "llm" / "Qwen3-14B-Q4_K_M.gguf")
    assert (home / "models" / "llm" / "Qwen3-14B-Q4_K_M.gguf").is_file()
    assert not (home / "models" / "llm" / "Qwen3-1.7B-Q8_0.gguf").exists()
    assert json.loads((home / "config" / "runtime.json").read_text(encoding="utf-8"))["dev_mode"] is False
    assert (home / "bin" / "qwentts" / ".ns-qwentts.done").exists()


def test_the_environment_variable_turns_dev_mode_on_like_the_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NIGHTSHIFT_DEV_MODE", "1")
    assert main(wizard_args(tmp_path, write_answers(tmp_path))) == 0
    assert load_install_state(Paths(tmp_path / "home")).dev_mode is True


def test_the_hand_over_json_from_stage_1_turns_dev_mode_on(tmp_path: Path):
    pf = tmp_path / "pf.json"
    pf.write_text(json.dumps({"schema": 1, "dev_mode": True, "profile": "cpu-dev", "ram_gb": 8,
                              "drives": [{"letter": "C", "free_gb": 30, "type": "SSD"}], "checks": []}), encoding="utf-8")
    args = wizard_args(tmp_path, write_answers(tmp_path))
    args[args.index("--preflight") + 1] = str(pf)
    assert main(args) == 0
    assert load_install_state(Paths(tmp_path / "home")).dev_mode is True


# ------------------------------------------------------------------------------- real install without a card
def test_a_real_cpu_install_with_the_voice_binary_runs_qwentts_not_the_reference_venv(tmp_path: Path):
    """Runtime.default("cpu-dev") says "reference", whose PyTorch venv s04 makes only on the fallback: a real
    install on a PC without a graphics card pointed its voice service at a venv that did not exist."""
    ctx = make_test_context(
        tmp_path / "r", raw_answers=example_answers(allow_cpu=True), resolve=True, dev_mode=False,
        preflight={"profile": "cpu-dev"},
    )
    assert ctx.profile == "cpu-dev" and not ctx.dev_mode
    S02DownloadBins().run(ctx)
    assert ctx.flag("qwentts") == "binary" and ctx.flag("tts_fallback") is False
    rt = build_runtime(ctx)
    assert rt.tts.engine == "qwentts" and rt.tts.device == "cpu" and rt.dev_mode is False
    ctx.set_flag("tts_fallback", True)                      # the PyTorch fallback still wins
    assert build_runtime(ctx).tts.engine == "reference"


# ------------------------------------------------------------------------------- engine test: fake_voice
def test_fake_voice_installs_the_real_models_with_the_fake_voice_engine(tmp_path: Path):
    """docs/DEV_MODE.md "Real engines, fake voice": no qwentts.cpp, no voice model, `tts.engine: fake`."""
    ctx = make_test_context(
        tmp_path / "r", raw_answers=example_answers(allow_cpu=True, fake_voice=True), resolve=True,
        dev_mode=False, preflight={"profile": "cpu-dev"},
    )
    assert ctx.fake_voice is True
    S02DownloadBins().run(ctx)
    assert ctx.flag("qwentts") == "none" and ctx.flag("tts_fallback") is False
    assert "Engine test" in ctx.ui.text
    assert not (ctx.paths.home / "bin" / "qwentts" / ".ns-qwentts.done").exists()
    names = [i.name for i in S03DownloadModels()._items(ctx)]
    assert not any(n.startswith("qwen-") for n in names), names        # talker and tokenizer GGUFs
    assert any(n.endswith(".gguf") for n in names) and "whisper/model.bin" in names     # the real models stay
    assert all(i.sha256 for i in S03DownloadModels()._items(ctx) if i.name.endswith(".gguf"))
    rt = build_runtime(ctx)
    assert rt.tts.engine == "fake" and rt.dev_mode is False and rt.profile == "cpu-dev"


def test_fake_voice_is_ignored_by_a_dev_install_which_fakes_the_voice_anyway(tmp_path: Path):
    ctx = dev_ctx(tmp_path, fake_voice=True)
    assert ctx.fake_voice is False
    S02DownloadBins().run(ctx)
    assert "Dev mode" in ctx.ui.text and "Engine test" not in ctx.ui.text
    rt = build_runtime(ctx)
    assert rt.tts.engine == "qwentts" and rt.dev_mode is True


# ----------------------------------------------------------------------------- engine test: fake_footage
def test_fake_footage_is_written_into_runtime_json_of_a_real_install(tmp_path: Path):
    """The twin of fake_voice: the answer reaches runtime.json, and a plain real install does not carry it."""
    ctx = make_test_context(
        tmp_path / "r", raw_answers=example_answers(allow_cpu=True, fake_footage=True), resolve=True,
        dev_mode=False, preflight={"profile": "cpu-dev"},
    )
    assert ctx.fake_footage is True and ctx.fake_voice is False
    rt = ensure_baseline(ctx)
    on_disk = json.loads(ctx.paths.runtime_file.read_text(encoding="utf-8"))
    assert on_disk["fake_footage"] is True and on_disk["dev_mode"] is False
    assert rt.tts.engine == "qwentts" and load_runtime(ctx.paths).fake_footage is True
    plain = make_test_context(
        tmp_path / "p", raw_answers=example_answers(allow_cpu=True), resolve=True, dev_mode=False,
        preflight={"profile": "cpu-dev"},
    )
    assert plain.fake_footage is False and build_runtime(plain).fake_footage is False


def test_fake_footage_is_ignored_by_a_dev_install_which_fakes_its_footage_anyway(tmp_path: Path):
    """A dev install already has fake footage through dev_mode; the answer must not double up on it."""
    ctx = dev_ctx(tmp_path, fake_footage=True)
    assert ctx.fake_footage is False
    rt = build_runtime(ctx)
    assert rt.fake_footage is False and rt.dev_mode is True


def test_s07_applies_the_engine_test_answers_to_an_existing_runtime_json(tmp_path: Path):
    """ensure_baseline writes runtime.json only once, so a re-run must still take the answers from s07."""
    ctx = make_test_context(
        tmp_path, raw_answers=example_answers(allow_cpu=True, fake_footage=True, fake_voice=True),
        resolve=True, dev_mode=False, preflight={"profile": "cpu-dev"},
    )
    older = Runtime.default("cpu-dev")                   # an earlier install: no flag, a real voice engine
    older.tts.engine = "qwentts"
    save_runtime(ctx.paths, older)
    assert load_runtime(ctx.paths).fake_footage is False
    S07Configs().run(ctx)
    rt = load_runtime(ctx.paths)
    assert rt.fake_footage is True and rt.tts.engine == "fake" and rt.dev_mode is False
    # the answer removed again: the footage switch follows it, the voice engine is left as it is
    ctx2 = make_test_context(
        tmp_path / "again", raw_answers=example_answers(allow_cpu=True), resolve=True, dev_mode=False,
        preflight={"profile": "cpu-dev"},
    )
    save_runtime(ctx2.paths, rt)
    S07Configs().run(ctx2)
    rt2 = load_runtime(ctx2.paths)
    assert rt2.fake_footage is False and rt2.tts.engine == "fake"
