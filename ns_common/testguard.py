"""The one answer to "is this code running under the test suite?", and the refusals built on it.

A test must never change the PC it runs on, whatever it does with its fakes (on 2026-11-10 a test called
`monkeypatch.undo()`, which took the fake Task Scheduler away, and the rest of that test registered the
laptop's real 'Nightshift Daily' task). So the refusals do not live in the tests. They sit at the lowest
points that start the real thing, and nothing a test does switches them off.

How "under test" is known, any one of three signs (`under_test`):

- `pytest` is imported in this process. A test cannot undo that, so in-process code is always covered.
- `NS_UNDER_TEST` is set: `tests/conftest.py` sets it when the session starts, and a child inherits it.
- `PYTEST_CURRENT_TEST` is set: pytest's own variable, for a session started without this conftest.

Two layers:

1. Function guards, called by the product at its own lowest call points: `refuse_task_scheduler` and
   `refuse_machine_writes` (the PowerShell runners), `refuse_real_keyring` / `real_keyring`,
   `refuse_real_shortcut`, `refuse_real_home`, `refuse_real` (dialogs, the clipboard, the user's
   environment). Each returns at once in a normal process.
2. An audit hook (`install`), added only when the process is under test. `sys.addaudithook` has no remove,
   so no test can take it away. It sees every process start of this interpreter, product code and test code
   alike, and: puts `NS_UNDER_TEST` into the child's environment (so a child given an environment built
   from nothing is still a test process), refuses a command line that would change the PC
   (`_machine_write`), refuses registry writes made through `winreg`, and refuses `os.startfile` and
   `webbrowser.open`. `ns_common/__init__.py` calls `install()`, so every product process a test starts has
   the hook too.

What this does NOT see (the limits): the text of a script a child is told to run by file name (`-File
x.ps1`, which is why every shipped script that changes the PC refuses a test run itself), a program that
changes the PC through an API with none of the watched words on its command line, and what a child does that
is neither Python with `ns_common` imported nor started with a watched command line.

Standard library only: the installer's bare-Python setup server imports this.
"""

from __future__ import annotations

import base64
import os
import re
import sys
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from pathlib import Path
from typing import Any

#: Set by tests/conftest.py for the whole session and inherited by (or put into) every child process.
ENV = "NS_UNDER_TEST"
PYTEST_ENV = "PYTEST_CURRENT_TEST"
ROOT = str(Path(__file__).resolve().parents[1])

#: The one command the hook starts WITHOUT marking its environment: `[sys.executable, "-I", "-c", OFF_PROBE,
#: ROOT]`. It is how a test proves the guard is off in a normal process. The code is fixed, imports only
#: `ns_common` from this checkout, and prints one line.
OFF_PROBE = (
    "import sys; sys.path.insert(0, sys.argv[1]); import ns_common; from ns_common import testguard as t; "
    "print('under_test=%s hook=%s' % (t.under_test(), t.hook_installed()))"
)


class RealMachineUnderTest(BaseException):
    """A test reached for the machine's real state. A `BaseException`, like `KeyboardInterrupt`, on purpose:
    the product is full of `except Exception` around best-effort work (a keyring that may be broken, a log
    line, a toast), and none of them may swallow this and carry on to the real thing or hide the refusal
    from the test. It exists only in test runs. Catch it by name, and only where "refused" is itself the
    answer (`ns_supervisor.notify.toast`)."""


def under_test(modules: Mapping[str, Any] | None = None, environ: Mapping[str, str] | None = None) -> bool:
    """True when this process is a test run, or was started by one. Reads its three signs and nothing else
    (`modules` and `environ` stand in for `sys.modules` and `os.environ`)."""
    m = sys.modules if modules is None else modules
    e = os.environ if environ is None else environ
    return "pytest" in m or bool(e.get(ENV)) or PYTEST_ENV in e


def _refused(what: str, instead: str) -> RealMachineUnderTest:
    return RealMachineUnderTest(
        f"a test tried to reach {what}; this is refused under pytest and cannot be switched off. {instead}"
    )


def refuse_real(what: str, instead: str) -> None:
    """Raise when a test is about to reach `what` (a real dialog, the clipboard, the user's environment).
    `instead` tells the test's author which fake to pass."""
    if under_test():
        raise _refused(what, instead)


# ---------------------------------------------------------------------------------------------------------
# what a command line says
# ---------------------------------------------------------------------------------------------------------
_TOKEN = re.compile(r"""[^\s"']+""")


def _is_encoded_switch(token: str) -> bool:
    """`-EncodedCommand` as PowerShell accepts it: any prefix (`-e`, `-en`, `-enc`, ... `-encodedcommand`),
    the alias `-ec`, with `-` or `/`, in any case."""
    if len(token) < 2 or token[0] not in "-/":
        return False
    name = token[1:].lower()
    return name == "ec" or "encodedcommand".startswith(name)


def _spelled_out(command: str | Sequence[str]) -> str:
    """A command (one string, or an argv) as text, followed by every `-EncodedCommand` script in it, decoded
    (also an encoded script that starts another one, three levels deep)."""
    text = command if isinstance(command, str) else " ".join(str(a) for a in command)
    parts = [text]
    todo = text
    for _ in range(3):
        tokens = _TOKEN.findall(todo)
        found: list[str] = []
        for i, token in enumerate(tokens[:-1]):
            if _is_encoded_switch(token):
                try:
                    raw = base64.b64decode(tokens[i + 1], validate=True)
                except ValueError:
                    continue
                found.append(raw.decode("utf-16-le", "replace"))
        if not found:
            break
        todo = "\n".join(found)
        parts.append(todo)
    return "\n".join(parts)


#: Every way to read or change a scheduled task: the ScheduledTask cmdlets (Get-, Register-, Unregister-,
#: Enable-, Disable-, Set-, Start-, Export-ScheduledTask, New-ScheduledTask*), schtasks.exe, the COM
#: scheduler (`New-Object -ComObject Schedule.Service`) and the PSScheduledJob cmdlets.
TASK_SCHEDULER = re.compile(r"ScheduledTask|schtasks|Schedule\.Service|ScheduledJob", re.IGNORECASE)
_CMDLET = re.compile(r"[A-Za-z]+-ScheduledTask[A-Za-z]*", re.IGNORECASE)
_OWN_FUNCTION = re.compile(r"function\s+([A-Za-z]+-ScheduledTask[A-Za-z]*)", re.IGNORECASE)
#: The ones that CHANGE a task (the hook refuses these; a test's own PowerShell may still read the real one).
_TASK_WRITE = re.compile(
    r"\b(?:Register|Unregister|Set|Enable|Disable|Start|Stop)-ScheduledTask\b"
    r"|Schedule\.Service"
    r"|[A-Za-z]+-ScheduledJob"
    r"|\bschtasks(?:\.exe)?\b[^|;&\r\n]*?[/-](?:create|delete|change|run|end)\b",
    re.IGNORECASE,
)
_ELEVATE = re.compile(r"-Verb(?:\s+|:)\s*['\"]?RunAs\b", re.IGNORECASE)
_USER_ENV = re.compile(
    r"SetEnvironmentVariable\b[^\r\n]*?,\s*(?:['\"](?:User|Machine)['\"]"
    r"|\[(?:System\.)?EnvironmentVariableTarget\]::(?:User|Machine))"
    r"|(?:^|[\s\"'&|;(\\/])setx(?:\.exe)?(?=[\s\"']|$)",
    re.IGNORECASE,
)
_REG_ROOT = re.compile(r"\b(?:HKCU|HKLM|HKCR|HKU|HKCC):|Registry::|\bHKEY_[A-Z_]+", re.IGNORECASE)
_REG_VERB = re.compile(
    r"\b(?:New|Set|Remove|Clear|Rename|Copy|Move)-Item(?:Property)?\b"
    r"|\.(?:CreateSubKey|DeleteSubKey(?:Tree)?|DeleteValue)\s*\("
    r"|Registry\]::SetValue"
    r"|\bwinreg\.(?:SetValue|CreateKey|DeleteKey|DeleteValue)",
    re.IGNORECASE,
)
_REG_EXE = re.compile(
    r"(?:^|[\s\"'&|;(\\/])reg(?:\.exe)?[\"']?\s+(?:add|delete|import|copy|restore|load|unload)\b",
    re.IGNORECASE,
)
#: The one registry target a test may write: a scratch toast identity with a name of its own, which
#: tests/test_toast_identity.py makes and removes to prove the registration script. No key of the product or
#: of the owner matches (the product's own identity is `Nightshift`, without `Test`).
_REG_SCRATCH = re.compile(
    r"HKCU:\\Software\\Classes\\AppUserModelId(?:\\|'\s*\+\s*\[char\]92\s*\+\s*')NightshiftTest[0-9A-Za-z]+"
    r"(?=['\"\s]|$)",
    re.IGNORECASE,
)
_TOAST = re.compile(r"ToastNotificationManager|CreateToastNotifier", re.IGNORECASE)
#: Explorer as the program that is started (a folder window, a settings page): not as a word in the text.
_EXPLORER = re.compile(r"^\s*\"?(?:[^\"\s]*[\\/])?explorer(?:\.exe)?\"?(?:\s|$)", re.IGNORECASE)


def _only_its_own_stand_ins(text: str) -> bool:
    """True when every ScheduledTask command a script names is a FUNCTION the same script defines (in
    PowerShell a function wins over the cmdlet of that name), and it names neither schtasks, the COM
    scheduler, a scheduled job, nor the module that holds the real cmdlets. Such a script runs its own
    stand-ins and cannot reach the Task Scheduler: this is how the tests run the generated scripts under a
    real PowerShell. It is a property of the script, not a switch."""
    lowered = text.lower()
    if "schtasks" in lowered or "scheduledtasks\\" in lowered or "import-module" in lowered:
        return False
    if "schedule.service" in lowered or "scheduledjob" in lowered:
        return False
    named = {m.lower() for m in _CMDLET.findall(text)}
    own = {m.lower() for m in _OWN_FUNCTION.findall(text)}
    return bool(named) and named <= own


def _registry_write(text: str) -> bool:
    """True when the text writes the registry anywhere but the scratch toast identity."""
    if _REG_EXE.search(text):
        return True
    if not _REG_VERB.search(text):
        return False
    roots = list(_REG_ROOT.finditer(text))
    return bool(roots) and not all(_REG_SCRATCH.match(text, m.start()) for m in roots)


def _machine_write(text: str) -> tuple[str, str] | None:
    """What a spelled-out command would change on this PC and what a test uses instead, or None."""
    if _TASK_WRITE.search(text) and not _only_its_own_stand_ins(text):
        return ("the real Windows Task Scheduler",
                "Use the fake (FakeWindows in tests/test_nightly_optional.py; FakeRunner for the installer).")
    if _ELEVATE.search(text):
        return ("an administrator (UAC) prompt",
                "Pass a fake for the elevated runner (Effects.run_elevated).")
    if _USER_ENV.search(text):
        return ("the user's or the machine's environment variables",
                "Pass a fake (Effects.set_home_var, Effects.add_user_path, FakeRunner).")
    if _registry_write(text):
        return ("the Windows registry",
                "Use FakeRunner; only the scratch key AppUserModelId\\NightshiftTest* is let through.")
    if _EXPLORER.search(text):
        return ("a real Explorer window", "Pass a fake opener (Effects, a FolderOpener).")
    if _TOAST.search(text):
        return ("a real Windows notification",
                "Pass a notifier, or read the morning note in the test's home.")
    return None


def refuse_task_scheduler(command: str | Sequence[str]) -> None:
    """Raise when a test is about to run `command` (a PowerShell script, or an argv) and it names the Task
    Scheduler in any spelling, a read as much as a change (a read shapes what the product does next). Let
    through: a script that only carries such text as DATA (the parse tests pass it in an environment
    variable, so the command does not name it), and one whose every ScheduledTask command is its own
    stand-in function (`_only_its_own_stand_ins`)."""
    if not under_test():
        return
    text = _spelled_out(command)
    if TASK_SCHEDULER.search(text) and not _only_its_own_stand_ins(text):
        raise _refused(
            "the real Windows Task Scheduler",
            "Use the fake (FakeWindows in tests/test_nightly_optional.py replaces "
            "ns_supervisor.pwsh.run_powershell; FakeRunner replaces the installer's runner).",
        )


def refuse_machine_writes(command: str | Sequence[str]) -> None:
    """Raise when a test is about to run `command` and it would change this PC: a scheduled task, an
    administrator prompt, the user's or the machine's environment, the registry, a notification."""
    if not under_test():
        return
    found = _machine_write(_spelled_out(command))
    if found is not None:
        raise _refused(*found)


# ---------------------------------------------------------------------------------------------------------
# the keyring
# ---------------------------------------------------------------------------------------------------------
def real_keyring(module: Any) -> bool:
    """True when this is a test run and `module` is the real `keyring` package (Windows Credential Manager).
    A stand-in a test put in `sys.modules` has no file of its own."""
    return under_test() and bool(getattr(module, "__file__", None))


def refuse_real_keyring(module: Any) -> None:
    """Raise when a test is about to use the real `keyring` package."""
    if real_keyring(module):
        raise _refused(
            "the real Windows Credential Manager through the keyring package",
            "Use MemoryStore, or put a stand-in module in sys.modules.",
        )


# ---------------------------------------------------------------------------------------------------------
# the real shell folders and the real home (read once, at import, by reading only)
# ---------------------------------------------------------------------------------------------------------
_CSIDL = {
    "desktop": 0x10, "public_desktop": 0x19, "start_menu": 0x0B, "common_start_menu": 0x16,
    "appdata": 0x1A, "local_appdata": 0x1C, "profile": 0x28,
}


def _shell_folder(csidl: int) -> str | None:
    """A folder as the Windows shell knows it (it follows a Desktop moved into OneDrive, and does not read
    the environment variables a test may have changed). None off Windows or when it cannot be read."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) != 0:
            return None
    except (ImportError, AttributeError, OSError, ValueError):
        return None
    return buf.value or None


def _user_home_variable() -> str | None:
    """`NIGHTSHIFT_HOME` as the owner's own account has it (HKCU, Environment): the home of a real install."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, "NIGHTSHIFT_HOME")[0]) or None
    except (ImportError, OSError, ValueError):
        return None


def _norm(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _inside(path: str | os.PathLike[str], folders: Sequence[str]) -> bool:
    p = _norm(path)
    return any(p == f or p.startswith(f.rstrip("\\/") + os.sep) for f in folders)


def folders_from(known: Mapping[str, str | None]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(the folders a real shortcut lives in, the real homes), from what the shell said. Pure."""
    def join(key: str, *more: str) -> str | None:
        base = known.get(key)
        return os.path.join(base, *more) if base else None

    shortcuts = [
        join("desktop"), join("public_desktop"), join("start_menu"), join("common_start_menu"),
        join("appdata", "Microsoft", "Internet Explorer", "Quick Launch"),     # holds the taskbar pins too
        join("profile", "Desktop"),                                            # where it was before OneDrive
    ]
    homes = [
        join("local_appdata", "Nightshift"), join("profile", "AppData", "Local", "Nightshift"),
        join("home_variable"),
    ]

    def uniq(items: list[str | None]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(_norm(i) for i in items if i))

    return uniq(shortcuts), uniq(homes)


def _read_known() -> dict[str, str | None]:
    known: dict[str, str | None] = {name: _shell_folder(csidl) for name, csidl in _CSIDL.items()}
    if known["profile"] is None:            # not Windows: what the environment says, as it was at import
        known["profile"] = os.path.expanduser("~")
        known["local_appdata"] = os.environ.get("LOCALAPPDATA")
    known["home_variable"] = _user_home_variable()
    return known


#: Where a real shortcut lives (Desktop, Public Desktop, Start menu, Quick Launch) and the real homes
#: (`%LOCALAPPDATA%/Nightshift`, the owner's NIGHTSHIFT_HOME). Empty in a normal process: nothing is read.
REAL_FOLDERS: tuple[str, ...] = ()
REAL_HOMES: tuple[str, ...] = ()
if under_test():
    REAL_FOLDERS, REAL_HOMES = folders_from(_read_known())


def refuse_real_shortcut(lnk: str | os.PathLike[str] | None, folders: Sequence[str] | None = None) -> None:
    """Raise when a test is about to create or change a shortcut in (or under) a real shell folder. This is
    the ONE refusal for shortcuts and the Desktop, over the one list `REAL_FOLDERS`: `winenv.create_shortcut`,
    `update_shortcut` and `set_shortcut_aumid` pass the shortcut, whatever the runner. `lnk` None: the caller
    is about to ask the real Windows where the Desktop is, to work on what is there (`winenv.desktop_dir`
    and `launch_reach.run` with the real runner): refused for any folder."""
    if not under_test():
        return
    if lnk is None:
        raise _refused(
            "this PC's real Desktop (asking Windows where it is, to change the shortcuts on it)",
            "Hand the code a Desktop folder under tmp_path, or a stand-in runner.",
        )
    if _inside(lnk, REAL_FOLDERS if folders is None else folders):
        raise _refused(
            f"a shortcut in a real shell folder ({lnk})",
            "Give the shortcut a folder under tmp_path (FakeRunner(tmp_path / 'desktop')).",
        )


def refuse_real_home(home: str | os.PathLike[str], homes: Sequence[str] | None = None) -> None:
    """Raise when a test is about to use the real Nightshift home of this PC's owner."""
    if under_test() and _inside(home, REAL_HOMES if homes is None else homes):
        raise _refused(
            f"the real Nightshift home ({home})",
            "Use the tmp_home fixture, or set NIGHTSHIFT_HOME to a folder under tmp_path.",
        )


# ---------------------------------------------------------------------------------------------------------
# the audit hook
# ---------------------------------------------------------------------------------------------------------
#: Access bits that let a registry handle change something: KEY_SET_VALUE, KEY_CREATE_SUB_KEY,
#: KEY_CREATE_LINK, DELETE, WRITE_DAC, WRITE_OWNER, MAXIMUM_ALLOWED, GENERIC_ALL, GENERIC_WRITE.
_REG_WRITE_BITS = (
    0x0002 | 0x0004 | 0x0020 | 0x00010000 | 0x00040000 | 0x00080000 | 0x02000000 | 0x10000000 | 0x40000000
)
_REG_ALWAYS = frozenset({
    "winreg.CreateKey", "winreg.DeleteKey", "winreg.DeleteValue", "winreg.SetValue", "winreg.LoadKey",
    "winreg.SaveKey",
})
_STARTS = frozenset({
    "subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "_winapi.CreateProcess",
})
_WINDOWS = frozenset({"os.startfile", "os.startfile/2", "webbrowser.open"})
_WATCHED = _STARTS | _REG_ALWAYS | _WINDOWS | {"winreg.OpenKey"}
_HOOK_MARK = "_ns_testguard_hook"


def _is_off_probe(executable: Any, args: Any) -> bool:
    want = [sys.executable, "-I", "-c", OFF_PROBE, ROOT]
    if executable not in (None, sys.executable):
        return False
    if isinstance(args, str):               # Windows: Popen has already joined the argv into one line
        joiner = getattr(sys.modules.get("subprocess"), "list2cmdline", None)   # the event came from it
        return joiner is not None and args == joiner(want)
    try:
        return [os.fspath(a) for a in args] == want
    except TypeError:
        return False


def _mark_child(env: Any) -> None:
    """Make the child a test process: `NS_UNDER_TEST=1` goes into the environment it is about to get."""
    if env is None or env is os.environ:
        if not os.environ.get(ENV):
            os.environ[ENV] = "1"           # a test removed it: put back before the child inherits
        return
    if isinstance(env, Mapping) and env.get(ENV):
        return
    if isinstance(env, MutableMapping):
        env[ENV] = "1"                      # the very mapping the start then uses
        return
    raise _refused(
        "a child process whose environment cannot be marked as a test's",
        f"Pass a dict as env, or one that has {ENV}=1.",
    )


def _command_text(parts: Sequence[Any]) -> str:
    out: list[str] = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, (str, bytes, os.PathLike)):
            out.append(os.fsdecode(part))
        else:
            try:
                out.extend(os.fsdecode(p) for p in part)
            except TypeError:
                out.append(str(part))
    return " ".join(out)


def _on_event(event: str, args: tuple[Any, ...]) -> None:
    if event in _STARTS:
        env: Any = None
        if event == "subprocess.Popen":
            executable, argv, _cwd, env = args
            if _is_off_probe(executable, argv):
                return
            text = _command_text((executable, argv))
        elif event == "os.system":
            text = _command_text(args[:1])
        elif event == "_winapi.CreateProcess":
            text = _command_text(args[:2])
        elif event == "os.spawn":
            text, env = _command_text(args[1:3]), args[3]
        else:                               # os.exec, os.posix_spawn: (path, args, env)
            text, env = _command_text(args[:2]), args[2]
        found = _machine_write(_spelled_out(text))
        if found is not None:
            raise _refused(*found)
        _mark_child(env)
        return
    if event == "winreg.OpenKey":
        if len(args) > 2 and isinstance(args[2], int) and args[2] & _REG_WRITE_BITS:
            raise _refused("the Windows registry (a key opened for writing)", "Read it, or pass a fake.")
        return
    if event in _REG_ALWAYS:
        raise _refused(f"the Windows registry ({event})", "Pass a fake (Effects.set_home_var, FakeRunner).")
    raise _refused(                         # os.startfile, webbrowser.open: a window, a browser, a player
        f"a real window ({args[0] if args else ''})", "Pass a fake opener (startfile=..., a FolderOpener).",
    )


def _hook(
    event: str, args: tuple[Any, ...],
    _watched: frozenset[str] = _WATCHED, _handle: Callable[[str, tuple[Any, ...]], None] = _on_event,
) -> None:
    if event not in _watched:               # first line: almost every event leaves here
        return
    _handle(event, args)


def install() -> bool:
    """Add the audit hook when this process is under test (once per interpreter). True when it is in place.
    In a normal process this does nothing and returns False."""
    if getattr(sys, _HOOK_MARK, False):
        return True
    if not under_test():
        return False
    sys.addaudithook(_hook)
    setattr(sys, _HOOK_MARK, True)
    return True


def hook_installed() -> bool:
    """True when the audit hook is in place in this interpreter."""
    return bool(getattr(sys, _HOOK_MARK, False))
