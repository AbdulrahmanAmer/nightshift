"""The launch script reaches an install however it got this version (card launcher-reach).

Setup writes `<home>/nightshift-launch.ps1` and points the Desktop shortcuts and the nightly task at it. An
install that 0.1.14's own updater brought here never ran setup: it has no such file, shortcuts that start
the launchers directly (after an interrupted update they only flash a window) and a task that starts
`studio.exe` directly (after one, nobody is told the night was missed). So the first engine start of every
newly placed program folder does it too, in a thread that never delays or stops the engine:

1. the script: copied from the program folder unless the one in the home has a higher format number;
2. the shortcuts on the Desktop that start a launcher of THIS install's home directly are pointed at the
   script, each under its own name, icon and working folder. A shortcut that points at any other home is
   never touched: another install that exists, one that was deleted, a drive that is not there (or whose
   letter another drive took). Nothing is created and nothing is taken over;
3. the nightly task, when it is this install's and enabled, gets the action that runs through the script;
   its time, logon type and settings stay (`schedule.refresh_action`).

Before all that, at EVERY engine start (also when the three steps are done): a night the launch script
counted as missed is cleared (`_clear_missed`: one morning note, the Desktop note removed, the count
removed), by the script itself, so there is one implementation and one starter at a time. The Desktop note
says it goes "the next time Nightshift starts", and setup's Open dashboard, `ns up` and the restart after
an update start the engine without the script.

The order matters: what calls the script never exists before the script. Every step is safe to repeat. The
marker `<home>/run/launch-reach.json`, written last, says which program folder this was done for and what
failed; a failure is tried again at every engine start and shown by `ns doctor` (row "launch script"). It
is not a morning note: nothing is broken while the program folder is there.

A test run never asks Windows for the real Desktop: `run` refuses when it would (`RealMachineUnderTest`),
and so do the calls below it (`winenv.desktop_dir`, `update_shortcut`). A dev install never touches the
Desktop either: its shortcuts are left as they are (`ns_common.devmode.is_dev`).
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ns_common import first_start
from ns_common.atomic import read_text_retry, write_text_atomic
from ns_common.devmode import is_dev
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_common.singleton import AlreadyRunning, FileLock
from ns_supervisor import schedule
from ns_supervisor.testguard import RealMachineUnderTest, refuse_real_shortcut

MARKER_NAME = "launch-reach.json"
ROW = "launch script"
#: The launch script's count of nights the nightly run could not start (`installer/launch.ps1`).
MISSED_NAME = "launch-missed.json"
#: The script format that knows `-Cleanup`.
CLEANUP_SCHEMA = 3
FAILED = "failed: "


def marker_file(paths: Paths) -> Path:
    return paths.home / "run" / MARKER_NAME


def read_marker(paths: Paths) -> dict[str, Any]:
    """The marker of the last reach ({} when there is none or it cannot be read)."""
    try:
        data = json.loads(read_text_retry(marker_file(paths)))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _why(text: object) -> str:
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    return (lines[-1] if lines else "no reason given")[:200]


def run(
    paths: Paths, app: Path, *, desktop: Path | None = None, runner: Any = None, log: NsLogger | None = None
) -> dict[str, Any]:
    """Do the three steps for the program folder `app` when they are not done for it yet, or one of them
    failed last time; return the marker. `desktop` is the Desktop folder (default: the one Windows names),
    `runner` an installer `Runner` for the PowerShell that changes a shortcut. Under `run/shortcuts.lock`:
    a second start that finds it taken leaves the work to the one that holds it."""
    from installer.stage2 import shortcut_owner as so
    from installer.stage2.runner import SubprocessRunner

    log = log or NsLogger("launch_reach", paths.logs)
    runner = runner or SubprocessRunner()
    if desktop is None and isinstance(runner, SubprocessRunner):
        refuse_real_shortcut(None)      # a test run: it would ask Windows for the real Desktop and rewrite it
    _clear_missed(paths, app, desktop, runner, so, log)
    stamp = first_start.install_stamp(app)
    old = read_marker(paths)
    current = so.stub_schema(paths.home / so.STUB_NAME) >= so.stub_schema(app / so.STUB_SOURCE)
    if old.get("stamp") == stamp and not old.get("failed") and current:
        return old
    marker_file(paths).parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(paths.home / "run" / "shortcuts.lock")
    try:
        lock.acquire()
    except AlreadyRunning:
        log.info("launch_reach_busy", "another start is doing it")
        return old
    try:
        marker = _reach(paths, app, stamp, desktop, runner, so)
        write_text_atomic(marker_file(paths), json.dumps(marker, indent=2) + "\n")
    finally:
        with contextlib.suppress(OSError):
            lock.release()
    level = log.warn if marker["failed"] else log.info
    level("launch_reach", f"stub {marker['stub']}; task {marker['task'] or 'not looked at'}", **marker)
    return marker


def _clear_missed(paths: Paths, app: Path, desktop: Path | None, runner: Any, so: Any, log: NsLogger) -> None:
    """The program runs (this is its engine), so a night the launch script counted is over: let the script
    clear it. Nothing is started when there is no count. The script holds the count's file open while it
    works, so a shortcut, the nightly task and this start never clear the same night twice; it writes the
    morning note first and removes the count last, so a failure or a kill keeps the count for the next
    start. Never raises: the three steps of the reach do not depend on it."""
    if not (paths.home / "run" / MISSED_NAME).is_file():
        return
    try:
        stub = so.install_stub(app, paths.home)
    except OSError:
        stub = paths.home / so.STUB_NAME
    if stub is None or so.stub_schema(stub) < CLEANUP_SCHEMA:
        log.warn("launch_missed_kept", f"no launch script that can clear it ({stub}); a shortcut start will")
        return
    argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
    argv += ["-File", str(stub), "-Cleanup"] + (["-DesktopDir", str(desktop)] if desktop is not None else [])
    res = runner.run(argv, tag="ps:launch-cleanup", timeout=120)
    said = _why(res.text)
    (log.info if res.ok and said == "cleared" else log.warn)("launch_missed_cleanup", said)


def _reach(paths: Paths, app: Path, stamp: str, desktop: Path | None, runner: Any, so: Any) -> dict[str, Any]:
    marker: dict[str, Any] = {
        "stamp": stamp, "at": datetime.now().isoformat(timespec="seconds"), "stub": "left", "desktop": "",
        "shortcuts": {}, "task": "", "failed": [],
    }
    if not (app / so.STUB_SOURCE).is_file():
        return marker           # a program folder without the script (an older version): all left as it is
    try:
        stub = so.install_stub(app, paths.home)
    except OSError as e:
        stub, marker["stub"] = None, FAILED + _why(e)
    if stub is None:
        marker["failed"].append("stub")     # nothing may call a script that is not there
        return marker
    marker["stub"] = "ok"
    _shortcuts(paths, app, stub, desktop, runner, so, marker)
    try:
        marker["task"] = "done" if schedule.refresh_action(paths, app) else "ok"
    except schedule.ScheduleError as e:
        marker["task"] = FAILED + _why(e)
        marker["failed"].append("task")
    return marker


def _shortcuts(
    paths: Paths, app: Path, stub: Path, desktop: Path | None, runner: Any, so: Any, marker: dict[str, Any]
) -> None:
    from installer.stage2 import winenv

    if is_dev(paths):           # a dev install never changes the Desktop of the PC it is developed on
        marker["desktop"] = "dev install: shortcuts left as they are"
        return
    try:
        folder = desktop or winenv.desktop_dir(runner)
        if folder is None or not folder.is_dir():
            raise OSError(f"Windows named no Desktop folder that exists ({folder})")
        links = sorted(p for p in folder.iterdir() if p.suffix.lower() == ".lnk" and p.is_file())
    except OSError as e:
        marker["desktop"] = FAILED + _why(e)
        marker["failed"].append("desktop")
        return
    marker["desktop"] = str(folder)
    states: dict[str, str] = marker["shortcuts"]
    for left in folder.glob(winenv.LNK_TEMP_GLOB):      # a change that lost power before its rename
        with contextlib.suppress(OSError):
            left.unlink()
    for lnk in links:
        try:
            have = so.read_lnk(lnk)
        except ValueError:
            states[lnk.name] = so.FOREIGN
            continue
        except OSError as e:    # held by another program: one of setup's own names counts as not done
            ours = lnk.name.lower().startswith("nightshift")
            states[lnk.name] = (FAILED if ours else "unread: ") + _why(e)
            continue
        # only a shortcut that starts a launcher in THIS home: any other home's is its own install's
        who, found = so.owner(have.arguments, paths.home), so.parse(have.arguments)
        if found is None or who != so.MINE:
            states[lnk.name] = who
            continue
        if so.is_stub_form(have.arguments, paths.home):
            states[lnk.name] = "ok"
            continue
        if not Path(found[0]).is_file():    # a launcher this program no longer has: the script could only
            states[lnk.name] = "left"       # say "the program folder is missing", which would be untrue
            continue
        arguments = so.arguments_for(found[0], stub)
        res = winenv.update_shortcut(runner, lnk, arguments)
        try:
            took = res.ok and so.read_lnk(lnk).arguments == arguments
        except (OSError, ValueError):
            took = False
        why = res.text if not res.ok else "the shortcut did not take the change"
        states[lnk.name] = "done" if took else FAILED + _why(why)
    if any(state.startswith(FAILED) for state in states.values()):
        marker["failed"].append("shortcuts")


def start(
    paths: Paths, app: Path, log: NsLogger | None = None, *, run: Callable[..., Any] = run
) -> threading.Thread:
    """Run the reach in a daemon thread and return at once: the engine start never waits for it, and
    whatever it raises is logged and tried again at the next start."""
    say = log or NsLogger("launch_reach", paths.logs)

    def work() -> None:
        try:
            run(paths, app, log=say)
        except RealMachineUnderTest as e:
            say.info("launch_reach_skipped", str(e))
        except Exception as e:  # never stops or delays the engine
            say.warn("launch_reach_failed", f"{e!r}")

    thread = threading.Thread(target=work, name="launch-reach", daemon=True)
    thread.start()
    return thread


def doctor_row(paths: Paths) -> tuple[str, bool, str]:
    """`ns doctor`, row "launch script": what the last reach could not do, with the way out."""
    marker = read_marker(paths)
    failed = marker.get("failed") or []
    if not marker:
        return (ROW, True, "ok (not checked yet: Nightshift checks it each time its engine starts)")
    if not failed:
        return (ROW, True, "ok")
    said: list[str] = []

    def why(key: str) -> str:
        return str(marker.get(key))[len(FAILED):]

    if "stub" in failed:
        said.append(
            f"The launch script could not be copied to {paths.home} ({why('stub')}), so the Desktop "
            "shortcuts and the nightly task still start Nightshift directly. They work; only after an "
            "interrupted update would a missing program folder go unnoticed. Run INSTALL.cmd and choose "
            "Repair."
        )
    if "desktop" in failed:
        said.append(
            f"Nightshift could not look at the Desktop shortcuts ({why('desktop')}). "
            "They work; one made by an older version would only close without a word after an interrupted "
            "update. Run INSTALL.cmd and choose Repair to update them."
        )
    listed = marker.get("shortcuts")
    states: dict[str, Any] = listed if isinstance(listed, dict) else {}
    bad = sorted({str(s)[len(FAILED):] for s in states.values() if str(s).startswith(FAILED)})
    count = sum(1 for s in states.values() if str(s).startswith(FAILED))
    if count:
        one = count == 1
        said.append(
            f"{count} Desktop shortcut{' still starts' if one else 's still start'} Nightshift directly "
            f"(Nightshift could not update {'it' if one else 'them'}: {'; '.join(bad)}). "
            f"{'It works' if one else 'They work'}; only after an interrupted update would "
            f"{'it' if one else 'they'} close without a word. Run INSTALL.cmd and choose Repair to update "
            f"{'it' if one else 'them'}."
        )
    if "task" in failed:
        said.append(
            f"The nightly task starts Nightshift directly ({why('task')}), so a missing program folder "
            "would go unnoticed at night. Run INSTALL.cmd and choose Repair."
        )
    said.append(f"(As of the last start of Nightshift, {marker.get('at')}; the next start tries again.)")
    return (ROW, False, " ".join(said))
