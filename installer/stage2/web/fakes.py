"""Fakes for the setup server: a stage 1 hand-over, a recording command runner, a scripted wizard process.

``--simulate`` uses them so the whole server runs in CI (and on the authoring laptop) without installing
anything. Standard library only.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from installer.stage2.web import modelcheck
from installer.stage2.web.bootstrap import CmdResult

TOOL_EXE = {
    "Microsoft.VCRedist.2015+.x64": "vcruntime", "Gyan.FFmpeg": "ffmpeg.exe",
    "Microsoft.WindowsTerminal": "wt.exe",
}
DEFAULT_PRESENT = {"vcruntime", "ffmpeg.exe", "wt.exe", "winget.exe"}      # a PC with every tool


def sample_handover(app_path: Path, *, cuda: str = "13.0", home: str = r"C:\Nightshift") -> dict[str, Any]:
    """A green, RTX-5060-Ti-like hand-over JSON, the shape ``stage1.ps1`` writes."""
    letters = set("CDEFGH")
    if len(home) > 1 and home[1] == ":":
        letters.add(home[0].upper())
    rows = [
        ("os", "Windows 10/11, 64-bit", "Windows 11 Pro 10.0.26200"),
        ("gpu", "NVIDIA RTX GPU, 16 GB", "NVIDIA GeForce RTX 5060 Ti, 16311 MB"),
        ("driver", "NVIDIA driver (CUDA 12.4 or newer; 12.8+ for RTX 50-series)",
         f"CUDA {cuda}, driver 581.57"),
        ("ram", "Memory, 16 GB minimum", "32 GB (Balanced and Best models available)"),
        ("disk", "Free disk space, 60 GB", "500 GB free on C:"),
        ("disktype", "Disk type, SSD", "C: SSD"),
        ("network", "Internet", "all reachable"),
        ("winget", "winget (App Installer)", "available"),
        ("vcruntime", "Visual C++ runtime", "installed"),
        ("longpaths", "Windows long paths enabled", "LongPathsEnabled = 1"),
        ("waketimers", "Wake timers allowed (nightly run)", "enabled"),
    ]
    return {
        "schema": 1, "simulated": True, "app_path": str(app_path), "home": home, "profile": "gpu",
        "llama_pair": "cuda13", "cuda": cuda, "ram_gb": 32.0, "is_admin": False,
        "os_caption": "Windows 11 Pro", "os_version": "10.0.26200",
        "gpu": {"name": "NVIDIA GeForce RTX 5060 Ti", "vram_mb": 16311, "driver": "581.57"},
        "drives": [{"letter": c, "free_gb": 500.0, "type": "SSD"} for c in sorted(letters)],
        "winget": True, "unreachable": [],
        "tools": {"ffmpeg": True, "wt": True, "uv": True},
        "checks": [{"id": i, "label": label, "status": "ok", "code": "", "value": value, "hint": ""}
                   for i, label, value in rows],
    }


class FakeCmd:
    """Records every command and answers the ones the bootstrap steps run."""

    def __init__(self, present: set[str] | None = None, fail: dict[str, CmdResult] | None = None) -> None:
        self.present = set(present or ())
        self.fail = dict(fail or {})
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: Sequence[str], env: Any = None, cwd: Any = None, timeout: float | None = None
    ) -> CmdResult:
        args = [str(a) for a in argv]
        self.calls.append(args)
        for needle, result in self.fail.items():
            if needle in " ".join(args):
                return result
        exe = Path(args[0]).name.lower()
        if exe.startswith("winget") and "install" in args:
            tool = TOOL_EXE.get(args[args.index("--id") + 1])
            if tool:
                self.present.add(tool)
            return CmdResult(0, "installed", "")
        if len(args) > 1 and args[1] == "venv":
            py = Path(args[-1]) / "Scripts" / "python.exe"
            py.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(sys.executable, py)
            return CmdResult(0, "", "")
        return CmdResult(0, "", "")

    def which(self, name: str) -> str | None:
        """``shutil.which`` for the fake PC."""
        return rf"C:\fake\{name}" if name.lower() in self.present else None

    def vc_missing(self) -> list[str]:
        """The runtime DLLs the fake PC lacks (all three until the runtime was "installed")."""
        if "vcruntime" in self.present:
            return []
        return ["msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"]


DEFAULT_PICK = "E:\\Nightshift"           # --simulate: what the fake folder dialog picks


def fake_effects(base: Path, present: set[str] | None = None, **overrides: Any) -> tuple[Any, FakeCmd]:
    """An ``Effects`` that touches nothing outside ``base``. Returns it with its FakeCmd."""
    from installer.stage2.web.controller import Effects

    cmd = FakeCmd(present if present is not None else DEFAULT_PRESENT)
    uv = base / "fake-uv" / "uv.exe"
    uv.parent.mkdir(parents=True, exist_ok=True)
    uv.write_bytes(b"MZ")
    var: dict[str, str] = {}
    launched: list[list[str]] = []
    console: list[str] = []
    downloads: list[tuple[str, str]] = []
    next_pick: list[str | None] = []        # what the fake folder dialog answers next (None = Cancel)

    def fetch_uv(url: str, sha256: str, dest: Path) -> None:
        downloads.append((url, sha256))
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "uv.exe").write_bytes(b"MZ")

    fetched: list[tuple[str, str]] = []         # pinned direct downloads: (url, sha256)
    elevated: list[tuple[str, list[str]]] = []
    path_adds: list[str] = []

    def fetch_file(url: str, sha256: str, dest: Path, say: Any = None) -> None:
        fetched.append((url, sha256))
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.suffix.lower() == ".zip":           # the shape of a static FFmpeg build
            with zipfile.ZipFile(dest, "w") as z:
                for exe in ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe"):
                    z.writestr(f"ffmpeg-fake-win64/bin/{exe}", b"MZ")
        else:
            dest.write_bytes(b"MZ")

    def run_elevated(exe: Path, args: Sequence[str]) -> CmdResult:
        elevated.append((str(exe), [str(a) for a in args]))
        cmd.present.add("vcruntime")                # the "installer" put the runtime in System32
        return CmdResult(0, "", "")

    def add_user_path(directory: str) -> bool:
        path_adds.append(directory)
        return True

    def set_var(name: str, value: str) -> bool:
        var[name] = value
        return True

    fx = Effects(
        cmd=cmd, which=cmd.which, refresh_path=lambda: None, set_home_var=set_var, get_home_var=var.get,
        probe=lambda app, out, home: sample_handover(app, home=home or r"C:\Nightshift"),
        free_gb=lambda _p: 500.0,
        key_check=lambda service, key: {"ok": bool(key), "code": None if key else "NS-E501", "message": None},
        head=modelcheck.fake_head,
        launch=lambda argv, env=None: launched.append(list(argv)),
        launch_captured=lambda argv, env=None, **kw: (launched.append(list(argv)), (0, ""))[1],
        elevate_longpaths=lambda: True, console=lambda text, level: console.append(text),
        uv_path=lambda home: str(uv), clipboard=lambda text: None, fetch_uv=fetch_uv,
        pick_folder=lambda title, start: next_pick.pop(0) if next_pick else DEFAULT_PICK,
        drive_type=lambda _letter: 3, vc_missing=cmd.vc_missing, fetch_file=fetch_file,
        run_elevated=run_elevated, add_user_path=add_user_path,
    )
    for k, v in overrides.items():
        setattr(fx, k, v)
    fx.launched = launched          # type: ignore[attr-defined]
    fx.console_lines = console      # type: ignore[attr-defined]
    fx.home_var = var               # type: ignore[attr-defined]
    fx.uv_downloads = downloads     # type: ignore[attr-defined]
    fx.next_pick = next_pick        # type: ignore[attr-defined]
    fx.fetched = fetched            # type: ignore[attr-defined]
    fx.elevated = elevated          # type: ignore[attr-defined]
    fx.path_adds = path_adds        # type: ignore[attr-defined]
    return fx, cmd


class ScriptedEngine:
    """Stands in for ``EngineProcess``: a script plays the wizard's side and sees the commands P1 sends."""

    def __init__(self, script: Callable[[ScriptedEngine], None]) -> None:
        self._script = script
        self.commands: list[dict[str, Any]] = []
        self._cond = threading.Condition()
        self.on_event: Callable[[dict[str, Any]], None] = lambda ev: None
        self.on_line: Callable[[str], None] = lambda text: None
        self.on_exit: Callable[[int], None] = lambda code: None
        self._alive = False
        self.exit_code: int | None = None

    def factory(self) -> Callable[..., ScriptedEngine]:
        """An ``engine_factory`` for ``Effects`` that returns this engine."""
        def make(argv: Sequence[str], *, env: Any, cwd: Any, on_event: Any, on_line: Any, on_exit: Any,
                 **_: Any) -> ScriptedEngine:
            self.argv = list(argv)
            self.on_event, self.on_line, self.on_exit = on_event, on_line, on_exit
            return self
        return make

    @property
    def alive(self) -> bool:
        """True until the script exits."""
        return self._alive

    def start(self) -> None:
        """Run the script on a thread."""
        self._alive = True
        threading.Thread(target=self._run, name="scripted-engine", daemon=True).start()

    def _run(self) -> None:
        try:
            self._script(self)
        finally:
            if self._alive:
                self.exit(self.exit_code if self.exit_code is not None else 0)

    def send(self, command: dict[str, Any]) -> bool:
        """Record a command from P1."""
        with self._cond:
            self.commands.append(command)
            self._cond.notify_all()
        return True

    def kill(self) -> None:
        """End now."""
        self.exit(137)

    def emit(self, event: dict[str, Any]) -> None:
        """Send an event as the wizard would."""
        self.on_event(event)

    def wait_command(self, name: str, timeout: float = 10.0) -> dict[str, Any] | None:
        """Block until a command named ``name`` arrived (None on timeout)."""
        import time

        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for c in self.commands:
                    if c.get("cmd") == name:
                        self.commands.remove(c)
                        return c
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._cond.wait(left)

    def exit(self, code: int) -> None:
        """Report the exit code once."""
        if not self._alive:
            return
        self._alive = False
        self.exit_code = code
        self.on_exit(code)


def stub_uv_dir() -> Path:
    """A temp folder with a stub ``uv.exe`` (for runs that need a path to copy)."""
    d = Path(tempfile.mkdtemp(prefix="ns-fake-uv-"))
    (d / "uv.exe").write_bytes(b"MZ")
    return d


__all__ = ["FakeCmd", "ScriptedEngine", "fake_effects", "sample_handover", "stub_uv_dir"]
