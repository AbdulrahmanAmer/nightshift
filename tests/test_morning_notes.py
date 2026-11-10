# ruff: noqa: E501
"""Card FN-2: the morning notes have one owner (`ns_supervisor.notify`), one lock, and a dismiss by id.

Every test enters where the product does: `notify.leave_note` (what every Python writer calls), the dashboard's
real routes `GET /api/notes` and `POST /api/notes/clear`, a second Python process, and the real
`installer/launch.ps1` under Windows PowerShell 5.1. A step that must be slow is stopped for real (a writer waits
on an `Event` between its read and its write). Every process a test starts is stopped by its pid on every path.
Nothing here touches this PC: the home, the Desktop folder and the lock are under `tmp_path`.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_common.singleton import FileLock
from ns_supervisor import notify, pwsh, schedule
from tests.test_dashboard_api import make_env

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "installer" / "launch.ps1"
HARD = "Jo's Nightshift (2)"
TODAY = date.today().isoformat()
needs_powershell = pytest.mark.skipif(pwsh.find_powershell() is None, reason="powershell not on PATH")
#: On the base there is no such class; `OSError` keeps the test going to its assertion.
NotesBusy: type[OSError] = getattr(notify, "NotesBusy", OSError)
HAS_ONCE = "once" in inspect.signature(notify.leave_note).parameters


def lock_file(paths: Paths) -> Path:
    return paths.home / "run" / "morning_note.lock"


def notes_file(paths: Paths) -> Path:
    return paths.home / "run" / "morning_note.json"


def spooled(paths: Paths) -> list[Path]:
    """The notes waiting in the spool (complete files only)."""
    folder = paths.home / "run" / "morning_note.spool"
    return sorted(folder.glob("*.json")) if folder.is_dir() else []


def log_records(paths: Paths) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in sorted(paths.logs.glob("*.jsonl")) if paths.logs.is_dir() else []:
        out += [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
    return out


def messages(paths: Paths) -> list[str]:
    """What the file holds, read as 0.1.14 reads it (plain UTF-8, `notes`, dicts)."""
    raw = notes_file(paths).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "0.1.14 reads plain UTF-8: a byte order mark would hide every note"
    return [n["message"] for n in json.loads(raw.decode("utf-8"))["notes"]]


class Paused:
    """Writer 1: a real `notify.leave_note` stopped between its read and its write until `finish()`.

    The owner evaluates `once` between the read and the write, under its lock, so a `once` that waits is that
    pause. The base has no `once`: there the pause sits behind its read (`notify.read_notes`), for this
    thread only.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, paths: Paths, message: str) -> None:
        self.reached, self.go = threading.Event(), threading.Event()
        self.errors: list[BaseException] = []

        def wait_here() -> None:
            self.reached.set()
            assert self.go.wait(120), "the test never let writer 1 go on"

        if HAS_ONCE:
            def once(_notes: Any) -> bool:
                wait_here()
                return False

            def call() -> None:
                notify.leave_note(paths, "Nightshift", message, once=once)  # pyright: ignore[reportCallIssue]
        else:
            real = notify.read_notes

            def slow_read(p: Paths) -> Any:
                got = real(p)
                if threading.current_thread() is self.thread:
                    wait_here()
                return got

            monkeypatch.setattr(notify, "read_notes", slow_read)

            def call() -> None:
                notify.leave_note(paths, "Nightshift", message)

        def run() -> None:
            try:
                call()
            except BaseException as e:  # noqa: BLE001 - handed to the test's thread
                self.errors.append(e)

        self.thread = threading.Thread(target=run, daemon=True)

    def start(self) -> Paused:
        self.thread.start()
        assert self.reached.wait(60), f"writer 1 never reached its pause: {self.errors}"
        return self

    def finish(self) -> None:
        self.go.set()
        if self.thread.is_alive() or self.thread.ident is not None:
            self.thread.join(120)
        assert not self.thread.is_alive()
        if self.errors:
            raise self.errors[0]


@contextlib.contextmanager
def started(argv: list[str], **kw: Any) -> Iterator[subprocess.Popen[str]]:
    """A child process that is stopped by its pid whatever the test does."""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")]), **kw.pop("env", {})}
    proc = subprocess.Popen(  # noqa: S603
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(ROOT),
        env=env, **kw,
    )
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired, ValueError, OSError):
            proc.communicate(timeout=30)


def wait_for(path: Path, what: str, proc: subprocess.Popen[str] | None = None, seconds: float = 90) -> None:
    end = time.monotonic() + seconds
    while not path.exists():
        gone = proc is not None and proc.poll() is not None
        assert not gone and time.monotonic() < end, f"{what}: {proc.communicate() if gone and proc else 'timed out'}"
        time.sleep(0.02)


PY_WRITER = (
    "import sys; from pathlib import Path; from ns_common.paths import Paths; from ns_supervisor import notify; "
    "Path(sys.argv[2]).write_text('up', encoding='ascii'); "
    "notify.leave_note(Paths(Path(sys.argv[1])), 'Nightshift', sys.argv[3])"
)
#: A writer that is inside its read-modify-write (the lock taken, the file read) and stays there: to be killed.
PY_STUCK = (
    "import sys, time; from pathlib import Path; from ns_common.paths import Paths; from ns_supervisor import notify; "
    "notify.leave_note(Paths(Path(sys.argv[1])), 'Nightshift', 'never written', "
    "once=lambda notes: (Path(sys.argv[2]).write_text('in', encoding='ascii'), time.sleep(300)) and False)"
)


# ------------------------------------------------------------------------------------- the stub's side
def stub_home(tmp_path: Path) -> tuple[Path, Path]:
    """An install home with the launch script beside the program folder, and a folder that stands for the Desktop."""
    home, desk = tmp_path / HARD, tmp_path / "Desk"
    for name in ("config", "run", "logs", "app/ns_studio"):
        (home / name).mkdir(parents=True)
    (home / "app" / "ns_studio" / "cli.py").write_text("", encoding="ascii")
    (home / "nightshift-launch.ps1").write_bytes(STUB.read_bytes())
    desk.mkdir()
    return home, desk


def missed(home: Path, first: str) -> Path:
    record = home / "run" / "launch-missed.json"
    record.write_text(json.dumps({"first": first, "last": first, "nights": 2, "what": "folder"}), encoding="ascii")
    return record


def stub_said(first: str) -> str:
    return (
        f"Nightshift did not run on 2 nights, from {first} to {first}: its program folder was missing until it "
        f"was put back on {TODAY}."
    )


def stub_argv(home: Path, desk: Path) -> list[str]:
    exe = pwsh.find_powershell()
    assert exe is not None
    script = str(home / "nightshift-launch.ps1")
    return [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-Cleanup", "-DesktopDir", str(desk)]


def stub_cleanup(home: Path, desk: Path) -> str:
    """The engine's start: `nightshift-launch.ps1 -Cleanup`, which prints what it did."""
    with started(stub_argv(home, desk)) as proc:
        out, err = proc.communicate(timeout=180)
    assert proc.returncode == 0, out + err
    return out.strip()


def stub_function(name: str) -> str:
    """The text of one function of the launch script (each ends with a brace at the start of a line)."""
    text = STUB.read_text(encoding="ascii")
    found = re.search(rf"^function {re.escape(name)} \{{.*?^\}}\r?$", text, re.MULTILINE | re.DOTALL)
    return found.group(0) if found else ""


def stub_holder(tmp_path: Path) -> Path | None:
    """A stand-in script that takes the notes lock with the launch script's own function and keeps it until
    told (a file appears) or for 120 seconds. None when the launch script has no such function."""
    helper = stub_function("Open-NsNotesLock")
    if not helper:
        return None
    script = tmp_path / "hold-notes-lock.ps1"
    script.write_text(
        "\r\n".join([
            "param([string]$RunDir, [string]$Held, [string]$Release)",
            "Set-StrictMode -Version Latest",
            "$ErrorActionPreference = 'Stop'",
            helper,
            "$lock = Open-NsNotesLock -RunDir $RunDir",
            "if ($null -eq $lock) { exit 3 }",
            "[IO.File]::WriteAllText($Held, 'held')",
            "$until = [DateTime]::UtcNow.AddSeconds(120)",
            "while (-not (Test-Path -LiteralPath $Release) -and [DateTime]::UtcNow -lt $until) { Start-Sleep -Milliseconds 50 }",
            "$lock.Dispose()",
            "exit 0",
            "",
        ]),
        encoding="ascii",
    )
    return script


def holder_argv(script: Path, paths: Paths, held: Path, release: Path) -> list[str]:
    exe = pwsh.find_powershell()
    assert exe is not None
    return [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), str(paths.home / "run"), str(held), str(release)]


# ----------------------------------------------------------------------- 1, 7: the bell's two routes
def test_a_note_written_after_the_page_loaded_survives_mark_all_read(tmp_home: Path) -> None:
    client, state = make_env(tmp_home)
    paths = state.paths
    notify.leave_note(paths, "Nightshift", "A", now=datetime(2026, 11, 10, 3, 0, 1))
    notify.leave_note(paths, "Nightshift", "B", now=datetime(2026, 11, 10, 3, 0, 2))
    shown = client.get("/api/notes").json()
    assert [n["message"] for n in shown] == ["A", "B"]
    assert all(set(n) == {"id", "at", "title", "message"} and len(n["id"]) == 16 for n in shown), shown
    notify.leave_note(paths, "Nightshift", "C", now=datetime(2026, 11, 10, 3, 0, 3))     # the page is still open
    r = client.post("/api/notes/clear", json={"ids": [n["id"] for n in shown]})
    assert r.status_code == 200 and r.json() == {"ok": True, "removed": 2, "left": 1}
    assert [n["message"] for n in client.get("/api/notes").json()] == ["C"]
    # another tab presses Mark all read with the same list: those notes are gone already, and C stays
    again = client.post("/api/notes/clear", json={"ids": [n["id"] for n in shown]})
    assert again.status_code == 200 and again.json() == {"ok": True, "removed": 0, "left": 1}
    (c,) = client.get("/api/notes").json()
    assert client.post("/api/notes/clear", json={"ids": [c["id"]]}).json() == {"ok": True, "removed": 1, "left": 0}
    assert client.get("/api/notes").json() == [] and not notes_file(paths).exists()


@pytest.mark.parametrize("body", [None, {}, {"ids": "all"}, {"ids": [1, 2]}, {"ids": None}])
def test_clear_without_ids_changes_nothing(tmp_home: Path, body: Any) -> None:
    """An old page bundle in a tab that was open during the update posts no ids: it cannot wipe the notes."""
    client, state = make_env(tmp_home)
    notify.leave_note(state.paths, "Nightshift", "still unread")
    before = notes_file(state.paths).read_bytes()
    r = client.post("/api/notes/clear") if body is None else client.post("/api/notes/clear", json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_options", r.text
    assert notes_file(state.paths).read_bytes() == before
    empty = client.post("/api/notes/clear", json={"ids": []})          # nothing was shown: nothing to remove
    assert empty.status_code == 200 and empty.json() == {"ok": True, "removed": 0, "left": 1}
    assert notes_file(state.paths).read_bytes() == before


def test_a_held_lock_never_hides_the_notes_and_a_dismiss_then_says_busy(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer that holds the lock for long (or hangs): the owner still reads every note; Mark all read is
    refused in words, with nothing removed, and works once the holder is gone."""
    client, state = make_env(tmp_home)
    paths = state.paths
    notify.leave_note(paths, "Nightshift", "readable while locked")
    before = notes_file(paths).read_bytes()
    monkeypatch.setattr(notify, "NOTES_REQUEST_WAIT_S", 0.3, raising=False)
    held = FileLock(lock_file(paths))
    held.acquire()
    try:
        began = time.monotonic()
        (shown,) = client.get("/api/notes").json()
        assert shown["message"] == "readable while locked" and time.monotonic() - began < 2
        r = client.post("/api/notes/clear", json={"ids": [shown.get("id", "")]})
        assert notes_file(paths).exists() and notes_file(paths).read_bytes() == before
        assert r.status_code == 503 and r.json()["error"]["code"] == "notes_busy", r.text
        assert "Mark all read" in r.json()["error"]["hint"] and "nothing was removed" in r.json()["error"]["message"]
    finally:
        held.release()
    assert client.post("/api/notes/clear", json={"ids": [shown["id"]]}).json() == {"ok": True, "removed": 1, "left": 0}


# ------------------------------------------------------------------------ 2, 3: two writers at once
def test_two_writers_at_once_keep_both_notes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = Paths(tmp_path / "home")
    # another thread of this process (the engine's reach thread beside its start)
    one = Paused(monkeypatch, paths, "writer 1").start()
    two = threading.Thread(target=lambda: notify.leave_note(paths, "Nightshift", "writer 2, a thread"), daemon=True)
    try:
        two.start()
        two.join(1.0)                 # the base: it wrote and returned; now: it waits for writer 1's lock
    finally:
        one.finish()
    two.join(60)
    assert not two.is_alive()
    assert sorted(messages(paths)) == ["writer 1", "writer 2, a thread"]
    # another process (a studio child, the dashboard, `ns verify`), started with this venv's Python
    one = Paused(monkeypatch, paths, "writer 1 again").start()
    up = tmp_path / "writer-2-up"
    with started([sys.executable, "-c", PY_WRITER, str(paths.home), str(up), "writer 2, a process"]) as proc:
        try:
            wait_for(up, "the second process never came up", proc)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=1.5)                                 # as above
        finally:
            one.finish()
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, out + err
    assert sorted(messages(paths)) == ["writer 1", "writer 1 again", "writer 2, a process", "writer 2, a thread"]


def test_the_once_check_and_the_write_are_one_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two schedule checks (the engine's and `ns verify`'s) find the task switched off at the same moment:
    NS-W207 is said once. The first is stopped between deciding and writing."""
    paths = Paths(tmp_path / "home")
    log = NsLogger("supervisor", paths.logs, echo=False)
    real = notify.leave_note
    reached, go = threading.Event(), threading.Event()
    first: list[threading.Thread] = []

    def slow_leave(*a: Any, **kw: Any) -> Any:
        if threading.current_thread() in first:
            reached.set()
            assert go.wait(120)
        return real(*a, **kw)

    monkeypatch.setattr(notify, "leave_note", slow_leave)
    one = threading.Thread(target=lambda: schedule._note_switched_off_outside(paths, log), daemon=True)
    first.append(one)
    one.start()
    try:
        assert reached.wait(60)
        schedule._note_switched_off_outside(paths, log)        # the second caller, start to end
    finally:
        go.set()
        one.join(120)
    said = [m for m in messages(paths) if m.startswith("NS-W207:")]
    assert len(said) == 1, said
    # and inside the owner: a writer that is deciding holds the lock, so the next one decides on what it wrote
    monkeypatch.setattr(notify, "leave_note", real)
    same = "NS-W208: said once"

    def waiting(notes: Any) -> bool:
        return any(n["message"] == same for n in notes)

    deciding, written = threading.Event(), threading.Event()

    def slow_once(notes: Any) -> bool:
        deciding.set()
        written.wait(120)
        return waiting(notes)

    answers: list[Any] = []
    a = threading.Thread(target=lambda: answers.append(notify.leave_note(paths, "Nightshift", same, once=slow_once)), daemon=True)  # pyright: ignore[reportCallIssue]
    b = threading.Thread(target=lambda: answers.append(notify.leave_note(paths, "Nightshift", same, once=waiting)), daemon=True)  # pyright: ignore[reportCallIssue]
    a.start()
    try:
        assert deciding.wait(60)
        b.start()
        b.join(0.5)
        assert b.is_alive(), "the second writer decided while the first was between its check and its write"
    finally:
        written.set()
    a.join(60)
    b.join(60)
    assert sorted(answers) == [False, True] and messages(paths).count(same) == 1


def test_a_writer_that_fails_inside_its_write_releases_the_lock(tmp_path: Path) -> None:
    """Every way out of the read-modify-write lets go: a `once` that raises, a file that cannot be written."""
    paths = Paths(tmp_path / "home")
    notify.leave_note(paths, "Nightshift", "before")
    before = notes_file(paths).read_bytes()

    def broken(_notes: Any) -> bool:
        raise RuntimeError("the caller's own check failed")

    with pytest.raises(RuntimeError):
        notify.leave_note(paths, "Nightshift", "never", once=broken)  # pyright: ignore[reportCallIssue]
    assert notes_file(paths).read_bytes() == before
    began = time.monotonic()
    assert notify.leave_note(paths, "Nightshift", "after") is True and time.monotonic() - began < 2
    # the folder of the notes cannot be written (here: a file stands where `run` should be): said as an
    # OSError, which every caller logs as morning_note_failed, and nothing else is touched
    other = Paths(tmp_path / "blocked")
    other.home.mkdir()
    (other.home / "run").write_text("not a folder", encoding="ascii")
    with pytest.raises(OSError):
        notify.leave_note(other, "Nightshift", "nowhere to go")
    assert notify.read_notes(other) == [] and (other.home / "run").read_text(encoding="ascii") == "not a folder"
    # neither the notes nor their spool can take it: the log line, level ERROR, keeps its text
    (line,) = [r for r in log_records(other) if r["event"] == "morning_note_failed"]
    assert line["level"] == "ERROR" and line["data"]["message"] == "nowhere to go" and line["data"]["spooled"] is None


# --------------------------------------------------------------- 4, 5: the launch script and Python
@needs_powershell
def test_the_launch_stub_waits_for_the_lock_and_keeps_both(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, desk = stub_home(tmp_path)
    paths = Paths(home)
    record = missed(home, "2026-11-08")
    one = Paused(monkeypatch, paths, "from Python").start()
    with started(stub_argv(home, desk)) as proc:
        try:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=3)             # the base: it wrote and left; now: it waits (its bound is 5 s)
        finally:
            one.finish()
        out, err = proc.communicate(timeout=180)
    assert proc.returncode == 0, out + err
    # Python's note first: the script wrote after Python released, on top of what Python wrote
    assert messages(paths) == ["from Python", stub_said("2026-11-08")]
    assert out.strip() == "cleared" and not record.exists()
    # held past the script's bound: no note, the count kept and said; the next start writes it
    record = missed(home, "2026-10-20")
    one = Paused(monkeypatch, paths, "a slow writer").start()
    try:
        began = time.monotonic()
        result = stub_cleanup(home, desk)
        assert result.startswith("kept: ") and "notes" in result, result
        assert time.monotonic() - began >= 4.5 and record.exists()
        assert stub_said("2026-10-20") not in messages(paths)
    finally:
        one.finish()
    assert stub_cleanup(home, desk) == "cleared" and not record.exists()
    assert messages(paths) == ["from Python", stub_said("2026-11-08"), "a slow writer", stub_said("2026-10-20")]
    assert stub_cleanup(home, desk) == "nothing"


@needs_powershell
def test_the_stub_and_python_agree_on_the_lock(tmp_path: Path) -> None:
    """The other direction: PowerShell holds, Python is refused within its bound and writes nothing."""
    paths = Paths(tmp_path / "home")
    notify.leave_note(paths, "Nightshift", "before")
    before = notes_file(paths).read_bytes()
    script = stub_holder(tmp_path)
    assert script is not None, "the launch script has no lock helper (Open-NsNotesLock)"
    held, release = tmp_path / "held", tmp_path / "release"
    with started(holder_argv(script, paths, held, release)) as proc:
        wait_for(held, "the PowerShell holder never took the lock", proc)
        began = time.monotonic()
        with pytest.raises(NotesBusy):
            notify.leave_note(paths, "Nightshift", "refused", wait_s=0.5, spool=False)  # pyright: ignore[reportCallIssue]
        assert 0.4 <= time.monotonic() - began < 3 and notes_file(paths).read_bytes() == before
        assert [n["message"] for n in notify.read_notes(paths)] == ["before"]      # a reader takes no lock
        release.write_text("go", encoding="ascii")
        assert proc.wait(timeout=60) == 0
    assert notify.leave_note(paths, "Nightshift", "after") is True
    assert messages(paths) == ["before", "after"]


@needs_powershell
def test_a_writer_killed_while_it_holds_the_lock_blocks_nobody(tmp_path: Path) -> None:
    """The lock is the operating system's: it goes with the dead holder's handle. The file it leaves behind
    (`morning_note.lock`) is not the lock, and the half-done write changed nothing."""
    home, desk = stub_home(tmp_path)
    paths = Paths(home)
    notify.leave_note(paths, "Nightshift", "before")
    # a PowerShell holder is killed: Python writes at once
    script = stub_holder(tmp_path)
    assert script is not None, "the launch script has no lock helper (Open-NsNotesLock)"
    held = tmp_path / "held"
    with started(holder_argv(script, paths, held, tmp_path / "never")) as proc:
        wait_for(held, "the PowerShell holder never took the lock", proc)
        with pytest.raises(NotesBusy):
            notify.leave_note(paths, "Nightshift", "refused", wait_s=0.2, spool=False)  # pyright: ignore[reportCallIssue]
        proc.kill()
        proc.wait(timeout=30)
        began = time.monotonic()
        assert notify.leave_note(paths, "Nightshift", "after the PowerShell holder died") is True
        assert time.monotonic() - began < 2 and lock_file(paths).exists()
    # a Python writer is killed between its read and its write: the launch script writes, nothing was lost
    inside = tmp_path / "inside"
    record = missed(home, "2026-11-08")
    with started([sys.executable, "-c", PY_STUCK, str(home), str(inside)]) as proc:
        wait_for(inside, "the Python writer never got inside its write", proc)
        with pytest.raises(NotesBusy):
            notify.leave_note(paths, "Nightshift", "refused", wait_s=0.2, spool=False)  # pyright: ignore[reportCallIssue]
        proc.kill()
        proc.wait(timeout=30)
        assert stub_cleanup(home, desk) == "cleared" and not record.exists()
    assert messages(paths) == ["before", "after the PowerShell holder died", stub_said("2026-11-08")]
    assert notify.leave_note(paths, "Nightshift", "and Python again") is True


# ------------------------------------------------------------------- a reader while a write is half done
def test_a_reader_never_sees_half_a_file(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    stop, seen, bad = threading.Event(), [0], []

    def read() -> None:
        while not stop.is_set():
            try:
                raw = json.loads(notes_file(paths).read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except PermissionError:        # Windows, at the instant of the swap: the product's reader retries
                continue
            except ValueError as e:
                bad.append(e)
                return
            if len(raw["notes"]) < seen[0]:
                bad.append(AssertionError(f"{len(raw['notes'])} notes after {seen[0]}"))
            seen[0] = len(raw["notes"])

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        for i in range(40):
            notify.leave_note(paths, "Nightshift", f"note {i} " + "x" * 400)
    finally:
        stop.set()
        reader.join(30)
    assert bad == [] and len(notify.read_notes(paths)) == 40
    assert [p.name for p in notes_file(paths).parent.iterdir() if p.name.endswith(".tmp")] == []


# ---------------------------------------------------------- 6: a file left by 0.1.14, rc3 or the stub
OLD_FILE = (
    '{\r\n    "notes":  [\r\n                  {\r\n                      "at":  "2026-11-08T03:31:07",\r\n'
    '                      "title":  "Nightshift",\r\n                      "message":  "2 Shorts ready for review"\r\n'
    "                  },\r\n                  {\r\n"
    '                      "at":  "2026-11-09T03:30:00",\r\n                      "title":  "Nightshift",\r\n'
    '                      "message":  "NS-W207: Automatic runs were switched off outside Nightshift."\r\n'
    "                  }\r\n              ]\r\n}\r\n"
)


def test_a_dismiss_by_id_of_a_note_left_by_0_1_14_works(tmp_home: Path) -> None:
    client, state = make_env(tmp_home)
    paths = state.paths
    notes_file(paths).parent.mkdir(parents=True, exist_ok=True)
    notes_file(paths).write_bytes(OLD_FILE.encode("utf-8"))       # no ids: 0.1.14's and the launch script's shape
    first, second = client.get("/api/notes").json(), client.get("/api/notes").json()
    assert [n["message"] for n in first] == ["2 Shorts ready for review", "NS-W207: Automatic runs were switched off outside Nightshift."]
    assert all(n.get("id") for n in first) and first == second and first[0]["id"] != first[1]["id"]
    assert notes_file(paths).read_bytes() == OLD_FILE.encode("utf-8"), "reading a good file writes nothing"
    assert [n["id"] for n in notify.read_notes(paths)] == [n["id"] for n in first]
    # a new writer's rule still sees the old note (the NS-W207 prefix), and adds nothing
    schedule._note_switched_off_outside(paths, NsLogger("supervisor", paths.logs, echo=False))
    assert len(client.get("/api/notes").json()) == 2
    r = client.post("/api/notes/clear", json={"ids": [first[0]["id"]]})
    assert r.status_code == 200 and r.json() == {"ok": True, "removed": 1, "left": 1}
    assert client.get("/api/notes").json() == [first[1]]
    # what is on disk after this version wrote it is what 0.1.14 reads after a rollback: no id is stored
    notify.leave_note(paths, "Nightshift", "written by this version", now=datetime(2026, 11, 10, 3, 0, 0))
    raw = json.loads(notes_file(paths).read_text(encoding="utf-8"))
    assert raw == {"notes": [
        {"at": "2026-11-09T03:30:00", "title": "Nightshift", "message": "NS-W207: Automatic runs were switched off outside Nightshift."},
        {"at": "2026-11-10T03:00:00", "title": "Nightshift", "message": "written by this version"},
    ]}
    # two notes that are the same to the second share an id and go together
    for _ in range(2):
        notify.leave_note(paths, "Nightshift", "twice", now=datetime(2026, 11, 10, 3, 0, 5))
    twins = [n["id"] for n in client.get("/api/notes").json() if n["message"] == "twice"]
    assert len(twins) == 2 and len(set(twins)) == 1
    assert client.post("/api/notes/clear", json={"ids": twins[:1]}).json() == {"ok": True, "removed": 2, "left": 2}


# -------------------------------------------------------------------------------- a damaged notes file
def kept_aside(paths: Paths) -> list[Path]:
    return sorted(p for p in notes_file(paths).parent.iterdir() if p.name.startswith("morning_note.json.bad-"))


@pytest.mark.parametrize(
    ("torn", "readable"),
    [
        (b'{"notes": [{"at": "2026-11-09T03:30:00", "title": "Nightshift", "message": "the night', []),
        (b"", []),
        (b'{"notes": {"at": "x"}}', []),
        (b'{"notes": [{"at": "2026-11-09T03:30:00", "title": "Nightshift", "message": "kept"}, "junk", 7]}', ["kept"]),
    ],
)
def test_a_damaged_file_is_set_aside_and_said(tmp_home: Path, torn: bytes, readable: list[str]) -> None:
    """Never silently emptied: the bytes are kept beside the file, the bell says so, and what could be read
    stays. Found by the first reader or writer, whichever comes first."""
    client, state = make_env(tmp_home)
    paths = state.paths
    notes_file(paths).parent.mkdir(parents=True, exist_ok=True)
    notes_file(paths).write_bytes(torn)
    shown = client.get("/api/notes").json()                       # the owner opens the bell
    assert len(kept_aside(paths)) == 1 and shown, "a torn file is shown as no notes, and the next writer replaces it"
    (aside,) = kept_aside(paths)
    assert aside.read_bytes() == torn
    assert [n["message"] for n in shown[:-1]] == readable
    said = shown[-1]["message"]
    assert said.startswith("Nightshift could not read its waiting notes") and str(aside) in said and "Notepad" in said
    assert shown[-1]["title"] == "Nightshift" and client.get("/api/notes").json() == shown      # said once
    assert len(kept_aside(paths)) == 1
    notify.leave_note(paths, "Nightshift", "after the damage")
    assert messages(paths) == [*readable, said, "after the damage"]
    # dismissed like any note; the file kept aside stays for the owner
    ids = [n["id"] for n in client.get("/api/notes").json()]
    assert client.post("/api/notes/clear", json={"ids": ids}).json()["left"] == 0 and aside.read_bytes() == torn


def test_a_damaged_file_found_by_a_writer_is_not_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The nightly run is the first to touch a torn file, and its own write fails once (a full disk, a kill):
    the torn bytes are still there, and the next writer sets them aside once and says so."""
    paths = Paths(tmp_path / "home")
    notes_file(paths).parent.mkdir(parents=True)
    torn = b'{"notes": [{"at": "2026-11-09T03:30:00", "title": "Nightshift", "message": "the night'
    notes_file(paths).write_bytes(torn)
    real = notify.write_text_atomic

    def no_room(*_a: Any, **_kw: Any) -> Any:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(notify, "write_text_atomic", no_room)
    # the note is not lost: logged with its text and spooled (one small file, written without the lock)
    assert notify.leave_note(paths, "Nightshift", "the summary of the night") is True
    assert notes_file(paths).read_bytes() == torn                    # not emptied, not replaced
    assert len(spooled(paths)) == 1
    (line,) = [r for r in log_records(paths) if r["event"] == "morning_note_failed"]
    assert "No space left" in line["msg"] and line["data"]["message"] == "the summary of the night"
    monkeypatch.setattr(notify, "write_text_atomic", real)
    assert notify.drain_spool(paths) == 1                            # the engine's start
    assert spooled(paths) == [] and notify.drain_spool(paths) == 0
    assert len(kept_aside(paths)) == 1, "the torn file was overwritten: its bytes are nowhere"
    assert kept_aside(paths)[0].read_bytes() == torn
    said, last = messages(paths)
    assert said.startswith("Nightshift could not read its waiting notes") and last == "the summary of the night"


@needs_powershell
def test_the_stub_sets_a_damaged_file_aside_and_says_so(tmp_path: Path) -> None:
    home, desk = stub_home(tmp_path)
    paths = Paths(home)
    torn = b'{"notes": [{"at": "2026-11-09T03:30:00", "title": "Nightshift", "message": "the night'
    notes_file(paths).write_bytes(torn)
    record = missed(home, "2026-11-08")
    assert stub_cleanup(home, desk) == "cleared" and not record.exists()
    assert len(kept_aside(paths)) == 1, "the launch script replaced a torn file: its bytes are nowhere"
    (aside,) = kept_aside(paths)
    assert aside.read_bytes() == torn
    said, night = messages(paths)
    assert said.startswith("Nightshift could not read its waiting notes") and str(aside) in said
    assert night == stub_said("2026-11-08")
    # the same words as the owner's module, and ids for both
    assert said == notify.damaged_message(aside) and len({n["id"] for n in notify.read_notes(paths)}) == 2
    # a file with notes of 0.1.14 and of this version goes through the script unchanged, whatever the text:
    # nothing lost, the same ids before and after (PowerShell 5.1 writes its own escapes and spacing)
    notes_file(paths).write_bytes(OLD_FILE.encode("utf-8"))
    hard = "Jo's caf\u00e9 <b>\"night\"</b> & \u0644\u064a\u0644\u0629 \U0001f319 C:\\Users\\Jo\ttab"
    notify.leave_note(paths, "Nightshift", hard)
    before = notify.read_notes(paths)
    assert [n["message"] for n in before][2] == hard
    missed(home, "2026-10-01")
    assert stub_cleanup(home, desk) == "cleared"
    assert notify.read_notes(paths)[:3] == before and messages(paths)[3] == stub_said("2026-10-01")
    assert len(kept_aside(paths)) == 1




# ------------------------------------------------- the bound: the newest note that matters is never lost
ROUTINE = "1 master + 5 Shorts ready for review"
FAILED = "run failed (NS-E203), open Dashboard"


def full_file(paths: Paths, texts: list[str], *, mark_every: int = 2) -> list[str]:
    """`texts` as waiting notes, a minute apart; every `mark_every`-th routine one carries the mark this
    version writes, the others are as rc3 left them (the rule reads the words too). Returns their `at`."""
    start = datetime(2026, 5, 1, 3, 0, 0)
    notes: list[dict[str, Any]] = []
    for i, text in enumerate(texts):
        note: dict[str, Any] = {"at": (start + timedelta(minutes=i)).isoformat(), "title": "Nightshift", "message": text}
        if i % mark_every == 0 and notify.is_routine(text):
            note["routine"] = True
        notes.append(note)
    notes_file(paths).parent.mkdir(parents=True, exist_ok=True)
    notes_file(paths).write_text(json.dumps({"notes": notes}, indent=2) + "\n", encoding="utf-8")
    return [n["at"] for n in notes]


def raw_notes(paths: Paths) -> list[dict[str, Any]]:
    return json.loads(notes_file(paths).read_text(encoding="utf-8"))["notes"]


def test_routine_is_defined_once_and_a_failure_is_never_routine() -> None:
    from ns_studio.notifications import failure_message, nightly_message

    assert notify.is_routine(nightly_message({"jobs": [{}]}, 1, 5) or "") and notify.is_routine(ROUTINE)
    assert notify.is_routine(nightly_message({"jobs": [{}, {}]}, 0, 0) or "")       # "2 jobs finished, open ..."
    assert not notify.is_routine(nightly_message({"jobs": [{}], "failures": [{"code": "NS-E203"}]}, 1, 0) or "")
    assert not notify.is_routine(failure_message("NS-E203")) and not notify.is_routine(stub_said("2026-11-08"))
    assert not notify.is_routine("NS-W207: Automatic runs were switched off outside Nightshift.")
    assert not notify.is_routine(notify.damaged_message(Path("x"))) and not notify.is_routine("")


def test_at_the_bound_a_failure_pushes_out_the_oldest_routine_note(tmp_home: Path) -> None:
    """Six months of "ready for review" nobody dismissed (200 notes): the night that fails is still told."""
    client, state = make_env(tmp_home)
    paths = state.paths
    ats = full_file(paths, [ROUTINE] * 200)
    assert notify.leave_note(paths, "Nightshift", FAILED) is True
    got = raw_notes(paths)
    assert len(got) == 201 and got[-1]["message"] == FAILED
    assert got[0] == {"at": got[0]["at"], "title": "Nightshift", "message": notify.removed_message(paths, 1), "removed": 1}
    assert [n["at"] for n in got[1:200]] == ats[1:], "not the oldest routine note went"
    assert all(n.get("routine") is True for n in got[1:200]), "the launch script reads the mark: every routine note has it"
    # the next one: the same one note counts both, updated in place, never a second one
    assert notify.leave_note(paths, "Nightshift", FAILED + " again") is True
    got = raw_notes(paths)
    assert len(got) == 201 and [n for n in got if "removed" in n] == [got[0]] and got[0]["removed"] == 2
    assert got[0]["message"] == notify.removed_message(paths, 2) and str(paths.logs) in got[0]["message"]
    assert [n["message"] for n in got[-2:]] == [FAILED, FAILED + " again"]
    dropped = [r for r in log_records(paths) if r["event"] == "morning_note_dropped"]
    assert [n["at"] for r in dropped for n in r["data"]["notes"]] == ats[:2]
    # the bell: the count first in the answer (the file's order) and last on screen (the page reverses it)
    shown = client.get("/api/notes").json()
    assert shown[0]["message"] == got[0]["message"] and shown[-1]["message"] == FAILED + " again"
    assert notify.dismiss(paths, [shown[0]["id"]]) == 1 and not [n for n in raw_notes(paths) if "removed" in n]


def test_at_the_bound_of_failures_the_newest_is_kept_and_a_routine_note_moves_none(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    failures = [f"{FAILED} (night {i})" for i in range(200)]
    full_file(paths, failures)
    assert notify.leave_note(paths, "Nightshift", "run failed (NS-E204), open Dashboard") is True
    got = messages(paths)
    assert got[0] == notify.removed_message(paths, 1) and got[1:] == [*failures[1:], "run failed (NS-E204), open Dashboard"]
    # a routine note never makes room by pushing out a failure: it is not added, and the file is not touched
    before = notes_file(paths).read_bytes()
    assert notify.leave_note(paths, "Nightshift", ROUTINE) is False
    assert notes_file(paths).read_bytes() == before
    last = [r for r in log_records(paths) if r["event"] == "morning_note_dropped"][-1]
    assert [n["message"] for n in last["data"]["notes"]] == [ROUTINE]


@needs_powershell
def test_the_launch_stub_keeps_the_same_bound(tmp_path: Path) -> None:
    """The missed-night note of the launch script at 200 waiting notes (Windows PowerShell 5.1): written,
    the oldest routine note goes, then the oldest failure; one count note in the program's words."""
    home, desk = stub_home(tmp_path)
    paths = Paths(home)
    texts = [f"{FAILED} (night {i})" for i in range(200)]
    texts[3] = ROUTINE
    full_file(paths, texts, mark_every=1)
    missed(home, "2026-11-08")
    assert stub_cleanup(home, desk) == "cleared"
    got = raw_notes(paths)
    assert len(got) == 201 and got[0]["message"] == notify.removed_message(paths, 1) and got[0]["removed"] == 1
    assert [n["message"] for n in got[1:]] == [*texts[:3], *texts[4:], stub_said("2026-11-08")]
    launch_log = [
        json.loads(line) for f in sorted(paths.logs.glob("launch-*.jsonl"))
        for line in f.read_text(encoding="utf-8").splitlines() if line
    ]
    (dropped,) = [r for r in launch_log if r["event"] == "morning_note_dropped"]
    assert [n["message"] for n in dropped["data"]["notes"]] == [ROUTINE]
    # no routine note left: the oldest failure goes, the newest is kept, the count is replaced in place
    missed(home, "2026-10-01")
    assert stub_cleanup(home, desk) == "cleared"
    got = raw_notes(paths)
    assert len(got) == 201 and [n for n in got if "removed" in n] == [got[0]] and got[0]["removed"] == 2
    assert got[0]["message"] == notify.removed_message(paths, 2)
    assert [n["message"] for n in got[1:]] == [*texts[1:3], *texts[4:], stub_said("2026-11-08"), stub_said("2026-10-01")]
    # and the program goes on from what the script wrote
    assert notify.leave_note(paths, "Nightshift", FAILED) is True
    assert raw_notes(paths)[0]["removed"] == 3 and messages(paths)[-1] == FAILED


# --------------------------------------- a note that cannot be written waits in the spool, then comes once
def test_the_waits_are_longer_than_the_longest_hold() -> None:
    from ns_common.atomic import REPLACE_WAIT_S

    assert notify.NOTES_HOLD_MAX_S >= REPLACE_WAIT_S + notify.NOTES_SWAP_WAIT_S + notify.NOTES_DRAIN_S
    assert notify.NOTES_WAIT_S > notify.NOTES_HOLD_MAX_S and notify.NOTES_REQUEST_WAIT_S <= 5


def test_a_writer_slowed_past_five_seconds_makes_nobody_spool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = Paths(tmp_path / "home")
    one = Paused(monkeypatch, paths, "a slow writer").start()
    two = threading.Thread(target=lambda: notify.leave_note(paths, "Nightshift", "waits its turn"), daemon=True)
    try:
        two.start()
        two.join(6.0)
        assert two.is_alive() and spooled(paths) == [], "a writer gave up on a holder that was only slow"
    finally:
        one.finish()
    two.join(60)
    assert messages(paths) == ["a slow writer", "waits its turn"] and spooled(paths) == []


def test_a_writer_blocked_past_its_wait_spools_and_the_next_write_delivers_it_once(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    notify.leave_note(paths, "Nightshift", "before")
    before = notes_file(paths).read_bytes()
    held = FileLock(lock_file(paths))
    held.acquire()                                             # a holder that hangs
    try:
        began = time.monotonic()
        assert notify.leave_note(paths, "Nightshift", FAILED, wait_s=0.3) is True  # pyright: ignore[reportCallIssue]
        assert time.monotonic() - began < 3 and notes_file(paths).read_bytes() == before
        (kept,) = spooled(paths)
        assert json.loads(kept.read_text(encoding="utf-8"))["message"] == FAILED
        (line,) = [r for r in log_records(paths) if r["event"] == "morning_note_failed"]
        assert line["level"] == "WARN" and "held by another program" in line["msg"]
        assert line["data"]["message"] == FAILED and line["data"]["spooled"] == str(kept)
    finally:
        held.release()
    copy = kept.read_bytes()
    assert notify.leave_note(paths, "Nightshift", "next") is True
    assert messages(paths) == ["before", FAILED, "next"] and spooled(paths) == []
    # a holder killed after its write and before it removed the spool file: the note is not added again
    kept.write_bytes(copy)
    assert notify.leave_note(paths, "Nightshift", "and next") is True
    assert messages(paths) == ["before", FAILED, "next", "and next"] and spooled(paths) == []
    # the same leftover while the owner dismisses that note: the dismiss drains first, so it never comes back
    kept.write_bytes(copy)
    ids = [n["id"] for n in notify.read_notes(paths) if n["message"] == FAILED]
    assert notify.dismiss(paths, ids) == 1 and spooled(paths) == []
    notify.leave_note(paths, "Nightshift", "last")
    assert messages(paths) == ["before", "next", "and next", "last"]


def test_the_nightly_result_is_never_lost_and_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`toast_or_note` is how the night's result reaches the notes in session 0."""
    paths = Paths(tmp_path / "home")
    monkeypatch.setattr(notify, "NOTES_WAIT_S", 0.3, raising=False)
    held = FileLock(lock_file(paths))
    held.acquire()
    try:
        assert notify.toast_or_note("Nightshift", FAILED, paths=paths, interactive=lambda: False) is False
        assert len(spooled(paths)) == 1 and not notes_file(paths).exists()
    finally:
        held.release()
    assert notify.drain_spool(paths) == 1 and messages(paths) == [FAILED] and spooled(paths) == []
    assert "notify.drain_spool(paths)" in (ROOT / "ns_supervisor" / "app.py").read_text(encoding="utf-8")
    # neither the notes nor the spool can be written: the log keeps the text, nothing reaches the job
    other = Paths(tmp_path / "blocked")
    other.home.mkdir()
    (other.home / "run").write_text("not a folder", encoding="ascii")
    assert notify.toast_or_note("Nightshift", FAILED, paths=other, interactive=lambda: False) is False
    (line,) = [r for r in log_records(other) if r["event"] == "morning_note_failed"]
    assert line["level"] == "ERROR" and line["data"]["message"] == FAILED
    # the spool is bounded: a holder that hangs for days cannot fill the disk
    monkeypatch.setattr(notify, "SPOOL_MAX", 2, raising=False)
    held.acquire()
    try:
        assert notify.leave_note(paths, "Nightshift", "one", wait_s=0.1) is True  # pyright: ignore[reportCallIssue]
        assert notify.leave_note(paths, "Nightshift", "two", wait_s=0.1) is True  # pyright: ignore[reportCallIssue]
        with pytest.raises(OSError):
            notify.leave_note(paths, "Nightshift", "three", wait_s=0.1)  # pyright: ignore[reportCallIssue]
    finally:
        held.release()
    assert [r["data"]["message"] for r in log_records(paths) if r["event"] == "morning_note_failed"][-1] == "three"


def test_a_damaged_spool_file_is_set_aside_once_and_never_read_again(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    folder = paths.home / "run" / "morning_note.spool"
    folder.mkdir(parents=True)
    note = {"at": "2026-11-10T03:00:00", "title": "Nightshift", "message": "spooled", "once": False}
    (folder / "20261110T030000000001-1-1-1.json").write_text(json.dumps(note), encoding="utf-8")
    (folder / "20261110T030000000002-1-1-2.json").write_text('{"at": "2026-11-10T03:00:01", "title": "Nigh', encoding="utf-8")
    (folder / "20261110T030000000003-1-1-3.json").write_text('["not", "a note"]', encoding="utf-8")
    (folder / "~ns123-1-1.tmp").write_text('{"at"', encoding="utf-8")          # a writer killed mid-file
    assert notify.drain_spool(paths) == 1 and messages(paths) == ["spooled"]
    assert sorted(p.name for p in folder.iterdir()) == [
        "20261110T030000000002-1-1-2.json.bad", "20261110T030000000003-1-1-3.json.bad", "~ns123-1-1.tmp"]
    said = [r for r in log_records(paths) if r["event"] == "morning_note_spool_damaged"]
    assert len(said) == 2 and notify.drain_spool(paths) == 0
    assert len([r for r in log_records(paths) if r["event"] == "morning_note_spool_damaged"]) == 2
    # a note spooled with once= is not added while the same words wait
    again = {**note, "at": "2026-11-10T03:05:00", "once": True}
    (folder / "20261110T030500000000-1-1-4.json").write_text(json.dumps(again), encoding="utf-8")
    assert notify.leave_note(paths, "Nightshift", "after") is True
    assert messages(paths) == ["spooled", "after"] and spooled(paths) == []


def test_the_bell_shows_newest_first_and_says_busy() -> None:
    shell = (ROOT / "ns_dashboard" / "ui" / "src" / "shell" / "Shell.tsx").read_text(encoding="utf-8")
    assert ".then((l) => [...l].reverse())" in shell
    assert 'e?.code === "notes_busy" ? "Nightshift is writing a note right now; try again"' in shell
    bundle = (ROOT / "ns_dashboard" / "static" / "app.js").read_text(encoding="utf-8")
    assert "Nightshift is writing a note right now; try again" in bundle
