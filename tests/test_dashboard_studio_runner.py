# ruff: noqa: E501
"""Dashboard jobs run in the studio venv, as a child process (review row 1).

The dashboard service runs in the supervisor venv, which has no faster-whisper, spaCy or OpenCV, so a story
run, a clip job, a series part, the transcript of a voice sample and a link download are started as
`<studio python> -m ns_dashboard.studio_job <kind> --args <file>`. These tests use a fake interpreter script
that records its argv, working directory and environment and answers with the protocol lines.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from ns_common.errors import NsError, describe
from ns_common.logging import NsLogger
from ns_common.paths import Paths, repo_root
from ns_dashboard import studio_job
from ns_dashboard.adapters import RealClipAdapter, RealRunAdapter, RealSeriesAdapter, RealVoiceMaker
from ns_dashboard.events import EventHub
from ns_dashboard.jobs import JobCancelled, JobControl, JobStore, JobWorker
from ns_dashboard.state import open_db
from ns_dashboard.studio_runner import ENTRY, StudioRunner, failure_error, parse_event, studio_python
from ns_dashboard.supervisor_link import SupervisorLink

FAKE = r'''
import json, os, subprocess, sys, time

args_file = sys.argv[sys.argv.index("--args") + 1]
with open(args_file, encoding="utf-8") as fh:
    args = json.load(fh)
record = {
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "env": {k: os.environ.get(k) for k in ("NIGHTSHIFT_HOME", "PYTHONUTF8", "PYTHONIOENCODING", "PYTHONPATH")},
    "args": args,
    "args_file": args_file,
}
with open(os.environ["FAKE_RECORD"], "w", encoding="utf-8") as fh:
    json.dump(record, fh)


def emit(ev, **data):
    print("NSJOB " + json.dumps({"ev": ev, **data}), flush=True)


mode = os.environ.get("FAKE_MODE", "ok")
if mode == "ok":
    print("a stray line from a library")
    emit("job", job_id="after-dark-20261102-aaaa")
    emit("stage", stage="voice", index=0, total=2, job_index=0, job_total=2)
    emit("stage", stage="captions", index=1, total=2, job_index=0, job_total=2)
    emit("result", output_ids=["after-dark-20261102-aaaa/s1"], job_ids=["after-dark-20261102-aaaa"], path="x.mp4")
elif mode == "text":
    emit("result", text="  this is what I said ")
elif mode == "coded":
    emit("error", code="NS-E202", detail="whisper failed to load: No module named 'faster_whisper'", message="x")
    sys.exit(1)
elif mode == "uncoded":
    emit("error", code=None, detail="", message="KeyError: 'voice'")
    sys.exit(1)
elif mode == "crash":
    print("Traceback (most recent call last): boom", file=sys.stderr)
    sys.exit(3)
elif mode == "obey_cancel_file":
    emit("stage", stage="download", index=0, total=1, job_index=0, job_total=1)
    end = time.time() + 30
    while not os.path.exists(args["cancel_file"]) and time.time() < end:
        time.sleep(0.02)
    with open(os.environ["FAKE_OBEYED"], "w", encoding="utf-8") as fh:
        fh.write("stopped by itself")
    emit("error", code="NS-E409", detail="cancelled", message="x")
    sys.exit(1)
elif mode == "hang":
    grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    with open(os.environ["FAKE_PIDS"], "w", encoding="utf-8") as fh:
        json.dump({"child": os.getpid(), "grandchild": grandchild.pid}, fh)
    emit("stage", stage="voice", index=0, total=2, job_index=0, job_total=1)
    time.sleep(600)
'''


@pytest.fixture
def fake(tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "fake_studio.py"
    script.write_text(FAKE, encoding="utf-8")
    monkeypatch.setenv("FAKE_RECORD", str(tmp_path / "record.json"))
    monkeypatch.setenv("FAKE_PIDS", str(tmp_path / "pids.json"))
    monkeypatch.setenv("FAKE_OBEYED", str(tmp_path / "obeyed.txt"))
    return script


def recorded(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "record.json").read_text(encoding="utf-8"))


def runner_for(fake: Path, tmp_path: Path, **kw: Any) -> StudioRunner:
    app = tmp_path / "app"
    app.mkdir(exist_ok=True)
    kw.setdefault("grace_s", 0.3)  # the stand-in never stops by itself: do not sit out the real 10 s
    return StudioRunner(Paths.from_env(), python=[sys.executable, str(fake)], cwd=app, poll_s=0.05, **kw)


def make_ctl(paths: Paths) -> tuple[JobStore, JobControl]:
    store = JobStore(open_db(paths))
    rec = store.create("run", "after-dark", {})
    return store, JobControl(rec, store, EventHub())


def alive(pid: int) -> bool:
    psutil = pytest.importorskip("psutil")
    return bool(psutil.pid_exists(pid))


def wait_gone(pid: int, seconds: float = 10) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.1)
    return not alive(pid)


# ------------------------------------------------------------------ interpreter, folder, environment


def test_interpreter_is_the_studio_venv_python_when_it_exists(tmp_home: Path):
    paths = Paths.from_env()
    assert studio_python(paths) == sys.executable  # CI and development: no studio venv
    exe = paths.venvs / "studio" / "Scripts" / "python.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert studio_python(paths) == str(exe)
    runner = StudioRunner(paths)
    cmd = runner.command("run", tmp_home / "a.json")
    assert cmd == [str(exe), "-m", ENTRY, "run", "--args", str(tmp_home / "a.json")]


def test_child_gets_the_app_folder_the_home_and_utf8(fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "supervisor-venv-site"))
    events: list[dict[str, Any]] = []
    done = runner_for(fake, tmp_path).run("run", {"channel": "after-dark", "fmt": "shorts_only", "count": 2},
                                          on_event=events.append)
    rec = recorded(tmp_path)
    assert rec["argv"][:4] == ["-m", ENTRY, "run", "--args"]
    assert Path(rec["cwd"]).resolve() == (tmp_path / "app").resolve()
    assert rec["env"]["NIGHTSHIFT_HOME"] == str(Paths.from_env().home)
    assert rec["env"]["PYTHONUTF8"] == "1" and rec["env"]["PYTHONIOENCODING"] == "utf-8"
    assert rec["env"]["PYTHONPATH"] is None  # the dashboard's own interpreter variables stay out
    assert rec["args"] == {"channel": "after-dark", "fmt": "shorts_only", "count": 2}
    assert Path(rec["args_file"]).parent == Paths.from_env().home / "tmp"
    assert not Path(rec["args_file"]).exists()  # removed when the call ends
    assert [e["ev"] for e in events] == ["job", "stage", "stage"]
    assert done["output_ids"] == ["after-dark-20261102-aaaa/s1"] and done["job_ids"] == ["after-dark-20261102-aaaa"]


def test_default_working_directory_is_the_app_checkout(tmp_home: Path):
    assert StudioRunner(Paths.from_env()).workdir() == repo_root()


def test_parse_event_ignores_everything_that_is_not_a_protocol_line():
    assert parse_event("NSJOB " + json.dumps({"ev": "stage", "stage": "voice"})) == {"ev": "stage", "stage": "voice"}
    assert parse_event("loading model...") is None
    assert parse_event("NSJOB not json") is None
    assert parse_event('NSJOB {"no_ev": 1}') is None


# ------------------------------------------------------------------ failures


def test_a_coded_failure_in_the_child_is_an_ns_error(fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_MODE", "coded")
    with pytest.raises(NsError) as err:
        runner_for(fake, tmp_path).run("run", {})
    assert err.value.code == "NS-E202" and "faster_whisper" in err.value.detail


def test_an_uncoded_failure_in_the_child_becomes_ns_e327(fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_MODE", "uncoded")
    with pytest.raises(NsError) as err:
        runner_for(fake, tmp_path).run("run", {})
    assert err.value.code == "NS-E327" and "KeyError: 'voice'" in err.value.detail
    assert err.value.definition.hint  # the Queue card shows the errors.json hint


def test_an_unknown_code_in_the_child_becomes_ns_e327_with_the_code_in_the_detail():
    err = failure_error({"ev": "error", "code": "NS-E999", "detail": "x", "message": "Boom: x"})
    assert isinstance(err, NsError) and err.code == "NS-E327" and "NS-E999" in err.detail


def test_a_crash_becomes_ns_e327_with_the_exit_code_and_the_last_output(fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_MODE", "crash")
    with pytest.raises(NsError, match=r"exit 3.*Traceback") as err:
        runner_for(fake, tmp_path).run("run", {})
    assert err.value.code == "NS-E327"


def test_a_missing_interpreter_is_a_plain_error(tmp_home: Path, tmp_path: Path):
    runner = StudioRunner(Paths.from_env(), python=str(tmp_path / "nope.exe"), cwd=tmp_path)
    with pytest.raises(RuntimeError, match="could not be started"):
        runner.run("run", {})


# ------------------------------------------------------------------ cancel kills the process tree


def test_cancel_kills_the_child_and_its_children(fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_MODE", "hang")
    seen: list[str] = []

    def cancel_after_first_stage() -> None:
        if seen:
            raise JobCancelled("j")

    with pytest.raises(JobCancelled):
        runner_for(fake, tmp_path).run(
            "run", {}, on_event=lambda e: seen.append(str(e["ev"])), check_cancel=cancel_after_first_stage
        )
    pids = json.loads((tmp_path / "pids.json").read_text(encoding="utf-8"))
    assert wait_gone(pids["child"])
    if sys.platform == "win32":  # taskkill /T takes the whole tree
        assert wait_gone(pids["grandchild"])
    else:  # no tree kill off Windows: clean up the stand-in ourselves
        import signal

        if alive(pids["grandchild"]):
            os.kill(pids["grandchild"], signal.SIGKILL)


def test_cancel_while_the_child_is_quiet_still_stops_it(fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_MODE", "hang")
    started = time.monotonic()

    def cancel_after_two_seconds() -> None:
        if time.monotonic() - started > 2:
            raise JobCancelled("j")

    with pytest.raises(JobCancelled):
        runner_for(fake, tmp_path).run("run", {}, check_cancel=cancel_after_two_seconds)
    pids = json.loads((tmp_path / "pids.json").read_text(encoding="utf-8"))
    assert wait_gone(pids["child"])
    if sys.platform != "win32" and alive(pids["grandchild"]):
        import signal

        os.kill(pids["grandchild"], signal.SIGKILL)


# ------------------------------------------------------------------ the adapters use the runner


def test_run_adapter_reports_stages_and_job_ids_through_the_job(fake: Path, tmp_path: Path):
    paths = Paths.from_env()
    store, ctl = make_ctl(paths)
    adapter = RealRunAdapter(
        paths, SupervisorLink("http://127.0.0.1:8099", timeout_s=0.2), fake=("llm", "tts"),
        runner=runner_for(fake, tmp_path),
    )
    ids = adapter.run(ctl, "after-dark", "shorts_only", 2)
    assert ids == ["after-dark-20261102-aaaa/s1"]
    assert ctl.extras["studio_job_ids"] == ["after-dark-20261102-aaaa"]
    rec = store.get(ctl.job_id)
    assert rec is not None and rec.message == "captions (2/2)"
    assert rec.progress == pytest.approx((0 + 1 / 2) / 2)  # stage 2 of 2 in job 1 of 2
    args = recorded(tmp_path)["args"]
    assert args["channel"] == "after-dark" and args["fmt"] == "shorts_only" and args["count"] == 2
    assert args["fake"] == ["llm", "tts"] and args["supervisor_url"] == "http://127.0.0.1:8099"


@pytest.mark.parametrize(
    ("mode", "code"), [("uncoded", "NS-E327"), ("crash", "NS-E327"), ("coded", "NS-E202")]
)
def test_the_job_record_of_a_failed_child_carries_a_code_and_a_hint(
    fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, code: str
):
    """An uncoded child error is stored as NS-E327 with the errors.json hint; a coded one is untouched."""
    monkeypatch.setenv("FAKE_MODE", mode)
    paths = Paths.from_env()
    store = JobStore(open_db(paths))
    adapter = RealRunAdapter(
        paths, SupervisorLink("http://127.0.0.1:9", timeout_s=0.2), runner=runner_for(fake, tmp_path)
    )

    def handler(ctl: JobControl) -> dict[str, Any]:
        adapter.run(ctl, "after-dark", "shorts_only", 1)
        return {}

    worker = JobWorker(store, {"run": handler}, EventHub(), NsLogger("dashboard", paths.logs))
    job = store.create("run", "after-dark", {})
    assert worker.run_next()
    rec = store.get(job.job_id)
    assert rec is not None and rec.status == "failed" and rec.result is not None
    assert rec.result["error"]["code"] == code
    assert rec.result["error"]["hint"] == describe(code).hint
    assert code in rec.message


def test_run_adapter_stops_the_child_when_the_owner_cancels(fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_MODE", "hang")
    paths = Paths.from_env()
    store, ctl = make_ctl(paths)
    adapter = RealRunAdapter(paths, SupervisorLink("http://127.0.0.1:9", timeout_s=0.2), runner=runner_for(fake, tmp_path))

    def cancel_once_the_child_runs() -> None:  # the owner presses Cancel while the stage is running
        deadline = time.monotonic() + 20
        while not (tmp_path / "pids.json").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        store.request_cancel(ctl.job_id)

    threading.Thread(target=cancel_once_the_child_runs, daemon=True).start()
    with pytest.raises(JobCancelled):
        adapter.run(ctl, "after-dark", "shorts_only", 1)
    pids = json.loads((tmp_path / "pids.json").read_text(encoding="utf-8"))
    assert wait_gone(pids["child"])
    if sys.platform != "win32" and alive(pids["grandchild"]):
        import signal

        os.kill(pids["grandchild"], signal.SIGKILL)


def test_clip_adapter_passes_the_options_and_notes_the_studio_job(fake: Path, tmp_path: Path):
    paths = Paths.from_env()
    _store, ctl = make_ctl(paths)
    adapter = RealClipAdapter(paths, SupervisorLink("http://127.0.0.1:9", timeout_s=0.2), runner=runner_for(fake, tmp_path))
    ids = adapter.clip(ctl, paths.home / "talk.mp4", "src-1", {"template": "podcast_single", "caption_style": "boxed_dark"})
    assert ids == ["after-dark-20261102-aaaa/s1"]
    assert ctl.extras["studio_job_id"] == "after-dark-20261102-aaaa" and "studio_job_ids" not in ctl.extras
    args = recorded(tmp_path)["args"]
    assert args["source_path"] == str(paths.home / "talk.mp4")
    assert args["params"]["channel"] == "after-dark" and args["params"]["caption_style"] == "boxed_dark"
    assert recorded(tmp_path)["argv"][2] == "clip"


def test_series_adapter_runs_the_part_in_the_child(fake: Path, tmp_path: Path):
    paths = Paths.from_env()
    store = JobStore(open_db(paths))
    rec = store.create("series_part", "after-dark", {})
    ctl = JobControl(rec, store, EventHub())
    adapter = RealSeriesAdapter(paths, SupervisorLink("http://127.0.0.1:9", timeout_s=0.2), runner=runner_for(fake, tmp_path))
    ids = adapter.produce(ctl, {"story_slug": "the-lift", "part_n": 2})
    assert ids == ["after-dark-20261102-aaaa/s1"]
    args = recorded(tmp_path)["args"]
    assert args["params"] == {"story_slug": "the-lift", "part_n": 2, "job_id": ctl.job_id}
    assert recorded(tmp_path)["argv"][2] == "series"


def test_link_download_runs_in_the_child_and_cancel_removes_the_partial_folder(
    fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_dashboard import studio_bridge

    paths = Paths.from_env()
    _store, ctl = make_ctl(paths)
    adapter = RealClipAdapter(paths, SupervisorLink("http://127.0.0.1:9", timeout_s=0.2), runner=runner_for(fake, tmp_path))
    assert adapter.fetch_url(ctl, "https://example.com/v", "src-20261101-aaaa") == Path("x.mp4")
    assert recorded(tmp_path)["argv"][2] == "download"
    assert recorded(tmp_path)["args"]["url"] == "https://example.com/v"
    # the owner cancels during the download: yt-dlp dies with the child, the half-file folder is deleted
    monkeypatch.setenv("FAKE_MODE", "hang")
    partial = studio_bridge.download_dir(paths, "src-20261101-bbbb")
    partial.mkdir(parents=True)
    (partial / "video.mp4.part").write_bytes(b"x" * 100)
    _store2, ctl2 = make_ctl(paths)
    (tmp_path / "pids.json").unlink(missing_ok=True)

    def cancel_once_the_child_runs() -> None:
        deadline = time.monotonic() + 20
        while not (tmp_path / "pids.json").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        _store2.request_cancel(ctl2.job_id)

    threading.Thread(target=cancel_once_the_child_runs, daemon=True).start()
    with pytest.raises(JobCancelled):
        adapter.fetch_url(ctl2, "https://example.com/v", "src-20261101-bbbb")
    assert not partial.exists()
    pids = json.loads((tmp_path / "pids.json").read_text(encoding="utf-8"))
    assert wait_gone(pids["child"])
    if sys.platform != "win32" and alive(pids["grandchild"]):
        import signal

        os.kill(pids["grandchild"], signal.SIGKILL)


# ------------------------------------------------------------------ voice clone: whisper runs in the studio venv


def test_clone_transcribes_in_the_child_while_holding_the_whisper_tenant(fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_MODE", "text")
    events: list[tuple[str, str]] = []

    class Acq:
        def acquire(self, service: str, owner: str, **_kw: Any) -> Any:
            events.append(("acquire", service))
            return type("E", (), {"base_url": "http://127.0.0.1:1"})()

        def release(self, service: str, owner: str) -> None:
            events.append(("release", service))

    maker = RealVoiceMaker(
        Paths.from_env(), SupervisorLink("http://127.0.0.1:9", timeout_s=0.2),
        acquirer=Acq(),  # type: ignore[arg-type]
        runner=runner_for(fake, tmp_path),
    )
    assert maker._words_in(tmp_path / "me.wav") == "this is what I said"
    assert events == [("acquire", "whisper"), ("release", "whisper")]
    rec = recorded(tmp_path)
    assert rec["argv"][2] == "transcribe" and rec["args"] == {"wav": str(tmp_path / "me.wav")}


# ------------------------------------------------------------------ the child side (`python -m ns_dashboard.studio_job`)


def lines_of(out: str) -> list[dict[str, Any]]:
    return [e for e in (parse_event(line) for line in out.splitlines()) if e is not None]


def test_studio_job_main_prints_events_then_the_result(tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    from ns_dashboard import studio_bridge

    def fake_run(channel: str, fmt: str, count: int | None, report: Any, **kw: Any) -> list[str]:
        kw["seen"].append("job-1")
        report("voice", 0, 3, 0, 1)
        assert kw["fake"] == ("llm",) and channel == "after-dark" and fmt == "shorts_only" and count == 1
        return ["job-1/s1"]

    monkeypatch.setattr(studio_bridge, "run_story_jobs", fake_run)
    args = tmp_path / "args.json"
    args.write_text(json.dumps({"channel": "after-dark", "fmt": "shorts_only", "count": 1, "fake": ["llm"]}), encoding="utf-8")
    assert studio_job.main(["run", "--args", str(args)]) == 0
    events = lines_of(capsys.readouterr().out)
    assert [e["ev"] for e in events] == ["job", "stage", "result"]
    assert events[1]["stage"] == "voice" and events[2]["output_ids"] == ["job-1/s1"] and events[2]["job_ids"] == ["job-1"]


def test_studio_job_main_reports_a_coded_failure(tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    from ns_dashboard import studio_bridge

    def boom(*_a: Any, **_kw: Any) -> list[str]:
        raise NsError("NS-E306", "no stages for series_compilation")

    monkeypatch.setattr(studio_bridge, "run_story_jobs", boom)
    args = tmp_path / "args.json"
    args.write_text(json.dumps({"channel": "c", "fmt": "series_compilation"}), encoding="utf-8")
    assert studio_job.main(["run", "--args", str(args)]) == 1
    (event,) = lines_of(capsys.readouterr().out)
    assert event["ev"] == "error" and event["code"] == "NS-E306" and "series_compilation" in event["detail"]


def test_studio_job_main_reports_other_failures_without_a_code(tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    from ns_dashboard import studio_bridge

    def boom(*_a: Any, **_kw: Any) -> list[str]:
        raise ValueError("need 5 <= min_s < max_s <= 179")

    monkeypatch.setattr(studio_bridge, "run_clip", boom)
    args = tmp_path / "args.json"
    args.write_text(json.dumps({"source_path": "a.mp4", "params": {}}), encoding="utf-8")
    assert studio_job.main(["clip", "--args", str(args)]) == 1
    (event,) = lines_of(capsys.readouterr().out)
    assert event["code"] is None and event["message"] == "ValueError: need 5 <= min_s < max_s <= 179"


# ------------------------------------------------------------------ cancel reaches the Clipper CancelToken


def test_cancel_asks_the_download_child_to_stop_before_the_tree_is_killed(
    fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Cancel creates the flag file; the child cancels its `CancelToken`, ends by itself, then the kill backstop runs."""
    monkeypatch.setenv("FAKE_MODE", "obey_cancel_file")
    paths = Paths.from_env()
    store, ctl = make_ctl(paths)
    states: list[bool] = []

    def record_kill(proc: Any) -> None:
        states.append(proc.poll() is not None)  # True: the child was already gone when the backstop ran
        proc.kill()

    adapter = RealClipAdapter(
        paths,
        SupervisorLink("http://127.0.0.1:9", timeout_s=0.2),
        runner=runner_for(fake, tmp_path, kill=record_kill, cancel_grace_s=10),
    )
    def cancel_once_the_child_runs() -> None:
        deadline = time.monotonic() + 20
        while not (tmp_path / "record.json").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        store.request_cancel(ctl.job_id)

    threading.Thread(target=cancel_once_the_child_runs, daemon=True).start()
    with pytest.raises(JobCancelled):
        adapter.fetch_url(ctl, "https://example.com/v", "src-20261101-cccc")
    assert (tmp_path / "obeyed.txt").read_text(encoding="utf-8") == "stopped by itself"
    assert states == [True]
    assert recorded(tmp_path)["args"]["cancel_file"].endswith("cancel-download-src-20261101-cccc")
    assert not Path(recorded(tmp_path)["args"]["cancel_file"]).exists()  # tidied up


def test_runner_without_on_cancel_kills_at_once(fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_MODE", "hang")
    seen: list[bool] = []

    def record_kill(proc: Any) -> None:
        seen.append(proc.poll() is None)
        proc.kill()

    class Stop(Exception):
        pass

    def stop() -> None:
        if (tmp_path / "pids.json").exists():
            raise Stop

    with pytest.raises(Stop):
        runner_for(fake, tmp_path, kill=record_kill, cancel_grace_s=30).run("run", {}, check_cancel=stop)
    assert seen == [True]  # still running when killed: no grace wait without on_cancel


def test_watch_cancel_file_cancels_the_token_when_the_flag_appears(tmp_path: Path):
    flag = tmp_path / "flag"
    stop = threading.Event()

    class Token:
        cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    token = Token()
    thread = studio_job.watch_cancel_file(str(flag), token, stop, poll_s=0.01)
    assert thread is not None
    time.sleep(0.05)
    assert not token.cancelled
    flag.touch()
    thread.join(5)
    assert token.cancelled
    assert studio_job.watch_cancel_file(None, token, stop) is None


def test_download_kind_cancels_the_real_cancel_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`do_download` hands `download_source` a real `CancelToken` that the flag file cancels."""
    from ns_dashboard import studio_bridge
    from ns_studio.clipper.source import CancelToken

    flag = tmp_path / "flag"
    got: dict[str, Any] = {}

    def download_source(paths: Any, url: str, source_id: str, on_progress: Any, cancel_token: Any = None) -> Path:
        got["token"] = cancel_token
        flag.touch()  # the dashboard pressed Cancel
        assert cancel_token is not None
        end = time.monotonic() + 5
        while not cancel_token.cancelled and time.monotonic() < end:
            time.sleep(0.01)
        got["cancelled"] = cancel_token.cancelled
        return tmp_path / "x.mp4"

    monkeypatch.setattr(studio_bridge, "download_source", download_source)
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path / "home"))
    studio_job.do_download({"url": "https://example.com/v", "source_id": "s", "cancel_file": str(flag)})
    assert isinstance(got["token"], CancelToken) and got["cancelled"] is True


# ------------------------------------------------ D-25: the hint a check attached crosses the process boundary
REAL_CHILD = r'''
import sys

sys.path.insert(0, sys.argv[sys.argv.index("--repo") + 1])
from ns_common.errors import NsError
from ns_dashboard import studio_job


def refuse(_args):
    raise NsError("NS-E605", "self_similarity: finished video exists", hint="Change the script, or use that video.")


studio_job.HANDLERS["run"] = refuse
sys.exit(studio_job.main(["run", "--args", sys.argv[sys.argv.index("--args") + 1]]))
'''


def real_child_runner(tmp_path: Path) -> StudioRunner:
    """A runner whose child is a real interpreter running the real `studio_job.main`, with one failing handler."""
    script = tmp_path / "real_child.py"
    script.write_text(REAL_CHILD, encoding="utf-8")
    app = tmp_path / "app"
    app.mkdir(exist_ok=True)
    return StudioRunner(
        Paths.from_env(), python=[sys.executable, str(script), "--repo", str(repo_root())], cwd=app, poll_s=0.05
    )


def test_a_hint_attached_by_a_check_survives_the_child_process(tmp_home: Path, tmp_path: Path):
    from ns_studio.errors import hint_of

    with pytest.raises(NsError) as err:
        real_child_runner(tmp_path).run("run", {})
    assert err.value.code == "NS-E605"
    assert hint_of(err.value) == "Change the script, or use that video."
    assert "new idea" in err.value.definition.hint  # errors.json is unchanged


def test_the_failed_job_of_a_script_run_shows_the_script_hint_after_the_child(tmp_home: Path, tmp_path: Path):
    """The Queue card: owner script refused by NS-E605 in the child; the job's stored hint is the script one."""
    paths = Paths.from_env()
    store = JobStore(open_db(paths))
    adapter = RealRunAdapter(
        paths, SupervisorLink("http://127.0.0.1:9", timeout_s=0.2), runner=real_child_runner(tmp_path)
    )

    def handler(ctl: JobControl) -> dict[str, Any]:
        adapter.run(ctl, "after-dark", "shorts_only", 1)
        return {}

    worker = JobWorker(store, {"run": handler}, EventHub(), NsLogger("dashboard", paths.logs))
    job = store.create("run", "after-dark", {})
    assert worker.run_next()
    rec = store.get(job.job_id)
    assert rec is not None and rec.status == "failed" and rec.result is not None
    assert rec.result["error"]["code"] == "NS-E605"
    assert rec.result["error"]["hint"] == "Change the script, or use that video."
    assert "new idea" not in rec.result["error"]["hint"]


def test_a_coded_error_without_a_hint_keeps_the_errors_json_hint():
    err = failure_error({"ev": "error", "code": "NS-E605", "detail": "x", "message": "m"})
    assert err.code == "NS-E605" and "hint" not in err.data
