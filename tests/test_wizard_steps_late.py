"""C1: wizard steps s06 to s10 and runtime.json construction."""

import json
import wave
from pathlib import Path

import pytest

from installer.stage2.runner import RunResult
from installer.stage2.runtime_cfg import build_runtime, ensure_baseline
from installer.stage2.simulate import FakeRunner
from installer.stage2.steps import default_steps
from installer.stage2.steps.s06_tune import S06Tune
from installer.stage2.steps.s07_configs import OPENCODE_CONFIG, S07Configs, opencode_config
from installer.stage2.steps.s08_smoke import S08Smoke, parse_report
from installer.stage2.steps.s09_shortcuts_scheduler import S09ShortcutsScheduler
from installer.stage2.steps.s10_summary import S10Summary
from installer.stage2.sysinfo import sample_preflight
from installer.stage2.testing import example_answers, make_test_context
from ns_common.config import load_channel, load_runtime, save_runtime
from ns_common.errors import NsError, describe
from ns_supervisor import check_names as names
from ns_supervisor.schedule_cfg import write_run_time

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
    assert rt.tts.model_dir == str(p.models_tts) and rt.tts.engine == "qwentts"
    assert (rt.whisper.model, rt.whisper.device, rt.whisper.compute_type) == (
        str(p.models_whisper / "distil-large-v3"), "cuda", "int8")   # spec 153
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
    llama = load_runtime(ctx.paths).llama   # the port, the context and the model name follow runtime.json
    assert json.loads(ctx.paths.opencode_file.read_text(encoding="utf-8")) == opencode_config(
        llama.host, llama.port, llama.flags.ctx, llama.model_path)
    assert OPENCODE_CONFIG["enabled_providers"] == ["local"] and OPENCODE_CONFIG["model"] == "local/code"
    assert load_runtime(ctx.paths).llama.model_path
    assert step.check(ctx) is True


def test_opencode_config_takes_the_llama_host_and_port_from_runtime():
    """The baked 8080 is only the default: a different llama port must reach OpenCode's baseURL."""
    assert opencode_config() == OPENCODE_CONFIG
    cfg = opencode_config(host="127.0.0.1", port=9191)
    assert cfg["provider"]["local"]["options"]["baseURL"] == "http://127.0.0.1:9191/v1"
    assert cfg["provider"]["local"]["options"]["apiKey"] == "local"
    assert cfg["model"] == "local/code"
    assert OPENCODE_CONFIG["provider"]["local"]["options"]["baseURL"] == "http://127.0.0.1:8080/v1"


def test_s07_writes_opencode_baseurl_from_the_existing_runtime_port(tmp_path: Path):
    ctx, _app = keyed_context(tmp_path)
    ensure_baseline(ctx)
    rt = load_runtime(ctx.paths)
    rt.llama.port = 9191
    save_runtime(ctx.paths, rt)
    S07Configs().run(ctx)
    written = json.loads(ctx.paths.opencode_file.read_text(encoding="utf-8"))
    assert written["provider"]["local"]["options"]["baseURL"] == "http://127.0.0.1:9191/v1"


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
    assert "NS-E703" in ctx2.ui.text


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
    ctx, _ = smoke_ctx(tmp_path, {names.NVENC_PROBE})
    S08Smoke().run(ctx)
    rt = load_runtime(ctx.paths)
    assert (rt.video.encoder, rt.video.preset) == ("libx264", "veryfast")
    assert [c for c, _ in ctx.warnings] == ["NS-W401"]


def test_s08_has_no_mtp_branch_because_verify_has_no_mtp_check(tmp_path: Path):
    # MTP is decided by `ns tune` (tests/test_ns_tune.py); `ns verify --quick` reports no such check.
    assert not any("mtp" in n for n in names.QUICK_CHECKS)
    ctx, _ = smoke_ctx(tmp_path, llm_tier="balanced")
    S08Smoke().run(ctx)
    assert load_runtime(ctx.paths).llama.flags.spec_type == "draft-mtp" and ctx.warnings == []


def test_s08_tts_failure_switches_to_the_pytorch_engine(tmp_path: Path):
    ctx, runner = smoke_ctx(tmp_path, {names.TTS_VOICES})
    S08Smoke().run(ctx)
    assert load_runtime(ctx.paths).tts.engine == "reference"
    assert ctx.flag("tts_fallback") is True and ctx.flag("tts_venv_installed") is True
    assert [c for c, _ in ctx.warnings] == ["NS-W301"]
    assert runner.called("uv venv tts") and len(runner.called("ns verify --quick")) == 2
    assert (ctx.paths.models_tts / "hf" / "Qwen3-TTS-12Hz-1.7B-Base" / "model.safetensors").exists()
    assert ctx.flag("smoke") == "passed"


def test_s08_tts_failing_even_in_the_fallback_is_e108(tmp_path: Path):
    ctx, _ = smoke_ctx(tmp_path, {names.TTS_VOICES})
    ctx.set_flag("tts_fallback", True)
    with pytest.raises(NsError) as ei:
        S08Smoke().run(ctx)
    assert ei.value.code == "NS-E108"


def whisper_cfg(ctx):
    w = load_runtime(ctx.paths).whisper
    return w.device, w.compute_type


def test_s08_whisper_transcribes_a_generated_wav_on_the_gpu(tmp_path: Path):
    ctx, runner = smoke_ctx(tmp_path)
    S08Smoke().run(ctx)
    probes = runner.called("whisper probe")
    assert [c.tag for c in probes] == ["whisper probe cuda"]
    assert probes[0].argv[-3:-1] == ["cuda", "int8"]
    wav = Path(probes[0].argv[-1])
    assert wav.name == "whisper-probe.wav" and wav.is_file()
    with wave.open(str(wav), "rb") as w:
        assert w.getnframes() / w.getframerate() == pytest.approx(3.0, abs=0.01)
    assert whisper_cfg(ctx) == ("cuda", "int8") and ctx.warnings == []


def test_s08_whisper_gpu_failure_falls_back_to_the_cpu_with_w404(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop", results={
        "whisper probe cuda": RunResult(1, "", "RuntimeError: Library cublas64_12.dll is not found")})
    ctx = resolved(tmp_path, runner=runner)
    S08Smoke().run(ctx)                                      # never a hard stop
    assert [c.tag for c in runner.called("whisper probe")] == ["whisper probe cuda", "whisper probe cpu"]
    assert whisper_cfg(ctx) == ("cpu", "int8")
    assert [c for c, _ in ctx.warnings] == ["NS-W404"] and "cublas64_12" in ctx.warnings[0][1]
    assert "captions will be slow" in describe("NS-W404").message.lower()
    assert ctx.flag("smoke") == "passed"


def force_cpu_whisper(ctx) -> None:
    rt = ensure_baseline(ctx)
    rt.whisper.device, rt.whisper.compute_type = "cpu", "int8"
    save_runtime(ctx.paths, rt)


def test_s08_a_working_graphics_card_brings_whisper_back_from_the_cpu(tmp_path: Path):
    # an earlier run (a missing DLL, an old driver) left runtime.json on the CPU; the card works now
    ctx, runner = smoke_ctx(tmp_path)
    force_cpu_whisper(ctx)
    assert whisper_cfg(ctx) == ("cpu", "int8")
    S08Smoke().run(ctx)
    assert [c.tag for c in runner.called("whisper probe")] == ["whisper probe cuda"]
    assert whisper_cfg(ctx) == ("cuda", "int8") and ctx.warnings == []
    assert ctx.flag("smoke") == "passed"


def test_s08_a_failing_graphics_card_keeps_whisper_on_the_cpu_and_warns_again(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop", results={
        "whisper probe cuda": RunResult(1, "", "RuntimeError: cudnn64_9.dll is not found")})
    ctx = resolved(tmp_path, runner=runner)
    force_cpu_whisper(ctx)
    S08Smoke().run(ctx)
    assert [c.tag for c in runner.called("whisper probe")] == ["whisper probe cuda", "whisper probe cpu"]
    assert whisper_cfg(ctx) == ("cpu", "int8")
    assert [c for c, _ in ctx.warnings] == ["NS-W404"] and "cudnn64_9" in ctx.warnings[0][1]


def test_s08_whisper_goes_cpu_then_cuda_then_cpu_across_runs(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop", results={"whisper probe cuda": RunResult(1, "", "no cublas")})
    ctx = resolved(tmp_path, runner=runner)
    S08Smoke().run(ctx)
    assert whisper_cfg(ctx) == ("cpu", "int8")
    runner.results.pop("whisper probe cuda")             # the DLL is installed / the driver was updated
    S08Smoke().run(ctx)
    assert whisper_cfg(ctx) == ("cuda", "int8")
    runner.results["whisper probe cuda"] = RunResult(1, "", "broken again")
    S08Smoke().run(ctx)
    assert whisper_cfg(ctx) == ("cpu", "int8")


def test_s08_whisper_that_did_not_load_on_the_gpu_skips_the_probe_and_uses_the_cpu(tmp_path: Path):
    ctx, runner = smoke_ctx(tmp_path, {names.WHISPER_LOAD})
    S08Smoke().run(ctx)
    assert [c.tag for c in runner.called("whisper probe")] == ["whisper probe cpu"]
    assert whisper_cfg(ctx) == ("cpu", "int8") and [c for c, _ in ctx.warnings] == ["NS-W404"]
    assert ctx.flag("smoke") == "passed"


def test_s08_whisper_failing_on_the_cpu_too_stops_the_install(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop", results={
        "whisper probe cuda": RunResult(1, "", "no cuda"), "whisper probe cpu": RunResult(1, "", "no model")})
    ctx = resolved(tmp_path, runner=runner)
    with pytest.raises(NsError) as ei:
        S08Smoke().run(ctx)
    assert ei.value.code == "NS-E411" and "no model" in ei.value.detail
    assert ctx.flag("smoke") != "passed"


def test_s08_cpu_profile_probes_the_cpu_only_and_a_load_failure_is_fatal(tmp_path: Path):
    pf = sample_preflight(tmp_path / "home", ROOT)
    pf["profile"] = "cpu-dev"
    runner = FakeRunner(tmp_path / "desktop")
    answers = example_answers()
    ctx = make_test_context(tmp_path, raw_answers=answers, resolve=True, preflight=pf, runner=runner)
    S08Smoke().run(ctx)
    assert [c.tag for c in runner.called("whisper probe")] == ["whisper probe cpu"] and ctx.warnings == []
    runner2 = FakeRunner(tmp_path / "d2", verify_failures={names.WHISPER_LOAD})
    ctx2 = make_test_context(tmp_path / "b", raw_answers=example_answers(), resolve=True, preflight=pf,
                             runner=runner2)
    with pytest.raises(NsError) as ei:
        S08Smoke().run(ctx2)
    assert ei.value.code == "NS-E411"


def test_s08_service_failures_stop_the_install(tmp_path: Path):
    for i, bad in enumerate((names.LLAMA_START, names.LLAMA_HEALTH, names.LLAMA_JSON, names.EMBED_VECTOR)):
        ctx2, _ = smoke_ctx(tmp_path / f"b{i}", {bad})
        with pytest.raises(NsError) as e2:
            S08Smoke().run(ctx2)
        assert e2.value.code == "NS-E203" and bad in e2.value.detail


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
    # the file is read as a fallback by parse_report, but s08 deletes a report from an earlier run before it
    # runs `ns verify`, so a verify that wrote nothing can no longer pass on it
    with pytest.raises(NsError) as ei2:
        S08Smoke().run(ctx2)
    assert ei2.value.code == "NS-E202" and "no report" in ei2.value.detail


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
    # nightly-optional b: the step asks `ns schedule install --setup` to decide; here no first install was
    # declared and no answer given, so nothing is registered and the decision is recorded as off
    argv = runner.called("ns schedule install")[0].argv
    assert argv[argv.index("schedule"):] == ["schedule", "install", "--time", "04:30", "--setup", "again"]
    assert (ctx.flag("schedule"), ctx.flag("schedule_mode")) == ("installed", "off")
    assert "Automatic runs: off" in ctx.ui.text
    assert "not part of this version yet" in ctx.ui.text
    assert step.check(ctx) is True


def test_s09_runs_again_when_the_program_folder_moved_to_home_app(tmp_path: Path):
    # INSTALL.cmd over an old install: the shortcuts and the task still point at the extracted folder.
    old = launcher_app(tmp_path / "extracted", ["nightshift-dashboard.ps1"])
    new = launcher_app(tmp_path / "home" / "app", ["nightshift-dashboard.ps1"])
    runner = FakeRunner(tmp_path / "desktop")
    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, app_path=old,
                            runner=runner)
    step = S09ShortcutsScheduler()
    step.run(ctx)
    assert ctx.flag("shortcuts_app") == str(old) and step.check(ctx) is True
    ctx.app_path = new                                   # b02 has copied the program; install_state follows
    assert step.check(ctx) is False                      # the .lnk files exist, but point at the old folder
    step.run(ctx)
    assert ctx.flag("shortcuts_app") == str(new) and step.check(ctx) is True
    last = runner.called("ps:shortcut")[-1]
    assert str(new / "launchers") in last.meta["arguments"]
    assert len(runner.called("ns schedule install")) == 2
    assert runner.called("ns schedule install")[-1].cwd == new


def test_s09_state_from_before_the_flag_existed_runs_again(tmp_path: Path):
    app = launcher_app(tmp_path, ["nightshift-dashboard.ps1"])
    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, app_path=app)
    step = S09ShortcutsScheduler()
    step.run(ctx)
    flags = json.loads((ctx.paths.config / "wizard_flags.json").read_text(encoding="utf-8"))
    del flags["shortcuts_app"]
    (ctx.paths.config / "wizard_flags.json").write_text(json.dumps(flags), encoding="utf-8")
    assert step.check(ctx) is False


def _script_of(call) -> str:
    import base64

    return base64.b64decode(call.argv[call.argv.index("-EncodedCommand") + 1]).decode("utf-16-le")


def test_s09_points_every_shortcut_at_the_brand_icon_when_the_build_ships_it(tmp_path: Path):
    app = launcher_app(tmp_path, ["nightshift-dashboard.ps1", "open-outbox.ps1"])
    icon = app / "ns_dashboard" / "static" / "brand" / "nightshift.ico"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"ico")
    runner = FakeRunner(tmp_path / "desktop")
    ctx = make_test_context(
        tmp_path, raw_answers=example_answers(), resolve=True, app_path=app, runner=runner
    )
    S09ShortcutsScheduler().run(ctx)
    calls = runner.called("ps:shortcut")
    assert len(calls) == 2
    for call in calls:
        assert call.meta["icon"] == str(icon)
        assert f"$s.IconLocation = '{icon},0'" in _script_of(call)


def test_s09_keeps_the_default_icon_when_the_ico_is_missing(tmp_path: Path):
    app = launcher_app(tmp_path, ["nightshift-dashboard.ps1"])
    runner = FakeRunner(tmp_path / "desktop")
    ctx = make_test_context(
        tmp_path, raw_answers=example_answers(), resolve=True, app_path=app, runner=runner
    )
    S09ShortcutsScheduler().run(ctx)
    call = runner.called("ps:shortcut")[0]
    script = _script_of(call)
    assert "icon" not in call.meta and "$s.IconLocation" not in script   # the fallback sets none
    create_line = script.split("[NsLink]::Create(")[1].splitlines()[0]
    assert ", '')" in create_line                                       # the helper gets an empty icon


def test_s09_nightshift_code_shortcut_only_with_install_code(tmp_path: Path):
    app = launcher_app(tmp_path, ["nightshift-code.ps1"])
    ctx = make_test_context(tmp_path, raw_answers=example_answers(install_code=False), resolve=True,
                            app_path=app)
    S09ShortcutsScheduler().run(ctx)
    assert list((tmp_path / "desktop").glob("*.lnk")) == []


def test_s09_missing_schedule_command_is_a_warning_not_a_failure(tmp_path: Path):
    app = launcher_app(tmp_path, [])
    missing = RunResult(2, "", "Error: No such command 'schedule'.")
    runner = FakeRunner(tmp_path / "desktop", results={
        "ns schedule install --time 03:00 --setup again": missing})
    ctx = make_test_context(
        tmp_path, raw_answers=example_answers(), resolve=True, app_path=app, runner=runner
    )
    step = S09ShortcutsScheduler()
    step.run(ctx)
    step.verify(ctx)
    assert ctx.flag("schedule") == "unavailable" and "not part of this version" in ctx.ui.text


def test_s09_a_refused_scheduler_never_blocks_the_install(tmp_path: Path):
    app = launcher_app(tmp_path, [])
    runner = FakeRunner(tmp_path / "desktop", results={
        "ns schedule install --time 03:00 --setup again": RunResult(1, "", "access denied")})
    ctx = make_test_context(
        tmp_path, raw_answers=example_answers(), resolve=True, app_path=app, runner=runner
    )
    step = S09ShortcutsScheduler()
    step.run(ctx)                                        # does not raise
    step.verify(ctx)
    assert ctx.flag("schedule") == "failed"
    assert ("NS-W206", "access denied") in ctx.warnings
    from ns_supervisor.schedule_cfg import read_run_time, schedule_file
    assert not schedule_file(ctx.paths).exists() and read_run_time(ctx.paths) is None   # no task, no file
    saved = json.loads((ctx.paths.config / "dashboard.json").read_text(encoding="utf-8"))
    assert saved["run_time"] == "03:00"                  # the chosen time stays a preference


def test_s09_an_interactive_only_task_is_registered_and_the_owner_is_told(tmp_path: Path):
    app = launcher_app(tmp_path, [])
    answer = {"case": "first", "on": True, "run_time": "03:00", "signed_in_only": True}
    runner = FakeRunner(tmp_path / "desktop", results={
        "ns schedule install --time 03:00 --setup first --nightly yes": RunResult(
            0, "setup: " + json.dumps(answer) + "\n")})
    ctx = make_test_context(
        tmp_path, raw_answers=example_answers(nightly=True), resolve=True, app_path=app, runner=runner
    )
    ctx.set_flag("first_install", True)                  # what s01 decides on a home with no install
    step = S09ShortcutsScheduler()
    step.run(ctx)
    assert ctx.flag("schedule") == "installed" and ctx.flag("schedule_mode") == "interactive"
    assert any(code == "NS-W205" for code, _ in ctx.warnings)
    assert "only while you are signed in" in ctx.ui.text and ctx.flag("first_install") is False


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
    write_run_time(ctx.paths, "03:00")                   # Automatic runs are on: the record of the task
    ctx.warnings.append(("NS-W401", "runtime.json now uses libx264"))
    step = S10Summary()
    assert step.check(ctx) is False
    step.run(ctx)
    step.verify(ctx)
    bundle = Path(ctx.flag("bundle"))
    assert bundle.parent == tmp_path / "desktop" and bundle.name.startswith("nightshift-diagnostics-")
    assert str(bundle) in ctx.ui.text and "upload" in ctx.ui.text.lower()
    assert "NS-W401" in ctx.ui.text and "Run now" in ctx.ui.text
    assert "Automatic runs: on at 03:00" in ctx.ui.text
    assert ctx.ui.tables[0][0] == "Setup result" and len(ctx.ui.tables[0][1]) == 11
    assert any(str(ctx.paths.outbox) in r[1] for r in ctx.ui.tables[1][1])
    stops = runner.called("ns stop")
    assert len(stops) == 1 and stops[0].argv[-1] == "stop"        # no headless verify engine is left behind
    assert runner.calls.index(stops[0]) > runner.calls.index(runner.called("ns doctor --bundle")[0])


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


def test_s07_queues_a_designed_voice_for_the_first_run(tmp_path: Path):
    """Gap 5: the voice chosen at setup is written down for `studio`, with a clear message."""
    ctx, _ = keyed_context(tmp_path, voice={"mode": "design", "description": "warm, slow, older man"})
    S07Configs().run(ctx)
    queued = json.loads((ctx.paths.config / "pending_voice.json").read_text(encoding="utf-8"))
    assert queued == {
        "channel": "after-dark-tales", "mode": "design", "name": "after-dark-tales-voice",
        "description": "warm, slow, older man",
    }
    assert "first time Nightshift runs" in ctx.ui.text and "studio voice apply-pending" in ctx.ui.text


def test_s07_queues_a_cloned_voice_with_its_recording_and_consent(tmp_path: Path):
    ctx, _ = keyed_context(
        tmp_path, voice={"mode": "clone"}, voice_ref_audio_path="D:/me.wav", voice_ref_text="Hello there.",
        voice_rights_confirmed=True,
    )
    S07Configs().run(ctx)
    queued = json.loads((ctx.paths.config / "pending_voice.json").read_text(encoding="utf-8"))
    assert queued["mode"] == "clone" and queued["ref_audio_path"] == "D:/me.wav"
    assert queued["ref_text"] == "Hello there." and queued["rights_confirmed"] is True


def test_s07_queues_nothing_for_a_preset_voice_or_an_existing_one(tmp_path: Path):
    ctx, _ = keyed_context(tmp_path)
    S07Configs().run(ctx)
    assert not (ctx.paths.config / "pending_voice.json").exists()
    ctx2, _ = keyed_context(tmp_path / "b", voice={"mode": "design", "description": "calm"})
    made = ctx2.paths.models_tts_voices / "after-dark-tales-voice"
    made.mkdir(parents=True)
    (made / "voice.json").write_text("{}", encoding="utf-8")
    S07Configs().run(ctx2)
    assert not (ctx2.paths.config / "pending_voice.json").exists()


# ----------------------------------------------------------------------- s05/s08: local/code (gap 39)
def test_s08_checks_that_opencode_sees_local_code_once_s07_wrote_the_config(tmp_path: Path):
    ctx, runner = smoke_ctx(tmp_path)
    assert S07Configs.id == "s07_configs"                      # runs before s08: the file exists by then
    ctx.paths.opencode_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.paths.opencode_file.write_text(json.dumps(OPENCODE_CONFIG), encoding="utf-8")
    S08Smoke().run(ctx)
    call = runner.called("opencode models")[0]
    assert call.env["OPENCODE_CONFIG"] == str(ctx.paths.opencode_file)
    assert ctx.flag("opencode_local_code") is True


def test_a_missing_local_code_model_is_said_but_does_not_fail_the_install(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop", results={"opencode models": RunResult(0, "openai/gpt\n")})
    ctx = resolved(tmp_path, runner=runner)
    ctx.paths.opencode_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.paths.opencode_file.write_text("{}", encoding="utf-8")
    S08Smoke().run(ctx)
    assert ctx.flag("opencode_local_code") is False and ctx.flag("smoke") == "passed"
    assert "cannot find local/code" in ctx.ui.text


def test_s05_no_longer_pretends_to_check_the_model_before_the_config_exists():
    from installer.stage2.steps import s05_opencode_prewarm

    doc = s05_opencode_prewarm.__doc__ or ""
    assert "confirm_local_code" in doc and "after this step" in doc


# ---------------------------------------------------------------- qwentts binary names (CR-C4-6)
def test_the_qwentts_zip_must_hold_the_real_programs(tmp_path: Path):
    from installer.stage2 import downloader as dl

    item = dl.release_zip_item("qwentts", "https://x/q.zip", None, tmp_path)
    assert item.expect == ("qwen-tts.exe", "qwen-codec.exe")           # there is no qwentts.exe


def test_the_installer_and_the_reference_engine_use_one_hf_folder(tmp_path: Path):
    from installer.stage2.fallback import fallback_items

    ctx = resolved(tmp_path)
    for item in fallback_items(ctx):
        assert ctx.paths.models_tts_hf in item.dest.parents or item.dest.parent == ctx.paths.models_tts_hf


def test_s08_falls_back_to_the_reference_engine_when_the_unpacked_zip_lacks_qwen_tts(tmp_path: Path):
    ctx, runner = smoke_ctx(tmp_path)
    ctx.set_flag("qwentts", "binary")
    ctx.paths.bin_qwentts.mkdir(parents=True, exist_ok=True)
    (ctx.paths.bin_qwentts / "tts-server.exe").write_bytes(b"MZ")     # only the old, wrong program is there
    S08Smoke().run(ctx)
    assert ctx.flag("tts_fallback") is True and [c for c, _ in ctx.warnings] == ["NS-W301"]


def test_s08_keeps_qwentts_when_both_programs_are_there(tmp_path: Path):
    ctx, _ = smoke_ctx(tmp_path)
    ctx.set_flag("qwentts", "binary")
    ctx.paths.bin_qwentts.mkdir(parents=True, exist_ok=True)
    for exe in ("qwen-tts.exe", "qwen-codec.exe"):
        (ctx.paths.bin_qwentts / exe).write_bytes(b"MZ")
    S08Smoke().run(ctx)
    assert ctx.flag("tts_fallback", False) is False and ctx.warnings == []
