# ruff: noqa: E501
"""Dev install (docs/DEV_MODE.md): the shared switch, the dev extra, the fake llama and embedding servers,
the service table, `ns verify`, the disabled nightly task and the studio's fakes."""

from __future__ import annotations

import json
import threading
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from ns_common import devmode
from ns_common.config import save_runtime
from ns_common.embed_client import EmbedClient
from ns_common.llm_client import LlmClient
from ns_common.models import GateReport, Runtime
from ns_common.paths import Paths, contracts_dir
from ns_common.venv_extras import DEV_VENV_EXTRAS, VENV_EXTRAS, extras_suffix
from ns_dashboard import studio_job
from ns_studio import jobs
from ns_studio.cli import app as studio_app
from ns_studio.context import FAKEABLE
from ns_supervisor import devfake, pwsh, schedule, services, verify
from ns_supervisor.schedule_cfg import read_run_time, read_schedule_mode, schedule_file
from ns_supervisor.testing import StubTable, SyncSupervisor

ROOT = Path(__file__).resolve().parents[1]


def dev_runtime(**over: Any) -> Runtime:
    return Runtime.default("cpu-dev").model_copy(update={"dev_mode": True, **over})


# ---------------------------------------------------------------------------------------------- the switch
def test_the_dev_fakes_are_the_studio_fakeable_services():
    assert set(devmode.DEV_FAKES) == set(FAKEABLE)


def test_a_missing_or_broken_runtime_json_is_a_real_install(tmp_path: Path):
    paths = Paths(tmp_path / "ns")
    assert devmode.is_dev(paths) is False and devmode.dev_fakes(paths) == frozenset()
    paths.config.mkdir(parents=True)
    paths.runtime_file.write_text("{ not json", encoding="utf-8")
    assert devmode.is_dev(paths) is False


def test_runtime_json_with_dev_mode_turns_every_service_fake(tmp_path: Path):
    paths = Paths(tmp_path / "ns")
    save_runtime(paths, dev_runtime())
    assert devmode.is_dev(paths) is True and devmode.dev_fakes(paths) == frozenset(FAKEABLE)
    save_runtime(paths, Runtime.default("cpu-dev"))
    assert devmode.is_dev(paths) is False


def test_dev_mode_is_in_the_runtime_install_state_and_report_schemas():
    for name in ("runtime", "install_state", "gate_report"):
        schema = json.loads((contracts_dir() / "schemas" / f"{name}.schema.json").read_text("utf-8"))
        assert schema["properties"]["dev_mode"]["default"] is False, name
    assert Runtime().dev_mode is False and GateReport(slice="x", date="d", machine={}, checks=[]).dev_mode is False


# ---------------------------------------------------------------------------------------------- the dev extra
def test_the_dev_install_extra_is_the_studio_extra_without_the_speech_and_gpu_wheels():
    extras = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "optional-dependencies"]

    def names(specs: list[str]) -> set[str]:
        return {s.split(">")[0].split("<")[0].split("=")[0].strip().lower() for s in specs}

    gpu_and_speech = {"faster-whisper", "av", "ctranslate2"}
    assert gpu_and_speech <= names(extras["studio"])
    assert names(extras["dev-install"]) == names(extras["studio"]) - gpu_and_speech
    every = {n for specs in extras.values() for n in names(specs)}
    assert "torch" not in every and not any(n.startswith("nvidia") for n in every)


def test_the_dev_install_installs_the_dev_extra_and_no_tts_venv():
    assert extras_suffix("studio", dev=True) == "[dev-install,supervisor]"
    assert extras_suffix("studio") == "[studio,supervisor]"
    assert extras_suffix("supervisor", dev=True) == extras_suffix("supervisor")
    assert extras_suffix("tts") == "[tts]" and extras_suffix("tts", dev=True) == ""
    assert "tts" in VENV_EXTRAS and "tts" not in DEV_VENV_EXTRAS


# ---------------------------------------------------------------------------------------------- devfake
def test_example_for_satisfies_the_schemas_the_pipeline_sends():
    schema = {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "n": {"type": "integer", "minimum": 3},
            "kind": {"enum": ["a", "b"]},
            "tags": {"type": "array", "items": {"type": "string"}, "minItems": 2},
            "inner": {"$ref": "#/$defs/Inner"},
            "maybe": {"anyOf": [{"type": "null"}, {"type": "string"}]},
        },
        "$defs": {"Inner": {"type": "object", "properties": {"x": {"type": "number", "minimum": 1.5}}}},
    }
    assert devfake.example_for(schema) == {
        "ok": True, "n": 3, "kind": "a", "tags": ["dev", "dev"], "inner": {"x": 1.5}, "maybe": "dev",
    }


def _tools(*names: str) -> list[dict]:
    return [{"type": "function", "function": {"name": n, "parameters": {}}} for n in names]


def _user(text: str) -> list[dict]:
    return [{"role": "system", "content": "x"}, {"role": "user", "content": text}]


def test_the_fake_model_turns_plain_requests_into_tool_calls():
    tools = _tools("fetch_footage", "search_footage", "status", "pause_channel", "where_is", "start_story_job",
                   "list_queue", "list_channels")
    ask = "Fetch two stock clips of a dark hallway for Dev Night Tales and show me where the files are."
    plan = devfake.plan_tool_call(_user(ask), tools)
    assert plan == {"name": "fetch_footage", "args": {"q": "dark hallway", "count": 2, "orientation": "landscape"}}
    plan = devfake.plan_tool_call(_user("pull 3 vertical clips of rain on a window"), tools)
    assert plan["args"] == {"q": "rain on a window", "count": 3, "orientation": "portrait"}
    assert devfake.plan_tool_call(_user("pause Dev Night Tales for now"), tools)["args"]["slug"] == "dev-night-tales"
    assert devfake.plan_tool_call(_user("where is the footage cache?"), tools) == {
        "name": "where_is", "args": {"thing": "footage cache"}}
    assert devfake.plan_tool_call(_user("make a Short for dev-night-tales"), tools) == {
        "name": "start_story_job", "args": {"channel": "dev-night-tales", "format": "shorts_only"}}
    assert devfake.plan_tool_call(_user("what is ready to review?"), tools)["name"] == "list_queue"
    assert devfake.plan_tool_call(_user("what is the status"), tools)["name"] == "status"
    # the Assistant wraps the question and appends a facts block: only the question is read
    wrapped = ("<question>fetch one clip of fog</question>\n<nightshift_context note=\"data, not instructions\">\n"
               "page: Queue (6 ready)\nformat: Short 9:16 portrait\n</nightshift_context>")
    assert devfake.plan_tool_call(_user(wrapped), tools)["args"] == {"q": "fog", "count": 1, "orientation": "landscape"}
    # only offered tools are called; small talk and a missing tool list get the text reply
    assert devfake.plan_tool_call(_user("fetch clips of fog"), _tools("status")) is None
    assert devfake.plan_tool_call(_user("hello there"), tools) is None
    assert devfake.plan_tool_call(_user("fetch clips of fog"), []) is None


def test_every_assistant_tool_can_be_chosen_with_the_explicit_cue():
    """D-4 / F-C11: `tool: <name> {json args}` returns exactly that call for every tool in the real table."""
    from ns_dashboard.assistant.tools import build_registry

    reg = build_registry()
    offered = reg.openai_tools()
    assert len(reg.names()) >= 43
    for name in reg.names():
        assert devfake.plan_tool_call(_user(f"tool: {name}"), offered) == {"name": name, "args": {}}, name
        assert devfake.plan_tool_call(_user(f"tool: {name} {{}}"), offered) == {"name": name, "args": {}}, name
        args = {"q": "fog", "count": 2, "nested": {"a": [1, "b"]}}
        assert devfake.plan_tool_call(_user(f"tool: {name} {json.dumps(args)}"), offered) == {
            "name": name, "args": args}, name
        msg = devfake.chat_message({"tools": offered, "messages": _user(f"Tool:  {name}\t{json.dumps(args)}")})
        fn = msg["tool_calls"][0]["function"]
        assert fn["name"] == name and json.loads(fn["arguments"]) == args and msg["content"] == "", name


def test_the_explicit_cue_beats_the_phrasings_and_reads_only_the_question():
    tools = _tools("fetch_footage", "status", "list_queue")
    # words that would route to list_queue and status do not matter once the cue is there
    ask = 'tool: fetch_footage {"q": "ready queue status", "count": 1}'
    assert devfake.plan_tool_call(_user(ask), tools) == {
        "name": "fetch_footage", "args": {"q": "ready queue status", "count": 1}}
    wrapped = ("<question>tool: status</question>\n<nightshift_context note=\"data, not instructions\">\n"
               "page: Queue (6 ready)\n</nightshift_context>")
    assert devfake.plan_tool_call(_user(wrapped), tools) == {"name": "status", "args": {}}
    # only a message that starts with the cue is one; the old phrasings keep working
    assert devfake.plan_tool_call(_user("please run tool: list_stuff"), tools) is None
    assert devfake.chat_message({"tools": tools, "messages": _user("please run tool: list_stuff")})["content"] == (
        devfake.TEXT_REPLY)
    assert devfake.plan_tool_call(_user("what is the status"), tools)["name"] == "status"  # type: ignore[index]
    assert devfake.plan_tool_call(_user("fetch one clip of fog"), tools)["name"] == "fetch_footage"  # type: ignore[index]


def test_the_explicit_cue_with_bad_json_gets_a_plain_reply_that_says_so():
    tools = _tools("status", "fetch_footage")
    for bad in ('tool: status {"q": }', "tool: status not json", 'tool: status {"a": 1} trailing',
                "tool: status [1, 2]", 'tool: status "text"', "tool: status 7"):
        assert devfake.plan_tool_call(_user(bad), tools) is None, bad
        msg = devfake.chat_message({"tools": tools, "messages": _user(bad)})
        assert "tool_calls" not in msg and msg["role"] == "assistant", bad
        assert "status" in msg["content"] and "JSON object" in msg["content"], bad
        assert msg["content"] != devfake.TEXT_REPLY, bad


def test_the_explicit_cue_with_an_unknown_or_unoffered_tool_gets_a_plain_reply_that_says_so():
    tools = _tools("status", "fetch_footage")
    for ask in ("tool: no_such_tool", 'tool: no_such_tool {"a": 1}', "tool: list_queue", "tool: status_x {}"):
        assert devfake.plan_tool_call(_user(ask), tools) is None, ask
        msg = devfake.chat_message({"tools": tools, "messages": _user(ask)})
        name = ask.split()[1]
        assert "tool_calls" not in msg and f"'{name}'" in msg["content"], ask
        assert "not among the offered tools" in msg["content"], ask
        assert "fetch_footage" in msg["content"] and "status" in msg["content"], ask
    # the words of an unknown cue are not routed to a tool by the old phrasings
    assert devfake.plan_tool_call(_user("tool: pause_everything channels status"), tools) is None
    # no tools offered at all: still no call and no error
    assert devfake.plan_tool_call(_user("tool: status"), []) is None
    assert devfake.chat_message({"messages": _user("tool: status")})["content"] == devfake.TEXT_REPLY


def test_the_fake_model_answers_in_text_after_a_tool_ran():
    body = {"tools": _tools("fetch_footage"), "messages": _user("fetch a clip of fog")}
    msg = devfake.chat_message(body)
    assert msg["tool_calls"][0]["function"]["name"] == "fetch_footage" and msg["content"] == ""
    after = _user("fetch a clip of fog") + [
        msg,
        {"role": "tool", "name": "fetch_footage", "tool_call_id": "call_dev_fetch_footage",
         "content": json.dumps({"clips": [{"path": r"D:\\x\\a.mp4"}, {"path": r"D:\\x\\b.mp4"}]})},
    ]
    text = devfake.chat_message({**body, "messages": after})["content"]
    assert "a.mp4" in text and "b.mp4" in text
    assert "tool_calls" not in devfake.chat_message({**body, "messages": after})
    gate = after[:-1] + [{"role": "tool", "name": "fetch_footage", "tool_call_id": "c",
                          "content": json.dumps({"status": "awaiting_owner_confirmation", "will_do": "Download 1 clip."})}]
    assert "Download 1 clip." in devfake.chat_message({**body, "messages": gate})["content"]
    # a schema request and a plain chat are unchanged
    assert devfake.chat_message({"messages": _user("hi")})["content"] == devfake.TEXT_REPLY


def test_embeddings_are_deterministic_unit_vectors():
    a, b = devfake.embedding_for("night"), devfake.embedding_for("night")
    assert a == b and len(a) == devfake.EMBED_DIM and devfake.embedding_for("day") != a
    assert abs(sum(x * x for x in a) - 1.0) < 1e-3


@pytest.fixture
def fake_server() -> Iterator[Any]:
    started: list[Any] = []

    def start(service: str) -> str:
        srv = devfake.make_server(service, 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    yield start
    for srv in started:
        srv.shutdown()
        srv.server_close()


def test_the_fake_llama_answers_the_real_llm_client(fake_server: Any):
    url = fake_server("llama")
    client = LlmClient(url, "code")
    assert client.health() is True
    out = client.complete_json(
        prompt_name="verify", system="json", user="go",
        schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
    )
    assert out == {"ok": True}
    assert "dev-mode" in client.complete_text(prompt_name="t", system="s", user="u")
    native = httpx.post(f"{url}/completion", json={"n_predict": 8}).json()
    assert native["timings"]["predicted_per_second"] > 0
    assert httpx.post(f"{url}/v1/embeddings", json={"input": ["x"]}).status_code == 404      # not an embed server


def test_the_fake_embed_server_answers_the_real_embed_client(fake_server: Any):
    url = fake_server("embed")
    client = EmbedClient(url)
    assert client.health() is True
    vecs = client.embed(["one", "two", "one"])
    assert len(vecs) == 3 and vecs[0] == vecs[2] and vecs[0] != vecs[1]


def test_make_server_refuses_an_unknown_service():
    with pytest.raises(ValueError):
        devfake.make_server("tts", 0)


# ---------------------------------------------------------------------------------------------- service table
def test_a_dev_install_starts_the_fakes_not_llama_server(tmp_path: Path):
    paths = Paths(tmp_path / "ns")
    rt = dev_runtime()
    llama = services.build_service(rt, paths, "llama")
    embed = services.build_service(rt, paths, "embed")
    tts = services.build_service(rt, paths, "tts")
    assert llama.cmd[1:] == ["-m", "ns_supervisor.devfake", "--service", "llama", "--port", "8080",
                             "--host", "127.0.0.1"]
    assert embed.cmd[3:5] == ["--service", "embed"] and embed.port == 8081
    assert tts.cmd[1:] == ["-m", "ns_tts.server", "--port", "8091", "--engine", "fake"]
    for d in (llama, embed, tts):
        assert d.gpu is False and "llama-server" not in " ".join(d.cmd)
    assert llama.health_url == "http://127.0.0.1:8080/health"


def test_a_real_install_still_starts_llama_server(tmp_path: Path):
    paths = Paths(tmp_path / "ns")
    cmd = services.build_service(Runtime.default("gpu"), paths, "llama").cmd
    assert cmd[0].endswith("llama-server.exe") and "devfake" not in " ".join(cmd)


def test_the_dev_voice_engine_runs_in_the_supervisor_venv_even_when_runtime_says_reference(tmp_path: Path):
    paths = Paths(tmp_path / "ns")
    py = paths.venvs / "supervisor" / "Scripts" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_bytes(b"")
    cmd = services.build_service(dev_runtime(), paths, "tts").cmd      # cpu-dev default is the reference engine
    assert cmd[0] == str(py) and cmd[-2:] == ["--engine", "fake"]


# ---------------------------------------------------------------------------------------------- ns verify
def dev_deps(**over: Any) -> verify.Deps:
    def boom(*_a: Any) -> Any:
        raise AssertionError("a dev install must not run this probe")

    d = verify.Deps(whisper=boom, nvenc=boom, x264=lambda: None, job=boom, vram_used=boom,
                    gpu_name=boom, ram=lambda: 8.0, sleep=lambda s: None)
    for k, v in over.items():
        setattr(d, k, v)
    return d


def test_verify_in_a_dev_install_passes_without_gpu_whisper_or_nvenc(tmp_path: Path):
    paths = Paths(tmp_path / "ns")
    rt = dev_runtime()
    sync = SyncSupervisor(paths, rt, resolver=StubTable())
    try:
        report = verify.run_verify(paths, rt, sync, quick=True, deps=dev_deps())
    finally:
        sync.close()
    assert report.passed, [c for c in report.checks if not c.passed]
    assert report.dev_mode is True and report.machine["gpu"] is None and report.machine["profile"] == "cpu-dev"
    by = {c.name: c for c in report.checks}
    assert by["whisper.load"].log and "dev mode" in by["whisper.load"].log
    assert by["nvenc.probe"].value == "libx264 ok" and by["vram.idle"].value == "n/a"
    data = json.loads(verify.write_report(paths, report).read_text(encoding="utf-8"))
    assert data["dev_mode"] is True


def test_verify_in_a_real_install_says_dev_mode_false_and_still_probes_nvenc(tmp_path: Path):
    paths = Paths(tmp_path / "ns")
    rt = Runtime.default("gpu")
    sync = SyncSupervisor(paths, rt, resolver=StubTable())
    seen: list[str] = []
    deps = verify.Deps(whisper=lambda p, r: 1.0, nvenc=lambda: seen.append("nvenc"), x264=lambda: seen.append("x264"),
                       job=lambda p: 1.0, vram_used=lambda: 100, gpu_name=lambda: "GPU", ram=lambda: 32.0,
                       sleep=lambda s: None)
    try:
        report = verify.run_verify(paths, rt, sync, quick=True, deps=deps)
    finally:
        sync.close()
    assert report.dev_mode is False and seen == ["nvenc"]


def test_the_quick_check_names_are_the_same_in_a_dev_install(tmp_path: Path):
    from ns_supervisor import check_names

    paths = Paths(tmp_path / "ns")
    rt = dev_runtime()
    sync = SyncSupervisor(paths, rt, resolver=StubTable())
    try:
        report = verify.run_verify(paths, rt, sync, quick=True, deps=dev_deps())
    finally:
        sync.close()
    assert [c.name for c in report.checks if not c.name.startswith("bench.")][:3] == list(
        check_names.QUICK_CHECKS)[:3]
    assert set(check_names.QUICK_CHECKS) <= {c.name for c in report.checks}


# ---------------------------------------------------------------------------------------------- schedule
def done(out: str) -> Any:
    import subprocess

    return subprocess.CompletedProcess(["powershell"], 0, out, "")


def test_the_disabled_task_script_registers_the_task_disabled_in_one_step(tmp_path: Path):
    paths = Paths(tmp_path / "ns")
    plain = schedule.build_install_script(paths, "03:00", tmp_path)
    dev = schedule.build_install_script(paths, "03:00", tmp_path, disabled=True)
    assert "-Disable" not in plain
    assert "Disable-ScheduledTask" not in dev  # no second call: no window in which the task can fire
    settings = next(ln for ln in dev.splitlines() if ln.startswith("$settings"))
    assert settings.endswith("-Disable")
    assert dev.splitlines()[-1].startswith("Write-Output ('registered:'")


def test_a_disabled_task_is_no_schedule_for_the_supervisor_or_the_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = Paths(tmp_path / "ns")
    from tests.test_schedule_keeps_state import install_fake

    install_fake(monkeypatch, None)
    msg = schedule.install(paths, "03:00", tmp_path, disabled=True)
    assert "DISABLED" in msg
    assert json.loads(schedule_file(paths).read_text(encoding="utf-8"))["mode"] == "disabled"
    assert read_run_time(paths) is None and read_schedule_mode(paths) == "disabled"
    schedule.install(paths, "03:00", tmp_path)
    assert read_run_time(paths) == "03:00" and read_schedule_mode(paths) == "registered"


def test_ns_schedule_install_takes_disabled(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    from ns_supervisor.cli import app

    seen: list[bool] = []

    def fake_install(
        paths: Paths,
        run_time: str | None,
        app_path: Path,
        *,
        disabled: bool = False,
        keep_state: bool = False,
        enable: bool = False,
    ) -> str:
        seen.append(disabled)
        return "registered"

    monkeypatch.setattr(schedule, "install", fake_install)
    runner = CliRunner()
    assert runner.invoke(app, ["schedule", "install", "--time", "04:00", "--disabled"]).exit_code == 0
    assert runner.invoke(app, ["schedule", "install", "--time", "04:00"]).exit_code == 0
    assert seen == [True, False]


def test_ns_tune_in_a_dev_install_has_nothing_to_tune(tmp_home: Path):
    from ns_supervisor.cli import app

    save_runtime(Paths.from_env(), dev_runtime())
    res = CliRunner().invoke(app, ["tune"])
    assert res.exit_code == 0 and "nothing to tune" in res.output


# ---------------------------------------------------------------------------------------------- the studio
def test_the_dashboard_child_fakes_every_service_in_a_dev_install(tmp_home: Path):
    paths = Paths.from_env()
    common = studio_job._common({})
    assert common["fake"] == () and common["acquirer"] is None
    save_runtime(paths, dev_runtime())
    assert set(studio_job._common({})["fake"]) == set(FAKEABLE)
    assert studio_job._common({"fake": ["llm"]})["fake"] == ("llm",)       # what the dashboard sent wins


def test_the_transcribe_job_needs_no_whisper_in_a_dev_install(tmp_home: Path):
    save_runtime(Paths.from_env(), dev_runtime())
    text = studio_job.do_transcribe({"wav": "unused.wav"})["text"]
    assert len(text.split()) >= 2


def test_studio_open_env_fakes_everything_in_a_dev_install(tmp_home: Path):
    paths = Paths.from_env()
    save_runtime(paths, dev_runtime())
    env = jobs.open_env()
    try:
        assert env.fake == FAKEABLE and env.runtime.dev_mode is True
        assert env.services.llm.__class__.__name__ == "FakeLlm"
        assert env.services.tts.__class__.__name__ == "FakeTts"
    finally:
        env.db.close()
    save_runtime(paths, Runtime.default("cpu-dev"))
    env = jobs.open_env()
    try:
        assert env.fake == frozenset() and env.services.llm.__class__.__name__ == "LlmClient"
    finally:
        env.db.close()


def test_studio_run_without_the_fake_services_flag_runs_on_the_fakes_in_a_dev_install(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_common.config import list_channels
    from ns_studio.orchestrator import FORMAT_STAGES
    from ns_studio.stages import REGISTRY

    class One:
        name = "dev_stub"
        tenant = None
        inputs = ()
        outputs = ("dev.json",)

        def run(self, ctx: Any) -> None:
            ctx.write_json("dev.json", {"llm": type(ctx.services.llm).__name__})

    monkeypatch.setitem(REGISTRY, "dev_stub", One())
    monkeypatch.setitem(FORMAT_STAGES, "shorts_only", ["dev_stub"])
    monkeypatch.setattr(jobs, "STAGE_MODULES", ())
    monkeypatch.setattr("ns_studio.policy.gate.available", lambda: False)
    paths = Paths.from_env()
    save_runtime(paths, dev_runtime())
    res = CliRunner().invoke(studio_app, ["run", "--format", "shorts_only"])
    assert res.exit_code == 0, res.output
    assert "demo" in list_channels(paths)           # only fake services start with the demo channel
    written = list(paths.jobs_dir.glob("*/dev.json"))
    assert written and json.loads(written[0].read_text(encoding="utf-8")) == {"llm": "FakeLlm"}


def test_doctor_does_not_ask_a_dev_install_for_nvidia_smi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from ns_supervisor import doctor, procs

    class Down:
        base_url = "http://127.0.0.1:1"

        def is_up(self) -> bool:
            return False

    monkeypatch.setattr(procs, "nvidia_smi_report", lambda: None)
    paths = Paths(tmp_path / "ns")
    save_runtime(paths, dev_runtime())
    rows = {name: (ok, detail) for name, ok, detail in doctor.diagnose(paths, Down())}  # type: ignore[arg-type]
    assert rows["nvidia-smi"] == (True, "not needed (dev mode)")
    save_runtime(paths, Runtime.default("cpu-dev"))
    rows = {name: (ok, detail) for name, ok, detail in doctor.diagnose(paths, Down())}  # type: ignore[arg-type]
    assert rows["nvidia-smi"][0] is False


def test_a_dev_install_keeps_its_task_disabled_when_anything_re_registers_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = Paths(tmp_path / "ns")
    save_runtime(paths, dev_runtime())
    scripts: list[str] = []

    def capture(script: str, **kw: Any) -> Any:
        scripts.append(script)
        return done("registered:s4u\n")

    monkeypatch.setattr(pwsh, "run_powershell", capture)
    schedule.install(paths, "05:00", tmp_path)          # no disabled=True: the Settings page does this
    assert "-ExecutionTimeLimit (New-TimeSpan -Hours 6) -Disable" in scripts[0]
    assert read_schedule_mode(paths) == "disabled"
