"""P30a: `ns verify --quick` speaks one sentence with EVERY installed talker, not only the preset.

The voice service here is the product's own (`ns_tts.server.create_app` on the fake engine, a voice library
on disk), served over real HTTP, so `TtsClient` crosses the same boundary it crosses at install. Only the
engine is a fake (tone bursts). Every route into `run_verify` is entered where the product enters it: the
`ns verify` command, the command line the updater of 0.1.14 runs, setup's s08 step and the dashboard's verify
job.

The owner's side of each state (who is told, where, the words, the way out) is in the card's report table;
the rows asserted below are the ones that table names.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")

from typer.testing import CliRunner  # noqa: E402

from installer.stage2.runner import RunResult  # noqa: E402
from installer.stage2.simulate import FakeRunner  # noqa: E402
from installer.stage2.steps.s08_smoke import S08Smoke, load_report_rows  # noqa: E402
from installer.stage2.testing import example_answers, make_test_context  # noqa: E402
from installer.stage2.web import labels  # noqa: E402
from ns_common.acquire import Endpoint, Service  # noqa: E402
from ns_common.config import load_runtime  # noqa: E402
from ns_common.errors import NsError  # noqa: E402
from ns_common.logging import NsLogger  # noqa: E402
from ns_common.models import GateReport, Runtime  # noqa: E402
from ns_common.paths import Paths  # noqa: E402
from ns_supervisor import check_names as names  # noqa: E402
from ns_supervisor import cli, update, verify  # noqa: E402
from ns_supervisor.testing import StubTable, SyncSupervisor, wait_until  # noqa: E402
from ns_tts.engine import FakeEngine  # noqa: E402
from ns_tts.server import create_app  # noqa: E402
from ns_tts.voices import VoiceLibrary, VoiceSpec  # noqa: E402
from tests.test_ns_verify import good_deps  # noqa: E402

NO_SCHEDULE_CALLS: list[Any] = []


# ---------------------------------------------------------------------------------------------- the service
def voices_spoken(engine: RecordingEngine) -> list[str]:
    """The voices in the order they spoke, one entry per request (a sentence is spoken in two chunks)."""
    out: list[str] = []
    for name in engine.spoken:
        if not out or out[-1] != name:
            out.append(name)
    return out


class RecordingEngine(FakeEngine):
    """The fake engine that remembers which voice spoke, and can be slow for one voice."""

    def __init__(self) -> None:
        super().__init__()
        self.spoken: list[str] = []
        self.slow: dict[str, float] = {}

    def synth(  # type: ignore[override]
        self, text: str, voice: VoiceSpec, *, speed: float = 1.0, seed: int | None = None
    ) -> Any:
        self.spoken.append(voice.name)
        time.sleep(self.slow.get(voice.name, 0.0))
        return super().synth(text, voice, speed=speed, seed=seed)


def make_voice(root: Path, name: str, mode: str, *, with_file: bool = True) -> None:
    """A designed or cloned voice as the library stores it (voice.json and the speaker file)."""
    folder = root / name
    folder.mkdir(parents=True)
    meta = {"name": name, "mode": mode, "description": "x", "language": "English", "files": ["speaker.bin"]}
    (folder / "voice.json").write_text(json.dumps(meta), encoding="utf-8")
    if with_file:
        (folder / "speaker.bin").write_bytes(bytes(range(1, 65)))


class Service_:
    """One running voice service on a free port."""

    def __init__(self, home: Path) -> None:
        self.voices_dir = home / "voices"
        self.engine = RecordingEngine()
        self.engine.load()
        app = create_app(self.engine, VoiceLibrary(self.voices_dir), log=NsLogger("tts", home / "tts-logs"))
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        assert wait_until(lambda: self.server.started, 15.0), "uvicorn did not start"
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(15.0)


@pytest.fixture
def service(tmp_path: Path) -> Iterator[Service_]:
    s = Service_(tmp_path / "svc")
    yield s
    s.stop()


@pytest.fixture
def three_talkers(service: Service_) -> Service_:
    """The preset voices plus one designed and one cloned voice: three installed modes."""
    make_voice(service.voices_dir, "Dee", "design")
    make_voice(service.voices_dir, "Cleo", "clone")
    return service


class OneServiceCtl:
    """The supervisor as `run_verify` sees it, with only the voice service behind it."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.events: list[tuple[str, str]] = []

    def acquire(
        self, service: Service, owner: str, *, model_alias: str | None = None, timeout_s: int = 7200
    ) -> Endpoint:
        self.events.append(("acquire", service))
        if service == "tts":
            return Endpoint(self.url)
        raise NsError("NS-E202", f"{service} is not part of this test")

    def release(self, service: Service, owner: str) -> None:
        self.events.append(("release", service))

    def status(self) -> dict[str, Any]:
        return {"services": {}}

    def stop_all(self) -> None:
        return None


def run(tmp_path: Path, svc: Service_, *, quick: bool = True, **deps: Any) -> GateReport:
    paths = Paths(tmp_path / "ns")
    return verify.run_verify(
        paths, Runtime.default("gpu"), OneServiceCtl(svc.url), quick=quick, deps=good_deps(**deps)
    )


def tts_rows(report: GateReport) -> dict[str, Any]:
    return {c.name: c for c in report.checks if c.name.startswith("tts.") and c.name != "tts.start"}


def break_voice(svc: Service_, name: str) -> None:
    """One voice file gone, as an antivirus or a cleaning tool leaves it: the voice is still listed."""
    (svc.voices_dir / name / "speaker.bin").unlink()


# ---------------------------------------------------------------------------------------- the card's tests
def test_quick_mode_speaks_one_sentence_per_installed_mode(tmp_path: Path, three_talkers: Service_) -> None:
    report = run(tmp_path, three_talkers, quick=True)
    rows = tts_rows(report)
    assert [n for n in rows if n != "tts.voices"] == ["tts.preset", "tts.design", "tts.clone"]
    assert all(rows[n].passed for n in rows), {n: rows[n].log for n in rows}
    assert rows["tts.voices"].value == "preset,design,clone"
    # one sentence each, one after the other
    assert voices_spoken(three_talkers.engine) == ["Ryan", "Dee", "Cleo"]
    for n, voice in (("tts.preset", "Ryan"), ("tts.design", "Dee"), ("tts.clone", "Cleo")):
        assert re.fullmatch(rf"voice={voice} audio_s=\d+\.\d+", rows[n].log or ""), rows[n].log
    assert "tts_rtf" not in report.benchmarks and not any(c.name == "bench.tts_rtf" for c in report.checks)
    # no RTF limit in quick
    assert all(rows[n].limit is None for n in ("tts.preset", "tts.design", "tts.clone"))
    assert report.slice == "verify-quick"


@pytest.mark.parametrize("broken", ["Ryan", "Dee", "Cleo"])
def test_a_failing_second_talker_fails_the_install_check_with_its_mode_named(
    tmp_path: Path, three_talkers: Service_, broken: str
) -> None:
    mode = {"Ryan": "preset", "Dee": "design", "Cleo": "clone"}[broken]
    if broken == "Ryan":
        # a preset has no file to lose: make the service refuse this voice name instead
        real = three_talkers.engine.synth

        def refuse(text: str, voice: VoiceSpec, **kw: Any) -> Any:
            if voice.name == "Ryan":
                raise NsError("NS-E310", "the engine died on this sentence")
            return real(text, voice, **kw)

        three_talkers.engine.synth = refuse  # type: ignore[method-assign]
    else:
        break_voice(three_talkers, broken)
    report = run(tmp_path, three_talkers, quick=True)
    rows = tts_rows(report)
    failed = {n for n, c in rows.items() if not c.passed}
    assert failed == {f"tts.{mode}"}, {n: rows[n].log for n in rows}
    log = rows[f"tts.{mode}"].log or ""
    assert f"{mode} voice" in log and repr(broken) in log      # the mode and the voice are named
    assert "NS-E31" in log                                   # and the service's own code and reason
    for other in {"preset", "design", "clone"} - {mode}:
        assert rows[f"tts.{other}"].passed                    # the others were still spoken
    assert rows["tts.voices"].passed is True and mode in (rows["tts.voices"].log or "")
    assert report.passed is False
    lines = verify.format_lines(report)
    assert any(f"[FAIL] tts.{mode}" in ln for ln in lines)
    assert any(f"{mode} voice" in ln for ln in lines)         # the console shows why, under the row


def test_quick_mode_with_only_the_preset_installed_is_unchanged(tmp_path: Path, service: Service_) -> None:
    report = run(tmp_path, service, quick=True)
    rows = tts_rows(report)
    assert list(rows) == ["tts.preset", "tts.voices"]
    preset = rows["tts.preset"]
    assert preset.passed is True and preset.limit is None and isinstance(preset.value, float)
    assert re.fullmatch(r"voice=Ryan audio_s=\d+\.\d+", preset.log or "")
    assert (rows["tts.voices"].value, rows["tts.voices"].log, rows["tts.voices"].passed) == (
        "preset", "modes exercised", True,
    )
    line = next(ln for ln in verify.format_lines(report) if "tts.preset" in ln)
    assert re.fullmatch(r"\[PASS\] tts\.preset = \d+\.\d+", line)
    assert not any(c.name == "bench.tts_rtf" for c in report.checks)


def test_the_reference_engine_row_is_unchanged(tmp_path: Path, service: Service_) -> None:
    report = run(tmp_path, service, quick=True, tts_engine=lambda url: "reference")
    row = next(c for c in report.checks if c.name == names.TTS_ENGINE)
    assert row.passed is True and row.value == "reference" and (row.log or "").startswith("NS-W301")


def test_full_mode_is_unchanged(tmp_path: Path, three_talkers: Service_) -> None:
    report = run(tmp_path, three_talkers, quick=False)
    rows = tts_rows(report)
    assert [n for n in rows if n != "tts.voices"] == ["tts.preset", "tts.design", "tts.clone"]
    assert all(rows[n].limit == "<= 0.5 RTF" for n in ("tts.preset", "tts.design", "tts.clone"))
    assert "tts_rtf" in report.benchmarks
    assert any(c.name == "bench.tts_rtf" and c.passed for c in report.checks)
    assert report.slice == "verify"


def test_s08_smoke_reports_the_per_mode_rows(tmp_path: Path, three_talkers: Service_) -> None:
    ctx = smoke_ctx(tmp_path, three_talkers)
    S08Smoke().run(ctx)
    table = dict(map(tuple, ctx.ui.tables[0][1]))
    assert {"tts.preset", "tts.design", "tts.clone"} <= set(table), sorted(table)
    assert table["tts.preset"] == table["tts.design"] == table["tts.clone"] == "ok"
    assert ctx.flag("smoke") == "passed" and ctx.warnings == []


# ---------------------------------------------------------------------------------------------- setup's s08
def real_report_runner(
    tmp_path: Path, svc: Service_, *, more: list[Any] | None = None
) -> FakeRunner:
    """A setup runner whose `ns verify --quick` answers with what the real verify finds on this service.

    Each call runs the real `run_verify` against the service as it is at that moment (so a test can repair a
    voice between two calls); `more` are actions run after each call (they change the service).
    """
    state = {"n": 0}
    base = FakeRunner(tmp_path / "base")

    def ns_verify(call: Any) -> RunResult:
        state["n"] += 1
        real = run(tmp_path / f"verify-{state['n']}", svc, quick=True)
        # everything but the voice rows is the simulated green report; the voice rows are the real ones
        green = [c for c in base._verify_report()["checks"] if not str(c["name"]).startswith("tts.")]
        at = next(i for i, c in enumerate(green) if c["name"] == names.EMBED_VECTOR) + 1
        voice = [c.model_dump(mode="json") for c in real.checks if c.name.startswith("tts.")]
        checks = green[:at] + voice + green[at:]
        data = json.dumps({"slice": "verify-quick", "date": "2026-11-10", "checks": checks, "benchmarks": {}})
        home = call.env.get("NIGHTSHIFT_HOME")
        if home:
            logs = Path(home) / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            (logs / "verify-report.json").write_text(data, encoding="utf-8")
        for action in (more or []):
            action()
        return RunResult(0 if all(c["passed"] for c in checks) else 1, data)

    return FakeRunner(tmp_path / "desktop", results={"ns verify --quick": ns_verify})


def smoke_ctx(tmp_path: Path, svc: Service_, runner: FakeRunner | None = None) -> Any:
    runner = runner or real_report_runner(tmp_path, svc)
    ctx = make_test_context(tmp_path / "setup", raw_answers=example_answers(), resolve=True, runner=runner)
    return ctx


def test_s08_a_failing_second_talker_stops_the_install_check_with_its_mode_named_and_does_not_switch(
    tmp_path: Path, three_talkers: Service_
) -> None:
    break_voice(three_talkers, "Cleo")
    ctx = smoke_ctx(tmp_path, three_talkers)
    runner = ctx.runner
    with pytest.raises(NsError) as ei:
        S08Smoke().run(ctx)
    err = ei.value
    assert err.code == "NS-E310"
    assert "clone" in err.detail and "'Cleo'" in err.detail and "NS-E311" in err.detail
    assert "preset" not in err.detail.replace("preset voice", "")          # only the failing mode is named
    # the NS-W301 fallback is for the preset only: nothing was switched, nothing was downloaded
    assert load_runtime(ctx.paths).tts.engine != "reference"
    assert not ctx.flag("tts_fallback", False) and ctx.warnings == []
    assert not runner.called("uv venv tts") and len(runner.called("ns verify --quick")) == 1
    table = dict(map(tuple, ctx.ui.tables[0][1]))
    assert table["tts.clone"] == "fail" and table["tts.preset"] == "ok" and table["tts.design"] == "ok"
    assert ctx.flag("smoke") != "passed"


def test_s08_a_failing_preset_still_switches_to_the_reference_engine_even_when_another_talker_fails(
    tmp_path: Path, three_talkers: Service_
) -> None:
    real = three_talkers.engine.synth

    def refuse_preset_until_switched(text: str, voice: VoiceSpec, **kw: Any) -> Any:
        if voice.name == "Ryan" and not switched["done"]:
            raise NsError("NS-E310", "qwentts.cpp died on this sentence")
        return real(text, voice, **kw)

    switched = {"done": False}
    three_talkers.engine.synth = refuse_preset_until_switched  # type: ignore[method-assign]
    break_voice(three_talkers, "Cleo")
    runner = real_report_runner(tmp_path, three_talkers)
    ctx = smoke_ctx(tmp_path, three_talkers, runner)

    def switch(call: Any = None) -> None:
        switched["done"] = True

    # the PyTorch venv install is a uv call: the preset speaks once it has run
    runner.results["uv venv tts"] = lambda call: (switch(), runner._default(call))[1]
    with pytest.raises(NsError) as ei:
        S08Smoke().run(ctx)
    assert [c for c, _ in ctx.warnings] == ["NS-W301"] and ctx.flag("tts_fallback") is True
    assert runner.called("uv venv tts") and len(runner.called("ns verify --quick")) == 2
    assert ei.value.code == "NS-E310" and "clone" in ei.value.detail  # the second pass names the other talker


def test_s08_retry_after_a_talker_failed_speaks_again_and_passes(
    tmp_path: Path, three_talkers: Service_
) -> None:
    break_voice(three_talkers, "Dee")
    ctx = smoke_ctx(tmp_path, three_talkers)
    runner = ctx.runner
    step = S08Smoke()
    with pytest.raises(NsError):
        step.run(ctx)
    assert step.check(ctx) is False                       # no stale marker: setup's resume runs s08 again
    three_talkers.engine.spoken.clear()
    # the owner repaired the voice
    (three_talkers.voices_dir / "Dee" / "speaker.bin").write_bytes(bytes(range(1, 65)))
    step.run(ctx)
    step.verify(ctx)
    assert step.check(ctx) is True and len(runner.called("ns verify --quick")) == 2
    # every talker spoke again, not only the one that was repaired
    assert voices_spoken(three_talkers.engine) == ["Ryan", "Dee", "Cleo"]


def test_s08_killed_halfway_leaves_nothing_that_skips_the_next_run(
    tmp_path: Path, three_talkers: Service_
) -> None:
    ctx = smoke_ctx(tmp_path, three_talkers)
    ctx.set_flag("smoke", "passed")                       # an earlier pass of the same version ...
    ctx.set_flag("smoke_version", ctx.manifest.version)
    assert S08Smoke().check(ctx) is True
    for flag in S08Smoke.clears_on_run:                   # ... which the wizard clears before the step starts
        ctx.set_flag(flag, None)
    runner = ctx.runner
    real = runner.results["ns verify --quick"]

    def killed(call: Any) -> RunResult:
        raise KeyboardInterrupt

    runner.results["ns verify --quick"] = killed
    with pytest.raises(KeyboardInterrupt):
        S08Smoke().run(ctx)
    assert S08Smoke().check(ctx) is False
    runner.results["ns verify --quick"] = real
    three_talkers.engine.spoken.clear()
    S08Smoke().run(ctx)
    assert voices_spoken(three_talkers.engine) == ["Ryan", "Dee", "Cleo"] and S08Smoke().check(ctx) is True


def test_a_report_written_by_the_previous_release_still_loads(tmp_path: Path) -> None:
    """0.1.14 wrote one tts row (`tts.preset`) and `tts.voices`; setup, the labels and the model read it."""
    old = {
        "slice": "verify-quick", "date": "2026-10-01T00:00:00", "machine": {}, "benchmarks": {},
        "dev_mode": False,
        "checks": [
            {"name": "tts.start", "passed": True, "value": 3.0, "limit": None, "log": "http://x"},
            {"name": "tts.preset", "passed": True, "value": 0.31, "limit": None,
             "log": "voice=Ryan audio_s=3.4"},
            {"name": "tts.voices", "passed": True, "value": "preset", "limit": None,
             "log": "modes exercised"},
        ],
    }
    GateReport.model_validate(old)
    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True)
    rows = load_report_rows(ctx, RunResult(0, json.dumps(old)))
    assert rows is not None and [r["name"] for r in rows] == ["tts.start", "tts.preset", "tts.voices"]
    setup_rows = labels.verify_rows(rows, "Ryan")
    assert [(r["name"], r["status"], r["value"]) for r in setup_rows] == [
        ("tts", "passed", "Ryan, voice works")
    ]


def test_no_voice_listed_is_the_same_failed_row_as_before(tmp_path: Path) -> None:
    """The service lists no voice: no `tts.<mode>` row, and `tts.voices` fails as it did."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Empty(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:  # noqa: N802
            body = b"[]" if self.path.startswith("/v1/voices") else b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Empty)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        fake = type("S", (), {"url": f"http://127.0.0.1:{srv.server_address[1]}"})()
        report = run(tmp_path, fake)  # type: ignore[arg-type]
    finally:
        srv.shutdown()
    rows = tts_rows(report)
    assert list(rows) == ["tts.voices"] and rows["tts.voices"].passed is False
    assert "tts reports no voices" in (rows["tts.voices"].log or "")


def test_verify_twice_gives_the_same_rows(tmp_path: Path, three_talkers: Service_) -> None:
    first = run(tmp_path / "a", three_talkers)
    second = run(tmp_path / "b", three_talkers)

    def shape(r: GateReport) -> list[tuple[str, bool, str]]:
        return [
            (c.name, c.passed, re.sub(r"audio_s=[\d.]+", "audio_s=N", c.log or ""))
            for c in tts_rows(r).values()
        ]

    assert shape(first) == shape(second) and len(shape(first)) == 4
    assert voices_spoken(three_talkers.engine) == ["Ryan", "Dee", "Cleo"] * 2


# ---------------------------------------------------------------------------------- time, and never a hang
def test_a_talker_that_stalls_fails_its_own_row_within_the_bound_and_the_run_goes_on(
    tmp_path: Path, three_talkers: Service_, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "LATER_TALKER_TIMEOUT_S", 0.4, raising=False)
    three_talkers.engine.slow = {"Cleo": 2.0}          # the last talker: the service is busy with it
    t0 = time.monotonic()
    report = run(tmp_path, three_talkers)
    took = time.monotonic() - t0
    rows = tts_rows(report)
    assert {"tts.preset", "tts.design", "tts.clone"} <= set(rows), sorted(rows)
    assert rows["tts.preset"].passed and rows["tts.design"].passed
    assert rows["tts.clone"].passed is False and "clone voice 'Cleo'" in (rows["tts.clone"].log or "")
    assert "NS-E310" in (rows["tts.clone"].log or "")
    assert took < 1.9, took                             # gave up after the bound, not after the 2 s sentence
    assert report.checks[-1].name == "vram.idle"          # and the rest of verify still ran


def test_the_bound_keeps_the_first_talkers_old_time_and_the_whole_step_under_the_setup_limit() -> None:
    first = getattr(verify, "FIRST_TALKER_TIMEOUT_S", None)
    later = getattr(verify, "LATER_TALKER_TIMEOUT_S", None)
    assert first is not None and later is not None, "the talker time limits are named constants of verify"
    assert first == 1800                                 # what the preset always had: a cold model load
    assert first + 2 * later < 3600                        # s08's timeout for the whole `ns verify --quick`
    assert later > 0


# ---------------------------------------------------------------------------------- the owner's side
def test_the_setup_row_names_the_failed_mode_and_a_preset_failure_stays_a_fallback() -> None:
    def checks(failing: str) -> list[dict[str, Any]]:
        return [
            {"name": n, "passed": n != failing, "value": "0.3" if n.startswith("tts.") else None}
            for n in ("tts.start", "tts.preset", "tts.design", "tts.clone", "tts.voices")
        ]

    clone = labels.verify_rows(checks("tts.clone"), "Ryan")[0]
    assert (clone["name"], clone["status"], clone["value"]) == ("tts", "failed", "clone voice")
    design = labels.verify_rows(checks("tts.design"), "Ryan")[0]
    assert (design["status"], design["value"]) == ("failed", "design voice")
    preset = labels.verify_rows(checks("tts.preset"), "Ryan")[0]
    assert (preset["status"], preset["value"]) == ("fallback", "Ryan")      # the handled switch: as before
    ok = labels.verify_rows(checks("none"), "Ryan")[0]
    assert (ok["status"], ok["value"]) == ("passed", "Ryan, voice works")


def test_the_install_report_and_the_failure_screen_name_the_talker(
    tmp_path: Path, three_talkers: Service_, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_installer_web_controller import Setup

    break_voice(three_talkers, "Cleo")
    ctx = smoke_ctx(tmp_path, three_talkers)
    with pytest.raises(NsError) as ei:
        S08Smoke().run(ctx)
    err = ei.value
    s = Setup(tmp_path / "ctl", monkeypatch)
    s.ctl._on_event({  # what the setup program sends when a step raises (installer/stage2/bridge.py)
        "type": "failure", "step": "s08_smoke", "code": err.code, "message": err.definition.message,
        "hint": err.definition.hint, "detail": err.detail, "optional": False,
    })
    failure = s.snap()["failure"]
    assert failure["code"] == "NS-E310" and "clone" in failure["detail"] and "'Cleo'" in failure["detail"]
    assert failure["title"] == "A voice did not speak"
    assert "Retry" in failure["whatToDo"] and "Save a help file" in failure["whatToDo"]
    assert failure["canSkip"] is False
    written = s.ctl._write_install_report("failed")
    assert written is not None
    report = json.loads(written.read_text(encoding="utf-8"))
    assert report["code"] == "NS-E310" and report["failedStep"] == "s08_smoke"
    assert "clone" in report["lastError"]["detail"] and "'Cleo'" in report["lastError"]["detail"]


# ---------------------------------------------------------------------------------- every route into verify
class StubCtl:
    """`SupervisorClient` for `cmd_verify`: the stub supervisor, plus the two calls the command makes."""

    def __init__(self, sync: SyncSupervisor) -> None:
        self.sync = sync

    def is_up(self) -> bool:
        return True

    def shutdown(self, grace_s: float = 30) -> None:
        return None

    def acquire(self, *a: Any, **kw: Any) -> Endpoint:
        return self.sync.acquire(*a, **kw)

    def release(self, *a: Any, **kw: Any) -> None:
        self.sync.release(*a, **kw)

    def status(self) -> dict[str, Any]:
        return self.sync.status()

    def stop_all(self) -> None:
        self.sync.stop_all()


def updater_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The command line 0.1.14's `update.verify_quick` runs (it passes no timeout), minus the interpreter."""
    from types import SimpleNamespace

    seen: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        seen.append((list(cmd), kw))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(update, "subprocess", SimpleNamespace(run=fake_run))
    assert update.verify_quick(tmp_path, "python-of-the-venv") is True
    cmd, kw = seen[0]
    assert "timeout" not in kw          # the updater waits as long as the command takes: the bound is ours
    return cmd[1:]


def s08_argv(tmp_path: Path, three: Service_) -> list[str]:
    ctx = smoke_ctx(tmp_path, three)
    S08Smoke().run(ctx)
    return list(ctx.runner.called("ns verify --quick")[0].argv)[-2:]


@pytest.mark.parametrize("route", ["cli", "updater", "setup"])
def test_ns_verify_quick_prints_one_tts_row_per_installed_mode_on_every_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, three_talkers: Service_, route: str
) -> None:
    if route == "cli":
        argv = ["verify", "--quick"]
    elif route == "updater":
        argv = updater_argv(tmp_path, monkeypatch)
        assert argv[:2] == ["-m", "ns_supervisor.cli"]
        argv = argv[2:]
    else:
        argv = s08_argv(tmp_path / "s08", three_talkers)
    assert argv == ["verify", "--quick"]
    home = tmp_path / "home"
    Paths(home).ensure()
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(home))
    NO_SCHEDULE_CALLS.clear()
    # the stub voice service lists a preset and a clone voice; `ns verify` is entered as the product enters it
    sync = SyncSupervisor(Paths(home), Runtime.default("gpu"), resolver=StubTable())
    try:
        monkeypatch.setattr(cli, "_client", lambda paths: StubCtl(sync))
        monkeypatch.setattr(cli, "ensure_running", lambda *a, **k: None)
        monkeypatch.setattr(cli, "_settle_schedule", lambda paths: NO_SCHEDULE_CALLS.append(paths))
        machine = good_deps()
        monkeypatch.setattr(verify, "Deps", lambda: machine)    # no real whisper, NVENC or nvidia-smi here
        r = CliRunner().invoke(cli.app, argv)
    finally:
        sync.close()
    lines = [ln for ln in r.output.splitlines() if ln.startswith("[") and " tts." in ln]
    assert [ln.split()[1] for ln in lines if "tts.start" not in ln and "tts.voices" not in ln] == [
        "tts.preset", "tts.clone",
    ], r.output
    assert all(ln.startswith("[PASS]") for ln in lines)
    assert len(NO_SCHEDULE_CALLS) == 1          # the schedule look ran on the fake, never on Task Scheduler
    report = json.loads((Paths(home).logs / "verify-report.json").read_text(encoding="utf-8"))
    assert report["slice"] == "verify-quick"
    assert not any(c["name"] == "bench.tts_rtf" for c in report["checks"])


def test_the_dashboard_verify_job_gets_the_per_mode_rows(tmp_path: Path, three_talkers: Service_) -> None:
    from ns_dashboard.adapters import RealVerifyAdapter

    class Job:
        def check_cancel(self) -> None:
            return None

        def progress(self, *a: Any, **k: Any) -> None:
            return None

    paths = Paths(tmp_path / "ns")
    out = RealVerifyAdapter(paths, controller=OneServiceCtl(three_talkers.url), deps=good_deps()).verify(
        Job(), True  # type: ignore[arg-type]
    )
    modes = [c["name"] for c in out["checks"] if c["name"] in ("tts.preset", "tts.design", "tts.clone")]
    assert modes == ["tts.preset", "tts.design", "tts.clone"] and out["quick"] is True


def test_the_doctor_does_not_call_verify() -> None:
    """The dashboard doctor builds a diagnostics zip: it reads verify-report.json, it never runs verify."""
    src = (Path(__file__).resolve().parents[1] / "ns_supervisor" / "doctor.py").read_text(encoding="utf-8")
    assert "run_verify" not in src and "verify.run" not in src
