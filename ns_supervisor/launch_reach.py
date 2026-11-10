"""The launch script reaches an install however it got this version (card launcher-reach).

Setup writes `<home>/nightshift-launch.ps1` and points the Desktop shortcuts and the nightly task at it. An
install that 0.1.14's own updater brought here never ran setup: it has no such file, shortcuts that start
the launchers directly (after an interrupted update they only flash a window) and a task that starts
`studio.exe` directly (after one, nobody is told the night was missed). So the first engine start of every
newly placed program folder does it too, in a thread that never delays or stops the engine:

1. the script: copied from the program folder unless the one in the home has a higher format number;
2. the shortcuts on the Desktop that start a launcher of THIS install directly are pointed at the script,
   each under its own name, icon and working folder. A shortcut of another install that exists, or whose
   drive is not there, is never touched; one whose install folder was deleted is pointed at this install,
   and a morning note says so once;
3. the nightly task, when it is this install's and enabled, is registered again at its own time, so its
   action runs through the script (`schedule.refresh_action`).

The order matters: what calls the script never exists before the script. Every step is safe to repeat. The
marker `<home>/run/launch-reach.json`, written last, says which program folder this was done for and what
failed; a failure is tried again at every engine start and shown by `ns doctor` (row "launch script"). It
is not a morning note: nothing is broken while the program folder is there.

A test run never asks Windows for the real Desktop: `run` refuses when it would (`RealMachineUnderTest`).
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ns_common import first_start
from ns_common.atomic import read_text_retry, write_text_atomic
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_common.singleton import AlreadyRunning, FileLock
from ns_supervisor import notify, schedule
from ns_supervisor.testguard import RealMachineUnderTest, under_test

MARKER_NAME = "launch-reach.json"
ROW = "launch script"
ICON_RELATIVE = Path("ns_dashboard") / "static" / "brand" / "nightshift.ico"
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
    stamp = first_start.install_stamp(app)
    old = read_marker(paths)
    current = so.stub_schema(paths.home / so.STUB_NAME) >= so.stub_schema(app / so.STUB_SOURCE)
    if old.get("stamp") == stamp and not old.get("failed") and current:
        return old
    runner = runner or SubprocessRunner()
    if desktop is None and under_test() and isinstance(runner, SubprocessRunner):
        raise RealMachineUnderTest(
            "a test started the launch reach without a Desktop folder, so it would ask Windows for the real "
            "Desktop and rewrite the shortcuts on it; this is refused under pytest. Pass desktop= (a folder "
            "under tmp_path) or a stand-in runner."
        )
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
    icon = app / ICON_RELATIVE
    taken: dict[str, list[str]] = {}
    try:
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
            who, found = so.owner(have.arguments, paths.home, app), so.parse(have.arguments)
            if found is None or who not in (so.MINE, so.GONE):
                states[lnk.name] = who
                continue
            if who == so.MINE and so.is_stub_form(have.arguments, paths.home):
                states[lnk.name] = "ok"
                continue
            launcher: Path | str = found[0]
            extra: dict[str, Any] = {}
            if who == so.GONE:      # its install folder was deleted: it becomes a shortcut of this install
                mine = app / "launchers" / os.path.basename(found[0])
                if not mine.is_file():      # a launcher this program does not have: nothing to point it at
                    states[lnk.name] = "left"
                    continue
                launcher, extra = mine, {"workdir": paths.home, "icon": icon if icon.is_file() else None}
            arguments = so.arguments_for(launcher, stub)
            res = winenv.update_shortcut(runner, lnk, arguments, **extra)
            try:
                took = res.ok and so.read_lnk(lnk).arguments == arguments
            except (OSError, ValueError):
                took = False
            if not took:
                why = res.text if not res.ok else "the shortcut did not take the change"
                states[lnk.name] = FAILED + _why(why)
            elif who == so.GONE:
                states[lnk.name] = "taken"
                taken.setdefault(str(so.opens_home(have.arguments)), []).append(lnk.stem)
            else:
                states[lnk.name] = "done"
    finally:
        for gone, names in taken.items():
            with contextlib.suppress(OSError):
                notify.leave_note(
                    paths, "Nightshift",
                    f"The Desktop shortcuts {', '.join(names)} opened a Nightshift folder that no longer "
                    f"exists ({gone}); they now open this Nightshift ({paths.home}).",
                )
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
