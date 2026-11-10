# ruff: noqa: E501
"""Card launcher-reach: the launch script reaches every install, every start and the nightly task.

Nothing here touches this PC: the Desktop is a folder under `tmp_path` handed to `launch_reach.run`, the
shortcuts are written by a stand-in runner (`Shell`) or, for the one real round trip, by PowerShell into
`tmp_path`, and the Task Scheduler is `FakeWindows`. The stub's own tests are in tests/test_launchers.py.
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib
import json
import os
import struct
from pathlib import Path
from typing import Any

import httpx
import pytest

from installer.stage2.runner import RunResult
from installer.stage2.steps.s09_shortcuts_scheduler import SHORTCUTS
from installer.stage2.web import nightly_view
from ns_common import first_start, rollback_intent
from ns_common.paths import Paths
from ns_supervisor import doctor, pwsh, schedule
from ns_supervisor.client import SupervisorClient
from ns_supervisor.schedule_cfg import remembered_run_time, write_run_time
from tests.test_nightly_optional import FakeWindows
from tests.test_ns_schedule import needs_powershell


def _module(name: str) -> Any:
    """A module this card adds; None on the base, so a test fails at its assertion and not at an import."""
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


reach: Any = _module("ns_supervisor.launch_reach")
so: Any = _module("installer.stage2.shortcut_owner")
NEW = "this card's module is not there"
ROOT = Path(__file__).resolve().parents[1]
HARD = "Jo's Nightshift (2) \u0644\u064a\u0644\u0629"
SIX = [(name, script) for name, script, code in SHORTCUTS if not code]
HEAD = "-NoProfile -ExecutionPolicy Bypass -File"


def direct(home: Path, script: str) -> str:
    """What 0.1.14 wrote into a shortcut."""
    return f'{HEAD} "{home}\\app\\launchers\\{script}"'


def through_stub(home: Path, script: str) -> str:
    return f'{HEAD} "{home}\\nightshift-launch.ps1" -Launcher "{home}\\app\\launchers\\{script}"'


def write_lnk(path: Path, arguments: str, workdir: str, icon: str = "", name: str = "") -> None:
    """A minimal shell link (MS-SHLLINK): the header and Unicode StringData, no target list."""
    flags = 0x80 | (0x04 if name else 0) | 0x10 | 0x20 | (0x40 if icon else 0)
    head = struct.pack("<I16sI", 0x4C, bytes.fromhex("0114020000000000c000000000000046"), flags) + bytes(52)
    body = b"".join(
        struct.pack("<H", len(text)) + text.encode("utf-16-le") for text in (name, workdir, arguments, icon) if text
    )
    path.write_bytes(head + body + bytes(4))


class Shell:
    """The runner handed to the reach: it changes a shortcut as PowerShell would, and names the Desktop."""

    def __init__(self, desktop: Path | None = None) -> None:
        self.desktop, self.calls = desktop, []  # type: ignore[var-annotated]

    def run(self, argv: Any, *, tag: str = "", meta: Any = None, **kw: Any) -> RunResult:
        self.calls.append((tag, dict(meta or {})))
        if tag == "ps:desktop":
            return RunResult(0, __import__("base64").b64encode(str(self.desktop or "").encode()).decode())
        if tag == "ps:shortcut-update":
            lnk = Path(meta["lnk"])
            if not os.access(lnk, os.W_OK):
                return RunResult(1, "", "Access to the path is denied.")
            old = so.read_lnk(lnk)
            write_lnk(lnk, meta["arguments"], meta.get("workdir") or old.working_dir, meta.get("icon") or old.icon, old.description)
        return RunResult(0)

    def updates(self) -> list[str]:
        return [Path(m["lnk"]).name for tag, m in self.calls if tag == "ps:shortcut-update"]


@dataclasses.dataclass
class Inst:
    home: Path
    paths: Paths
    app: Path
    desktop: Path
    shell: Shell
    windows: FakeWindows
    icon: Path

    def run(self, **kw: Any) -> dict[str, Any]:
        return reach.run(self.paths, self.app, **{"desktop": self.desktop, "runner": self.shell, **kw})

    def args(self, name: str) -> str:
        return so.read_lnk(self.desktop / f"{name}.lnk").arguments

    def new_stamp(self) -> None:
        """The program folder was put in place again (an update): a new completion marker."""
        done = self.app / ".ns-app.done"
        later = done.stat().st_mtime_ns + 5_000_000_000
        os.utime(done, ns=(later, later))


@pytest.fixture
def inst(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Inst:
    """An install as 0.1.14's updater leaves it under this version: no stub in the home, six direct
    shortcuts, and the nightly task enabled at 05:30 with 0.1.14's action."""
    home = tmp_path / HARD
    paths = Paths(home)
    paths.ensure()
    app = home / "app"
    for _, script in SIX:
        (app / "launchers").mkdir(parents=True, exist_ok=True)
        (app / "launchers" / script).write_text("exit 0\r\n", encoding="ascii")
    (app / "installer").mkdir()
    (app / "installer" / "launch.ps1").write_bytes((ROOT / "installer" / "launch.ps1").read_bytes())
    (app / "VERSION").write_text("0.1.15\n", encoding="ascii")
    (app / ".ns-app.done").write_text("done", encoding="ascii")
    icon = app / "ns_dashboard" / "static" / "brand" / "nightshift.ico"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"ico")
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    for name, script in SIX:
        write_lnk(desktop / f"{name}.lnk", direct(home, script), str(home), str(icon), f"Nightshift: {name}")
    windows = FakeWindows(("Ready", "05:30"))
    assert windows.task is not None
    windows.task["home"] = str(home)
    write_run_time(paths, "05:30", "registered")
    monkeypatch.setattr(pwsh, "run_powershell", windows)
    return Inst(home, paths, app, desktop, Shell(), windows, icon)


def launch_row(paths: Paths) -> tuple[bool, str]:
    """The "launch script" row of `ns doctor`."""

    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no engine")

    client = SupervisorClient("http://sup", client=httpx.Client(transport=httpx.MockTransport(refuse)))
    rows = {name: (ok, detail) for name, ok, detail in doctor.diagnose(paths, client)}
    assert "launch script" in rows, sorted(rows)
    return rows["launch script"]


# ------------------------------------------------------------------------------- the task's own home
def test_task_owner_reads_the_home_from_the_new_action_and_from_the_0_1_14_one(inst: Inst):
    make = getattr(schedule, "action_arguments", None)
    assert make is not None, NEW
    studio = schedule.studio_exe(inst.paths)
    assert make(inst.paths) == (
        f'/d /s /c "set "NIGHTSHIFT_HOME={inst.home}" && if exist "{inst.home}\\nightshift-launch.ps1" '
        f'(powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{inst.home}\\nightshift-launch.ps1" '
        f'-Nightly -Studio "{studio}") else ("{studio}" run --daily)"'
    )
    head = "state: Ready\nstart: 2026-11-09T05:30:00\nlogon: S4U\nargs: "
    new = schedule.parse_task_state(head + make(inst.paths) + "\n")
    old = schedule.parse_task_state(head + f'/d /s /c "set "NIGHTSHIFT_HOME={inst.home}" && "{studio}" run --daily"\n')
    assert new is not None and old is not None
    assert schedule.task_owner(inst.paths, new) == schedule.task_owner(inst.paths, old) == "mine"
    assert (new.stub, old.stub) == (True, False)


@needs_powershell
def test_the_task_home_is_read_in_utf8(tmp_path: Path):
    home = tmp_path / HARD
    home.mkdir()
    stand_in = (
        "function Get-ScheduledTask { [CmdletBinding()] param([string]$TaskName)\n"
        "  [pscustomobject]@{ State = 'Ready'; Triggers = @([pscustomobject]@{ StartBoundary = '2026-11-09T05:30:00' });\n"
        "    Principal = [pscustomobject]@{ LogonType = 'S4U' }; Actions = @([pscustomobject]@{ Arguments = $env:NS_TEST_ACTION }) } }\n"
    )
    action = f'/d /s /c "set "NIGHTSHIFT_HOME={home}" && "studio" run --daily"'
    done = pwsh.run_powershell(stand_in + schedule.build_read_script(), env_extra={"NS_TEST_ACTION": action})
    assert done.returncode == 0, done.stderr
    task = schedule.parse_task_state(done.stdout)
    assert task is not None and task.home == str(home), done.stdout
    assert schedule.task_owner(Paths(home), task) == "mine"


def test_a_home_that_came_back_damaged_is_never_read_as_a_deleted_install(tmp_path: Path):
    task = schedule.TaskState(True, "05:30", False, str(tmp_path / "Night \ufffd\ufffd"))
    assert schedule.task_owner(Paths(tmp_path / "home"), task) == "unknown"


def setup_again(inst: Inst) -> tuple[Any, str]:
    res = schedule.setup_apply(inst.paths, inst.app, first=False, nightly=None, seen=None, run_time="03:00")
    return res, nightly_view.outcome_line(dataclasses.asdict(res))


def test_a_repair_over_a_task_whose_folder_is_gone_removes_it_and_says_so(inst: Inst, tmp_path: Path):
    gone = tmp_path / "Deleted Nightshift"
    assert inst.windows.task is not None
    inst.windows.task.update(home=str(gone), at="04:10")
    schedule.schedule_file(inst.paths).unlink()
    inst.windows.refuse_unregister = True                    # Windows refuses first: said, and still there
    res, line = setup_again(inst)
    assert inst.windows.task is not None and res.case == "refused" and res.on is False
    assert line.startswith(f"Automatic runs: off. A nightly task left by a deleted Nightshift ({gone}) could not be removed (")
    assert "Delete 'Nightshift Daily' in Task Scheduler" in line
    inst.windows.refuse_unregister = False
    res, line = setup_again(inst)
    assert inst.windows.task is None and res.on is False and remembered_run_time(inst.paths) == "04:10"
    assert line == (
        f"Automatic runs: off. A nightly task left by a deleted Nightshift ({gone}) was removed: it woke this PC "
        f"for nothing. Switch Automatic runs on in {nightly_view.WHERE} to run this one every night."
    )
    res, line = setup_again(inst)                            # the next Repair has nothing left to say about it
    assert inst.windows.changes().count("unregister") == 2 and "deleted Nightshift" not in line


def test_a_task_whose_drive_is_not_there_is_kept_and_said(inst: Inst):
    letter = next(c for c in "QRSTUVWXYZ" if not os.path.exists(f"{c}:\\"))
    away = f"{letter}:\\Nightshift"
    assert inst.windows.task is not None
    inst.windows.task["home"] = away
    schedule.schedule_file(inst.paths).unlink()
    before = dict(inst.windows.task)
    res, line = setup_again(inst)
    assert inst.windows.task == before and inst.windows.changes() == [] and res.on is False
    assert schedule.task_owner(inst.paths, schedule.TaskState(True, "05:30", False, away)) == "unreachable"
    assert (
        f"The nightly task runs the Nightshift at {away}, which cannot be reached now (its drive is not there); "
        "it was left as it is."
    ) in line


# ----------------------------------------------------------------- the rollback record's saved settings
def test_a_lost_swap_folder_keeps_the_rollbacks_saved_settings_in_a_file(inst: Inst, monkeypatch: pytest.MonkeyPatch):
    saved = b"PK\x05\x06" + bytes(18)                         # the settings from before the rollback, as a zip
    rollback_intent.write(inst.app, rollback_intent.Intent("0.1.16", "f-new", "0.1.15", "f-old", saved, stage="prepared"))
    assert not rollback_intent.swap_for(inst.app).exists()   # app.swap was moved away; setup made a fresh app
    monkeypatch.setattr(first_start, "program_dir", lambda: inst.app)
    with contextlib.suppress(AttributeError):
        first_start.reset()
    first_start.ensure(inst.paths.config)
    kept = sorted(inst.home.glob("app.rollback-settings-*.zip"))
    assert len(kept) == 1 and kept[0].read_bytes() == saved, [p.name for p in inst.home.iterdir()]
    assert not rollback_intent.path_for(inst.app).exists()
    words = (
        f"The settings saved before the interrupted rollback are in {kept[0]}. To use them, stop Nightshift "
        f"and unzip it into {inst.paths.config}."
    )
    logged = "".join(f.read_text(encoding="utf-8") for f in inst.paths.logs.glob("first_start-*.jsonl"))
    assert json.dumps(words, ensure_ascii=False)[1:-1] in logged
    first_start.reset()
    _, ok, detail = doctor.program_folders(inst.paths, inst.app)
    assert ok and words in detail                            # a later look still names the file
