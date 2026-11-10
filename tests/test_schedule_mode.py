"""Pre-tag item 3 (system-gaps row 3, NS-W205): a task Windows only accepted "while signed in" is not shown as
a normal nightly task. `schedule_mode` is `registered`, `interactive_only` or `none`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ns_common.models import Status
from ns_common.paths import Paths
from ns_supervisor import pwsh, schedule
from ns_supervisor.schedule_cfg import read_schedule_mode, schedule_file, write_run_time
from tests.test_dashboard_api import make_env
from tests.test_schedule_keeps_state import install_fake

SRC = Path(__file__).resolve().parents[1] / "ns_dashboard" / "ui" / "src"


def done(out: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["powershell"], 0, out, "")


def test_the_mode_is_none_without_a_task_and_registered_for_an_old_file(tmp_path: Path):
    paths = Paths(tmp_path / "ns")
    assert read_schedule_mode(paths) == "none"
    paths.config.mkdir(parents=True)
    schedule_file(paths).write_text(json.dumps({"run_time": "03:00", "task_name": "x"}), encoding="utf-8")
    assert read_schedule_mode(paths) == "registered"  # written before the mode existed
    schedule_file(paths).write_text("{broken", encoding="utf-8")
    assert read_schedule_mode(paths) == "none"


def test_ns_schedule_install_records_whether_windows_refused_the_unattended_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = Paths(tmp_path / "ns")
    fake = install_fake(monkeypatch, None)
    schedule.install(paths, "03:00", tmp_path)
    assert read_schedule_mode(paths) == "registered"
    fake.refuse_s4u = True
    msg = schedule.install(paths, "03:00", tmp_path)
    assert "only while you are logged on" in msg
    assert read_schedule_mode(paths) == "interactive_only"
    # removing the task clears the file, so the mode goes back to none
    schedule.remove(paths)
    assert read_schedule_mode(paths) == "none"


def test_status_and_settings_carry_the_mode_and_the_contract_accepts_it(tmp_home: Path):
    client, state = make_env(tmp_home)
    assert client.get("/api/status").json()["schedule_mode"] == "none"
    assert client.get("/api/settings").json()["schedule_mode"] == "none"
    write_run_time(state.paths, "03:00")
    assert client.get("/api/status").json()["schedule_mode"] == "registered"
    write_run_time(state.paths, "03:00", "interactive_only")
    body = client.get("/api/status").json()
    assert body["schedule_mode"] == "interactive_only" and body["schedule_registered"] is True
    assert client.get("/api/settings").json()["schedule_mode"] == "interactive_only"
    assert Status.model_validate(body).schedule_mode == "interactive_only"  # the contract (and its schema)


# F-E11: `schedule_registered` is "an enabled task exists" (Automatic runs on), not "schedule.json exists";
# interactive-only is a task that exists (the UI keeps saying so) and `schedule_mode` carries the difference.
# Off is every other shape, also the `switched_off` record 0.1.15-rc2 wrote for a task disabled by hand.
SHAPES: list[tuple[str, object | None, str, bool]] = [
    # (case, schedule.json content (dict is dumped, str written raw, None = no file), mode, registered)
    ("no file", None, "none", False),
    ("corrupt file", "{broken", "none", False),
    ("old file without a mode", {"run_time": "03:00", "task_name": "x"}, "registered", True),
    ("registered", {"run_time": "03:00", "task_name": "x", "mode": "registered"}, "registered", True),
    ("s4u spelling", {"run_time": "03:00", "task_name": "x", "mode": "s4u"}, "registered", True),
    ("interactive only", {"run_time": "03:00", "task_name": "x", "mode": "interactive_only"},
     "interactive_only", True),
    ("disabled (dev install)", {"run_time": "03:00", "task_name": "x", "mode": "disabled"},
     "disabled", False),
    ("switched off by the owner (D-28)", {"run_time": "03:00", "task_name": "x", "mode": "switched_off"},
     "switched_off", False),
    ("bad time", {"run_time": "25:99", "task_name": "x", "mode": "registered"}, "none", False),
]


@pytest.mark.parametrize(("case", "content", "mode", "registered"), SHAPES, ids=[s[0] for s in SHAPES])
def test_settings_report_a_task_as_registered_only_when_an_enabled_task_exists(
    tmp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    content: object | None,
    mode: str,
    registered: bool,
):
    # the fake services' schedule adapter never looks at Windows: only the record speaks here
    monkeypatch.setattr(pwsh, "run_powershell", lambda script, **kw: pytest.fail("asked the Task Scheduler"))
    client, state = make_env(tmp_home)
    if content is not None:
        state.paths.config.mkdir(parents=True, exist_ok=True)
        text = content if isinstance(content, str) else json.dumps(content)
        schedule_file(state.paths).write_text(text, encoding="utf-8")
    settings = client.get("/api/settings").json()
    assert settings["schedule_registered"] is registered
    shown = "none" if mode in ("disabled", "switched_off") else mode  # the contract has no off spellings
    assert settings["schedule_mode"] == shown
    assert read_schedule_mode(state.paths) == mode
    status = client.get("/api/status").json()
    assert status["schedule_registered"] is registered
    assert status["schedule_mode"] == shown
    assert Status.model_validate(status).schedule_registered is registered


def test_the_contract_rejects_an_unknown_mode():
    with pytest.raises(ValueError, match="schedule_mode"):
        bad = {"engine": {"state": "idle", "detail": "", "reachable": True}, "schedule_mode": "x"}
        Status.model_validate(bad)


def test_the_top_bar_and_settings_show_the_warning_text():
    lib = (SRC / "lib" / "format.ts").read_text(encoding="utf-8")
    assert "only while you are signed in" in lib and "export const interactiveOnly" in lib
    shell = (SRC / "shell" / "Shell.tsx").read_text(encoding="utf-8")
    assert (
        'status?.schedule_mode === "interactive_only"' in shell
        and "interactiveOnly(status?.schedule_time)" in shell
    )
    assert 'tone="attention"' in shell
    assert "fmtWhen(status.next_run)" in shell  # the countdown stays
    settings = (SRC / "pages" / "Settings.tsx").read_text(encoding="utf-8")
    assert 's.schedule_mode === "interactive_only"' in settings
    assert "interactiveOnly(s.schedule_time)" in settings
    types = (SRC / "lib" / "types.ts").read_text(encoding="utf-8")
    assert types.count("schedule_mode?: ScheduleMode") == 2
