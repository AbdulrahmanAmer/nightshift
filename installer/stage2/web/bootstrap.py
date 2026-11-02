"""The three bootstrap steps that used to live in stage1 (docs/design/installer-ui.md 1.6).

``b01_tools`` installs Git, FFmpeg and Windows Terminal with winget, ``b02_home`` creates the install folder
and the NIGHTSHIFT_HOME user variable, ``b03_setup_env`` creates ``venvs\\studio`` with uv. Each step has
``check`` (already satisfied?), ``run`` and ``verify`` like the wizard's ``Step``. Every outside effect goes
through the ``Boot`` object, so ``fakes.FakeCmd`` runs all of it in CI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

HOME_VAR = "NIGHTSHIFT_HOME"
TOP_DIRS = ("bin", "models", "venvs", "config", "data", "cache", "imports", "outbox", "rejected", "logs")
# key, winget id, command, label, required
TOOLS: tuple[tuple[str, str, str, str, bool], ...] = (
    ("git", "Git.Git", "git.exe", "Git", True),
    ("terminal", "Microsoft.WindowsTerminal", "wt.exe", "Windows Terminal", False),
    ("ffmpeg", "Gyan.FFmpeg", "ffmpeg.exe", "FFmpeg", True),
)
WINGET_ARGS = ("--silent", "--accept-package-agreements", "--accept-source-agreements")
BLOCKED_HINTS = ("virus", "os error 225", "winerror 225", "blocked by your organization")


class CmdResult(NamedTuple):
    """Exit code and output of a command."""

    returncode: int
    stdout: str
    stderr: str


class BootstrapError(Exception):
    """A bootstrap step failed; ``code`` is a code from errors.json."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class BootstrapStopped(Exception):
    """Stop was pressed between two bootstrap actions."""


def real_cmd(
    argv: Sequence[str], env: Mapping[str, str] | None = None, cwd: Path | None = None,
    timeout: float | None = None,
) -> CmdResult:
    """Run a command without a shell and capture its output (exit 127: not found, 124: timeout)."""
    try:
        proc = subprocess.run(
            list(argv), cwd=str(cwd) if cwd else None, env={**os.environ, **(env or {})}, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError as e:
        return CmdResult(127, "", str(e))
    except subprocess.TimeoutExpired:
        return CmdResult(124, "", f"timed out after {timeout} s")
    return CmdResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def refresh_path_from_registry(environ: dict[str, str] | None = None) -> None:
    """Rebuild this process's PATH from the machine and user values, so a fresh winget install is found."""
    try:
        import winreg
    except ImportError:             # not Windows (tests)
        return
    parts: list[str] = []
    for hive, sub in (
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ):
        try:
            with winreg.OpenKey(hive, sub) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
                parts.append(os.path.expandvars(str(value)))
        except OSError:
            continue
    if parts:
        (environ if environ is not None else os.environ)["PATH"] = ";".join(parts)


def set_user_variable(name: str, value: str) -> bool:
    """Set a user environment variable in ``HKCU\\Environment`` and tell Windows about it."""
    try:
        import ctypes
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(  # type: ignore[attr-defined]
            0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result)
        )
    except OSError:
        return False
    return True


def get_user_variable(name: str) -> str | None:
    """Read a user environment variable from ``HKCU\\Environment`` (None when unset or not Windows)."""
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value) or None
    except OSError:
        return None


def last_lines(text: str, count: int = 20) -> str:
    """The last ``count`` non-empty lines of a command's output."""
    return "\n".join([line for line in text.splitlines() if line.strip()][-count:])


def classify(text: str, default: str) -> str:
    """NS-E113 when the output looks like antivirus or policy blocked a file, else ``default``."""
    low = text.lower()
    return "NS-E113" if any(h in low for h in BLOCKED_HINTS) else default


def ignore_row(key: str, status: str, note: str | None) -> None:
    """Default row callback: nothing listens."""


def ignore_text(text: str) -> None:
    """Default log callback: nothing listens."""


def ignore_call() -> None:
    """Default hook: nothing to do."""


@dataclass
class Boot:
    """Everything the bootstrap steps touch."""

    app_path: Path
    home: Path
    uv: str
    python_version: str = "3.12"
    cmd: Callable[..., CmdResult] = real_cmd
    which: Callable[[str], str | None] = shutil.which
    refresh_path: Callable[[], None] = refresh_path_from_registry
    set_home_var: Callable[[str, str], bool] = set_user_variable
    get_home_var: Callable[[str], str | None] = get_user_variable
    on_row: Callable[[str, str, str | None], None] = ignore_row
    say: Callable[[str], None] = ignore_text
    write_answers: Callable[[], None] = ignore_call
    gate: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self.gate.set()

    def checkpoint(self) -> None:
        """Wait while paused; raise BootstrapStopped after Stop."""
        while not self.gate.wait(0.2):
            if self.stop.is_set():
                break
        if self.stop.is_set():
            raise BootstrapStopped()

    @property
    def uv_exe(self) -> str:
        """uv for the later steps: the copy in the install folder once b02 placed it, else the hand-over."""
        local = self.home / "bin" / "uv" / "uv.exe"
        return str(local) if local.is_file() else self.uv

    @property
    def venv_python(self) -> Path:
        """Python of the studio venv."""
        return self.home / "venvs" / "studio" / "Scripts" / "python.exe"

    @property
    def venv_marker(self) -> Path:
        """Written after the setup packages installed; holds the app path they came from."""
        return self.home / "venvs" / "studio" / ".ns-setup-env.done"


class B01Tools:
    """Git, FFmpeg and Windows Terminal."""

    id = "b01_tools"
    title = "Free tools"

    def check(self, boot: Boot) -> bool:
        """True when every tool is already on PATH."""
        boot.refresh_path()
        return all(boot.which(exe) for _k, _i, exe, _l, _r in TOOLS)

    def run(self, boot: Boot) -> None:
        """Install what is missing with winget."""
        boot.refresh_path()
        for key, winget_id, exe, label, required in TOOLS:
            boot.checkpoint()
            if boot.which(exe):
                boot.on_row(key, "done", "already here")
                boot.say(f"{label}: already here")
                continue
            if required and not boot.which("winget.exe"):
                msg = f"{label} is missing and winget (App Installer) is not available"
                raise BootstrapError("NS-E111", msg)
            boot.on_row(key, "active", "installing with winget, Windows may ask for permission")
            boot.say(f"{label}: installing with winget")
            res = boot.cmd(["winget.exe", "install", "--id", winget_id, "-e", *WINGET_ARGS], timeout=1800)
            boot.refresh_path()
            if boot.which(exe):
                boot.on_row(key, "done", "installed with winget")
                boot.say(f"{label}: installed with winget")
            elif required:
                text = last_lines(res.stdout + "\n" + res.stderr)
                boot.on_row(key, "failed", None)
                detail = f"{label} ({winget_id}) is still not available after the winget install.\n{text}"
                raise BootstrapError(classify(text, "NS-E110"), detail)
            else:
                boot.on_row(key, "skipped", "could not be installed, setup goes on without it")
                boot.say(f"{label}: could not be installed; continuing without it")

    def verify(self, boot: Boot) -> None:
        """The two required tools are on PATH."""
        for _key, _id, exe, label, required in TOOLS:
            if required and not boot.which(exe):
                raise BootstrapError("NS-E110", f"{label} is not available")


class B02Home:
    """The install folder, the NIGHTSHIFT_HOME variable, uv and the answers file."""

    id = "b02_home"
    title = "Install folder"

    def check(self, boot: Boot) -> bool:
        """True when the folder tree, the variable, uv and the answers file are all there."""
        return (
            all((boot.home / d).is_dir() for d in TOP_DIRS)
            and boot.get_home_var(HOME_VAR) == str(boot.home)
            and (boot.home / "bin" / "uv" / "uv.exe").is_file()
            and (boot.home / "config" / "answers.json").is_file()
        )

    def run(self, boot: Boot) -> None:
        """Create everything, set the variable, copy uv, write answers.json."""
        for d in TOP_DIRS:
            (boot.home / d).mkdir(parents=True, exist_ok=True)
        boot.say(f"Install folder: {boot.home}")
        if not boot.set_home_var(HOME_VAR, str(boot.home)):
            raise BootstrapError("NS-E104", "could not set the NIGHTSHIFT_HOME user variable")
        os.environ[HOME_VAR] = str(boot.home)
        target = boot.home / "bin" / "uv" / "uv.exe"
        source = Path(boot.uv)
        if not target.is_file():
            if not source.is_file():
                raise BootstrapError(
                    "NS-E105", f"uv was not found at {source}; run INSTALL.cmd again so stage 1 downloads it"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            for item in source.parent.iterdir():
                if item.is_file() and item.name.lower().startswith("uv") and item.suffix.lower() == ".exe":
                    shutil.copy2(item, target.parent / item.name)
        boot.write_answers()

    def verify(self, boot: Boot) -> None:
        """The tree and the variable exist."""
        missing = [d for d in TOP_DIRS if not (boot.home / d).is_dir()]
        if missing:
            raise BootstrapError("NS-E104", f"{boot.home} lacks {', '.join(missing)}")
        if boot.get_home_var(HOME_VAR) != str(boot.home):
            raise BootstrapError("NS-E104", "NIGHTSHIFT_HOME does not point at the install folder")
        if not (boot.home / "bin" / "uv" / "uv.exe").is_file():
            raise BootstrapError("NS-E105", "uv was not copied into the install folder (bin/uv/uv.exe)")


class B03SetupEnv:
    """``venvs\\studio`` with the app installed in it, as stage 1 used to do."""

    id = "b03_setup_env"
    title = "Setup environment"

    def check(self, boot: Boot) -> bool:
        """True when the venv exists and was filled from this app folder."""
        try:
            return boot.venv_python.is_file() and boot.venv_marker.read_text(encoding="utf-8").strip() == str(
                boot.app_path
            )
        except OSError:
            return False

    def run(self, boot: Boot) -> None:
        """Create the venv when it is missing, then install the studio extra."""
        env = {"UV_LINK_MODE": "copy"}
        boot.on_row("setup_env", "active", "installing")
        if not boot.venv_python.is_file():
            boot.say("Creating the setup environment")
            venv_dir = str(boot.home / "venvs" / "studio")
            res = boot.cmd([boot.uv_exe, "venv", "--python", boot.python_version, venv_dir],
                           env, boot.app_path, 900)
            if res.returncode != 0:
                text = last_lines(res.stdout + "\n" + res.stderr)
                boot.on_row("setup_env", "failed", None)
                raise BootstrapError(classify(text, "NS-E105"), text)
        boot.checkpoint()
        boot.say("Installing the Python packages for setup")
        res = boot.cmd(
            [boot.uv_exe, "pip", "install", "--python", str(boot.venv_python), "-e",
             f"{boot.app_path}[studio]"],
            env, boot.app_path, 3600,
        )
        if res.returncode != 0:
            text = last_lines(res.stdout + "\n" + res.stderr)
            boot.on_row("setup_env", "failed", None)
            raise BootstrapError(classify(text, "NS-E105"), text)
        boot.venv_marker.write_text(str(boot.app_path), encoding="utf-8")
        boot.on_row("setup_env", "done", "installed")

    def verify(self, boot: Boot) -> None:
        """The interpreter exists."""
        if not boot.venv_python.is_file():
            raise BootstrapError("NS-E105", "the setup environment was not created")


def bootstrap_steps() -> list[B01Tools | B02Home | B03SetupEnv]:
    """The three steps in run order."""
    return [B01Tools(), B02Home(), B03SetupEnv()]
