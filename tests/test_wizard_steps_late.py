"""C1: wizard steps s06 to s10 and runtime.json construction."""

import json
from pathlib import Path

import pytest

from installer.stage2.runner import RunResult
from installer.stage2.runtime_cfg import build_runtime, ensure_baseline
from installer.stage2.simulate import FakeRunner
from installer.stage2.steps import default_steps
from installer.stage2.steps.s06_tune import S06Tune
from installer.stage2.steps.s07_configs import OPENCODE_CONFIG, S07Configs
from installer.stage2.steps.s08_smoke import S08Smoke, parse_report
from installer.stage2.steps.s09_shortcuts_scheduler import S09ShortcutsScheduler
from installer.stage2.steps.s10_summary import S10Summary
from installer.stage2.sysinfo import sample_preflight
from installer.stage2.testing import example_answers, make_test_context
from ns_common.config import load_channel, load_runtime, save_runtime
from ns_common.errors import NsError

ROOT = Path(__file__).resolve().parents[1]


def resolved(tmp_path: Path, runner=None, **answers):
    return make_test_context(tmp_path, raw_answers=example_answers(**answers), resolve=True, runner=runner)


# ---------------------------------------------------------------------------------------------- runtime.json
def test_baseline_runtime_small_tier(tmp_path: Path):
    ctx = resolved(tmp_path)
    rt = build_runtime(ctx)
    p = ctx.paths
    assert rt.profile == "gpu"
    assert rt.llama.model_path == str(p.models_llm / "Qwen3-14B-Q4_K_M.gguf")
    assert rt.embed.model_path == str(p.models_embed / "nomic-embed-text-v1.5.Q8_0.gguf")
    assert rt.tts.model_dir == str(p.models_tts) and rt.tts.engine == "faster"
    assert (rt.whisper.model, rt.whisper.device, rt.whisper.compute_type) == (
        str(p.models_whisper / "distil-large-v3"), "cuda", "float16")
    assert rt.llama.flags.kv_type == "f16" and rt.llama.flags.spec_type is None


def test_baseline_runtime_bigger_tiers_start_from_sensible_flags(tmp_path: Path):
    bal = build_runtime(resolved(tmp_path / "a", llm_tier="balanced"))
    best = build_runtime(resolved(tmp_path / "b", llm_tier="best"))
    assert bal.llama.model_path.endswith("Qwen3.6-35B-A3B-APEX-MTP-I-Mini.gguf")
    assert (bal.llama.flags.kv_type, bal.llama.flags.n_cpu_moe, bal.llama.flags.spec_type) == (
        "q8_0", 8, "draft-mtp")
    assert best.llama.model_path.endswith("Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf")
    assert (best.llama.flags.ctx, best.llama.flags.n_cpu_moe) == (32768, 12)


def test_baseline_runtime_cpu_profile_and_tts_fallback(tmp_path: Path):
    pf = sample_preflight(tmp_path / "home", ROOT)
    pf["profile"] = "cpu-dev"
    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, preflight=pf)
    rt = build_runtime(ctx)
    assert rt.profile == "cpu-dev" and rt.video.encoder == "libx264" and rt.llama.model_path.endswith(
        "Qwen3-1.7B-Q8_0.gguf")
    assert (rt.whisper.device, rt.whisper.compute_type) == ("cpu", "int8")
    ctx.set_flag("tts_fallback", True)
    assert build_runtime(ctx).tts.engine == "reference"


def test_ensure_baseline_never_overwrites_tuned_values(tmp_path: Path):
    ctx = resolved(tmp_path)
    rt = ensure_baseline(ctx)
    rt.llama.flags.ctx = 8192
    save_runtime(ctx.paths, rt)
    assert ensure_baseline(ctx).llama.flags.ctx == 8192


# ---------------------------------------------------------------------------------------------- s06
def test_s06_writes_baseline_then_tunes(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop")
    ctx = resolved(tmp_path, runner=runner)
    step = S06Tune()
    assert step.check(ctx) is False
    step.run(ctx)
    step.verify(ctx)
    assert load_runtime(ctx.paths).llama.model_path.endswith("Qwen3-14B-Q4_K_M.gguf")
    call = runner.called("ns tune")[0]
    assert call.argv[-1] == "tune" and call.env["NIGHTSHIFT_HOME"] == str(ctx.paths.home)
    assert call.cwd == ctx.app_path
    assert "51.2 tokens/s" in ctx.ui.text and ctx.flag("tuned") == "tuned"
    assert step.check(ctx) is True


def test_s06_failed_tune_keeps_defaults_and_continues(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop", results={"ns tune": RunResult(1, "", "no model")})
    ctx = resolved(tmp_path, runner=runner)
    step = S06Tune()
    step.run(ctx)
    step.verify(ctx)
    assert ctx.flag("tuned") == "defaults" and "safe default" in ctx.ui.text


# ---------------------------------------------------------------------------------------------- s07
def keyed_context(tmp_path: Path, **answers):
    app = tmp_path / "app"
    app.mkdir(parents=True)
    raw = example_answers(pexels_api_key="PEX-SECRET", pixabay_api_key="PIX-SECRET", **answers)
    ctx = make_test_context(tmp_path, raw_answers=raw, resolve=True, app_path=app, simulate=False)
    secrets = {"pexels_api_key": "PEX-SECRET", "pixabay_api_key": "PIX-SECRET", "run_time": "03:00"}
    leaked = json.dumps(secrets)
    (app / "answers.json").write_text(leaked, encoding="utf-8")
    (ctx.paths.config / "answers.json").write_text(leaked, encoding="utf-8")
    return ctx, app


def test_s07_writes_configs_moves_secrets_and_blanks_files(tmp_path: Path):
    ctx, app = keyed_context(tmp_path)
    step = S07Configs()
    assert step.check(ctx) is False
    step.run(ctx)
    step.verify(ctx)
    assert ctx.store.get("pexels_api_key") == "PEX-SECRET"
    assert ctx.store.get("pixabay_api_key") == "PIX-SECRET"
    for f in (app / "answers.json", ctx.paths.config / "answers.json"):
        text = f.read_text(encoding="utf-8")
        assert "SECRET" not in text and '"pexels_api_key": ""' in text
    ch = load_channel(ctx.paths, "after-dark-tales")
    assert (ch.display_name, ch.niche, ch.masters_per_week, ch.voice.speaker) == (
        "After Dark Tales", "horror fiction", 1, "Ryan")
    assert json.loads(ctx.paths.opencode_file.read_text(encoding="utf-8")) == OPENCODE_CONFIG
    assert OPENCODE_CONFIG["enabled_providers"] == ["local"] and OPENCODE_CONFIG["model"] == "local/code"
    assert load_runtime(ctx.paths).llama.model_path
    assert step.check(ctx) is True


def test_s07_is_idempotent_and_keeps_an_edited_channel(tmp_path: Path):
    ctx, _ = keyed_context(tmp_path)
    step = S07Configs()
    step.run(ctx)
    chan = ctx.paths.channel_file("after-dark-tales")
    chan.write_text(chan.read_text(encoding="utf-8").replace("critic_bar: 8.0", "critic_bar: 9.0"),
                    encoding="utf-8")
    runtime_before = ctx.paths.runtime_file.read_text(encoding="utf-8")
    step.run(ctx)
    assert "critic_bar: 9.0" in chan.read_text(encoding="utf-8")
    assert ctx.paths.runtime_file.read_text(encoding="utf-8") == runtime_before


def test_s07_no_opencode_json_without_install_code(tmp_path: Path):
    ctx, _ = keyed_context(tmp_path, install_code=False)
    S07Configs().run(ctx)
    assert not ctx.paths.opencode_file.exists()


def test_s07_design_voice_gets_a_library_name(tmp_path: Path):
    ctx, _ = keyed_context(tmp_path, voice={"mode": "design", "description": "warm, slow, older man"})
    S07Configs().run(ctx)
    ch = load_channel(ctx.paths, "after-dark-tales")
    assert ch.voice.mode == "design" and ch.voice.name == "after-dark-tales-voice"
    assert "studio voice" in ctx.ui.text


def test_s07_restore_from_calls_ns_restore(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop")
    ctx, _ = keyed_context(tmp_path, restore_from="D:\\backup.zip")
    ctx.runner = runner
    S07Configs().run(ctx)
    assert runner.called("ns restore")[0].argv[-2:] == ["restore", "D:\\backup.zip"]
    ctx2, _ = keyed_context(tmp_path / "bad", restore_from="D:\\backup.zip")
    ctx2.runner = FakeRunner(tmp_path / "d2", results={"ns restore D:\\backup.zip": RunResult(2)})
    S07Configs().run(ctx2)
    assert "Could not restore" in ctx2.ui.text


def test_s07_verify_fails_when_a_key_is_still_in_a_file(tmp_path: Path):
    ctx, app = keyed_context(tmp_path)
    step = S07Configs()
    step.run(ctx)
    (app / "answers.json").write_text(json.dumps({"pexels_api_key": "again"}), encoding="utf-8")
    with pytest.raises(NsError) as ei:
        step.verify(ctx)
    assert "answers.json" in ei.value.detail


# ---------------------------------------------------------------------------------------------- s08
def smoke_ctx(tmp_path: Path, failures: set[str] | None = None, **answers):
    runner = FakeRunner(tmp_path / "desktop", verify_failures=failures)
    return resolved(tmp_path, runner=runner, **answers), runner


def test_s08_all_green(tmp_path: Path):
    ctx, runner = smoke_ctx(tmp_path)
    step = S08Smoke()
    assert step.check(ctx) is False
    step.run(ctx)
    step.verify(ctx)
    assert ctx.warnings == [] and ctx.flag("smoke") == "passed" and step.check(ctx) is True
    assert runner.called("ns verify --quick")[0].argv[-2:] == ["verify", "--quick"]
    assert ctx.ui.tables[0][0] == "Smoke test"


def test_s08_nvenc_failure_switches_to_libx264(tmp_path: Path):
    ctx, _ = smoke_ctx(tmp_path, {"nvenc"})
    S08Smoke().run(ctx)
    rt = load_runtime(ctx.paths)
    assert (rt.video.encoder, rt.video.preset) == ("libx264", "veryfast")
    assert [c for c, _ in ctx.warnings] == ["NS-W401"]


def test_s08_mtp_that_is_not_faster_is_turned_off(tmp_path: Path):
    ctx, _ = smoke_ctx(tmp_path, {"mtp"}, llm_tier="balanced")
    S08Smoke().run(ctx)
    flags = load_runtime(ctx.paths).llama.flags
    assert flags.spec_type is None and flags.spec_draft_n_max is None
    assert [c for c, _ in ctx.warnings] == ["NS-W302"]


def test_s08_mtp_failure_on_a_plain_model_changes_nothing(tmp_path: Path):
    ctx, _ = smoke_ctx(tmp_path, {"mtp"})
    S08Smoke().run(ctx)
    assert ctx.warnings == []


def test_s08_tts_failure_switches_to_the_pytorch_engine(tmp_path: Path):
    ctx, runner = smoke_ctx(tmp_path, {"tts"})
    S08Smoke().run(ctx)
    assert load_runtime(ctx.paths).tts.engine == "reference"
    assert ctx.flag("tts_fallback") is True and ctx.flag("tts_venv_installed") is True
    assert [c for c, _ in ctx.warnings] == ["NS-W301"]
    assert runner.called("uv venv tts") and len(runner.called("ns verify --quick")) == 2
    assert (ctx.paths.models_tts / "hf" / "Qwen3-TTS-12Hz-1.7B-Base" / "model.safetensors").exists()
    assert ctx.flag("smoke") == "passed"


def test_s08_tts_failing_even_in_the_fallback_is_e108(tmp_path: Path):
    ctx, _ = smoke_ctx(tmp_path, {"tts"})
    ctx.set_flag("tts_fallback", True)
    with pytest.raises(NsError) as ei:
        S08Smoke().run(ctx)
    assert ei.value.code == "NS-E108"


def test_s08_whisper_and_service_failures_stop_the_install(tmp_path: Path):
    ctx, _ = smoke_ctx(tmp_path, {"whisper"})
    with pytest.raises(NsError) as ei:
        S08Smoke().run(ctx)
    assert ei.value.code == "NS-E112" and "cuBLAS" in ei.value.definition.message + ei.value.definition.hint
    ctx2, _ = smoke_ctx(tmp_path / "b", {"llm"})
    with pytest.raises(NsError) as e2:
        S08Smoke().run(ctx2)
    assert e2.value.code == "NS-E203"


def test_s08_no_report_is_e202_and_report_file_is_a_fallback(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop", results={"ns verify --quick": RunResult(1, "", "boom")})
    ctx = resolved(tmp_path, runner=runner)
    with pytest.raises(NsError) as ei:
        S08Smoke().run(ctx)
    assert ei.value.code == "NS-E202" and "boom" in ei.value.detail

    ctx2 = resolved(tmp_path / "b", runner=FakeRunner(tmp_path / "d2", results={
        "ns verify --quick": RunResult(0, "")}))
    report = {"checks": [{"name": "nvenc_encode", "passed": True}, {"name": "whisper_cuda", "passed": True}]}
    (ctx2.paths.logs / "verify-report.json").write_text(json.dumps(report), encoding="utf-8")
    assert parse_report(ctx2, RunResult(0, "")) == {"nvenc_encode": True, "whisper_cuda": True}
    S08Smoke().run(ctx2)


def test_parse_report_ignores_noise_around_the_json(tmp_path: Path):
    ctx = resolved(tmp_path)
    text = 'starting...\n{"checks": [{"name": "TTS", "passed": false}]}\ndone'
    assert parse_report(ctx, RunResult(0, text)) == {"tts": False}
    assert parse_report(ctx, RunResult(0, "no json")) is None


# ---------------------------------------------------------------------------------------------- s09
def launcher_app(tmp_path: Path, names: list[str]) -> Path:
    app = tmp_path / "app"
    (app / "launchers").mkdir(parents=True)
    for n in names:
        (app / "launchers" / n).write_text("# launcher", encoding="utf-8")
    return app


def test_s09_creates_shortcuts_for_existing_launchers_and_registers_the_task(tmp_path: Path):
    app = launcher_app(tmp_path, ["nightshift-dashboard.ps1", "open-outbox.ps1", "nightshift-code.ps1"])
    runner = FakeRunner(tmp_path / "desktop")
    ctx = make_test_context(tmp_path, raw_answers=example_answers(run_time="04:30"), resolve=True,
                            app_path=app, runner=runner)
    step = S09ShortcutsScheduler()
    assert step.check(ctx) is False
    step.run(ctx)
    step.verify(ctx)
    desk = tmp_path / "desktop"
    assert sorted(p.name for p in desk.glob("*.lnk")) == [
        "Nightshift Code.lnk", "Nightshift Dashboard.lnk", "Nightshift Outbox.lnk"]
    first = runner.called("ps:shortcut")[0]
    assert first.meta["target"] == "powershell.exe"
    assert "-ExecutionPolicy Bypass -File" in first.meta["arguments"]
    assert "launchers" in first.meta["arguments"]
    assert runner.called("ns schedule install")[0].argv[-4:] == ["schedule", "install", "--time", "04:30"]
    assert ctx.flag("schedule") == "installed"
    assert "not part of this version yet" in ctx.ui.text
    assert step.check(ctx) is True


def test_s09_nightshift_code_shortcut_only_with_install_code(tmp_path: Path):
    app = launcher_app(tmp_path, ["nightshift-code.ps1"])
    ctx = make_test_context(tmp_path, raw_answers=example_answers(install_code=False), resolve=True,
                            app_path=app)
    S09ShortcutsScheduler().run(ctx)
    assert list((tmp_path / "desktop").glob("*.lnk")) == []


def test_s09_missing_schedule_command_is_a_warning_not_a_failure(tmp_path: Path):
    app = launcher_app(tmp_path, [])
    runner = FakeRunner(tmp_path / "desktop", results={
        "ns schedule install --time 03:00": RunResult(2, "", "Error: No such command 'schedule'.")})
    ctx = make_test_context(
        tmp_path, raw_answers=example_answers(), resolve=True, app_path=app, runner=runner
    )
    step = S09ShortcutsScheduler()
    step.run(ctx)
    step.verify(ctx)
    assert ctx.flag("schedule") == "unavailable" and "not part of this version" in ctx.ui.text


def test_s09_real_scheduler_failure_is_e202(tmp_path: Path):
    app = launcher_app(tmp_path, [])
    runner = FakeRunner(tmp_path / "desktop", results={
        "ns schedule install --time 03:00": RunResult(1, "", "access denied")})
    ctx = make_test_context(
        tmp_path, raw_answers=example_answers(), resolve=True, app_path=app, runner=runner
    )
    with pytest.raises(NsError) as ei:
        S09ShortcutsScheduler().run(ctx)
    assert ei.value.code == "NS-E202" and "access denied" in ei.value.detail


def test_s09_shortcut_failure_and_desktop_override(tmp_path: Path):
    app = launcher_app(tmp_path, ["open-outbox.ps1"])
    runner = FakeRunner(tmp_path / "desktop", results={"ps:shortcut": RunResult(1)})
    ctx = make_test_context(
        tmp_path, raw_answers=example_answers(), resolve=True, app_path=app, runner=runner
    )
    with pytest.raises(NsError) as ei:
        S09ShortcutsScheduler().run(ctx)
    assert ei.value.code == "NS-E110"
    ctx2 = make_test_context(tmp_path / "o", raw_answers=example_answers(), resolve=True,
                             app_path=launcher_app(tmp_path / "o", ["open-outbox.ps1"]))
    ctx2.desktop_override = tmp_path / "elsewhere"
    S09ShortcutsScheduler().run(ctx2)
    assert (tmp_path / "elsewhere" / "Nightshift Outbox.lnk").exists()


# ---------------------------------------------------------------------------------------------- s10
def test_s10_summary_names_the_file_to_upload(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop")
    ctx = resolved(tmp_path, runner=runner)
    for s in default_steps():
        if s.id != "s10_summary":
            ctx.state.mark(s.id, "done")
    ctx.set_flag("schedule", "installed")
    ctx.warnings.append(("NS-W401", "runtime.json now uses libx264"))
    step = S10Summary()
    assert step.check(ctx) is False
    step.run(ctx)
    step.verify(ctx)
    bundle = Path(ctx.flag("bundle"))
    assert bundle.parent == tmp_path / "desktop" and bundle.name.startswith("nightshift-diagnostics-")
    assert str(bundle) in ctx.ui.text and "upload" in ctx.ui.text.lower()
    assert "NS-W401" in ctx.ui.text and "Run now" in ctx.ui.text
    assert "every night at 03:00" in ctx.ui.text
    assert ctx.ui.tables[0][0] == "Setup result" and len(ctx.ui.tables[0][1]) == 11
    assert any(str(ctx.paths.outbox) in r[1] for r in ctx.ui.tables[1][1])


def test_s10_without_a_bundle_command_says_so(tmp_path: Path):
    results = {"ns doctor --bundle": RunResult(2, "", "No such command")}
    runner = FakeRunner(tmp_path / "desktop", results=results)
    ctx = resolved(tmp_path, runner=runner)
    for s in default_steps():
        if s.id != "s10_summary":
            ctx.state.mark(s.id, "skipped")
    S10Summary().run(ctx)
    S10Summary().verify(ctx)
    assert "not available in this version" in ctx.ui.text and ctx.flag("bundle") is None


def test_s10_verify_names_unfinished_steps(tmp_path: Path):
    ctx = resolved(tmp_path)
    for s in default_steps():
        ctx.state.mark(s.id, "done")
    ctx.state.mark("s05_opencode_prewarm", "failed")
    with pytest.raises(NsError) as ei:
        S10Summary().verify(ctx)
    assert "s05_opencode_prewarm" in ei.value.detail


# ---------------------------------------------------------------------------------------------- updates
def test_new_app_version_resyncs_venvs_and_retests(tmp_path: Path):
    from installer.stage2.steps.s04_venvs import S04Venvs

    ctx = resolved(tmp_path)
    S04Venvs().run(ctx)
    S08Smoke().run(ctx)
    assert S04Venvs().check(ctx) is True and S08Smoke().check(ctx) is True
    ctx.manifest = ctx.manifest.model_copy(update={"version": "0.2.0"})
    assert S04Venvs().check(ctx) is False        # new zip + INSTALL.cmd: environments are refreshed
    assert S08Smoke().check(ctx) is False        # and the quick test runs again
