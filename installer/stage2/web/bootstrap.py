"""The three bootstrap steps that used to live in stage1 (docs/design/installer-ui.md 1.6).

``b01_tools`` makes sure the Visual C++ runtime, FFmpeg and Windows Terminal are there (winget first; the
runtime and FFmpeg have a pinned direct download behind it), ``b02_home`` creates the install folder, the
NIGHTSHIFT_HOME user variable and the program folder ``<home>\\app``, ``b03_setup_env`` creates
``venvs\\studio`` with uv. Git is not a tool setup needs. Each step has ``check`` (already satisfied?),
``run`` and ``verify`` like the wizard's ``Step``. Every outside effect goes through the ``Boot`` object, so
``fakes.FakeCmd`` runs all of it in CI.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import NamedTuple

import ns_common.vcruntime as vcruntime
from installer.stage2.web import appcopy, pathcheck
from ns_common.avblock import is_blocked_error, looks_blocked
from ns_common.uv import looks_full, uv_env, venv_args

HOME_VAR = "NIGHTSHIFT_HOME"
UV_VERSION = re.compile(r"(?:^|\s)uv\s+(\d+(?:\.\d+)+\S*)")      # "uv 0.13.6 (abc 2025-11-01)"
TOP_DIRS = ("bin", "models", "venvs", "config", "data", "cache", "imports", "outbox", "rejected", "logs")
# key, winget id, label, required. Git is not here: nothing in setup or at run time needs it (only the
# optional ``ns update --git``), and its machine-wide installer would add an administrator prompt.
TOOLS: tuple[tuple[str, str, str, bool], ...] = (
    ("vcruntime", vcruntime.WINGET_ID, "Visual C++ runtime", True),
    ("ffmpeg", "Gyan.FFmpeg", "FFmpeg", True),
    ("terminal", "Microsoft.WindowsTerminal", "Windows Terminal", False),
)
WINGET_ARGS = ("--source", "winget", "--silent", "--accept-package-agreements", "--accept-source-agreements")
#: Exit codes of vc_redist.x64.exe that mean "installed": ok, a newer one is there, reboot started or wanted.
VCREDIST_OK_CODES = (0, 1638, 1641, 3010)
ERROR_CANCELLED = 1223                  # the UAC prompt was declined or closed
ELEVATED_WAIT_S = 600                   # stage 1 installs the runtime; this is only the fallback wait
FFMPEG_EXES = ("ffmpeg.exe", "ffprobe.exe")


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
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        _broadcast_environment()
    except OSError:
        return False
    return True


def _broadcast_environment() -> None:
    """Tell running programs that the user environment changed (WM_SETTINGCHANGE)."""
    import ctypes

    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(  # type: ignore[attr-defined]
        0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result)
    )


def add_user_path_entry(directory: str) -> bool:
    """Append ``directory`` to the user's PATH (``HKCU\\Environment``) unless it is already there.

    The launchers, the shortcuts and the nightly task start with the user's environment, so a tool that only
    lives under the install folder (the FFmpeg fallback) is found by them and by the programs they start.
    """
    try:
        import winreg
    except ImportError:
        return False
    wanted = directory.rstrip("\\").lower()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            try:
                value, kind = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                value, kind = "", winreg.REG_EXPAND_SZ
            parts = [p for p in str(value).split(";") if p]
            if any(p.rstrip("\\").lower() == wanted for p in parts):
                return True
            parts.append(directory)
            winreg.SetValueEx(key, "Path", 0, kind if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ)
                              else winreg.REG_EXPAND_SZ, ";".join(parts))
        _broadcast_environment()
    except OSError:
        return False
    return True


def run_elevated_exe(exe: Path, args: Sequence[str], timeout: float = ELEVATED_WAIT_S) -> CmdResult:
    """Run a program with administrator rights (a UAC prompt) and wait; the result carries its exit code.

    ``Start-Process -Verb RunAs`` is the only way for a normal process to start an installer that demands
    elevation (CreateProcess answers error 740). A declined or closed prompt comes back as exit 1223.
    """
    quoted = ",".join("'" + a.replace("'", "''") + "'" for a in args)
    target = str(exe).replace("'", "''")
    script = (
        "try { $p = Start-Process -FilePath '" + target + "' -ArgumentList " + quoted
        + " -Verb RunAs -Wait -PassThru -ErrorAction Stop; exit $p.ExitCode } "
        + "catch { [Console]::Error.WriteLine($_.Exception.Message); exit " + str(ERROR_CANCELLED) + " }"
    )
    return real_cmd(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        timeout=timeout,
    )


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
    """NS-E113 when the output looks like antivirus or policy blocked a file, NS-E104 when uv ran out of disk
    space (only for the NS-E105 default), else ``default``."""
    if looks_blocked(text):
        return "NS-E113"
    return "NS-E104" if default == "NS-E105" and looks_full(text) else default


def download_uv(url: str, sha256: str, dest_dir: Path) -> None:
    """Download the pinned uv zip, check its SHA256 and extract every ``uv*.exe`` into ``dest_dir``.

    Standard library only (the setup server has no httpx). Raises ``BootstrapError`` ``NS-E105`` when the
    download fails or the digest does not match.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(suffix=".zip", prefix="nightshift-uv-", dir=str(dest_dir))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        digest = hashlib.sha256()
        try:
            with urllib.request.urlopen(url, timeout=120) as resp, tmp.open("wb") as out:
                for block in iter(lambda: resp.read(1024 * 1024), b""):
                    digest.update(block)
                    out.write(block)
        except OSError as e:
            raise BootstrapError("NS-E105", f"could not download uv from {url}: {e}") from e
        if digest.hexdigest().lower() != sha256.lower():
            got = digest.hexdigest()[:12]
            raise BootstrapError("NS-E105", f"SHA256 mismatch for the uv download ({got})")
        try:
            with zipfile.ZipFile(tmp) as z:
                for info in z.infolist():
                    leaf = Path(info.filename).name.lower()
                    if info.is_dir() or not (leaf.startswith("uv") and leaf.endswith(".exe")):
                        continue
                    with z.open(info) as src, (dest_dir / leaf).open("wb") as dst:
                        shutil.copyfileobj(src, dst)
        except (zipfile.BadZipFile, OSError) as e:
            raise BootstrapError("NS-E105", f"the uv download could not be unpacked: {e}") from e
    finally:
        tmp.unlink(missing_ok=True)


def download_file(
    url: str, sha256: str, dest: Path, say: Callable[[str], None] | None = None, *, attempts: int = 3,
    opener: Callable[..., object] = urllib.request.urlopen, sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Download ``url`` to the file ``dest``, check the pinned SHA256, and make sure the file is still there.

    Standard library only. Failures are mapped the way the wizard maps them: the site cannot be reached
    (``NS-E105``, after ``attempts`` tries), the digest is wrong (``NS-E109``), or Windows Security
    quarantined the file (``NS-E113``: WinError 225 while writing, or the finished file vanishing).
    """
    say = say or ignore_text
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    for attempt in range(1, attempts + 1):
        digest = hashlib.sha256()
        done, next_report = 0, 25 * 1024 * 1024
        try:
            with opener(url, timeout=120) as resp, part.open("wb") as out:  # type: ignore[attr-defined]
                for block in iter(lambda: resp.read(1024 * 1024), b""):
                    digest.update(block)
                    out.write(block)
                    done += len(block)
                    if done >= next_report:
                        say(f"{dest.name}: {done // (1024 * 1024)} MB downloaded")
                        next_report += 25 * 1024 * 1024
        except OSError as e:
            part.unlink(missing_ok=True)
            if is_blocked_error(e):
                raise BootstrapError("NS-E113", f"{dest.name} was blocked during the download: {e}") from e
            if attempt < attempts:
                say(f"{dest.name}: the download stopped ({e}); trying again")
                sleep(2.0 ** attempt)
                continue
            raise BootstrapError("NS-E105", f"could not download {url}: {e}") from e
        if digest.hexdigest().lower() != sha256.lower():
            part.unlink(missing_ok=True)
            raise BootstrapError(
                "NS-E109", f"{dest.name}: SHA256 {digest.hexdigest()[:12]} is not the pinned {sha256[:12]}"
            )
        try:
            os.replace(part, dest)
        except OSError as e:
            code = "NS-E113" if is_blocked_error(e) else "NS-E105"
            raise BootstrapError(code, f"{dest.name} could not be saved: {e}") from e
        if not dest.is_file():
            raise BootstrapError(
                "NS-E113", f"{dest.name} disappeared right after it was downloaded (Windows Security?)"
            )
        return


def ffmpeg_unpacked(folder: Path) -> bool:
    """True when ``folder`` holds both ``ffmpeg.exe`` and ``ffprobe.exe``."""
    return all((folder / exe).is_file() for exe in FFMPEG_EXES)


def unpack_ffmpeg(archive: Path, target: Path) -> list[str]:
    """Write ``ffmpeg.exe`` and ``ffprobe.exe`` (from any ``bin`` folder) of a static build into ``target``.

    Nothing else in the archive is touched. Returns the names written; raises ``NS-E110`` when the archive
    lacks one of them or is not a zip.
    """
    target.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    try:
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                parts = PurePosixPath(info.filename).parts
                leaf = parts[-1].lower() if parts else ""
                if info.is_dir() or leaf not in FFMPEG_EXES or leaf in written:
                    continue
                if len(parts) < 2 or parts[-2].lower() != "bin":
                    continue
                part = target / (leaf + ".part")
                with z.open(info) as src, part.open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                os.replace(part, target / leaf)
                written.append(leaf)
    except zipfile.BadZipFile as e:
        raise BootstrapError("NS-E110", f"{archive.name} is not a valid zip: {e}") from e
    except OSError as e:
        code = "NS-E113" if is_blocked_error(e) else "NS-E110"
        raise BootstrapError(code, f"FFmpeg could not be unpacked into {target}: {e}") from e
    missing = [exe for exe in FFMPEG_EXES if exe not in written]
    if missing:
        raise BootstrapError("NS-E110", f"{archive.name} has no {' or '.join(missing)} in a bin folder")
    return written


SHIM_MAX_BYTES = 1_000_000          # a real uv.exe is tens of MB; scoop and chocolatey shims are a few KB
_SHIM_PATH = re.compile(r"^\s*path\s*=\s*(.+?)\s*$", re.IGNORECASE)


def is_shim(exe: Path) -> bool:
    """True for a launcher stub rather than uv itself: a ``.shim`` file beside it, no PE header, or tiny."""
    try:
        if exe.with_suffix(".shim").is_file():
            return True
        if exe.stat().st_size < SHIM_MAX_BYTES:
            return True
        with exe.open("rb") as fh:
            return fh.read(2) != b"MZ"
    except OSError:
        return False


def shim_target(exe: Path) -> Path | None:
    """The real executable a Scoop-style shim points at (``path = ...`` in ``uv.shim``), if it exists."""
    try:
        text = exe.with_suffix(".shim").read_text(encoding="utf-8-sig")
    except OSError:
        return None
    for line in text.splitlines():
        m = _SHIM_PATH.match(line)
        if m:
            target = Path(m.group(1).strip().strip('"'))
            return target if target.is_file() and target.resolve() != exe.resolve() else None
    return None


def ignore_row(key: str, status: str, note: str | None) -> None:
    """Default row callback: nothing listens."""


def ignore_text(text: str) -> None:
    """Default log callback: nothing listens."""


def ignore_call() -> None:
    """Default hook: nothing to do."""


@dataclass
class Boot:
    """Everything the bootstrap steps touch."""

    app_path: Path                          # the folder setup was started from (the extracted bundle)
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
    uv_pin: Mapping[str, str] = field(default_factory=dict)       # manifest bins.uv: url and sha256
    fetch_uv: Callable[[str, str, Path], None] = download_uv
    # pinned direct downloads behind winget (manifest bins.vcredist, bins.ffmpeg): url and sha256
    bin_pins: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    vc_missing: Callable[[], list[str]] = vcruntime.missing
    fetch_file: Callable[..., None] = download_file               # (url, sha256, dest, say)
    run_elevated: Callable[..., CmdResult] = run_elevated_exe     # (exe, args) -> exit code in the result
    add_user_path: Callable[[str], bool] = add_user_path_entry
    # the program folder: with install_app, b02 copies ``app_path`` to ``<home>\\app`` and everything later
    # (the setup environment, the wizard, the shortcuts, the nightly task, ns update) runs from there
    install_app: bool = False
    copy_app: Callable[..., appcopy.CopyResult] = appcopy.install_app
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
    def run_app(self) -> Path:
        """The program folder everything runs from: ``<home>\\app`` when setup copies it there."""
        return appcopy.app_dir(self.home) if self.install_app else self.app_path

    @property
    def ffmpeg_dir(self) -> Path:
        """Where the pinned FFmpeg build is unpacked when winget could not install it."""
        return self.home / "bin" / "ffmpeg"

    @property
    def downloads_dir(self) -> Path:
        """Where bootstrap downloads wait before they are installed or unpacked."""
        return self.home / "bin" / "_downloads"

    @property
    def venv_python(self) -> Path:
        """Python of the studio venv."""
        return self.home / "venvs" / "studio" / "Scripts" / "python.exe"

    @property
    def venv_marker(self) -> Path:
        """Written after the setup packages installed; holds the app path and version they came from."""
        return self.home / "venvs" / "studio" / ".ns-setup-env.done"

    def venv_stamp(self) -> str:
        """What the marker must say for the setup environment to count as current."""
        return f"{self.run_app}|{appcopy.read_version(self.run_app) or ''}"


class B01Tools:
    """The Visual C++ runtime, FFmpeg and Windows Terminal (winget first; direct downloads behind it)."""

    id = "b01_tools"
    title = "Free tools"

    @staticmethod
    def have(boot: Boot, key: str) -> bool:
        """True when the tool is usable: the runtime DLLs are in System32, ffmpeg runs, wt.exe is on PATH."""
        if key == "vcruntime":
            return not boot.vc_missing()
        if key == "ffmpeg":
            return bool(boot.which("ffmpeg.exe")) or ffmpeg_unpacked(boot.ffmpeg_dir)
        return bool(boot.which("wt.exe"))

    def check(self, boot: Boot) -> bool:
        """True when every tool is already there."""
        boot.refresh_path()
        return all(self.have(boot, key) for key, _i, _l, _r in TOOLS)

    def run(self, boot: Boot) -> None:
        """Install what is missing: winget first, then the pinned download where there is one."""
        boot.refresh_path()
        for key, winget_id, label, required in TOOLS:
            boot.checkpoint()
            if self.have(boot, key):
                boot.on_row(key, "done", "already here")
                boot.say(f"{label}: already here")
                continue
            ask = "look for a flashing shield on the taskbar and press Yes" if key == "vcruntime" else (
                "Windows may ask for permission"
            )
            boot.on_row(key, "active", f"installing, {ask}")
            text = ""
            fallback = {"vcruntime": self._install_vcruntime, "ffmpeg": self._install_ffmpeg}.get(key)
            then = "; trying the pinned download" if fallback is not None else ""
            if boot.which("winget.exe"):
                boot.say(f"{label}: installing with winget")
                res = boot.cmd(
                    ["winget.exe", "install", "--id", winget_id, "-e", *WINGET_ARGS], timeout=ELEVATED_WAIT_S
                )
                text = last_lines(res.stdout + "\n" + res.stderr)
                boot.refresh_path()
                if self.have(boot, key):
                    boot.on_row(key, "done", "installed with winget")
                    boot.say(f"{label}: installed with winget")
                    continue
                boot.say(f"{label}: winget did not install it{then}")
            else:
                boot.say(f"{label}: winget is not available{then}")
            if fallback is not None:
                try:
                    fallback(boot)
                except BootstrapError:
                    boot.on_row(key, "failed", None)
                    raise
                if self.have(boot, key):
                    boot.on_row(key, "done", "installed from the pinned download")
                    boot.say(f"{label}: installed from the pinned download")
                    continue
            if required:
                boot.on_row(key, "failed", None)
                code = "NS-E114" if key == "vcruntime" else "NS-E110"
                raise BootstrapError(classify(text, code), f"{label} is still not available.\n{text}")
            boot.on_row(key, "skipped", "could not be installed, setup goes on without it")
            boot.say(f"{label}: could not be installed; continuing without it")

    @staticmethod
    def _pin(boot: Boot, key: str) -> tuple[str, str]:
        pin = boot.bin_pins.get(key) or {}
        url, digest = pin.get("url"), pin.get("sha256")
        if not url or not digest:
            raise BootstrapError("NS-E105", f"manifest.json pins no direct download for {key}")
        return str(url), str(digest)

    def _install_vcruntime(self, boot: Boot) -> None:
        """Download the pinned ``vc_redist.x64.exe`` and run it elevated (``/install /quiet /norestart``)."""
        try:
            url, digest = self._pin(boot, "vcredist")
        except BootstrapError as e:
            raise BootstrapError("NS-E114", e.detail) from e
        exe = boot.downloads_dir / "vc_redist.x64.exe"
        boot.say("Downloading the Visual C++ runtime (about 25 MB)")
        boot.fetch_file(url, digest, exe, boot.say)
        boot.say("Installing the Visual C++ runtime; click Yes when Windows asks for permission")
        res = boot.run_elevated(exe, ["/install", "/quiet", "/norestart"])
        if res.returncode in VCREDIST_OK_CODES:
            exe.unlink(missing_ok=True)
            boot.refresh_path()
            return
        text = last_lines(res.stdout + "\n" + res.stderr)
        if res.returncode == ERROR_CANCELLED:
            text = "The permission window was closed or declined. " + text
        raise BootstrapError(
            classify(text, "NS-E114"),
            f"the Visual C++ runtime installer stopped with exit code {res.returncode}. {text}",
        )

    def _install_ffmpeg(self, boot: Boot) -> None:
        """Download the pinned static FFmpeg build, unpack both programs and put their folder on PATH."""
        url, digest = self._pin(boot, "ffmpeg")
        archive = boot.downloads_dir / Path(url).name
        boot.say("Downloading FFmpeg (a static build, about 190 MB)")
        boot.fetch_file(url, digest, archive, boot.say)
        boot.say(f"Unpacking FFmpeg to {boot.ffmpeg_dir}")
        unpack_ffmpeg(archive, boot.ffmpeg_dir)
        archive.unlink(missing_ok=True)
        if not ffmpeg_unpacked(boot.ffmpeg_dir):
            raise BootstrapError(
                "NS-E113", f"ffmpeg.exe or ffprobe.exe vanished from {boot.ffmpeg_dir} right after unpacking"
            )
        if not boot.add_user_path(str(boot.ffmpeg_dir)):
            boot.say("FFmpeg could not be added to PATH; Nightshift finds it in its own folder anyway")
        boot.refresh_path()

    def verify(self, boot: Boot) -> None:
        """The runtime and FFmpeg are usable."""
        for key, _id, label, required in TOOLS:
            if required and not self.have(boot, key):
                code = "NS-E114" if key == "vcruntime" else "NS-E110"
                raise BootstrapError(code, f"{label} is not available")


class B02Home:
    """The install folder, the NIGHTSHIFT_HOME variable, uv and the answers file."""

    id = "b02_home"
    title = "Install folder"

    def check(self, boot: Boot) -> bool:
        """True when the folder tree, the variable, a uv that runs and the answers file are all there."""
        return (
            all((boot.home / d).is_dir() for d in TOP_DIRS)
            and boot.get_home_var(HOME_VAR) == str(boot.home)
            and self._uv_is_pinned(boot)
            and (boot.home / "config" / "answers.json").is_file()
            and (not boot.install_app or appcopy.is_current(boot.app_path, boot.home))
        )

    @staticmethod
    def _uv_runs(boot: Boot) -> bool:
        """``uv --version`` succeeds when run from the copy in the install folder.

        A shim copied away from its folder fails here, and a file that merely exists proves nothing.
        """
        return B02Home._uv_report(boot)[0]

    @staticmethod
    def _uv_report(boot: Boot) -> tuple[bool, str | None]:
        """Whether the install folder's uv runs, and the version it prints (None when it prints none)."""
        target = boot.home / "bin" / "uv" / "uv.exe"
        if not target.is_file():
            return False, None
        res = boot.cmd([str(target), "--version"], None, boot.home, 60)
        if res.returncode != 0:
            return False, None
        found = UV_VERSION.search(res.stdout)
        return True, found.group(1) if found else None

    @staticmethod
    def _uv_is_pinned(boot: Boot) -> bool:
        """The install folder's uv runs and is the version the manifest pins (``bins.uv.version``).

        A manifest pin without a version leaves nothing to compare with, so a uv that runs is accepted.
        """
        runs, version = B02Home._uv_report(boot)
        want = str(boot.uv_pin.get("version") or "")
        return runs and (not want or version == want)

    @staticmethod
    def _uv_mismatch(boot: Boot) -> str:
        """Why the install folder's uv is refused: it does not run, or reports another version."""
        runs, version = B02Home._uv_report(boot)
        want = boot.uv_pin.get("version")
        if not runs:
            return "does not run from the install folder"
        return f"reports {version or 'no version'}, not the pinned {want}"

    @staticmethod
    def _clear_uv_family(dest_dir: Path) -> None:
        """Delete the ``uv*.exe`` files of a copy that was refused, so no unpinned uv stays in the folder."""
        for item in dest_dir.glob("*.exe") if dest_dir.is_dir() else ():
            if item.is_file() and item.name.lower().startswith("uv"):
                item.unlink(missing_ok=True)

    @staticmethod
    def _copy_uv_family(src_dir: Path, dest_dir: Path) -> None:
        dest_dir.mkdir(parents=True, exist_ok=True)
        for item in src_dir.iterdir():
            if item.is_file() and item.name.lower().startswith("uv") and item.suffix.lower() == ".exe":
                there = dest_dir / item.name
                if there.exists() and there.samefile(item):
                    continue
                shutil.copy2(item, there)

    def _place_uv(self, boot: Boot) -> None:
        """Put the pinned uv at ``bin\\uv\\uv.exe``.

        A uv is accepted only when it runs from the install folder and reports ``bins.uv.version`` of the
        manifest (a uv from PATH, such as winget's, is another version and changes the resolver). Order: the
        hand-over copy; when that is a shim, the real exe behind it; else a fresh download of the pinned
        release (``bins.uv``: url and sha256).
        """
        target = boot.home / "bin" / "uv" / "uv.exe"
        source = Path(boot.uv)
        want = boot.uv_pin.get("version")
        problems: list[str] = []
        if source.is_file():
            self._copy_uv_family(source.parent, target.parent)
            if self._uv_is_pinned(boot):
                boot.say(f"uv {want or 'found'} copied from {source}")
                return
            problems.append(f"{source} {self._uv_mismatch(boot)}")
            if is_shim(source):
                real = shim_target(source) or self._self_where(boot, source)
                if real is not None:
                    boot.say(f"uv at {source} is a shim; using {real}")
                    self._copy_uv_family(real.parent, target.parent)
                    if self._uv_is_pinned(boot):
                        return
                    problems.append(f"{real} {self._uv_mismatch(boot)}")
            boot.say(f"Not using the uv at {source}: " + "; ".join(problems))
            self._clear_uv_family(target.parent)
        url, digest = boot.uv_pin.get("url"), boot.uv_pin.get("sha256")
        if not url or not digest:
            if problems:
                raise BootstrapError(
                    "NS-E105", "no usable uv and no download is pinned: " + "; ".join(problems)
                )
            raise BootstrapError(
                "NS-E105", f"uv was not found at {source}; run INSTALL.cmd again so stage 1 downloads it"
            )
        boot.say(f"Downloading the pinned uv {want or ''}".rstrip())
        boot.fetch_uv(str(url), str(digest), target.parent)
        if not self._uv_is_pinned(boot):
            self._clear_uv_family(target.parent)
            raise BootstrapError(
                "NS-E105",
                f"the downloaded uv {self._uv_mismatch(boot)}"
                + (f"; {'; '.join(problems)}" if problems else ""),
            )

    @staticmethod
    def _self_where(boot: Boot, source: Path) -> Path | None:
        """Ask the shim where the real uv is (``uv self where``); the answer must be an existing exe."""
        res = boot.cmd([str(source), "self", "where"], None, None, 30)
        lines = [ln.strip().strip('"') for ln in res.stdout.splitlines() if ln.strip()]
        if res.returncode != 0 or not lines:
            return None
        found = Path(lines[-1])
        return found if found.is_file() and found.suffix.lower() == ".exe" else None

    @staticmethod
    def _stop_engine(boot: Boot) -> None:
        """Ask a running engine of an earlier install to stop before its program folder is swapped."""
        ns = boot.home / "venvs" / "supervisor" / "Scripts" / "ns.exe"
        if ns.is_file():
            boot.say("Stopping the Nightshift engine of the installed version")
            boot.cmd([str(ns), "stop"], {HOME_VAR: str(boot.home)}, boot.home, 120)

    def _install_app(self, boot: Boot) -> None:
        """Copy the extracted program to ``<home>\\app``, or update the install that is already there."""
        if not boot.install_app:
            return
        problem = pathcheck.layout_problem(str(boot.home), boot.app_path)
        if problem:
            raise BootstrapError("NS-E115", problem)
        if appcopy.is_current(boot.app_path, boot.home):
            boot.say(f"Program folder: {appcopy.app_dir(boot.home)} is up to date")
            return
        try:
            res = boot.copy_app(
                boot.app_path, boot.home, stop_engine=lambda: self._stop_engine(boot), say=boot.say
            )
        except appcopy.AppCopyError as e:
            raise BootstrapError(e.code, e.detail) from e
        if res.action == "updated":
            kept = ", ".join(res.kept) or "nothing"
            boot.say(f"Updated the install: {res.old_version} -> {res.new_version}; kept {kept}")
        boot.say(f"Program folder: {appcopy.app_dir(boot.home)} (the folder you extracted can be deleted)")

    def run(self, boot: Boot) -> None:
        """Create everything, set the variable, copy the program folder and uv, write answers.json."""
        for d in TOP_DIRS:
            (boot.home / d).mkdir(parents=True, exist_ok=True)
        boot.say(f"Install folder: {boot.home}")
        self._install_app(boot)
        if not boot.set_home_var(HOME_VAR, str(boot.home)):
            raise BootstrapError("NS-E104", "could not set the NIGHTSHIFT_HOME user variable")
        os.environ[HOME_VAR] = str(boot.home)
        if not self._uv_is_pinned(boot):
            self._place_uv(boot)
        boot.write_answers()

    def verify(self, boot: Boot) -> None:
        """The tree and the variable exist, and the copied uv runs."""
        missing = [d for d in TOP_DIRS if not (boot.home / d).is_dir()]
        if missing:
            raise BootstrapError("NS-E104", f"{boot.home} lacks {', '.join(missing)}")
        if boot.get_home_var(HOME_VAR) != str(boot.home):
            raise BootstrapError("NS-E104", "NIGHTSHIFT_HOME does not point at the install folder")
        if not (boot.home / "bin" / "uv" / "uv.exe").is_file():
            raise BootstrapError("NS-E105", "uv was not copied into the install folder (bin/uv/uv.exe)")
        if not self._uv_runs(boot):
            raise BootstrapError("NS-E105", "uv in the install folder does not run (uv --version failed)")
        if not self._uv_is_pinned(boot):
            raise BootstrapError("NS-E105", f"the uv in the install folder {self._uv_mismatch(boot)}")
        if boot.install_app and not appcopy.is_current(boot.app_path, boot.home):
            raise BootstrapError("NS-E115", f"the program folder {appcopy.app_dir(boot.home)} is incomplete")


class B03SetupEnv:
    """``venvs\\studio`` with the app installed in it, as stage 1 used to do."""

    id = "b03_setup_env"
    title = "Setup helper"

    def check(self, boot: Boot) -> bool:
        """True when the venv exists and was filled from this program folder at this version."""
        try:
            return (
                boot.venv_python.is_file() and boot.venv_marker.read_text(encoding="utf-8").strip()
                == boot.venv_stamp()
            )
        except OSError:
            return False

    def run(self, boot: Boot) -> None:
        """Create the venv when it is missing, then install the studio extra."""
        env = uv_env(boot.home)             # copy mode, and uv's cache on the install drive
        boot.on_row("setup_env", "active", "installing")
        if not boot.venv_python.is_file():
            boot.say("Creating the setup environment")
            venv_dir = boot.home / "venvs" / "studio"
            res = boot.cmd([boot.uv_exe, *venv_args(venv_dir, boot.python_version)], env, boot.run_app, 900)
            if res.returncode != 0:
                text = last_lines(res.stdout + "\n" + res.stderr)
                boot.on_row("setup_env", "failed", None)
                raise BootstrapError(classify(text, "NS-E105"), text)
        boot.checkpoint()
        boot.say("Installing the Python packages for setup")
        res = boot.cmd(
            [boot.uv_exe, "pip", "install", "--python", str(boot.venv_python), "-e",
             f"{boot.run_app}[studio]"],
            env, boot.run_app, 3600,
        )
        if res.returncode != 0:
            text = last_lines(res.stdout + "\n" + res.stderr)
            boot.on_row("setup_env", "failed", None)
            raise BootstrapError(classify(text, "NS-E105"), text)
        boot.venv_marker.write_text(boot.venv_stamp(), encoding="utf-8")
        boot.on_row("setup_env", "done", "installed")

    def verify(self, boot: Boot) -> None:
        """The interpreter exists."""
        if not boot.venv_python.is_file():
            raise BootstrapError("NS-E105", "the setup environment was not created")


def bootstrap_steps() -> list[B01Tools | B02Home | B03SetupEnv]:
    """The three steps in run order."""
    return [B01Tools(), B02Home(), B03SetupEnv()]
