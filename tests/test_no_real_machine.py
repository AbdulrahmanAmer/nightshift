# ruff: noqa: E501
"""No test can reach this PC's real machine state, whatever it does with its fakes.

On 2026-11-10 a test called `monkeypatch.undo()`; that also removed the fake Task Scheduler, and the rest of
the test registered the laptop's real 'Nightshift Daily' task. The refusals therefore live in the product, at
the lowest points that start the real thing, and in an audit hook no test can remove
(`ns_common/testguard.py`). These tests install NO fake of a guard and call the real routes.

How they stay harmless themselves, also on a checkout that has no guard yet: where the guarded function would
start something, a recorder sits BELOW the guard (`subprocess.run`, a stand-in `winreg`, a recording runner)
and must never be reached; where the audit hook is the guard, the command is a Python child that only writes a
file under `tmp_path` and merely CARRIES the dangerous words as an extra argument. The real state is only ever
read (`Get-ScheduledTask`, a registry read).
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import os
import re
import subprocess
import sys
import types
import warnings
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from installer.stage2 import winenv
from installer.stage2.runner import RunResult, SubprocessRunner
from installer.stage2.secrets import KeyringStore
from installer.stage2.web import bootstrap, controller, folderdialog
from ns_common.paths import Paths
from ns_supervisor import notify, pwsh, schedule
from ns_supervisor.schedule_cfg import write_run_time

# The guard lives in ns_common. The fallback is only for the failing-first run of this file on the commit before
# it moved there: every new name is then missing and the tests below fail at their assertions.
_HAS_NEW = importlib.util.find_spec("ns_common.testguard") is not None
testguard: Any = importlib.import_module("ns_common.testguard" if _HAS_NEW else "ns_supervisor.testguard")
RealMachineUnderTest: Any = testguard.RealMachineUnderTest
BS = chr(92)

ROOT = Path(__file__).resolve().parents[1]
needs_powershell = pytest.mark.skipif(pwsh.find_powershell() is None, reason="needs PowerShell (Windows)")

READ_ONLY = "; ".join([
    "$t = Get-ScheduledTask -TaskName 'Nightshift Daily' -ErrorAction SilentlyContinue",
    "if ($null -eq $t) { 'none' } else { 'state=' + $t.State; 'args=' + (@($t.Actions) | Select-Object -First 1).Arguments; "
    "'start=' + (@($t.Triggers) | Select-Object -First 1).StartBoundary; 'logon=' + $t.Principal.LogonType }",
])


def powershell(command: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """The test's own PowerShell, outside the product's runners."""
    exe = pwsh.find_powershell()
    assert exe is not None
    return subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, env=env,
        stdin=subprocess.DEVNULL, timeout=120,
    )


def real_task() -> str:
    """What Windows holds for 'Nightshift Daily' right now. The test's own look: a read, nothing else."""
    done = powershell(READ_ONLY)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.fixture
def home(tmp_path: Path) -> Paths:
    paths = Paths(tmp_path / "home")
    paths.ensure()
    return paths


def new(name: str) -> Any:
    """A name this item adds to the guard (None on a checkout that does not have it yet)."""
    return getattr(testguard, name, None)


def refused(call: Callable[[], Any]) -> bool:
    """True when `call` was stopped by the guard, and by nothing else."""
    try:
        call()
    except RealMachineUnderTest:
        return True
    except Exception:  # noqa: BLE001 - any other end means the guard did not stop it
        return False
    return False


def hook_is_on() -> bool:
    return bool(new("hook_installed")) and testguard.hook_installed() is True


class Started:
    """Stands in for `subprocess.run`, BELOW the guards: a guard that works never lets a call reach it."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def __call__(self, argv: Any, *a: Any, **kw: Any) -> subprocess.CompletedProcess[Any]:
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")


class RecordingRunner:
    """An installer runner that starts nothing and remembers the tags it was asked for."""

    def __init__(self) -> None:
        self.tags: list[str] = []

    def run(self, argv: Any, **kw: Any) -> RunResult:
        self.tags.append(str(kw.get("tag", "")))
        return RunResult(0)


def parsed(source: str) -> ast.AST:
    """The syntax tree of a file, without the warnings its own string escapes would print here."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(source)


def carrier(sentinel: Path) -> list[str]:
    """A child that only creates `sentinel`. Extra arguments are ignored by it and read by the hook."""
    return [sys.executable, "-c", "import sys; open(sys.argv[1], 'w').close()", str(sentinel)]


PRINT_MARK = "import os; print(os.environ.get('NS_UNDER_TEST'))"
SCRATCH_ENV = {"SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}  # an environment built from nothing


def test_the_session_is_marked_for_every_child_process():
    assert os.environ.get(testguard.ENV) == "1"  # tests/conftest.py, at import: no fixture can undo it
    assert testguard.under_test() is True
    assert hook_is_on(), "tests/conftest.py did not put the audit hook in place"
    # a test with no home of its own falls back to a scratch home, never to the owner's install
    assert os.environ.get("NIGHTSHIFT_HOME") and not refused(Paths.from_env)


# ------------------------------------------------------------------------------- off in a normal process
BASE_PROBE = (
    "import sys; sys.path.insert(0, sys.argv[1]); from ns_supervisor import testguard as t; "
    "print('under_test=%s hook=%s' % (t.under_test(), getattr(t, 'hook_installed', lambda: None)()))"
)


def test_the_guard_is_off_in_a_normal_process():
    """The one child the hook starts without marking it: the fixed probe, by its exact command line."""
    probe = new("OFF_PROBE") or BASE_PROBE
    bare = {k: v for k, v in os.environ.items() if k not in (testguard.ENV, "PYTEST_CURRENT_TEST")}
    done = subprocess.run([sys.executable, "-I", "-c", probe, str(ROOT)], capture_output=True, text=True, env=bare, timeout=120)
    assert done.stdout.strip() == "under_test=False hook=False", (done.stdout, done.stderr)
    assert testguard.ENV not in bare
    # no other command line escapes: one more argument, and the same code is a test process again
    marked = subprocess.run([sys.executable, "-I", "-c", probe, str(ROOT), "x"], capture_output=True, text=True, env=dict(bare), timeout=120)
    assert marked.stdout.strip() == "under_test=True hook=True", (marked.stdout, marked.stderr)


def test_under_test_reads_only_its_three_signs():
    def sign(modules: dict[str, Any], environ: dict[str, str]) -> bool | None:
        try:
            return testguard.under_test(modules=modules, environ=environ)
        except TypeError:  # a guard that cannot be asked about anything but this process
            return None

    assert sign({}, {}) is False
    assert sign({"pytest": pytest}, {}) is True
    assert sign({}, {"NS_UNDER_TEST": "1"}) is True
    assert sign({}, {"PYTEST_CURRENT_TEST": "tests/x.py::test_y (call)"}) is True
    assert sign({}, {"NS_UNDER_TEST": ""}) is False
    assert sign({"os": os, "ns_common": object()}, {"NIGHTSHIFT_HOME": "x", "NS_DEV": "1", "PATH": "y"}) is False


PRODUCT_DIRS = ("installer", "launchers", "dev", "tools")
MARKS = ("NS_UNDER_TEST", "PYTEST_CURRENT_TEST")


def product_files(*suffixes: str) -> list[Path]:
    found: list[Path] = []
    for folder in sorted(p for p in ROOT.iterdir() if p.is_dir() and (p.name.startswith("ns_") or p.name in PRODUCT_DIRS)):
        found += [f for f in folder.rglob("*") if f.suffix in suffixes and "node_modules" not in f.parts]
    return found


def test_nothing_the_product_ships_sets_the_test_marks_or_imports_pytest():
    """In production nothing is under test: no shipped file imports pytest or names a mark, except the guard
    (which reads them, and whose hook only runs in a test process) and the scripts that refuse a test run."""
    names = {p.relative_to(ROOT).parts[0] for p in product_files(".py")}
    assert {"ns_common", "ns_supervisor", "ns_studio", "installer", "dev"} <= names  # the scan sees the product
    for path in product_files(".py"):
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(parsed(path.read_text(encoding="utf-8"))):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not [m for m in mods if m.split(".")[0] in ("pytest", "_pytest")], f"{rel} imports pytest"
            if rel.endswith("/testguard.py"):
                continue
            assert not (isinstance(node, ast.Constant) and node.value in MARKS), f"{rel} names a test mark"
            named = isinstance(node, ast.Attribute) and node.attr in ("ENV", "PYTEST_ENV") and "testguard" in ast.unparse(node.value)
            assert not named, f"{rel} line {getattr(node, 'lineno', 0)} uses the guard's mark"
    for path in product_files(".ps1", ".cmd"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for mark in MARKS:
            assert not re.search(r'(?:\$env:|set\s+"?)' + mark + r"\s*=(?!=)", text, re.IGNORECASE), f"{path.name} sets {mark}"


# ------------------------------------------------------------------------- a child is a test process too
def test_a_child_given_a_from_scratch_environment_still_knows_it_is_a_test():
    done = subprocess.run([sys.executable, "-c", PRINT_MARK], capture_output=True, text=True, env=dict(SCRATCH_ENV), timeout=120)
    assert done.stdout.strip() == "1", (done.stdout, done.stderr)
    # the handover goes on: a product child given nothing has the hook, and marks ITS child given nothing
    code = (
        "import os, subprocess, sys; sys.path.insert(0, sys.argv[1]); import ns_common; "
        "t = sys.modules.get('ns_common.testguard'); "
        "r = subprocess.run([sys.executable, '-c', sys.argv[2]], env={'SYSTEMROOT': os.environ.get('SYSTEMROOT', '')}, capture_output=True, text=True); "
        "print(bool(t and t.hook_installed()), r.stdout.strip())"
    )
    child = subprocess.run([sys.executable, "-c", code, str(ROOT), PRINT_MARK], capture_output=True, text=True, env=dict(SCRATCH_ENV), timeout=120)
    assert child.stdout.strip() == "True 1", (child.stdout, child.stderr)
    frozen: Any = types.MappingProxyType(SCRATCH_ENV)  # an environment that cannot be marked is refused
    assert refused(lambda: subprocess.run([sys.executable, "-c", PRINT_MARK], env=frozen, capture_output=True, timeout=120))


def test_a_test_that_removes_the_mark_does_not_lift_it_for_its_children(monkeypatch: pytest.MonkeyPatch):
    for attempt in ("first", "second"):  # and the second time is the same
        monkeypatch.delenv(testguard.ENV, raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        done = subprocess.run([sys.executable, "-c", PRINT_MARK], capture_output=True, text=True, timeout=120)
        assert done.stdout.strip() == "1", (attempt, done.stdout, done.stderr)
        assert os.environ.get(testguard.ENV) == "1", attempt  # put back before the child was started


# ---------------------------------------------------------------- the user's environment and the registry
def user_environment() -> dict[str, str]:
    """HKCU, Environment as it is now: a read."""
    if sys.platform != "win32":
        return {}
    import winreg

    found: dict[str, str] = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        for i in range(winreg.QueryInfoKey(key)[1]):
            name, value, _ = winreg.EnumValue(key, i)
            found[name] = str(value)
    return found


def test_the_user_environment_writers_refuse_under_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    before = user_environment()
    opened: list[Any] = []

    def open_key(*a: Any, **kw: Any) -> Any:
        opened.append(a)
        raise OSError("the stand-in registry was reached")

    stand_in = types.SimpleNamespace(
        HKEY_CURRENT_USER=1, KEY_SET_VALUE=2, KEY_READ=0x20019, REG_SZ=1, REG_EXPAND_SZ=2, OpenKey=open_key,
    )
    started = Started()
    fx = controller.Effects()
    writers = {
        "set_user_variable": lambda: bootstrap.set_user_variable("NIGHTSHIFT_GUARD_PROBE", "x"),
        "add_user_path_entry": lambda: bootstrap.add_user_path_entry(str(tmp_path)),
        "Effects.set_home_var": lambda: fx.set_home_var("NIGHTSHIFT_GUARD_PROBE", "x"),
        "Effects.add_user_path": lambda: fx.add_user_path(str(tmp_path)),
        "winenv.set_user_env": lambda: winenv.set_user_env(SubprocessRunner(), "NIGHTSHIFT_GUARD_PROBE", "x"),
    }
    with monkeypatch.context() as m:
        m.setitem(sys.modules, "winreg", stand_in)
        m.setattr(subprocess, "run", started)
        for name, write in writers.items():
            assert refused(write), name
            assert refused(write), name + ", again"
    assert opened == [] and started.calls == []
    assert user_environment() == before
    assert bootstrap.get_user_variable("NIGHTSHIFT_GUARD_PROBE") is None  # reading is not refused


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows registry")
def test_registry_writes_from_this_process_are_refused_and_reads_are_not():
    import winreg

    assert hook_is_on(), "without the audit hook the calls below would write the registry: not made"
    hkcu, probe = winreg.HKEY_CURRENT_USER, "Software" + BS + "NightshiftGuardProbe"
    writes = {
        "CreateKeyEx": lambda: winreg.CreateKeyEx(hkcu, probe, 0, winreg.KEY_WRITE),
        "CreateKeyEx, read access": lambda: winreg.CreateKeyEx(hkcu, probe, 0, winreg.KEY_READ),
        "CreateKey": lambda: winreg.CreateKey(hkcu, probe),
        "SetValue": lambda: winreg.SetValue(hkcu, probe, winreg.REG_SZ, "x"),
        "OpenKey to set a value": lambda: winreg.OpenKey(hkcu, "Environment", 0, winreg.KEY_SET_VALUE),
        "OpenKey, all access": lambda: winreg.OpenKey(hkcu, "Environment", 0, winreg.KEY_ALL_ACCESS),
        "OpenKeyEx to write": lambda: winreg.OpenKeyEx(hkcu, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE),
        "DeleteKey": lambda: winreg.DeleteKey(hkcu, probe + "-absent"),
        "DeleteValue": lambda: winreg.DeleteValue(hkcu, "NightshiftGuardProbe-absent"),
    }
    for name, write in writes.items():
        assert refused(write), name
    with pytest.raises(FileNotFoundError):
        winreg.OpenKey(hkcu, probe)  # nothing was made
    winreg.OpenKey(hkcu, "Environment", 0, winreg.KEY_READ).Close()
    winreg.OpenKey(hkcu, "Environment").Close()


def test_only_the_scratch_toast_identity_may_be_written(monkeypatch: pytest.MonkeyPatch):
    """`tests/test_toast_identity.py` proves the registration script on a key with a name of its own; the
    product's key (`Nightshift`) and anything beside the scratch name are refused."""
    started = Started()
    classes = "HKCU:" + BS + "Software" + BS + "Classes"
    with monkeypatch.context() as m:
        m.setattr(subprocess, "run", started)
        assert refused(lambda: winenv.register_aumid(SubprocessRunner(), "Nightshift", "Nightshift"))
        assert refused(lambda: winenv.unregister_aumid(SubprocessRunner(), "Nightshift"))
        assert refused(lambda: winenv.register_aumid(SubprocessRunner(), "NightshiftTest" + BS + "..", "x"))
        assert refused(lambda: winenv.run_ps(SubprocessRunner(), f"Remove-Item -LiteralPath '{classes}' -Recurse", tag="t"))
        assert started.calls == []
        assert winenv.register_aumid(SubprocessRunner(), "NightshiftTest0a1b2c3d", "Nightshift") is True
        assert winenv.unregister_aumid(SubprocessRunner(), "NightshiftTest0a1b2c3d") is True
    assert len(started.calls) == 2


# ------------------------------------------------------------------- any child, product code or test code
CHANGES = {
    "elevation": ["Start-Process vc_redist.x64.exe -Verb RunAs -Wait"],
    "register a task": ["Register-ScheduledTask -TaskName 'Nightshift Daily' -InputObject $t -Force"],
    "enable a task": ["Enable-ScheduledTask -TaskName 'Nightshift Daily'"],
    "schtasks": ["schtasks.exe", "/Create", "/TN", "Nightshift Daily", "/TR", "x"],
    "reg add": ["reg", "add", "HKCU" + BS + "Software" + BS + "X", "/v", "a", "/d", "b", "/f"],
    "setx": ["setx", "NIGHTSHIFT_HOME", "X:/nowhere"],
    "user variable": ["[Environment]::SetEnvironmentVariable('NIGHTSHIFT_HOME', 'x', 'User')"],
    "registry value": ["Set-ItemProperty HKLM:" + BS + "SYSTEM" + BS + "CurrentControlSet" + BS + "Control" + BS + "FileSystem LongPathsEnabled 1"],
    "a toast": ["[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Nightshift').Show($t)"],
    "encoded": ["-enc", pwsh.encode("Unregister-ScheduledTask -TaskName 'Nightshift Daily' -Confirm:$false")],
}


def test_any_child_that_would_elevate_or_write_machine_state_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sentinel = tmp_path / "started.txt"
    for attempt in ("first", "retried"):  # a refusal leaves nothing behind: the retry meets the same refusal
        for name, words in CHANGES.items():
            assert refused(lambda words=words: subprocess.run([*carrier(sentinel), *words], timeout=120)), (name, attempt)
            assert not sentinel.exists(), (name, attempt)
    assert refused(lambda: subprocess.Popen([*carrier(sentinel), *CHANGES["elevation"]]))
    assert refused(lambda: os.system('echo Register-ScheduledTask -TaskName x > "' + str(sentinel) + '"'))
    assert not sentinel.exists()
    subprocess.run([*carrier(sentinel), "Get-Date"], timeout=120, check=True)  # the words are refused, not the child
    assert sentinel.exists()

    started = Started()
    with monkeypatch.context() as m:  # the two product functions that ask for administrator rights
        m.setattr(subprocess, "run", started)
        assert refused(controller.elevate_longpaths)
        assert refused(lambda: bootstrap.run_elevated_exe(Path("C:/dl/vc_redist.x64.exe"), ["/install", "/quiet"]))
    assert started.calls == []


def test_a_child_that_only_reads_the_scheduler_still_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The hook sees test code too and refuses changes only, so a test may look at the real task; the
    product's own runners refuse the read as well (what is read shapes what the product does next)."""
    reads = (["Get-ScheduledTask -TaskName 'Nightshift Daily'"], ["schtasks.exe", "/Query", "/TN", "Nightshift Daily"], ["Export-ScheduledTask x"])
    for i, words in enumerate(reads):
        sentinel = tmp_path / f"read{i}.txt"
        subprocess.run([*carrier(sentinel), *words], timeout=120, check=True)
        assert sentinel.exists(), words
    started = Started()
    with monkeypatch.context() as m:
        m.setattr(subprocess, "run", started)
        assert refused(lambda: SubprocessRunner().run(["powershell.exe", "-NoProfile", "-Command", "Get-ScheduledTask"]))
        assert refused(lambda: pwsh.run_powershell("Export-ScheduledTask -TaskName 'Nightshift Daily'"))
    assert started.calls == []


def test_every_spelling_of_encoded_command_and_the_other_scheduler_doors_are_seen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    read = pwsh.encode("Get-ScheduledTask -TaskName 'Nightshift Daily'")
    change = pwsh.encode("Unregister-ScheduledTask -TaskName 'Nightshift Daily' -Confirm:$false")
    spellings = ("-e", "-ec", "-en", "-enc", "-encodedc", "-EncodedCommand", "/EncodedCommand", "-ENCODEDCOMMAND", "/enc")
    doors = (
        "$s = New-Object -ComObject Schedule.Service; $s.Connect()",
        "Register-ScheduledJob -Name x -ScriptBlock { 1 }",
        "Get-ScheduledJob",
        "powershell -NoProfile -en " + read,  # one encoded script inside another command
    )
    started = Started()
    with monkeypatch.context() as m:
        m.setattr(subprocess, "run", started)
        for switch in spellings:
            assert refused(lambda s=switch: SubprocessRunner().run(["powershell.exe", "-NoProfile", s, read])), switch
        for door in doors:
            assert refused(lambda d=door: pwsh.run_powershell(d)), door
            assert refused(lambda d=door: SubprocessRunner().run(["powershell.exe", "-Command", d])), door
            assert refused(lambda d=door: winenv.run_ps(SubprocessRunner(), d, tag="t")), door
        # not every short switch is that one, and an encoded script that is harmless runs
        assert SubprocessRunner().run(["powershell.exe", "-ex", "Bypass", "-enc", pwsh.encode("Get-Date")]).ok
    assert len(started.calls) == 1
    sentinel = tmp_path / "started.txt"
    for switch in spellings:  # the same spellings when test code starts the child itself
        assert refused(lambda s=switch: subprocess.run([*carrier(sentinel), s, change], timeout=120)), switch
    assert refused(lambda: subprocess.run([*carrier(sentinel), doors[0]], timeout=120))
    assert refused(lambda: subprocess.run([*carrier(sentinel), doors[1]], timeout=120))
    assert not sentinel.exists()


# ------------------------------------------------------------------------------------------ the keyring
def test_the_studio_keyring_read_treats_the_real_package_as_absent_under_test(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    from ns_studio import jobs

    asked: list[str] = []

    def get_password(service: str, name: str) -> str:
        asked.append(name)
        return "KEY"

    installed = types.ModuleType("keyring")  # what the real package looks like: it has a file
    installed.__file__ = str(Path(sys.prefix) / "Lib" / "site-packages" / "keyring" / "__init__.py")
    setattr(installed, "get_password", get_password)  # noqa: B010 - a module has no such attribute to assign
    monkeypatch.setitem(sys.modules, "keyring", installed)
    assert jobs.load_keys() == {} and asked == []
    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(get_password=get_password))
    assert jobs.load_keys() == {"pexels": "KEY", "pixabay": "KEY"} and len(asked) == 2  # a stand-in, as before


# ------------------------------------------------------------------------------------- shortcuts, the home
@pytest.mark.skipif(sys.platform != "win32", reason="the Windows shell folders")
def test_shortcuts_into_the_real_shell_folders_are_refused(tmp_path: Path):
    folders = new("REAL_FOLDERS")
    assert folders, "the guard does not know this PC's real shell folders"
    here = {os.path.normcase(f) for f in folders}  # this machine's own, whatever its user and drive
    for expected in (
        Path(os.environ["USERPROFILE"]) / "Desktop", Path(os.environ["PUBLIC"]) / "Desktop",
        Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu",
        Path(os.environ["APPDATA"]) / "Microsoft" / "Internet Explorer" / "Quick Launch",
    ):
        assert os.path.normcase(str(expected)) in here, expected
    runner = RecordingRunner()
    for folder in folders:
        for lnk in (Path(folder) / "Nightshift Guard Probe.lnk", Path(folder) / "Programs" / "Nightshift" / "Probe.lnk",
                    Path(folder.upper()) / "x.lnk", Path(folder) / "User Pinned" / "TaskBar" / "Probe.lnk"):
            assert refused(lambda lnk=lnk: winenv.create_shortcut(runner, lnk, "powershell.exe", "-NoProfile", tmp_path, "probe")), lnk
            assert refused(lambda lnk=lnk: winenv.set_shortcut_aumid(runner, lnk, "NightshiftTestProbe")), lnk
    assert runner.tags == []
    # a Desktop that OneDrive moved is the real Desktop, and the place it was before stays refused
    moved, _ = testguard.folders_from({"desktop": "C:/Users/u/OneDrive/Desktop", "profile": "C:/Users/u"})
    assert refused(lambda: testguard.refuse_real_shortcut("C:/Users/u/OneDrive/Desktop/Nightshift.lnk", moved))
    assert refused(lambda: testguard.refuse_real_shortcut("C:/Users/u/Desktop/Nightshift.lnk", moved))
    testguard.refuse_real_shortcut("C:/Users/u/Documents/Nightshift.lnk", moved)
    assert winenv.create_shortcut(runner, tmp_path / "desktop" / "Nightshift.lnk", "powershell.exe", "-NoProfile", tmp_path, "d") is True
    assert winenv.set_shortcut_aumid(runner, tmp_path / "desktop" / "Nightshift.lnk", "Nightshift") is True
    assert runner.tags == ["ps:shortcut", "ps:aumid-shortcut"]


def test_the_real_default_home_is_refused_under_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    homes = new("REAL_HOMES")
    assert homes, "the guard does not know this PC's real Nightshift home"
    real_local = os.path.dirname(homes[0])
    monkeypatch.delenv("NIGHTSHIFT_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", real_local)
    assert refused(Paths.from_env)
    monkeypatch.delenv("LOCALAPPDATA")
    assert refused(Paths.from_env)  # the fallback under the user's profile
    assert refused(lambda: Paths.from_env({"LOCALAPPDATA": real_local}))
    assert refused(lambda: Paths.from_env({"NIGHTSHIFT_HOME": homes[0]}))
    assert refused(lambda: Paths.from_env({"NIGHTSHIFT_HOME": os.path.join(homes[0], "config", "..")}))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert Paths.from_env().home == tmp_path / "Nightshift"
    assert Paths.from_env({"LOCALAPPDATA": str(tmp_path / "other")}).home == tmp_path / "other" / "Nightshift"
    assert Paths.from_env({"NIGHTSHIFT_HOME": str(tmp_path / "home")}).home == tmp_path / "home"


# ------------------------------------------------------------------------- notifications, windows, prompts
def test_a_real_toast_is_never_shown_under_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    started = Started()
    paths = Paths(tmp_path / "home")
    with monkeypatch.context() as m:
        m.setattr(subprocess, "run", started)
        assert notify.toast("Nightshift", "probe") is False  # unavailable is a no-op, as its contract says
        assert refused(lambda: pwsh.run_powershell(notify.TOAST_SCRIPT))
        assert refused(lambda: winenv.run_ps(SubprocessRunner(), notify.TOAST_SCRIPT, tag="t"))
        # what the owner's side becomes: the message waits as a morning note in the test's home
        assert notify.toast_or_note("Nightshift", "2 Shorts ready", paths=paths, interactive=lambda: True) is False
    assert started.calls == []
    assert [n["message"] for n in notify.read_notes(paths)] == ["2 Shorts ready"]


def test_opening_windows_and_prompts_is_refused_under_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    assert hook_is_on(), "without the audit hook the calls below would open real windows: not made"
    nothing = str(tmp_path / "nothing-here.txt")
    starter = getattr(os, "startfile", None)
    if starter is not None:
        assert refused(lambda: starter(nothing))
        assert refused(lambda: starter(nothing, "open"))
    assert refused(lambda: webbrowser.open(nothing))
    stub = tmp_path / "pick-folder.ps1"  # if the guard were lost, this is all that would run: no dialog
    stub.write_text("exit 0\n", encoding="ascii")
    assert refused(lambda: folderdialog.pick_folder("Choose", script=stub, tmp_dir=tmp_path))
    assert not list(tmp_path.glob("ns-pick-*"))  # refused before its temp file
    quiet = Started()
    assert folderdialog.pick_folder("Choose", script=stub, run=quiet, tmp_dir=tmp_path) is None and len(quiet.calls) == 1
    started = Started()
    with monkeypatch.context() as m:
        m.setattr(subprocess, "run", started)
        assert refused(lambda: controller.copy_to_clipboard("probe"))
        assert refused(lambda: controller.Effects().clipboard("probe"))
    assert started.calls == []


# -------------------------------------------------------------------------------------- the static checks
def undo_calls(source: str) -> list[int]:
    """Lines that call `.undo()` on anything but a name bound by `with <x>.context() as <name>`: such a call
    drops EVERY fake of its test (the fixture's, conftest's autouse ones, the test's fake Task Scheduler)."""
    found: list[int] = []

    def walk(node: ast.AST, scoped: frozenset[str]) -> None:
        if isinstance(node, (ast.With, ast.AsyncWith)):
            bound = {
                item.optional_vars.id for item in node.items
                if isinstance(item.optional_vars, ast.Name) and isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Attribute) and item.context_expr.func.attr == "context"
            }
            scoped = scoped | bound
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "undo":
            target = node.func.value
            if not (isinstance(target, ast.Name) and target.id in scoped):
                found.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            walk(child, scoped)

    walk(parsed(source), frozenset())
    return found


def undo_sites(folder: Path) -> list[str]:
    return [f"{p.relative_to(folder).as_posix()}:{line}" for p in sorted(folder.rglob("*.py")) for line in undo_calls(p.read_text(encoding="utf-8"))]


def test_no_test_calls_undo_on_its_monkeypatch(tmp_path: Path):
    assert undo_sites(ROOT / "tests") == [], "use `with monkeypatch.context() as m:` around what the test means to scope"
    # the check itself: one bad file among good ones is the one it names
    (tmp_path / "test_good.py").write_text(
        "def test_a(monkeypatch):\n    with monkeypatch.context() as m:\n        m.setattr('os.sep', '/')\n        m.undo()\n", encoding="utf-8")
    (tmp_path / "test_bad.py").write_text(
        "import pytest\n\ndef test_b(monkeypatch):\n    monkeypatch.undo()\n\ndef test_c():\n    mp = pytest.MonkeyPatch()\n    mp.undo()\n"
        "\ndef test_d(monkeypatch):\n    with monkeypatch.context() as m:\n        pass\n    m.undo()\n", encoding="utf-8")
    (tmp_path / "test_plain.py").write_text("def test_e():\n    assert True\n", encoding="utf-8")
    assert undo_sites(tmp_path) == ["test_bad.py:4", "test_bad.py:8", "test_bad.py:13"]


PC_WRITERS = re.compile(
    r"\b(?:Register|Unregister|Set|Enable|Disable)-ScheduledTask\b|\bschtasks\b|\bsetx\b|\breg(?:\.exe)?\s+(?:add|delete)\b"
    r"|SetEnvironmentVariable\s*\([^)]*,\s*'(?:User|Machine)'\s*\)|CreateShortcut\s*\(|\.lnk\b|(?:set|delete)_password"
    r"|\b(?:New|Set|Remove)-Item(?:Property)?\b[^\r\n]*(?:HK(?:CU|LM):|\$\w*(?:aumid|reg)\w*key)",
    re.IGNORECASE,
)
SELF_GUARD = re.compile(r"if\s*\(\(\$env:NS_UNDER_TEST\s+-or\s+\$env:PYTEST_CURRENT_TEST\)[^\r\n]*\{[^}]*?\bexit\s+\d", re.IGNORECASE)


def unguarded_writer(text: str) -> str | None:
    """The first statement of a script that changes the PC with no refusal of a test run before it."""
    no_blocks = re.sub(r"<#.*?#>", lambda m: "\n" * m.group().count("\n"), text, flags=re.DOTALL)
    code = "\n".join("" if line.lstrip().startswith("#") else line for line in no_blocks.splitlines())
    writer = PC_WRITERS.search(code)
    if writer is None:
        return None
    guard = SELF_GUARD.search(code)
    return None if guard is not None and guard.start() < writer.start() else writer.group()


def test_every_shipped_ps1_that_changes_the_pc_guards_itself():
    """A script run by `-File` shows the hook only its name, so it must refuse a test run itself: the two
    variables, before its first statement that changes the PC."""
    scripts = sorted([*(ROOT / "installer").glob("*.ps1"), *(ROOT / "launchers").glob("*.ps1")])
    writers = [p.name for p in scripts if PC_WRITERS.search(p.read_text(encoding="utf-8"))]
    assert "uninstall.ps1" in writers and len(scripts) > 5
    for path in scripts:
        assert unguarded_writer(path.read_text(encoding="utf-8")) is None, f"{path.name} changes this PC and does not refuse a test run first"
    guard = "if (($env:NS_UNDER_TEST -or $env:PYTEST_CURRENT_TEST) -and -not $DryRun) {\n    exit 3\n}\n"
    change = "Unregister-ScheduledTask -TaskName 'Nightshift Daily' -Confirm:$false\n"
    assert unguarded_writer(change) == "Unregister-ScheduledTask" and unguarded_writer(change + guard) == "Unregister-ScheduledTask"
    assert unguarded_writer(guard + change) is None and unguarded_writer("# Unregister-ScheduledTask\nGet-Date\n") is None
    assert unguarded_writer("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)\n") is not None


def test_the_product_has_no_firewall_command():
    """A tripwire: nothing shipped touches the firewall today. The day something does, it needs a guard at its
    lowest call point and a row in this file first."""
    for path in product_files(".py", ".ps1", ".cmd"):
        text = path.read_text(encoding="utf-8", errors="replace")
        assert not re.search(r"netsh|NetFirewall|FirewallAPI|HNetCfg", text, re.IGNORECASE), f"{path.relative_to(ROOT).as_posix()} names the firewall"


def test_in_this_process_no_environment_change_lifts_the_refusal(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(testguard.ENV)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert testguard.under_test() is True  # pytest is imported: a test's environment cannot change that
    with pytest.raises(RealMachineUnderTest, match="real Windows Task Scheduler"):
        pwsh.run_powershell(schedule.build_read_script())


@needs_powershell
def test_every_real_route_to_the_task_scheduler_raises_and_changes_nothing(home: Paths, tmp_path: Path):
    """No fake is installed here: `pwsh.run_powershell` and the installer's runner are the real ones."""
    assert pwsh.run_powershell.__module__ == "ns_supervisor.pwsh"
    before = real_task()

    routes = {
        "read": lambda: schedule.read_task(),
        "the switch on": lambda: schedule.set_automatic(home, tmp_path, on=True, run_time="04:30"),
        "the switch off": lambda: schedule.set_automatic(home, tmp_path, on=False),
        "ns schedule install": lambda: schedule.install(home, "04:30", tmp_path, keep_state=True),
        "install, task unread": lambda: schedule.install(home, "04:30", tmp_path),
        "install --disabled": lambda: schedule.install(home, "04:30", tmp_path, disabled=True),
        "ns schedule remove": lambda: schedule.remove(home),
        "ns schedule show": lambda: schedule.show(home),
        "setup": lambda: schedule.setup_apply(home, tmp_path, first=True, nightly=True, seen=None, run_time="04:30"),
        "the look": lambda: schedule.sync_record(home),
        "register script": lambda: pwsh.run_powershell(schedule.build_install_script(home, "04:30", tmp_path)),
        "remove script": lambda: pwsh.run_powershell(schedule.build_remove_script()),
        "schtasks": lambda: pwsh.run_powershell("schtasks /Delete /TN 'Nightshift Daily' /F"),
        "setup's own look": lambda: winenv.run_ps(SubprocessRunner(), schedule.build_read_script(), tag="ps:task-read"),
        "the runner, schtasks.exe": lambda: SubprocessRunner().run(["schtasks.exe", "/Query", "/TN", "Nightshift Daily"]),
        "the runner, a command": lambda: SubprocessRunner().run(
            ["powershell.exe", "-NoProfile", "-Command", "Unregister-ScheduledTask -TaskName 'Nightshift Daily'"]
        ),
    }
    for name, call in routes.items():
        with pytest.raises(RealMachineUnderTest, match="refused under pytest"):
            call()
        assert not (home.config / "schedule.json").exists(), name  # and nothing was written down as done
    write_run_time(home, "04:30", "registered")  # a record that says on: an update has work to do
    with pytest.raises(RealMachineUnderTest, match="refused under pytest"):
        schedule.reregister(home, tmp_path)
    assert real_task() == before

    # a child process a test starts: it has no pytest inside; the session's variable is what it inherits
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    env.update(NIGHTSHIFT_HOME=str(home.home), PYTHONPATH=str(ROOT), PYTHONUTF8="1")
    for args in (
        ["schedule", "install", "--time", "04:30"],
        ["schedule", "remove"],
        ["schedule", "install", "--time", "04:30", "--setup", "first", "--nightly", "yes"],
    ):
        child = subprocess.run(
            [sys.executable, "-m", "ns_supervisor.cli", *args], capture_output=True, text=True, env=env,
            encoding="utf-8", errors="replace", timeout=120,
        )
        assert child.returncode != 0 and "RealMachineUnderTest" in child.stderr, (args, child.stdout, child.stderr)
    assert real_task() == before


@needs_powershell
def test_the_uninstaller_refuses_a_test_run_before_it_changes_anything():
    """uninstall.ps1 removes the task, the shortcuts, the registration and the stored keys. It is never run
    whole here (were the refusal ever lost, this test would uninstall the machine it runs on): the refusal
    must stand before the first statement that changes something, and its own lines must exit 3."""
    script = (ROOT / "installer" / "uninstall.ps1").read_text(encoding="utf-8")
    start = script.index("if (($env:NS_UNDER_TEST -or $env:PYTEST_CURRENT_TEST)")
    end = script.index("}", script.index("exit 3", start)) + 1
    guard = script[start:end]
    assert "-not $SkipSystem -and -not $DryRun" in guard
    top = script[script.index("$ErrorActionPreference = 'Stop'"):start]  # what runs before the refusal
    for changing in ("Unregister-ScheduledTask", "Remove-Item", "delete_password", "SetEnvironmentVariable", "& $"):
        assert changing not in top, changing
    assert "Unregister-ScheduledTask" in script[end:]

    def run(switches: str, env: dict[str, str]) -> int:
        return powershell(switches + "; " + guard + "; exit 0", env).returncode

    marked = dict(os.environ)
    bare = {k: v for k, v in os.environ.items() if k not in (testguard.ENV, "PYTEST_CURRENT_TEST")}
    assert run("$SkipSystem = $false; $DryRun = $false", marked) == 3  # refused
    assert run("$SkipSystem = $true; $DryRun = $false", marked) == 0  # what the tests may call
    assert run("$SkipSystem = $false; $DryRun = $true", marked) == 0
    # an owner's real uninstall is not refused. The hook marks every child this test starts, so the two marks
    # are taken away inside the child, before the uninstaller's own lines
    unmark = "Remove-Item Env:NS_UNDER_TEST, Env:PYTEST_CURRENT_TEST -ErrorAction SilentlyContinue; "
    assert run(unmark + "$SkipSystem = $false; $DryRun = $false", bare) == 0


def test_a_script_that_only_carries_the_words_as_data_still_runs():
    """The parse tests hand the script over in an environment variable: nothing in the command names it."""
    testguard.refuse_task_scheduler("[void][System.Management.Automation.Language.Parser]::ParseInput($env:X)")
    testguard.refuse_task_scheduler(["uv.exe", "--version"])
    with pytest.raises(RealMachineUnderTest):
        testguard.refuse_task_scheduler(["powershell.exe", "-EncodedCommand", pwsh.encode("Get-ScheduledTask")])
    # a script whose every ScheduledTask command is its own stand-in function runs (the tests of the generated
    # scripts do this under a real PowerShell); one real command beside the stand-ins is refused again
    stub = "function Get-ScheduledTask { param($TaskName) $null }; "
    testguard.refuse_task_scheduler(stub + schedule.build_read_script())
    leaks = ("Unregister-ScheduledTask -TaskName x", "schtasks /Query", "ScheduledTasks" + chr(92) + "Get-ScheduledTask",
             "Import-Module ScheduledTasks -Force")
    for leak in leaks:
        with pytest.raises(RealMachineUnderTest):
            testguard.refuse_task_scheduler(stub + schedule.build_read_script() + "; " + leak)


def test_the_real_keyring_package_is_refused_and_a_stand_in_is_not(monkeypatch: pytest.MonkeyPatch):
    kept: dict[str, str] = {}
    stand_in = types.SimpleNamespace(
        get_password=lambda service, name: kept.get(name),
        set_password=lambda service, name, value: kept.__setitem__(name, value),
    )
    monkeypatch.setitem(sys.modules, "keyring", stand_in)
    KeyringStore().set("pexels_api_key", "k")
    assert KeyringStore().get("pexels_api_key") == "k"
    installed = types.ModuleType("keyring")
    installed.__file__ = str(Path(sys.prefix) / "Lib" / "site-packages" / "keyring" / "__init__.py")
    monkeypatch.setitem(sys.modules, "keyring", installed)  # what the real package looks like
    for call in (lambda: KeyringStore().get("x"), lambda: KeyringStore().set("x", "y"), lambda: KeyringStore().delete("x")):
        with pytest.raises(RealMachineUnderTest, match="Credential Manager"):
            call()
