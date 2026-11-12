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

import io
import json
import re
import threading
import time
from collections.abc import Iterator
from datetime import datetime
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
from ns_common.config import load_runtime, save_runtime  # noqa: E402
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
MACHINE = good_deps()      # the probes of a healthy PC, made before any test replaces `verify.Deps`


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


def refuse_voice(svc: Service_, name: str) -> None:
    """The engine dies on this voice (a preset has no file to lose, so the engine is made to refuse it)."""
    real = svc.engine.synth

    def refuse(text: str, voice: VoiceSpec, **kw: Any) -> Any:
        if voice.name == name:
            svc.engine.spoken.append(name)
            raise NsError("NS-E310", "the engine died on this sentence")
        return real(text, voice, **kw)

    svc.engine.synth = refuse  # type: ignore[method-assign]


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
    """The preset failing is a failed row (the engine check, as before); a designed or cloned voice failing
    is a WARNING row naming the mode and the voice, and every other talker is still spoken."""
    mode = {"Ryan": "preset", "Dee": "design", "Cleo": "clone"}[broken]
    if broken == "Ryan":
        refuse_voice(three_talkers, "Ryan")
    else:
        break_voice(three_talkers, broken)
    report = run(tmp_path, three_talkers, quick=True)
    rows = tts_rows(report)
    row = rows[f"tts.{mode}"]
    log = row.log or ""
    assert f"{mode} voice" in log.lower() and repr(broken) in log      # the mode and the voice are named
    assert "NS-E31" in log                                           # and the service's own code and reason
    for other in {"preset", "design", "clone"} - {mode}:
        assert rows[f"tts.{other}"].passed and rows[f"tts.{other}"].value != names.TALKER_WARNING_VALUE
    assert voices_spoken(three_talkers.engine) == ["Ryan", "Dee", "Cleo"]     # none was skipped
    assert rows["tts.voices"].passed is True and mode in (rows["tts.voices"].log or "")
    lines = verify.format_lines(report)
    if broken == "Ryan":
        assert row.passed is False and any(f"[FAIL] tts.{mode}" in ln for ln in lines)
        assert verify.talker_warnings(report.checks) == []
    else:
        assert row.passed is True and row.value == names.TALKER_WARNING_VALUE      # a warning, not a failure
        assert verify.talker_warnings(report.checks) == [log]
        assert any(f"[WARN] tts.{mode}" in ln for ln in lines)
        assert any("Channels > Edit channel" in ln and "Library > Voices" in ln for ln in lines)


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


def test_full_mode_speaks_with_every_talker_then_times_one_batch(
    tmp_path: Path, three_talkers: Service_
) -> None:
    """Card verify-limits (stale: this test pinned the RTF limit on every talker's single sentence, which
    timed the voice program's start, L1): the talker rows prove the voice speaks and carry no limit; the speed
    is the `tts.rate` row of one batch. The fake engine has no stream, so its rate is the whole call (L2)."""
    report = run(tmp_path, three_talkers, quick=False)
    rows = tts_rows(report)
    assert [n for n in rows if n != "tts.voices"] == [
        "tts.preset", "tts.design", "tts.clone", "tts.rate", "tts.start_s",
    ]  # fmt: skip
    for n in ("tts.preset", "tts.design", "tts.clone"):
        assert rows[n].passed and rows[n].limit is None and isinstance(rows[n].value, float), rows[n]
    rate = rows["tts.rate"]
    assert rate.passed and rate.limit == "<= 0.5 RTF" and isinstance(rate.value, float)
    assert "whole call" in (rate.log or "") and "no per-sentence times" in (rate.log or "")
    assert rows["tts.start_s"].passed and rows["tts.start_s"].value == "n/a"
    assert report.benchmarks["tts_rtf"] == rate.value and "tts_start_s" not in report.benchmarks
    assert any(c.name == "bench.tts_rtf" and c.passed for c in report.checks)
    assert report.slice == "verify"


def test_s08_smoke_reports_the_per_mode_rows(tmp_path: Path, three_talkers: Service_) -> None:
    ctx = smoke_ctx(tmp_path, three_talkers)
    S08Smoke().run(ctx)
    table = dict(map(tuple, ctx.ui.tables[0][1]))
    assert {"tts.preset", "tts.design", "tts.clone"} <= set(table), sorted(table)
    assert table["tts.preset"] == table["tts.design"] == table["tts.clone"] == "ok"
    assert ctx.flag("smoke") == "passed" and ctx.warnings == [] and ctx.flag("voice_warnings") == []


# --------------------------------------------------------------------------------- the whole run, all green
class HybridCtl:
    """The supervisor: the stub services for the story model and the memory index, the real voice service.

    Everything but the voices is green, so `report.passed` and the exit code say what the voices did.
    """

    def __init__(self, sync: SyncSupervisor, tts_url: str) -> None:
        self.sync, self.url = sync, tts_url

    def is_up(self) -> bool:
        return True

    def shutdown(self, grace_s: float = 30) -> None:
        return None

    def acquire(
        self, service: Service, owner: str, *, model_alias: str | None = None, timeout_s: int = 7200
    ) -> Endpoint:
        if service == "tts":
            return Endpoint(self.url)
        return self.sync.acquire(service, owner, model_alias=model_alias, timeout_s=timeout_s)

    def release(self, service: Service, owner: str) -> None:
        if service != "tts":
            self.sync.release(service, owner)

    def status(self) -> dict[str, Any]:
        return self.sync.status()

    def stop_all(self) -> None:
        self.sync.stop_all()


def run_green(tmp_path: Path, svc: Service_, *, quick: bool = True, **kw: Any) -> GateReport:
    paths = Paths(tmp_path / "ns")
    sync = SyncSupervisor(paths, Runtime.default("gpu"), resolver=StubTable())
    try:
        return verify.run_verify(
            paths, Runtime.default("gpu"), HybridCtl(sync, svc.url), quick=quick, deps=good_deps(), **kw
        )
    finally:
        sync.close()


def set_up(svc: Service_, preset: str, clone: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The case of the matrix: the preset ok or failing, the clone voice ok, failing or timing out."""
    if preset == "failing":
        refuse_voice(svc, "Ryan")
    if clone == "failing":
        break_voice(svc, "Cleo")
    elif clone == "timeout":
        monkeypatch.setattr(verify, "LATER_TALKER_TIMEOUT_S", 0.4)
        svc.engine.slow = {"Cleo": 1.5}


CASES = [(p, c) for p in ("ok", "failing") for c in ("ok", "failing", "timeout")]


@pytest.mark.parametrize(("preset", "clone"), CASES)
@pytest.mark.parametrize("quick", [True, False])
def test_the_run_fails_only_when_the_preset_does_a_voice_that_does_not_speak_is_a_warning(
    tmp_path: Path, three_talkers: Service_, monkeypatch: pytest.MonkeyPatch, quick: bool,
    preset: str, clone: str,
) -> None:
    if not quick and preset != "ok":
        pytest.skip("a failing preset fails the full run as it always did; the quick run covers the rows")
    set_up(three_talkers, preset, clone, monkeypatch)
    report = run_green(tmp_path, three_talkers, quick=quick)
    rows = tts_rows(report)
    assert report.passed is (preset == "ok"), {c.name: c.log for c in report.checks if not c.passed}
    assert rows["tts.preset"].passed is (preset == "ok")
    if clone == "ok":
        assert rows["tts.clone"].passed and rows["tts.clone"].value != names.TALKER_WARNING_VALUE
        assert verify.talker_warnings(report.checks) == []
    else:
        c = rows["tts.clone"]
        assert c.passed is True and c.value == names.TALKER_WARNING_VALUE
        assert "clone voice 'Cleo'" in (c.log or "") and "Library > Voices" in (c.log or "")
        assert ("NS-E311" if clone == "failing" else "NS-E310") in (c.log or "")
        assert verify.talker_warnings(report.checks) == [c.log]
    assert rows["tts.design"].passed and rows["tts.design"].value != names.TALKER_WARNING_VALUE
    # no talker is skipped; a full run then times the first voice on six sentences in one batch (L2)
    assert voices_spoken(three_talkers.engine) == ["Ryan", "Dee", "Cleo"] + ([] if quick else ["Ryan"])


def test_a_failing_design_voice_does_not_skip_the_clone_voice_after_it(
    tmp_path: Path, three_talkers: Service_
) -> None:
    break_voice(three_talkers, "Dee")
    report = run_green(tmp_path, three_talkers)
    rows = tts_rows(report)
    assert report.passed and rows["tts.design"].value == names.TALKER_WARNING_VALUE
    assert rows["tts.clone"].passed and rows["tts.clone"].value != names.TALKER_WARNING_VALUE


def test_progress_has_one_line_per_talker_as_it_starts(tmp_path: Path, three_talkers: Service_) -> None:
    seen: list[str] = []
    break_voice(three_talkers, "Cleo")
    run_green(tmp_path, three_talkers, progress=seen.append)
    assert seen == [
        "speaking a test sentence with the preset voice Ryan ...",
        "speaking a test sentence with the design voice Dee ...",
        "speaking a test sentence with the clone voice Cleo ...",
    ]


# ---------------------------------------------------------------------------------------------- setup's s08
def real_report_runner(tmp_path: Path, svc: Service_) -> FakeRunner:
    """A setup runner whose `ns verify --quick` answers with what the real verify finds on this service.

    Each call runs the real `run_verify` against the service as it is at that moment (so a test can repair a
    voice between two calls).
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
        return RunResult(0 if all(c["passed"] for c in checks) else 1, data)

    return FakeRunner(tmp_path / "desktop", results={"ns verify --quick": ns_verify})


def smoke_ctx(tmp_path: Path, svc: Service_, runner: FakeRunner | None = None, ui: Any = None) -> Any:
    runner = runner or real_report_runner(tmp_path, svc)
    return make_test_context(
        tmp_path / "setup", raw_answers=example_answers(), resolve=True, runner=runner, ui=ui
    )


@pytest.mark.parametrize("clone", ["ok", "failing", "timeout"])
def test_s08_a_repair_over_an_existing_voice_library_goes_on_when_a_later_talker_does_not_speak(
    tmp_path: Path, three_talkers: Service_, monkeypatch: pytest.MonkeyPatch, clone: str
) -> None:
    set_up(three_talkers, "ok", clone, monkeypatch)
    ctx = smoke_ctx(tmp_path, three_talkers)
    S08Smoke().run(ctx)                                             # nothing is raised
    runner = ctx.runner
    table = dict(map(tuple, ctx.ui.tables[0][1]))
    assert ctx.flag("smoke") == "passed"
    assert load_runtime(ctx.paths).tts.engine != "reference" and not ctx.flag("tts_fallback", False)
    assert not runner.called("uv venv tts") and len(runner.called("ns verify --quick")) == 1
    assert ctx.warnings == []                                       # no NS code is claimed (none fits)
    warnings = ctx.flag("voice_warnings")
    if clone == "ok":
        assert warnings == [] and table["tts.clone"] == "ok"
    else:
        assert len(warnings) == 1 and "clone voice 'Cleo'" in warnings[0]
        assert "Library > Voices" in warnings[0] and "Channels > Edit channel" in warnings[0]
        assert table["tts.clone"] == "warning" and table["tts.preset"] == "ok"
        assert any(warnings[0] in line for line in ctx.ui.said)       # said in the terminal / setup log


@pytest.mark.parametrize("clone", ["ok", "failing", "timeout"])
def test_s08_a_failing_preset_still_switches_to_the_reference_engine_whatever_the_later_talkers_do(
    tmp_path: Path, three_talkers: Service_, monkeypatch: pytest.MonkeyPatch, clone: str
) -> None:
    set_up(three_talkers, "ok", clone, monkeypatch)
    real = three_talkers.engine.synth
    switched = {"done": False}

    def refuse_preset_until_switched(text: str, voice: VoiceSpec, **kw: Any) -> Any:
        if voice.name == "Ryan" and not switched["done"]:
            raise NsError("NS-E310", "qwentts.cpp died on this sentence")
        return real(text, voice, **kw)

    three_talkers.engine.synth = refuse_preset_until_switched  # type: ignore[method-assign]
    runner = real_report_runner(tmp_path, three_talkers)
    ctx = smoke_ctx(tmp_path, three_talkers, runner)
    # the PyTorch venv install is a uv call: the preset speaks once it has run
    runner.results["uv venv tts"] = lambda call: (switched.update(done=True), runner._default(call))[1]
    S08Smoke().run(ctx)
    assert [c for c, _ in ctx.warnings] == ["NS-W301"] and ctx.flag("tts_fallback") is True
    assert runner.called("uv venv tts") and len(runner.called("ns verify --quick")) == 2
    assert ctx.flag("smoke") == "passed"
    assert len(ctx.flag("voice_warnings")) == (0 if clone == "ok" else 1)   # from the second pass only


def test_s08_a_failing_preset_after_the_switch_is_still_e108(tmp_path: Path, three_talkers: Service_) -> None:
    refuse_voice(three_talkers, "Ryan")
    ctx = smoke_ctx(tmp_path, three_talkers)
    ctx.set_flag("tts_fallback", True)
    with pytest.raises(NsError) as ei:
        S08Smoke().run(ctx)
    assert ei.value.code == "NS-E108"


def test_s08_a_first_install_has_the_preset_only_and_no_warning(tmp_path: Path, service: Service_) -> None:
    ctx = smoke_ctx(tmp_path, service)
    S08Smoke().run(ctx)
    table = dict(map(tuple, ctx.ui.tables[0][1]))
    assert [n for n in table if n.startswith("tts.")] == ["tts.start", "tts.preset", "tts.voices"]
    assert ctx.flag("voice_warnings") == [] and ctx.flag("smoke") == "passed" and ctx.warnings == []


def test_s08_retry_speaks_every_talker_again_and_a_repaired_voice_clears_the_warning(
    tmp_path: Path, three_talkers: Service_
) -> None:
    break_voice(three_talkers, "Dee")
    ctx = smoke_ctx(tmp_path, three_talkers)
    runner = ctx.runner
    step = S08Smoke()
    step.run(ctx)
    assert len(ctx.flag("voice_warnings")) == 1
    three_talkers.engine.spoken.clear()
    # the owner made the voice again (a voice file is back); setup is run again (a Repair)
    (three_talkers.voices_dir / "Dee" / "speaker.bin").write_bytes(bytes(range(1, 65)))
    for flag in S08Smoke.clears_on_run:                       # what the wizard does before it runs the step
        ctx.set_flag(flag, None)
    assert step.check(ctx) is False                           # no stale marker skips the test
    step.run(ctx)
    step.verify(ctx)
    assert step.check(ctx) is True and len(runner.called("ns verify --quick")) == 2
    assert voices_spoken(three_talkers.engine) == ["Ryan", "Dee", "Cleo"]     # every talker spoke again
    assert ctx.flag("voice_warnings") == []


def test_s08_killed_halfway_leaves_nothing_that_skips_the_next_run(
    tmp_path: Path, three_talkers: Service_
) -> None:
    ctx = smoke_ctx(tmp_path, three_talkers)
    ctx.set_flag("smoke", "passed")                       # an earlier pass of the same version ...
    ctx.set_flag("smoke_version", ctx.manifest.version)
    ctx.set_flag("voice_warnings", ["an old sentence"])
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
    assert S08Smoke().check(ctx) is False and not ctx.flag("voice_warnings")
    runner.results["ns verify --quick"] = real
    three_talkers.engine.spoken.clear()
    S08Smoke().run(ctx)
    assert voices_spoken(three_talkers.engine) == ["Ryan", "Dee", "Cleo"] and S08Smoke().check(ctx) is True


# ------------------------------------------------------------------------------------ the owner's side
def test_the_setup_row_and_install_report_name_the_voice_and_the_way_out(
    tmp_path: Path, three_talkers: Service_, monkeypatch: pytest.MonkeyPatch
) -> None:
    """s08 on the setup window's route: the bridge carries the sentence to the Voice engine row, and
    install-report.json (written by the window's controller) has it, whether the install is done or not."""
    from installer.stage2.bridge import BridgeUi
    from tests.test_installer_web_controller import Setup

    break_voice(three_talkers, "Cleo")
    out = io.StringIO()
    ctx = smoke_ctx(tmp_path, three_talkers, ui=BridgeUi(out=out, inp=io.StringIO("")))
    S08Smoke().run(ctx)
    events = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]
    verify_event = next(e for e in events if e["type"] == "verify")
    s = Setup(tmp_path / "ctl", monkeypatch)
    s.ctl._on_event(verify_event)
    row = next(r for r in s.snap()["verify"]["checks"] if r["name"] == "tts")
    assert row["status"] == "fallback" and row["value"] == "clone voice did not speak"      # not a green dot
    assert "clone voice 'Cleo'" in row["note"] and "Channels > Edit channel" in row["note"]
    # the window shows notices, not row notes: the sentence is a notice, on the Verify and the Done screen
    notice = next(n for n in s.snap()["notices"] if "Cleo" in n["title"])
    assert notice["code"] == ""
    assert notice["title"].startswith("The clone voice 'Cleo' did not speak a test sentence")
    assert "Library > Voices" in notice["whatToDo"] and "Save channel" in notice["whatToDo"]
    assert notice in s.ctl._build_result()["warnings"]
    s.ctl._on_event(verify_event)                                   # the same test again: still one notice
    assert len([n for n in s.snap()["notices"] if "Cleo" in n["title"]]) == 1
    flags = s.home / "config" / "wizard_flags.json"
    flags.parent.mkdir(parents=True, exist_ok=True)
    flags.write_text(json.dumps({"voice_warnings": ctx.flag("voice_warnings")}), encoding="utf-8")
    written = s.ctl._write_install_report("done")
    assert written is not None
    report = json.loads(written.read_text(encoding="utf-8"))
    assert len(report["voiceWarnings"]) == 1 and "clone voice 'Cleo'" in report["voiceWarnings"][0]
    assert "" not in report["warnings"]                              # the codes only; the sentence is above
    # a repaired voice: the next test replaces the notice
    repaired = [
        dict(c, value=0.2, note=None) if c["name"] == "tts.clone" else c for c in verify_event["checks"]
    ]
    s.ctl._on_event({"type": "verify", "checks": repaired})
    assert not [n for n in s.snap()["notices"] if "Cleo" in n["title"]]


def test_the_console_install_report_has_the_sentence_too(tmp_path: Path, three_talkers: Service_) -> None:
    from installer.stage2.diagzip import write_console_install_report

    break_voice(three_talkers, "Cleo")
    ctx = smoke_ctx(tmp_path, three_talkers)
    S08Smoke().run(ctx)
    out = write_console_install_report(ctx.paths, outcome="done", version="0.1.15")
    assert out is not None
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data["voice_warnings"]) == 1 and "clone voice 'Cleo'" in data["voice_warnings"][0]


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
    assert verify.read_talker_warnings(Paths(tmp_path / "no-report")) == []
    report_file = Paths(tmp_path / "home2").logs / "verify-report.json"
    report_file.parent.mkdir(parents=True)
    report_file.write_text(json.dumps(old), encoding="utf-8")
    assert verify.read_talker_warnings(Paths(tmp_path / "home2")) == []


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
    break_voice(three_talkers, "Cleo")
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
def test_a_talker_that_stalls_is_a_warning_within_the_bound_and_the_run_goes_on(
    tmp_path: Path, three_talkers: Service_, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_up(three_talkers, "ok", "timeout", monkeypatch)
    report = run(tmp_path, three_talkers)
    rows = tts_rows(report)
    assert rows["tts.preset"].passed and rows["tts.design"].passed
    assert rows["tts.clone"].passed is True and rows["tts.clone"].value == names.TALKER_WARNING_VALUE
    # The stalled talker alone, from verify's own lines: asked at "speaking", given up at "talker_failed". Not
    # the whole run: the preset, the design voice and the rest of verify take their own time on a busy PC.
    lines = [
        json.loads(ln)
        for f in sorted((tmp_path / "ns" / "logs").glob("verify-*.jsonl"))
        for ln in f.read_text(encoding="utf-8").splitlines()
    ]
    asked = [r["ts"] for r in lines if r["event"] == "speaking" and r["data"].get("mode") == "clone"]
    gave_up = [r["ts"] for r in lines if r["event"] == "talker_failed" and r["data"].get("mode") == "clone"]
    assert len(asked) == 1 and len(gave_up) == 1, lines
    took = (datetime.fromisoformat(gave_up[0]) - datetime.fromisoformat(asked[0])).total_seconds()
    assert took < 1.4, took                     # gave up after the 0.4 s bound, not after the 1.5 s sentence
    assert report.checks[-1].name == "vram.idle"          # and the rest of verify still ran


def test_the_bound_keeps_the_first_talkers_old_time_and_a_dead_engine_costs_minutes() -> None:
    first, later = verify.FIRST_TALKER_TIMEOUT_S, verify.LATER_TALKER_TIMEOUT_S
    assert first == 1800                                  # what the preset always had: a cold model load
    assert 0 < later <= 180                               # a 2 GB model loads in under a minute from a disk
    assert 2 * later <= 360                               # two later talkers: six minutes at the worst
    assert first + 2 * later < 3600                       # s08's timeout for the whole `ns verify --quick`


# ---------------------------------------------------------------------------------- every route into verify
def cli_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, svc: Service_, argv: list[str], *,
    home: Path | None = None,
) -> Any:
    """`ns verify` entered as the product enters it, on the stub services plus the real voice service."""
    home = home or tmp_path / "ns"
    Paths(home).ensure()
    if not Paths(home).runtime_file.exists():    # `ns verify` starts nothing without settings (NS-E125)
        save_runtime(Paths(home), Runtime.default("gpu"))
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(home))
    NO_SCHEDULE_CALLS.clear()
    sync = SyncSupervisor(Paths(home), Runtime.default("gpu"), resolver=StubTable())
    try:
        monkeypatch.setattr(cli, "_client", lambda paths: HybridCtl(sync, svc.url))
        monkeypatch.setattr(cli, "ensure_running", lambda *a, **k: None)
        monkeypatch.setattr(cli, "_settle_schedule", lambda paths: NO_SCHEDULE_CALLS.append(paths))
        monkeypatch.setattr(verify, "Deps", lambda: MACHINE)    # no real whisper, NVENC or nvidia-smi here
        return CliRunner().invoke(cli.app, argv)
    finally:
        sync.close()


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


@pytest.mark.parametrize("route", ["cli", "updater", "setup"])
@pytest.mark.parametrize(("preset", "clone"), [("ok", "ok"), ("ok", "failing"), ("ok", "timeout"),
                                               ("failing", "ok")])
def test_ns_verify_quick_exits_1_only_when_the_preset_fails_on_every_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, three_talkers: Service_, route: str,
    preset: str, clone: str,
) -> None:
    if route == "cli":
        argv = ["verify", "--quick"]
    elif route == "updater":
        argv = updater_argv(tmp_path, monkeypatch)
        assert argv[:2] == ["-m", "ns_supervisor.cli"]
        argv = argv[2:]
    else:
        ctx = smoke_ctx(tmp_path / "s08", three_talkers)
        S08Smoke().run(ctx)
        argv = list(ctx.runner.called("ns verify --quick")[0].argv)[-2:]
    assert argv == ["verify", "--quick"]
    set_up(three_talkers, preset, clone, monkeypatch)
    r = cli_run(tmp_path, monkeypatch, three_talkers, argv)
    lines = [ln for ln in r.output.splitlines() if ln.startswith("[") and " tts." in ln]
    modes = [ln.split()[1] for ln in lines if "tts.start" not in ln and "tts.voices" not in ln]
    assert modes == ["tts.preset", "tts.design", "tts.clone"], r.output
    assert r.exit_code == (0 if preset == "ok" else 1), r.output       # what both updaters read
    assert "speaking a test sentence with the clone voice Cleo ..." in r.output
    if clone == "ok":
        assert "[WARN]" not in r.output
    else:
        assert "[WARN] tts.clone = warning" in r.output and "Channels > Edit channel" in r.output
    report = json.loads((Paths(tmp_path / "ns").logs / "verify-report.json").read_text(encoding="utf-8"))
    assert report["slice"] == "verify-quick"
    assert not any(c["name"] == "bench.tts_rtf" for c in report["checks"])
    assert len(NO_SCHEDULE_CALLS) == 1          # the schedule look ran on the fake, never on Task Scheduler


def test_ns_verify_full_has_the_same_exit_code_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, three_talkers: Service_
) -> None:
    break_voice(three_talkers, "Cleo")
    r = cli_run(tmp_path, monkeypatch, three_talkers, ["verify"])
    assert r.exit_code == 0 and "[WARN] tts.clone = warning" in r.output, r.output
    assert "ALL CHECKS PASSED (a voice did not speak" in r.output


def test_the_morning_note_is_left_once_and_names_the_voice_and_the_way_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, three_talkers: Service_
) -> None:
    from ns_supervisor import notify

    break_voice(three_talkers, "Cleo")
    for _ in range(2):                                   # an update, then the owner runs it again
        assert cli_run(tmp_path, monkeypatch, three_talkers, ["verify", "--quick"]).exit_code == 0
    notes = notify.read_notes(Paths(tmp_path / "ns"))
    assert len(notes) == 1
    assert "clone voice 'Cleo'" in notes[0]["message"] and "Library > Voices" in notes[0]["message"]
    assert "Channels > Edit channel" in notes[0]["message"] and "Save channel" in notes[0]["message"]


def test_an_update_from_a_home_whose_clone_voice_is_broken_completes_and_leaves_the_note_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, three_talkers: Service_
) -> None:
    """A clone voice already broken on 0.1.14. The update's verify step is the real `ns verify --quick`;
    when it exited 1 for that voice, every update to this version rolled back."""
    from ns_supervisor import notify
    from tests.test_ns_update import make_app, make_zip

    break_voice(three_talkers, "Cleo")
    app, z = make_app(tmp_path), make_zip(tmp_path)
    home = tmp_path / "ns"
    calls: list[str] = []

    def verify_step(a: Path) -> bool:
        calls.append("verify")
        return cli_run(tmp_path, monkeypatch, three_talkers, ["verify", "--quick"], home=home).exit_code == 0

    env = update.UpdateEnv(
        stop=lambda: None, sync=lambda a: None, verify=verify_step, reregister=lambda a: [],
        untracked=lambda a: [],
    )
    res = update.update_app(z, app, env, paths=Paths(home))
    assert calls == ["verify"]                                           # ran once and passed
    assert (res.old_version, res.new_version) == ("0.1.0", "0.2.0")
    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.2.0"      # not rolled back
    mine = [n for n in res.notes if "clone voice 'Cleo'" in n]
    assert len(mine) == 1 and "Channels > Edit channel" in mine[0]       # in the update's result notes ...
    assert [n["message"] for n in notify.read_notes(Paths(home))] == mine   # ... and once as a morning note


def test_the_dashboard_verify_job_gets_the_per_mode_rows_and_a_progress_line_each(
    tmp_path: Path, three_talkers: Service_
) -> None:
    from ns_dashboard.adapters import RealVerifyAdapter

    break_voice(three_talkers, "Cleo")
    said: list[str] = []

    class Job:
        def check_cancel(self) -> None:
            return None

        def progress(self, fraction: float, message: str | None = None) -> None:
            said.append(str(message))

    paths = Paths(tmp_path / "ns")
    out = RealVerifyAdapter(paths, controller=OneServiceCtl(three_talkers.url), deps=good_deps()).verify(
        Job(), True  # type: ignore[arg-type]
    )
    modes = [c["name"] for c in out["checks"] if c["name"] in ("tts.preset", "tts.design", "tts.clone")]
    assert modes == ["tts.preset", "tts.design", "tts.clone"] and out["quick"] is True
    # the Settings page shows the warning beside the verdict, and the morning note is left as on the CLI
    from ns_supervisor import notify

    assert len(out["warnings"]) == 1 and "clone voice 'Cleo'" in out["warnings"][0]
    assert [n["message"] for n in notify.read_notes(paths)] == out["warnings"]
    assert [m for m in said if m.startswith("speaking")] == [
        "speaking a test sentence with the preset voice Ryan ...",
        "speaking a test sentence with the design voice Dee ...",
        "speaking a test sentence with the clone voice Cleo ...",
    ]
    RealVerifyAdapter(paths, controller=OneServiceCtl(three_talkers.url), deps=good_deps()).verify(
        Job(), True  # type: ignore[arg-type]
    )
    assert len(notify.read_notes(paths)) == 1                        # not left twice


def test_the_morning_note_is_keyed_by_mode_and_voice_not_by_the_error_detail(tmp_path: Path) -> None:
    from ns_supervisor import notify

    paths = Paths(tmp_path / "ns")
    first = [verify.talker_warning("clone", "Cleo", "NS-E310: engine said: stack A")]
    again = [verify.talker_warning("clone", "Cleo", "NS-E310: engine said: stack B, other numbers 4711")]
    other = [verify.talker_warning("clone", "Cleo2", "NS-E310: engine said: stack A")]
    design = [verify.talker_warning("design", "Cleo", "NS-E310: engine said: stack A")]
    assert first[0] != again[0]
    for checks in (first, again, first):
        verify.leave_voice_notes(paths, checks)
    assert len(notify.read_notes(paths)) == 1                     # a changed detail is not a new note
    verify.leave_voice_notes(paths, other)
    verify.leave_voice_notes(paths, design)
    assert len(notify.read_notes(paths)) == 3                     # another voice, another mode: its own note


def test_the_docstring_and_the_comment_say_what_is_true() -> None:
    import ast

    assert chr(11) not in (verify.read_talker_warnings.__doc__ or "")     # no vertical tab in the docstring
    src = (Path(__file__).resolve().parents[1] / "installer" / "stage2" / "diagzip.py").read_text("utf-8")
    assert "standard library plus two stdlib-only ns_common modules" in src.splitlines()[0]
    import sys

    for node in ast.walk(ast.parse(src)):
        mods = [a.name for a in node.names] if isinstance(node, ast.Import) else (
            [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else [])
        for m in mods:
            assert m.split(".")[0] in sys.stdlib_module_names or m in (
                "ns_common.atomic", "ns_common.paths", "__future__"), f"diagzip imports {m}"


def test_the_doctor_does_not_call_verify() -> None:
    """The dashboard doctor builds a diagnostics zip: it reads verify-report.json, it never runs verify."""
    src = (Path(__file__).resolve().parents[1] / "ns_supervisor" / "doctor.py").read_text(encoding="utf-8")
    assert "run_verify" not in src and "verify.run" not in src
