"""The one answer to "is this code running under the test suite?", and the refusals built on it.

A test must never reach the machine's real Task Scheduler or keyring, whatever it does with its fakes (on
2026-11-10 a test called `monkeypatch.undo()`, which took the fake Task Scheduler away, and the rest of that
test registered the laptop's real 'Nightshift Daily' task). So the refusal does not live in the tests: it sits
at the lowest points that start the real thing (`ns_supervisor.pwsh.run_powershell`, the installer's
`SubprocessRunner.run`, `KeyringStore`, `installer/uninstall.ps1`), and there is no way to switch it off from
a test.

How "under test" is known, any one of three:

- `pytest` is imported in this process. A test cannot undo that, so in-process code is always covered.
- `NS_UNDER_TEST` is set: `tests/conftest.py` sets it when the session starts, and a child process a test
  starts inherits it (`ns schedule install` run as a child, the wizard, uninstall.ps1).
- `PYTEST_CURRENT_TEST` is set: pytest's own variable, inherited the same way, for a session started without
  this repository's conftest.

The limit: a test that starts a child with an environment it built from nothing, without either variable,
is not seen by that child. Standard library only (the installer's runner imports this).
"""

from __future__ import annotations

import base64
import contextlib
import os
import re
import sys
from collections.abc import Sequence
from typing import Any

#: Set by tests/conftest.py for the whole session and inherited by every child process a test starts.
ENV = "NS_UNDER_TEST"

#: Every way to read or change a scheduled task: the ScheduledTask cmdlets (Get-, Register-, Unregister-,
#: Enable-, Disable-, Set-, Start-, Export-ScheduledTask, New-ScheduledTask*) and schtasks.exe.
TASK_SCHEDULER = re.compile(r"ScheduledTask|schtasks", re.IGNORECASE)
_CMDLET = re.compile(r"[A-Za-z]+-ScheduledTask[A-Za-z]*", re.IGNORECASE)
_OWN_FUNCTION = re.compile(r"function\s+([A-Za-z]+-ScheduledTask[A-Za-z]*)", re.IGNORECASE)


def _only_its_own_stand_ins(text: str) -> bool:
    """True when every ScheduledTask command a script names is a FUNCTION the same script defines (in
    PowerShell a function wins over the cmdlet of that name), and it names neither schtasks nor the module
    that holds the real cmdlets. Such a script runs its own stand-ins and cannot reach the Task Scheduler:
    this is how the tests run the generated scripts under a real PowerShell. It is a property of the script,
    not a switch."""
    lowered = text.lower()
    if "schtasks" in lowered or "scheduledtasks\\" in lowered or "import-module" in lowered:
        return False
    named = {m.lower() for m in _CMDLET.findall(text)}
    own = {m.lower() for m in _OWN_FUNCTION.findall(text)}
    return bool(named) and named <= own


class RealMachineUnderTest(RuntimeError):
    """A test reached for the machine's real state. Not an `OSError` and not a `ScheduleError` on purpose:
    no handler of an ordinary failure may swallow it and carry on."""


def under_test() -> bool:
    """True when this process is a test run, or was started by one."""
    return "pytest" in sys.modules or bool(os.environ.get(ENV)) or "PYTEST_CURRENT_TEST" in os.environ


def _spelled_out(argv: Sequence[str]) -> str:
    """An argv as text, with a PowerShell `-EncodedCommand` script decoded (the installer runs them so)."""
    parts = [str(a) for a in argv]
    for i, part in enumerate(parts[:-1]):
        if part.lower() in ("-encodedcommand", "-enc", "-e", "-ec"):
            with contextlib.suppress(ValueError):
                parts.append(base64.b64decode(parts[i + 1]).decode("utf-16-le", "replace"))
    return " ".join(parts)


def refuse_task_scheduler(command: str | Sequence[str]) -> None:
    """Raise when a test is about to run `command` (a PowerShell script, or an argv) and it names the Task
    Scheduler. Let through: a script that only carries such text as DATA (the parse tests pass it in an
    environment variable, so the command does not name it), and one whose every ScheduledTask command is
    its own stand-in function (`_only_its_own_stand_ins`)."""
    if not under_test():
        return
    text = command if isinstance(command, str) else _spelled_out(command)
    if TASK_SCHEDULER.search(text) and not _only_its_own_stand_ins(text):
        raise RealMachineUnderTest(
            "a test tried to reach the real Windows Task Scheduler; this is refused under pytest and cannot "
            "be switched off. Use the fake (FakeWindows in tests/test_nightly_optional.py replaces "
            "ns_supervisor.pwsh.run_powershell; FakeRunner replaces the installer's runner)."
        )


def refuse_real_keyring(module: Any) -> None:
    """Raise when a test is about to use the real `keyring` package (Windows Credential Manager). A stand-in
    a test put in `sys.modules` has no file of its own and is let through."""
    if under_test() and getattr(module, "__file__", None):
        raise RealMachineUnderTest(
            "a test tried to reach the real Windows Credential Manager through the keyring package; this is "
            "refused under pytest. Use MemoryStore, or put a stand-in module in sys.modules."
        )
