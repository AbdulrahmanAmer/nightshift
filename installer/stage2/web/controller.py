"""The setup controller: state, actions and the pipeline (docs/design/installer-ui.md 1.5 to 1.8).

``SetupController`` owns the SetupState dict (in a ``SetupStore``), runs the bootstrap steps b01 to b03,
starts the wizard as a child process with ``--ui-bridge stdio``, maps its events into the state, and answers
the actions the window sends. Standard library only. Every outside effect (winget, the wizard process, probes,
Explorer) comes in through ``Effects`` so the tests run it with fakes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from installer.stage2.web import (
    answers_view,
    appcopy,
    bootstrap,
    folderdialog,
    keepawake,
    labels,
    machine,
    modelcheck,
    pathcheck,
    plan,
    preflight,
    rows,
    samples,
)
from installer.stage2.web.engine import EngineProcess, wizard_argv, wizard_env
from installer.stage2.web.keys import check_key
from installer.stage2.web.model import BOOTSTRAP_STEPS, FILE_NAMES, STEP_ORDER, STEP_PHASE
from installer.stage2.web.store import SetupStore
from ns_common.atomic import BACKUP_SUFFIX, write_text_atomic
from ns_common.uv import find_uv
from ns_common.vcruntime import missing as vcruntime_missing

LOG_KEEP = 50
SAMPLE_TEXT = "This is how your channel will sound at night."
DEFAULT_SPEAKER = "Ryan"
EXIT_BLOCKED = 10
EXIT_STOPPED = 130
DETACHED = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP (Windows only)
SHORTCUTS = ("Nightshift Dashboard", "Nightshift Studio", "Nightshift Outbox", "Nightshift - Stop All",
             "Nightshift - Update", "Nightshift - Diagnostics")


RETRY_NOTES = {
    "stall": "No data for 60 s, trying again",
    "network": "The connection dropped, trying again",
    "offline": "No internet right now, waiting for it to come back",
    "hash": "The file failed its check, downloading it again",
}


def retry_note(reason: str, wait_s: float, attempt: int | None = None, of: int | None = None) -> str:
    """The row note while a transfer waits to be retried: what happened, how long until the next try and which
    try that is ("try 2 of 3"; ``attempt`` is the attempt that just failed)."""
    text = RETRY_NOTES.get(reason, "The download stopped, trying again")
    text = f"{text} in {int(wait_s)} s" if wait_s >= 1 else text
    if attempt and of and attempt < of:
        text += f" (try {attempt + 1} of {of})"
    return text


def now_iso() -> str:
    """Local time, to the second."""
    return datetime.now().isoformat(timespec="seconds")


def hhmm() -> str:
    """Local time as HH:MM."""
    return datetime.now().strftime("%H:%M")


def desktop_dir() -> Path:
    """The user's Desktop folder (follows OneDrive redirection when the registry says so)."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        ) as key:
            value, _ = winreg.QueryValueEx(key, "Desktop")
            return Path(os.path.expandvars(str(value)))
    except (ImportError, OSError):
        return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"


# ---------------------------------------------------------------------------------------------------------
# configuration and outside effects
# ---------------------------------------------------------------------------------------------------------
@dataclass
class Config:
    """How the server was started."""

    app_path: Path
    data_dir: Path
    preflight: Path | None = None
    answers: Path | None = None
    home: Path | None = None
    headless: bool = False
    simulate: bool = False
    no_window: bool = False
    show_window: bool = False           # -Ui: open the window even though answers.json exists
    port: int = 0
    fixture: str | None = None
    uninstall: bool = False
    python: str = field(default_factory=lambda: sys.executable)
    halt_after: str | None = None
    copy_app: bool | None = None        # copy the program to <home>\app; None: yes, except in --simulate


def default_free_gb(path: str) -> float:
    """Free GB on the drive holding ``path`` (nearest existing parent)."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free / 1024 ** 3


def launch_detached(argv: list[str], env: dict[str, str] | None = None) -> None:
    """Start a program that outlives this one (the dashboard, Explorer)."""
    flags = DETACHED if os.name == "nt" else 0
    subprocess.Popen(
        argv, env={**os.environ, **(env or {})}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags,
    )


class ActionFailed(Exception):
    """An action could not do what it was asked; the message goes to the window."""


def dashboard_answers(url: str = "http://127.0.0.1:8100/") -> bool:
    """True when the dashboard answers on its port."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=1.5) as r:  # noqa: S310 - fixed loopback URL
            return 200 <= r.status < 500
    except (OSError, urllib.error.URLError, ValueError):
        return False


def launch_captured(
    argv: list[str],
    env: dict[str, str] | None = None,
    *,
    wait_s: float = 150.0,
    sleep: Callable[[float], None] = time.sleep,
    popen: Callable[..., Any] = subprocess.Popen,
    up: Callable[[], bool] = dashboard_answers,
) -> tuple[int | None, str]:
    """Run a launcher with its output captured; returns (exit code or None while it still runs, output tail).

    A launcher started with DETACHED_PROCESS has no console, so its error text and its closing ``Read-Host``
    went nowhere and the window said "opening" forever. Here the output is read by a thread, the launcher gets
    no window, and the caller sees how it ended: exit 0 (done), an exit code with the text, or None when the
    dashboard answers while the launcher is still busy.
    """
    flags = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW
    proc = popen(
        argv, env={**os.environ, **(env or {})}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", creationflags=flags,
    )
    lines: list[str] = []

    def pump() -> None:
        for line in proc.stdout or ():
            if line.strip():
                lines.append(line.rstrip())

    reader = threading.Thread(target=pump, name="launcher-output", daemon=True)
    reader.start()
    deadline = time.monotonic() + wait_s
    code = proc.poll()
    while code is None and time.monotonic() < deadline:
        if up():
            return None, "\n".join(lines[-12:])
        sleep(0.5)
        code = proc.poll()
    if code is not None:
        reader.join(timeout=2)
    return code, "\n".join(lines[-12:])


def elevate_longpaths() -> bool:
    """Turn long paths on through a UAC prompt. True when the registry value is 1 afterwards."""
    script = (
        "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile','-Command',"
        "'Set-ItemProperty HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem "
        "LongPathsEnabled 1 -Type DWord'"
    )
    subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], capture_output=True, check=False,
                   timeout=300)
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            return int(winreg.QueryValueEx(key, "LongPathsEnabled")[0]) == 1
    except (ImportError, OSError):
        return False


def ignore_console(text: str, level: str) -> None:
    """Default console hook: print nothing."""


def copy_to_clipboard(text: str) -> None:
    """Put text on the Windows clipboard (clip.exe); silently does nothing when that fails."""
    try:
        subprocess.run(["clip.exe"], input=text, text=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return


def default_uv_path(home: Path) -> str:
    """uv for the bootstrap steps: the one resolver (install folder, stage-1 hand-over, bootstrap, PATH).

    Falls back to the (missing) install-folder path so b02 can say clearly that uv was not found.
    """
    return find_uv(home) or str(home / "bin" / "uv" / "uv.exe")


@dataclass
class Effects:
    """Outside effects, replaceable in tests."""

    cmd: Callable[..., bootstrap.CmdResult] = bootstrap.real_cmd
    which: Callable[[str], str | None] = shutil.which
    refresh_path: Callable[[], None] = bootstrap.refresh_path_from_registry
    set_home_var: Callable[[str, str], bool] = bootstrap.set_user_variable
    get_home_var: Callable[[str], str | None] = bootstrap.get_user_variable
    engine_factory: Callable[..., EngineProcess] = EngineProcess
    probe: Callable[[Path, Path, str | None], dict[str, Any] | None] = preflight.reprobe
    free_gb: Callable[[str], float] = default_free_gb
    key_check: Callable[[str, str], dict[str, Any]] = check_key
    head: modelcheck.Head = modelcheck.urllib_head
    launch: Callable[..., None] = launch_detached
    launch_captured: Callable[..., tuple[int | None, str]] = launch_captured
    elevate_longpaths: Callable[[], bool] = elevate_longpaths
    console: Callable[[str, str], None] = ignore_console
    uv_path: Callable[[Path], str] = default_uv_path
    fetch_uv: Callable[[str, str, Path], None] = bootstrap.download_uv
    clipboard: Callable[[str], None] = copy_to_clipboard
    pick_folder: Callable[[str, str], str | None] = folderdialog.pick_folder      # (title, start) -> path
    drive_type: Callable[[str], int] = preflight.default_drive_type               # GetDriveTypeW of a letter
    vc_missing: Callable[[], list[str]] = vcruntime_missing                      # DLLs not in System32
    fetch_file: Callable[..., None] = bootstrap.download_file                    # pinned direct downloads
    run_elevated: Callable[..., bootstrap.CmdResult] = bootstrap.run_elevated_exe
    add_user_path: Callable[[str], bool] = bootstrap.add_user_path_entry
    copy_app: Callable[..., appcopy.CopyResult] = appcopy.install_app
    set_execution_state: Callable[[int], int] = keepawake._set_execution_state    # keep the PC awake


# ---------------------------------------------------------------------------------------------------------
# install_state.json helpers (read and write only the steps this module owns)
# ---------------------------------------------------------------------------------------------------------
def read_install_state(home: Path) -> dict[str, Any]:
    """install_state.json as a dict; a torn file falls back to the last good copy, then to empty."""
    main = home / "config" / "install_state.json"
    for candidate in (main, main.with_name(main.name + BACKUP_SUFFIX)):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def write_step(
    home: Path, version: str, app_path: Path, step_id: str, status: str, error: str | None
) -> None:
    """Record one step in install_state.json the way the wizard's StateStore does."""
    data = read_install_state(home)
    steps = data.get("steps") if isinstance(data.get("steps"), dict) else {}
    finished = now_iso() if status in ("done", "skipped", "failed") else None
    steps[step_id] = {"status": status, "error": error, "finished_at": finished}
    data.update({"version": data.get("version", version), "app_path": str(app_path), "steps": steps})
    data.setdefault("profile", None)
    data.setdefault("started_at", now_iso())
    data.setdefault("completed_at", None)
    target = home / "config" / "install_state.json"
    write_text_atomic(target, json.dumps(data, indent=2) + "\n", keep_backup=True)


# ---------------------------------------------------------------------------------------------------------
# the controller
# ---------------------------------------------------------------------------------------------------------
class SetupController:
    """Everything the server needs: state, actions, pipeline."""

    def __init__(
        self, cfg: Config, fx: Effects | None = None, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.cfg = cfg
        self.fx = fx or Effects()
        self.meter = rows.SpeedMeter(clock)
        self.shutdown = threading.Event()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._gate = threading.Event()
        self._gate.set()
        self._engine: EngineProcess | None = None
        self._engine_done = threading.Event()
        self._engine_code: int | None = None
        self._stderr_tail: list[str] = []
        self._pipeline: threading.Thread | None = None
        self._started = False
        self._waiting_on: str | None = None       # engine (P2 awaits a decision), pipeline, disk
        self._pause_requested = False
        self._attempts: dict[str, int] = {}
        self._accepted: dict[str, Any] | None = None
        self._keys: dict[str, str | None] = {"pexels_api_key": None, "pixabay_api_key": None}
        self._keys_stored = {"pexelsKey": False, "pixabayKey": False}
        self._sub: dict[str, list[Any]] = {}      # wizard file key -> [done, total]
        self._finished: set[str] = set()
        self._progress_log: dict[str, float] = {}
        self._begun = time.monotonic()
        self._log_path: Path | None = cfg.data_dir / "bootstrap" / "setup-ui.jsonl"
        self.manifest = plan.load_manifest(cfg.app_path)
        self.version = self._read_version()
        self.pf = self._load_preflight()
        self.home = self._initial_home()
        self.store = SetupStore(self._initial_state(), self._finalize)

    # ---- the program folder ----
    @property
    def installs_app(self) -> bool:
        """True when setup copies the extracted program to ``<home>\\app`` (not in ``--simulate``)."""
        return self.cfg.copy_app if self.cfg.copy_app is not None else not self.cfg.simulate

    @property
    def run_app_path(self) -> Path:
        """The program folder everything runs from: ``<home>\\app`` once it is in place."""
        if self.installs_app and appcopy.installed_fingerprint(self.home) is not None:
            return appcopy.app_dir(self.home)
        return self.cfg.app_path

    # ---- construction ----
    def _read_version(self) -> str:
        try:
            return (self.cfg.app_path / "VERSION").read_text(encoding="utf-8-sig").strip() or "0.0.0"
        except OSError:
            return str(self.manifest.get("version", "0.0.0"))

    def _load_preflight(self) -> dict[str, Any]:
        pf = preflight.load(self.cfg.preflight) if self.cfg.preflight else None
        if pf is None:
            if not self.cfg.simulate:
                raise FileNotFoundError("no hardware summary (stage 1 hand-over file) was given")
            from installer.stage2.web.fakes import sample_handover

            home = str(self.cfg.home) if self.cfg.home else r"C:\Nightshift"
            pf = sample_handover(self.cfg.app_path, home=home)
        return pf

    def _initial_home(self) -> Path:
        if self.cfg.home:
            return self.cfg.home
        env_home = os.environ.get("NIGHTSHIFT_HOME") or self.fx.get_home_var(bootstrap.HOME_VAR)
        prefill = self._prefill_raw()
        if prefill and prefill.get("home_path"):
            return Path(str(prefill["home_path"]))
        if env_home:
            return Path(env_home)
        return Path(str(self.pf.get("home") or r"C:\Nightshift"))

    def _prefill_raw(self) -> dict[str, Any] | None:
        if not self.cfg.answers:
            return None
        try:
            return answers_view.read_answers(self.cfg.answers)
        except (OSError, ValueError):
            return None

    def _initial_state(self) -> dict[str, Any]:
        prefill = self._prefill_raw()
        saved = read_install_state(self.home)
        saved_steps = saved.get("steps") if isinstance(saved.get("steps"), dict) else {}
        done_before = [i for i, s in saved_steps.items() if isinstance(s, dict) and s.get("status") in
                       ("done", "skipped") and not i.startswith("b0")]
        locked = (saved_steps.get("s01_questions") or {}).get("status") == "done"
        raw = prefill
        saved_answers = self.home / "config" / "answers.json"
        if locked and saved_answers.is_file():
            try:
                raw = answers_view.read_answers(saved_answers)
            except (OSError, ValueError):
                raw = prefill
        default_tier = plan.recommended_tier(float(self.pf.get("ram_gb") or 0))
        view = None
        if raw:
            view = answers_view.answers_to_view(
                raw, pexels_set=locked, pixabay_set=locked, default_tier=default_tier
            )
            if locked:
                self._keys_stored = {"pexelsKey": True, "pixabayKey": True}
        tier = (view or {}).get("llmTier") or default_tier
        code = bool((view or {}).get("installCode", True))
        pf = self.pf
        gpu = pf.get("gpu") or {}
        the_plan = plan.build_plan(
            self.manifest, profile=pf.get("profile", "gpu"), tier=tier, include_code=code,
            cuda=pf.get("cuda"),
            llama_pair=pf.get("llama_pair") or None,
            ram_gb=float(pf.get("ram_gb") or 0), vram_mb=gpu.get("vram_mb"),
            custom=(view or {}).get("llmCustom"),
        )
        steps = []
        for sid in STEP_ORDER:
            st = saved_steps.get(sid) if isinstance(saved_steps.get(sid), dict) else {}
            status = st.get("status", "pending")
            steps.append({
                "id": sid, "title": labels.STEP_TITLES[sid], "phase": STEP_PHASE[sid],
                "status": status if status in ("done", "skipped") else "pending", "detail": None,
                "startedAt": None, "finishedAt": st.get("finished_at"),
            })
        state: dict[str, Any] = {
            "schema": 1, "rev": 1, "version": self.version, "appPath": str(self.cfg.app_path),
            "phase": "checks", "run": "idle", "uiStep": "welcome", "resumed": bool(done_before),
            "prefilledFrom": str(self.cfg.answers) if self.cfg.answers and prefill is not None else None,
            "startedAt": saved.get("started_at") if done_before else None, "updatedAt": now_iso(),
            "host": "none",
            "machine": preflight.machine_summary(pf, str(self.home), tier),
            "checks": preflight.check_rows(pf, str(self.home)), "checkedAt": hhmm(), "checking": False,
            "blocking": False, "plan": the_plan, "answers": view, "answersLocked": locked, "steps": steps,
            "downloads": {"items": [], "overall": {}, "source": (view or {}).get("modelsSource", {}).get(
                "kind", "internet"), "paused": False},
            "verify": None, "failure": None, "notices": [], "result": None, "log": [],
        }
        state["downloads"]["items"] = self._initial_rows(the_plan)
        state["downloads"]["overall"] = rows.overall(state["downloads"]["items"], self.meter)
        if prefill is not None:
            self._keys.update(answers_view.keys_in(prefill))
        return state

    def _initial_rows(self, the_plan: dict[str, Any]) -> list[dict[str, Any]]:
        tools = {k: bool(v) for k, v in (self.pf.get("tools") or {}).items()}
        out: list[dict[str, Any]] = []
        for g in the_plan["groups"]:
            for item in g["items"]:
                row = rows.make_row({**item, "group": g["id"]})
                if row["via"] == "winget" and tools.get({"terminal": "wt"}.get(item["key"], item["key"])):
                    row.update(via="present", status="done", note="already here")
                elif row["via"] == "download":
                    have, verified = rows.part_state(self.home, row)
                    if verified:
                        row.update(status="done", bytesDone=have, note="already here")
                    elif have:
                        row.update(bytesDone=have, note=f"will continue from {rows.format_gb(have)}")
                out.append(row)
        return out

    def _finalize(self, state: dict[str, Any]) -> None:
        machine.finalize(state, started=self._started)
        state["downloads"]["overall"] = rows.overall(state["downloads"]["items"], self.meter)

    # ---- logging ----
    def log(self, level: str, text: str) -> None:
        """Add a line to the window's log, the console and the log file."""
        line = {"t": datetime.now().strftime("%H:%M:%S"), "level": level, "text": text}

        def fn(s: dict[str, Any]) -> None:
            s["log"].append(line)
            del s["log"][:-LOG_KEEP]

        self.store.update(fn)
        prefix = "" if level == "info" else f"{level}  "
        self.fx.console(f"{line['t']}  {prefix}{text}", level)
        if self._log_path is not None:
            try:
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
                with self._log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"t": now_iso(), "level": level, "text": text}) + "\n")
            except OSError:
                pass

    def _switch_log_to_home(self) -> None:
        target = self.home / "logs" / f"installer-ui-{datetime.now():%Y-%m-%d}.jsonl"
        old = self._log_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if old is not None and old.is_file() and old != target:
                with target.open("a", encoding="utf-8") as out:
                    out.write(old.read_text(encoding="utf-8"))
                old.unlink(missing_ok=True)
            self._log_path = target
        except OSError:
            pass

    # ---- plain accessors for the server ----
    def snapshot(self) -> dict[str, Any]:
        """The current SetupState."""
        return self.store.snapshot()

    def wait_state(self, since: int | None, timeout: float = 15.0) -> dict[str, Any]:
        """Long poll."""
        return self.store.wait_newer(since, timeout)

    def set_host(self, kind: str) -> None:
        """Record which window launcher worked."""
        self.store.update(lambda s: s.__setitem__("host", kind))

    def file_path(self, name: str) -> Path | None:
        """The file behind ``/api/file/<name>`` (None when the name is not on the allowlist)."""
        if name not in FILE_NAMES:
            return None
        if name == "verify-report.json":
            return self.home / "logs" / name
        return self.home / "cache" / "setup-test" / name

    # ---- ui step, answers, keys, disk ----
    def set_ui_step(self, step: str) -> tuple[int, dict[str, Any]]:
        """Move between Welcome, Checks and Choices before Start."""
        if self._started:
            return self._reply(409, False, "Setup has already started.")
        self.store.update(lambda s: s.__setitem__("uiStep", step))
        return self._reply(200, True, None)

    def _layout_problem(self, home: str) -> str | None:
        """Why ``home`` clashes with the extracted folder or lives in Downloads or OneDrive (None: fine)."""
        if not self.installs_app:
            return None
        return pathcheck.layout_problem(home, self.cfg.app_path)

    def _existing_install_note(self, home: str) -> str | None:
        """A line for the folder picker when ``home`` already holds an install that setup would update."""
        found = appcopy.installed_version(Path(home))
        if found is None or not self.installs_app:
            return None
        if found == self.version:
            return f"Nightshift {found} is already installed here; setup repairs what is missing"
        return (f"Nightshift {found} is installed here. Setup updates it to {self.version} and keeps your "
                "videos, voices and settings")

    def _env(self, state: dict[str, Any]) -> answers_view.Env:
        drives = {d["letter"] for d in state["machine"]["drives"]} or None
        return answers_view.Env(
            ram_gb=float(state["machine"]["ramGb"]), tiers=state["plan"]["tiers"], drive_letters=drives,
            free_gb=lambda p: next((d["freeGb"] for d in state["machine"]["drives"]
                                    if d["letter"] == p[:1].upper()), None),
            keys_stored=self._keys_stored, layout_problem=self._layout_problem,
        )

    def submit_answers(self, draft: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/answers``: validate, accept, rebuild the plan. Returns an AnswersResult."""
        if "path" in draft and "channel" not in draft:
            return self._load_answers_file(str(draft["path"]))
        state = self.snapshot()
        if state["answersLocked"] or self._started:
            return {"ok": False, "errors": [{"field": "homePath", "code": None,
                                             "message": "Your choices are locked once setup has started"}]}
        errors = answers_view.validate(draft, self._env(state))
        home = str(draft.get("homePath") or "").strip()
        if answers_view.DRIVE_PATH_RE.match(home):
            self._apply_home(home, str(draft.get("llmTier") or "small"))
        if errors:
            return {"ok": False, "errors": errors}
        for field_name, key_name in (("pexelsKey", "pexels_api_key"), ("pixabayKey", "pixabay_api_key")):
            value = str(draft.get(field_name) or "").strip()
            if value:
                self._keys[key_name] = value
        self._accepted = draft
        raw = answers_view.draft_to_answers(draft, allow_cpu=self._allow_cpu())
        view = answers_view.answers_to_view(
            raw, pexels_set=bool(self._keys["pexels_api_key"]) or self._keys_stored["pexelsKey"],
            pixabay_set=bool(self._keys["pixabay_api_key"]) or self._keys_stored["pixabayKey"],
        )
        pf = self.pf
        new_plan = plan.build_plan(
            self.manifest, profile=pf.get("profile", "gpu"), tier=raw["llm_tier"],
            include_code=raw["install_code"],
            cuda=pf.get("cuda"), ram_gb=float(pf.get("ram_gb") or 0),
            vram_mb=(pf.get("gpu") or {}).get("vram_mb"),
            llama_pair=pf.get("llama_pair") or None,
            custom=raw.get("llm_custom"),
        )

        def fn(s: dict[str, Any]) -> None:
            s["answers"] = view
            s["plan"] = new_plan
            s["downloads"]["source"] = view["modelsSource"]["kind"]
            s["downloads"]["items"] = self._initial_rows(new_plan)

        self.store.update(fn)
        self.log("info", f"Choices saved for {view['channel']['displayName']}")
        return {"ok": True, "answers": view, "plan": new_plan}

    def _load_answers_file(self, path: str) -> dict[str, Any]:
        """Welcome > Advanced > "Use answers from a file": read it and run it through the same validation."""
        try:
            raw = answers_view.read_answers(Path(path.strip().strip('"')))
        except (OSError, ValueError) as e:
            return {"ok": False, "errors": [{"field": "homePath", "code": None,
                                             "message": f"That answers file could not be read: {e}"}]}
        default_tier = plan.recommended_tier(float(self.pf.get("ram_gb") or 0))
        view = answers_view.answers_to_view(raw, default_tier=default_tier)
        draft = {k: v for k, v in view.items() if k not in ("pexelsKeySet", "pixabayKeySet")}
        keys = answers_view.keys_in(raw)
        draft["pexelsKey"] = keys["pexels_api_key"]
        draft["pixabayKey"] = keys["pixabay_api_key"]
        return self.submit_answers(draft)

    def _allow_cpu(self) -> bool:
        prefill = self._prefill_raw() or {}
        return bool(prefill.get("allow_cpu", False))

    def _apply_home(self, home: str, tier: str) -> None:
        """Re-evaluate the disk rows and meters for a chosen install folder."""
        self.home = Path(home)

        def fn(s: dict[str, Any]) -> None:
            s["checks"] = preflight.check_rows(self.pf, home)
            s["machine"] = preflight.machine_summary(self.pf, home, tier)

        self.store.update(fn)

    def check_key(self, service: str, key: str) -> dict[str, Any]:
        """``POST /api/check-key``."""
        return self.fx.key_check(service, key)

    def check_model(self, repo: str, file: str) -> dict[str, Any]:
        """``GET /api/model-check``: does this Hugging Face file exist, and does it fit a 16 GB card."""
        return modelcheck.check_model(repo, file, self.fx.head)

    def disk(self, path: str, tier: str, source: bool) -> dict[str, Any]:
        """``GET /api/disk``."""
        state = self.snapshot()
        if source:
            expected = preflight.expected_model_files(self.manifest, self.pf.get("profile", "gpu"), tier)
            found = preflight.scan_models_source(Path(path), expected)
            return preflight.disk_check(path, tier, state["machine"]["drives"], self.fx.free_gb, models=found)
        return preflight.disk_check(path, tier, state["machine"]["drives"], self.fx.free_gb,
                                    drive_type_fn=self.fx.drive_type, layout_fn=self._layout_problem,
                                    existing_fn=self._existing_install_note)

    def pick_folder(self, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/pick-folder``: the Windows folder dialog, then the same check as typing the path.

        ``target`` is ``install`` (the install folder: a drive root or a folder with other files gets a
        ``Nightshift`` folder inside it) or ``models`` (a folder of model files). Nothing is created.
        Answers ``{ok, path, cancelled, message, check}``; ``check`` is a DiskCheck for the picked path.
        """
        target = "models" if body.get("target") == "models" else "install"
        tier = str(body.get("tier") or "small")
        start = str(body.get("start") or "")
        if self.snapshot()["answersLocked"]:
            return {"ok": False, "path": None, "cancelled": False, "check": None,
                    "message": "Your choices are locked once setup has started"}
        title = "Choose the install folder"
        if target == "models":
            title = "Choose the folder with the model files"
        try:
            picked = self.fx.pick_folder(title, start)
        except (folderdialog.FolderDialogError, OSError) as e:
            self.log("warn", f"The folder window could not open: {e}")
            return {"ok": False, "path": None, "cancelled": False, "check": None,
                    "message": "The folder window could not open. Type the folder instead"}
        if not picked:
            return {"ok": True, "path": None, "cancelled": True, "message": None, "check": None}
        path = folderdialog.install_folder_for(picked) if target == "install" else picked
        return {"ok": True, "path": path, "cancelled": False, "message": None,
                "check": self.disk(path, tier, target == "models")}

    # ---- actions ----
    def _reply(self, status: int, ok: bool, message: str | None) -> tuple[int, dict[str, Any]]:
        return status, {"ok": ok, "message": message, "state": self.snapshot()}

    def do_action(self, name: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        """``POST /api/action/<name>``: check the precondition, run the action, answer with the new state."""
        body = body or {}
        path = body.get("path") if isinstance(body.get("path"), str) else None
        state = self.snapshot()
        problem = machine.check(name, state, path)
        if name == "start" and not problem and self._accepted is None and not state["answersLocked"]:
            problem = self._accept_prefill()
        if problem:
            return self._reply(409, False, problem)
        handler = getattr(self, "_act_" + name.replace("-", "_"), None)
        if handler is None:
            return self._reply(404, False, f"Unknown action {name}")
        try:
            message = handler(path)
        except ActionFailed as e:
            return self._reply(200, False, str(e))
        return self._reply(200, True, message)

    def _accept_prefill(self) -> str | None:
        """-Ui with answers.json: accept the loaded answers on Start when valid. Returns a problem."""
        state = self.snapshot()
        view = state.get("answers")
        if not view or not self._prefill_raw():
            return "Choose your settings first."
        draft = dict(view)
        draft["pexelsKey"] = self._keys.get("pexels_api_key")
        draft["pixabayKey"] = self._keys.get("pixabay_api_key")
        result = self.submit_answers(draft)
        return None if result["ok"] else "answers.json had problems. Fix them on the Choices screen."

    def _act_start(self, _path: str | None) -> str | None:
        self._begin_pipeline()
        return None

    def _act_pause(self, _path: str | None) -> str | None:
        with self._lock:
            self._gate.clear()
            engine = self._engine
            if engine is not None and engine.alive:
                engine.send({"cmd": "pause"})
                run = "paused"
            else:
                self._pause_requested = True
                run = "pausing"
        self.meter.reset()

        def fn(s: dict[str, Any]) -> None:
            s["run"] = run
            s["downloads"]["paused"] = True
            for r in s["downloads"]["items"]:
                if r["status"] == "active":
                    r["status"], r["speedBps"], r["etaS"] = "paused", None, None

        self.store.update(fn)
        self.log("info", "Downloads paused" if run == "paused" else "Setup will pause after this tool")
        return None

    def _act_resume(self, _path: str | None) -> str | None:
        state = self.snapshot()
        if state["run"] == "stopped":
            self._begin_pipeline()
            return None
        with self._lock:
            self._pause_requested = False
            self._gate.set()
            engine = self._engine
            if engine is not None and engine.alive:
                engine.send({"cmd": "resume"})

        def fn(s: dict[str, Any]) -> None:
            s["run"] = "running"
            s["downloads"]["paused"] = False
            for r in s["downloads"]["items"]:
                if r["status"] == "paused":
                    r["status"] = "active"

        self.store.update(fn)
        self.log("info", "Downloads resumed")
        return None

    def _act_retry(self, _path: str | None) -> str | None:
        waiting = self._waiting_on
        self._waiting_on = None
        if waiting == "engine" and self._engine is not None and self._engine.alive:
            self._clear_failure()
            self._engine.send({"cmd": "decision", "action": "retry"})
            return None
        if waiting == "disk":
            if self.fx.free_gb(str(self.home)) < rows.LOW_DISK_GB:
                self._waiting_on = "disk"
                return "There is still less than 2 GB free. Free some space, then press Retry."
            self._clear_failure()
            self._gate.set()
            if self._engine is not None and self._engine.alive:
                self._engine.send({"cmd": "resume"})
            return None
        self._clear_failure()
        self._begin_pipeline()
        return None

    def _act_skip(self, _path: str | None) -> str | None:
        self._waiting_on = None
        self._clear_failure()
        if self._engine is not None and self._engine.alive:
            self._engine.send({"cmd": "decision", "action": "skip"})
        return None

    def _act_cancel(self, _path: str | None) -> str | None:
        self._stop.set()
        self._gate.set()
        engine = self._engine
        alive = engine is not None and engine.alive
        pipeline_alive = self._pipeline is not None and self._pipeline.is_alive()
        if alive and engine is not None:
            engine.send({"cmd": "stop"})
        stopping = alive or pipeline_alive
        self.store.update(lambda s: s.__setitem__("run", "stopping" if stopping else "stopped"))
        self.log("info", "Stopping setup. Everything finished so far is kept.")
        return None

    def _act_recheck(self, _path: str | None) -> str | None:
        self.store.update(lambda s: s.__setitem__("checking", True))
        threading.Thread(target=self._recheck, name="recheck", daemon=True).start()
        return None

    def _recheck(self) -> None:
        out = self.cfg.data_dir / "bootstrap" / "preflight.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        ram = float(self.pf.get("ram_gb") or 0)
        tier = (self.snapshot().get("answers") or {}).get("llmTier") or plan.recommended_tier(ram)
        new = self.fx.probe(self.cfg.app_path, out, str(self.home))
        if new is None:
            self.log("warn", "The PC check could not run. Press Check again")
            self.store.update(lambda s: s.__setitem__("checking", False))
            return
        self.pf = new

        def fn(s: dict[str, Any]) -> None:
            s["checks"] = preflight.check_rows(new, str(self.home))
            s["machine"] = preflight.machine_summary(new, str(self.home), tier)
            s["checkedAt"] = hhmm()
            s["checking"] = False

        self.store.update(fn)
        ok, warnings = self._counts()
        self.log("info", f"Checks done: {ok} ok, {warnings} warnings")

    def _counts(self) -> tuple[int, int]:
        checks = self.snapshot()["checks"]
        return sum(c["status"] == "ok" for c in checks), sum(c["status"] == "warn" for c in checks)

    def _act_open_dashboard(self, _path: str | None) -> str | None:
        script = self.run_app_path / "launchers" / "nightshift-dashboard.ps1"
        code, tail = self.fx.launch_captured(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            {bootstrap.HOME_VAR: str(self.home), "NS_DASHBOARD_NO_HOST": "1"},
        )
        if code not in (None, 0):
            self.log("error", f"The dashboard launcher stopped with exit code {code}: {tail}")
            raise ActionFailed(
                tail or f"The dashboard did not start (exit code {code}). "
                "Use the Nightshift Diagnostics shortcut and send the zip."
            )
        return "Opening the dashboard"

    def _act_reveal(self, path: str | None) -> str | None:
        if path:
            target = Path(path)
            argv = ["explorer.exe", f"/select,{path}"] if target.is_file() else ["explorer.exe", path]
            self.fx.launch(argv)
        return None

    def _act_elevate_longpaths(self, _path: str | None) -> str | None:
        if self.fx.elevate_longpaths():
            def fn(s: dict[str, Any]) -> None:
                for c in s["checks"]:
                    if c["id"] == "longpaths":
                        c.update(status="ok", value="LongPathsEnabled = 1", code=None, title=None,
                                 whatToDo=None, fix=None)

            self.store.update(fn)
            self.log("info", "Long file paths are on")
            return None
        return "Windows did not give permission. Setup works without it"

    def _act_diagnostics(self, _path: str | None) -> str | None:
        path = self.write_diagnostics()
        if path is None:
            return "The diagnostics zip could not be written. Use the Nightshift Diagnostics shortcut."
        self.fx.clipboard(str(path))
        return str(path)

    def _act_close_setup(self, _path: str | None) -> str | None:
        self.shutdown.set()
        self.store.wake_all()
        return None

    # ---- diagnostics ----
    def write_diagnostics(self) -> Path | None:
        """The diagnostics zip on the Desktop (``ns doctor --bundle`` when the studio venv exists)."""
        py = self.home / "venvs" / "studio" / "Scripts" / "python.exe"
        desktop = desktop_dir()
        if py.is_file() and not self.cfg.simulate:
            res = self.fx.cmd([str(py), "-m", "ns_supervisor.cli", "doctor", "--bundle"],
                              {bootstrap.HOME_VAR: str(self.home), "PYTHONPATH": str(self.run_app_path),
                               "PYTHONUTF8": "1"}, self.run_app_path, 900)
            if res.returncode == 0:
                found = sorted(desktop.glob("nightshift-diagnostics-*.zip"), key=lambda p: p.stat().st_mtime)
                if found:
                    return found[-1]
        target = desktop / f"nightshift-diagnostics-{datetime.now():%Y%m%d}-setup.zip"
        try:
            desktop.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
                for folder in (self.home / "logs", self.cfg.data_dir / "bootstrap"):
                    if folder.is_dir():
                        for f in folder.rglob("*"):
                            if f.is_file():
                                zf.write(f, f"logs/{f.name}")
                state_file = self.home / "config" / "install_state.json"
                if state_file.is_file():
                    zf.write(state_file, "install_state.json")
                zf.writestr("preflight.json", json.dumps(self.pf, indent=2))
        except OSError:
            return None
        return target

    # ---- failures ----
    def _clear_failure(self) -> None:
        def fn(s: dict[str, Any]) -> None:
            s["failure"] = None
            s["run"] = "running"

        self.store.update(fn)

    def _set_failure(
        self, step: str, code: str, message: str, hint: str, detail: str, optional: bool
    ) -> None:
        title, what = preflight.copy_for(code, message, hint)
        self._attempts[step] = self._attempts.get(step, 0) + 1
        view = {
            "step": step, "stepTitle": labels.STEP_TITLES.get(step, step), "code": code, "title": title,
            "whatToDo": what, "detail": detail, "canSkip": optional, "attempts": self._attempts[step],
            "at": hhmm(),
        }

        def fn(s: dict[str, Any]) -> None:
            s["failure"] = view
            s["run"] = "waiting"
            for st in s["steps"]:
                if st["id"] == step:
                    st["status"] = "failed"
            for r in s["downloads"]["items"]:
                if r["status"] == "active":
                    r["status"], r["speedBps"], r["etaS"] = "failed", None, None
                    r["error"] = {"code": code, "message": title}

        self.store.update(fn)
        self.log("error", f"{code}  {title}")

    # ---- pipeline ----
    def _begin_pipeline(self) -> None:
        old = self._pipeline
        if old is not None and old.is_alive() and old is not threading.current_thread():
            old.join(timeout=5.0)           # a pipeline that just failed may still be unwinding
        with self._lock:
            self._started = True
            self._stop.clear()
            self._engine_done.clear()
            self._engine_code = None
            self._waiting_on = None
            self.meter.reset()
            if self._pipeline is not None and self._pipeline.is_alive():
                return
            thread = threading.Thread(target=self._pipeline_main, name="pipeline", daemon=True)
            self._pipeline = thread

        def fn(s: dict[str, Any]) -> None:
            s["run"] = "pausing" if self._pause_requested else "running"
            s["startedAt"] = s["startedAt"] or now_iso()
            s["failure"] = None

        self.store.update(fn)
        thread.start()

    def _pipeline_main(self) -> None:
        with keepawake.keep_awake(self.fx.set_execution_state):     # released when the run ends or fails
            self._pipeline_run()

    def _pipeline_run(self) -> None:
        step_id = BOOTSTRAP_STEPS[0]
        try:
            for step in bootstrap.bootstrap_steps():
                step_id = step.id
                self._run_bootstrap_step(step)
            if self._stop.is_set():
                raise bootstrap.BootstrapStopped()
            self._run_engine()
        except bootstrap.BootstrapStopped:
            self._finish_stopped()
        except bootstrap.BootstrapError as e:
            self._bootstrap_failed(step_id, e)
        except Exception as e:  # noqa: BLE001 - anything else must reach the window as a failure
            self._set_failure(step_id, "unexpected", "An unexpected error stopped setup.", "",
                              f"{type(e).__name__}: {e}", False)
            self._waiting_on = "pipeline"

    def _boot(self) -> bootstrap.Boot:
        fx = self.fx
        return bootstrap.Boot(
            app_path=self.cfg.app_path, home=self.home, uv=fx.uv_path(self.home),
            python_version=str(self.manifest.get("python_version", "3.12")), cmd=fx.cmd, which=fx.which,
            refresh_path=fx.refresh_path, set_home_var=fx.set_home_var, get_home_var=fx.get_home_var,
            on_row=self._on_boot_row, say=lambda t: self.log("info", t), write_answers=self._write_answers,
            gate=self._gate, stop=self._stop, fetch_uv=fx.fetch_uv,
            uv_pin=(self.manifest.get("bins") or {}).get("uv") or {},
            bin_pins=self.manifest.get("bins") or {}, vc_missing=fx.vc_missing, fetch_file=fx.fetch_file,
            run_elevated=fx.run_elevated, add_user_path=fx.add_user_path, install_app=self.installs_app,
            copy_app=fx.copy_app,
        )

    def _run_bootstrap_step(self, step: Any) -> None:
        boot = self._boot()
        self._set_step(step.id, "running", None)
        if not step.check(boot):
            step.run(boot)
        step.verify(boot)
        if step.id == "b02_home":
            self._switch_log_to_home()
            self.store.update(lambda s: s.__setitem__("appPath", str(self.run_app_path)))
            self._scrub_source_answers()
        self._set_step(step.id, "done", None)
        if step.id == "b02_home":
            self._flush_earlier_steps()

    def _scrub_source_answers(self) -> None:
        """Blank the API keys of the answers file next to INSTALL.cmd once they are held in memory.

        The wizard used to scrub it because the extracted folder was the program folder; now the program is
        copied to ``<home>/app`` (without that file) and the extracted folder is meant to be thrown away, so
        nothing else would. The window never reads the keys from the file again; the unattended run does,
        which is why this is skipped there.
        """
        if self.cfg.headless or not self.installs_app:
            return
        for path in dict.fromkeys([self.cfg.answers, self.cfg.app_path / "answers.json"]):
            if path is None or not path.is_file():
                continue
            try:
                raw = answers_view.read_answers(path)
                if any(raw.get(k) for k in ("pexels_api_key", "pixabay_api_key")):
                    answers_view.write_answers(path, raw)
                    self.log("info", f"Removed the API keys from {path.name}")
            except (OSError, ValueError):
                continue

    def _flush_earlier_steps(self) -> None:
        """b01 finished before the install folder existed: record it in install_state.json now."""
        for st in self.snapshot()["steps"]:
            if st["id"] in BOOTSTRAP_STEPS and st["id"] != "b02_home" and st["status"] == "done":
                self._persist_step(st["id"], "done", None)

    def _write_answers(self) -> None:
        if self.cfg.headless:
            return                          # the unattended wizard reads the answers file it was given
        if self._accepted is None:
            raw = self._prefill_raw()
            if raw is None:
                return
            data = raw
        else:
            data = answers_view.draft_to_answers(self._accepted, allow_cpu=self._allow_cpu())
        target = self.home / "config" / "answers.json"
        if self.snapshot()["answersLocked"] and target.is_file():
            return
        answers_view.write_answers(target, data)

    def _on_boot_row(self, key: str, status: str, note: str | None) -> None:
        mapped = {"active": "active", "done": "done", "failed": "failed", "skipped": "skipped"}[status]

        def fn(s: dict[str, Any]) -> None:
            for r in s["downloads"]["items"]:
                if r["key"] == key:
                    r["status"] = mapped
                    r["note"] = note
                    if mapped == "done":
                        r["bytesDone"] = r["bytesTotal"] or 0

        self.store.update(fn)

    def _set_step(self, step_id: str, status: str, detail: str | None) -> None:
        now = now_iso()

        def fn(s: dict[str, Any]) -> None:
            for st in s["steps"]:
                if st["id"] != step_id:
                    continue
                st["status"] = status
                if detail is not None or status in ("running", "done", "skipped", "pending"):
                    st["detail"] = detail
                if status == "running":
                    st["startedAt"], st["finishedAt"] = now, None
                    if s["failure"] and s["failure"]["step"] == step_id:
                        s["failure"] = None
                elif status in ("done", "skipped", "failed", "warning"):
                    st["finishedAt"] = now
                elif status == "pending":
                    st["startedAt"] = None

        self.store.update(fn)
        if step_id in BOOTSTRAP_STEPS and status in ("done", "failed", "running"):
            state = "pending" if status == "running" else status
            self._persist_step(step_id, state, None)

    def _persist_step(self, step_id: str, status: str, error: str | None) -> None:
        if (self.home / "config").is_dir():
            try:
                write_step(self.home, self.version, self.run_app_path, step_id, status, error)
            except OSError:
                pass

    # ---- the wizard process ----
    def _run_engine(self) -> None:
        self._prepare_models_source()
        out = self.cfg.data_dir / "bootstrap" / "preflight.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(self.pf, indent=2), encoding="utf-8")
        venv_py = self.home / "venvs" / "studio" / "Scripts" / "python.exe"
        python = self.cfg.python if self.cfg.simulate else str(venv_py)
        argv = wizard_argv(
            python, app_path=self.run_app_path, home=self.home, answers=self.home / "config" / "answers.json",
            preflight=out, bridge=True, simulate=self.cfg.simulate, halt_after=self.cfg.halt_after,
        )
        self._finished.clear()
        self._sub.clear()
        engine = self.fx.engine_factory(
            argv, env=wizard_env(self.run_app_path), cwd=self.run_app_path, on_event=self._on_event,
            on_line=self._on_line, on_exit=self._on_exit,
        )
        self._stderr_tail.clear()
        self._engine = engine
        engine.start()
        engine.send({"cmd": "keys", **self._keys})
        if self._pause_requested or not self._gate.is_set():
            engine.send({"cmd": "pause"})
            self.store.update(lambda s: s.__setitem__("run", "paused"))
        else:
            self.store.update(lambda s: s.__setitem__("run", "running"))
        self._watch_until_exit()
        self._after_engine()

    def _watch_until_exit(self) -> None:
        last_disk = time.monotonic()
        while not self._engine_done.wait(1.0):
            if time.monotonic() - last_disk >= 30.0:
                last_disk = time.monotonic()
                self._check_disk()

    def _check_disk(self) -> None:
        state = self.snapshot()
        if state["run"] != "running" or state["phase"] != "downloading":
            return
        try:
            free = self.fx.free_gb(str(self.home))
        except OSError:
            return
        if free < rows.LOW_DISK_GB:
            self._gate.clear()
            if self._engine is not None:
                self._engine.send({"cmd": "pause"})
            step = next((s["id"] for s in state["steps"] if s["status"] == "running"), "s03_download_models")
            self._waiting_on = "disk"
            self._set_failure(step, "NS-E104", "Not enough free disk space.", "",
                              f"{free:.1f} GB free at {self.home}",
                              False)

    def _after_engine(self) -> None:
        code = self._engine_code
        if self._stop.is_set() or code == EXIT_STOPPED:
            self._finish_stopped()
        elif code == 0 or (code == 75 and self.cfg.halt_after):
            self._finish_done()
        else:
            state = self.snapshot()
            if not state["failure"]:
                tail = "\n".join(self._stderr_tail[-20:]) or f"The setup program exited with code {code}."
                running = (s["id"] for s in state["steps"] if s["status"] in ("running", "pending"))
                step = next(running, "s01_questions")
                self._set_failure(step, "unexpected", "An unexpected error stopped setup.", "", tail, False)
            self._waiting_on = "pipeline"
            self.store.update(lambda s: s.__setitem__("run", "waiting"))

    def _finish_stopped(self) -> None:
        self._started = True
        self._pause_requested = False
        self._gate.set()

        def fn(s: dict[str, Any]) -> None:
            s["run"] = "stopped"
            s["downloads"]["paused"] = False
            for r in s["downloads"]["items"]:
                if r["status"] in ("active", "paused"):
                    r["status"], r["speedBps"], r["etaS"] = "queued", None, None
            for st in s["steps"]:
                if st["status"] == "running":
                    st["status"] = "pending"

        self.store.update(fn)
        self.meter.reset()
        self.log("info", "Setup stopped. Run INSTALL.cmd again to carry on from here.")

    def _finish_done(self) -> None:
        result = self._build_result()

        def fn(s: dict[str, Any]) -> None:
            s["run"] = "finished"
            s["result"] = result
            s["failure"] = None
            s["answersLocked"] = True

        self.store.update(fn)
        self.log("info", "Setup is done")

    def _bootstrap_failed(self, step_id: str, e: bootstrap.BootstrapError) -> None:
        self._set_step(step_id, "failed", e.code)
        self._persist_step(step_id, "failed", f"{e.code}: {e.detail}")
        self._set_failure(step_id, e.code, "", "", e.detail, False)
        self._waiting_on = "pipeline"

    # ---- models source (Google Drive zips) ----
    def _prepare_models_source(self) -> None:
        answers = self.snapshot().get("answers") or {}
        source = answers.get("modelsSource") or {}
        if source.get("kind") != "drive-zip" or not source.get("path"):
            return
        incoming = self.home / "cache" / "models_incoming"
        zips = sorted(Path(source["path"]).glob("*.zip"))
        self.log("info", f"Unpacking {len(zips)} zips from Google Drive")
        for z in zips:
            self._unpack(z, incoming)
        path = self.home / "config" / "answers.json"
        raw = answers_view.read_answers(path)
        raw["models_source"] = str(incoming)
        answers_view.write_answers(path, raw)

    def _unpack(self, archive: Path, target: Path) -> None:
        root = target.resolve()
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if self._stop.is_set():
                    raise bootstrap.BootstrapStopped()
                self._gate.wait()
                out = (root / info.filename).resolve()
                if out != root and root not in out.parents:
                    msg = f"unsafe path in {archive.name}: {info.filename}"
                    raise bootstrap.BootstrapError("NS-E109", msg)
                if info.is_dir():
                    out.mkdir(parents=True, exist_ok=True)
                    continue
                if out.is_file() and out.stat().st_size == info.file_size:
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, out.open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)

    def _drop_models_incoming(self) -> None:
        shutil.rmtree(self.home / "cache" / "models_incoming", ignore_errors=True)

    # ---- events from the wizard ----
    def _on_line(self, text: str) -> None:
        self._stderr_tail.append(text)
        del self._stderr_tail[:-50]
        self.fx.console(text, "info")

    def _on_exit(self, code: int) -> None:
        self._engine_code = code
        self._engine_done.set()

    def _on_event(self, ev: dict[str, Any]) -> None:
        kind = ev.get("type")
        handler = getattr(self, "_ev_" + str(kind), None)
        if handler is not None:
            handler(ev)

    def _ev_step(self, ev: dict[str, Any]) -> None:
        sid, status, detail = str(ev.get("id")), str(ev.get("status")), ev.get("detail")
        if sid not in STEP_ORDER:
            return
        title = labels.STEP_TITLES[sid]
        if status == "running":
            self.log("info", f"{title}: started")
        elif status in ("done", "warning"):
            self.log("info", f"{title}: {'already here' if detail == 'already complete' else 'done'}")
        elif status == "skipped":
            self.log("info", f"{title}: skipped" + (f" ({detail})" if detail else ""))
        self._set_step(sid, status, detail if status in ("skipped", "warning") else None)
        if sid == "s08_smoke" and status == "running":
            self._verify_pending()
        if sid == "s03_download_models" and status == "done":
            self._drop_models_incoming()
        if sid == "v01_samples" and status == "warning":
            self.store.update(self._note_render_failed)

    def _ev_say(self, ev: dict[str, Any]) -> None:
        text = str(ev.get("text", "")).strip()
        style = ev.get("style")
        if not text or style in ("rule", "markdown"):
            return
        self.fx.console(f"{datetime.now():%H:%M:%S}  {text}", "info")

        def fn(s: dict[str, Any]) -> None:
            for st in s["steps"]:
                if st["status"] == "running":
                    st["detail"] = text[:160]

        self.store.update(fn)

    def _ev_table(self, ev: dict[str, Any]) -> None:
        return None

    def _ev_plan(self, ev: dict[str, Any]) -> None:
        items = ev.get("items") or []

        def fn(s: dict[str, Any]) -> None:
            by_key = {r["key"]: r for r in s["downloads"]["items"]}
            for it in items:
                rk = labels.row_key(str(it.get("key")))
                row = by_key.get(rk)
                if row is None:
                    row = rows.make_row({"key": rk, "label": labels.label_for_file(str(it["key"])),
                                         "file": it.get("file"), "group": self._group_for(rk),
                                         "via": "download",
                                         "bytes": it.get("bytes")})
                    s["downloads"]["items"].append(row)
                    by_key[rk] = row
                elif row["bytesTotal"] is None and it.get("bytes") and rk == it.get("key"):
                    row["bytesTotal"] = int(it["bytes"])

        self.store.update(fn)

    @staticmethod
    def _group_for(key: str) -> str:
        programs = ("llama", "cudart", "qwentts", "opencode", "yt_dlp", "webview2")
        return "programs" if key.startswith(programs) else "models"

    def _ev_file(self, ev: dict[str, Any]) -> None:
        key = str(ev.get("key"))
        rk = labels.row_key(key)
        event = ev.get("event")
        if event == "start":
            self._sub[key] = [0, ev.get("total")]
        elif event == "progress":
            self._sub[key] = [int(ev.get("done") or 0), ev.get("total")]
            self.meter.update(key, int(ev.get("done") or 0))
            self._console_progress(key, rk)
        elif event == "finish":
            self._finished.add(key)
            self.meter.drop(key)
        elif event == "retry":
            self.meter.drop(key)
        outcome = ev.get("outcome")

        def fn(s: dict[str, Any]) -> None:
            items = s["downloads"]["items"]
            row = next((r for r in items if r["key"] == rk), None)
            if row is None:
                row = rows.make_row({"key": rk, "label": labels.label_for_file(key), "file": key,
                                     "group": self._group_for(rk), "via": "download", "bytes": None})
                items.append(row)
            subs = [(k, v) for k, v in self._sub.items() if labels.row_key(k) == rk]
            totals = [v[1] for _k, v in subs if v[1]]
            row["bytesDone"] = sum(int(v[0]) for _k, v in subs)
            if totals and (len(subs) == 1 or len(totals) == len(subs)):
                row["bytesTotal"] = sum(int(t) for t in totals)
            if event in ("start", "progress") and row["status"] not in ("paused",):
                row["status"] = "active"
                speeds = [self.meter.speed(k) for k, _v in subs]
                speed = sum(x for x in speeds if x) or None
                row["speedBps"] = speed
                remaining = (row["bytesTotal"] or 0) - row["bytesDone"]
                row["etaS"] = rows.eta_seconds(remaining, speed) if row["bytesTotal"] and speed else None
                row["note"] = None
            elif event == "retry":
                row["speedBps"], row["etaS"] = None, None
                row["note"] = retry_note(
                    str(ev.get("reason")), float(ev.get("waitS") or 0),
                    int(ev.get("attempt") or 0), int(ev.get("of") or 0))
            elif event == "finish":
                all_done = all(k in self._finished for k, _v in subs)
                if outcome == "failed":
                    row["status"], row["speedBps"], row["etaS"] = "failed", None, None
                elif outcome == "missing":
                    row["status"], row["note"] = "skipped", "not available, setup goes on without it"
                elif all_done:
                    row["status"], row["speedBps"], row["etaS"] = "done", None, None
                    if row["bytesTotal"]:
                        row["bytesDone"] = row["bytesTotal"]
                    row["note"] = {"copied": "copied from your folder", "skipped": "already here"}.get(
                        str(outcome), None)

        self.store.update(fn)

    def _console_progress(self, key: str, rk: str) -> None:
        now = time.monotonic()
        if now - self._progress_log.get(key, 0.0) < 10.0:
            return
        self._progress_log[key] = now
        snap = self.snapshot()["downloads"]
        row = next((r for r in snap["items"] if r["key"] == rk), None)
        if not row or not row["bytesTotal"]:
            return
        # Measured numbers only: the file's own speed, then the whole download's speed and time left
        # (the same trailing average the setup window shows), never an assumed line speed.
        speed = f"  {row['speedBps'] / 1e6:.1f} MB/s" if row["speedBps"] else ""
        total = snap.get("overall") or {}
        left = ""
        if total.get("etaS"):
            mins = max(1, int(total["etaS"] // 60))
            left = f"  all files: about {mins // 60} h {mins % 60:02d} min left" if mins >= 60 else \
                f"  all files: about {mins} min left"
            if total.get("speedBps"):
                left += f" at {total['speedBps'] / 1e6:.1f} MB/s"
        elif row["status"] == "active":
            left = "  measuring the speed"
        self.fx.console(
            f"{datetime.now():%H:%M:%S}  {row['file'] or key}  {row['bytesDone'] / 1024 ** 3:.1f} of "
            f"{row['bytesTotal'] / 1024 ** 3:.1f} GB{speed}{left}", "info")

    def _ev_warn(self, ev: dict[str, Any]) -> None:
        code = str(ev.get("code", ""))
        if code == "prompt":
            self.log("warn", f"The setup program asked a question it should not ask: {ev.get('detail')}")
            return
        title, what = preflight.copy_for(code)
        step = next((s["id"] for s in self.snapshot()["steps"] if s["status"] == "running"), None)

        def fn(s: dict[str, Any]) -> None:
            if not any(n["code"] == code for n in s["notices"]):
                s["notices"].append({"code": code, "title": title, "whatToDo": what, "step": step})

        self.store.update(fn)
        self.log("warn", f"{code}  {title}")

    def _ev_failure(self, ev: dict[str, Any]) -> None:
        self._waiting_on = "engine"         # before the state shows "waiting": a quick Retry must find it
        self._set_failure(str(ev.get("step")), str(ev.get("code")), str(ev.get("message", "")),
                          str(ev.get("hint", "")), str(ev.get("detail", "")), bool(ev.get("optional")))

    def _verify_pending(self) -> None:
        rows = labels.VERIFY_PENDING_ROWS

        def fn(s: dict[str, Any]) -> None:
            if s["verify"] is None:
                s["verify"] = {"checks": [], "voice": None, "render": None, "renderNote": None,
                               "reportReady": False}
            s["verify"]["checks"] = [
                {"name": n, "label": label, "status": "running", "value": None, "note": None}
                for n, label in rows
            ]

        self.store.update(fn)

    def _ev_verify(self, ev: dict[str, Any]) -> None:
        speaker = ((self.snapshot().get("answers") or {}).get("voice") or {}).get("speaker")
        checks = labels.verify_rows([c for c in ev.get("checks") or [] if isinstance(c, dict)], speaker)

        def fn(s: dict[str, Any]) -> None:
            if s["verify"] is None:
                s["verify"] = {"checks": [], "voice": None, "render": None, "renderNote": None,
                               "reportReady": False}
            s["verify"]["checks"] = checks
            s["verify"]["reportReady"] = (self.home / "logs" / "verify-report.json").is_file()

        self.store.update(fn)

    def _ev_sample(self, ev: dict[str, Any]) -> None:
        kind = ev.get("kind")
        path = Path(str(ev.get("path", "")))
        seconds = float(ev.get("seconds") or 0)
        answers = self.snapshot().get("answers") or {}
        speaker = (answers.get("voice") or {}).get("speaker") or DEFAULT_SPEAKER

        def fn(s: dict[str, Any]) -> None:
            if s["verify"] is None:
                s["verify"] = {"checks": [], "voice": None, "render": None, "renderNote": None,
                               "reportReady": False}
            v = s["verify"]
            if kind == "voice":
                v["voice"] = {"url": "/api/file/voice-sample.wav", "speaker": speaker,
                              "seconds": round(seconds, 2),
                              "text": SAMPLE_TEXT, "bars": samples.wav_bars(path)}
            elif kind == "render":
                v["render"] = {"videoUrl": "/api/file/render-test.mp4",
                               "thumbUrl": "/api/file/render-test.jpg",
                               "seconds": round(seconds, 2) or 6.0, "width": 1080, "height": 1920,
                               "encoder": self._encoder()}
                v["renderNote"] = None

        self.store.update(fn)

    def _encoder(self) -> str:
        try:
            data = json.loads((self.home / "config" / "runtime.json").read_text(encoding="utf-8-sig"))
            enc = (data.get("video") or {}).get("encoder")
            return "libx264" if enc == "libx264" else "h264_nvenc"
        except (OSError, ValueError):
            return "h264_nvenc"

    def _note_render_failed(self, s: dict[str, Any]) -> None:
        if s["verify"] is None:
            s["verify"] = {"checks": [], "voice": None, "render": None, "renderNote": None,
                           "reportReady": False}
        if s["verify"]["render"] is None:
            title, what = preflight.copy_for("NS-W403")
            s["verify"]["renderNote"] = f"{title}. {what}"

    def _ev_bundle(self, ev: dict[str, Any]) -> None:
        path = ev.get("path")
        self._bundle = str(path) if path else None

    _bundle: str | None = None

    # ---- result ----
    def _build_result(self) -> dict[str, Any]:
        state = self.snapshot()
        answers = state.get("answers") or {}
        flags = {}
        try:
            flags = json.loads((self.home / "config" / "wizard_flags.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            pass
        sizes = {g["id"]: g for g in state["plan"]["groups"]}
        models = {i["label"]: i for i in sizes["models"]["items"]}
        llm = next((i for i in sizes["models"]["items"] if i["label"].startswith("Story model")), None)

        def gb(n: float | None) -> str:
            return f"{n / 1024 ** 3:.1f} GB" if n else "installed"

        voices = sum((i["bytes"] or 0) for i in sizes["models"]["items"] if i["label"].startswith("Voice"))
        rest = sum((i["bytes"] or 0) for k, i in models.items()
                   if k in ("Memory index", "Caption timing", "Face finder for Clipper"))
        installed = [
            {"label": llm["label"] if llm else "Story model", "detail": gb(llm["bytes"] if llm else None)},
            {"label": "Voices: presets and your own", "detail": gb(voices)},
            {"label": "Captions, memory, faces", "detail": gb(rest)},
            {"label": "Nightshift Code", "detail": "yes" if answers.get("installCode", True) else "no"},
            {"label": "Night run", "detail": answers.get("runTime", "03:00")},
            {"label": "Everything else", "detail": str(self.home)},
        ]
        warnings = list(state["notices"])
        if state["machine"].get("isAdmin"):
            user = os.environ.get("USERNAME", "this user")
            title = f"Setup ran as administrator. Shortcuts were made for {user}"
            warnings.append({"code": "admin", "title": title,
                             "whatToDo": "", "step": None})
        for c in state["checks"]:
            if c["status"] == "warn" and c["id"] == "longpaths":
                warnings.append({"code": c["code"] or "", "title": c["title"] or "",
                                 "whatToDo": c["whatToDo"] or "",
                                 "step": None})
        started = state.get("startedAt")
        duration = 0
        if started:
            try:
                duration = max(0, int((datetime.now() - datetime.fromisoformat(started)).total_seconds()))
            except ValueError:
                duration = 0
        return {
            "home": str(self.home), "outbox": str(self.home / "outbox"), "appPath": str(self.run_app_path),
            "installed": installed, "shortcuts": list(SHORTCUTS), "runTime": answers.get("runTime", "03:00"),
            "scheduled": flags.get("schedule") == "installed",
            "diagnosticsZip": self._bundle or flags.get("bundle"),
            "durationS": duration, "warnings": warnings,
        }

    # ---- headless (answers.json next to INSTALL.cmd, or -NoUi) ----
    def run_headless(self) -> int:
        """Checks, bootstrap, then the wizard without a bridge and without a window. Returns the exit code."""
        state = self.snapshot()
        say = self.fx.console
        for c in state["checks"]:
            tag = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}[c["status"]]
            say(f"{tag} {c['label']:<28} {c['value']}", "info")
            if c["status"] != "ok" and c["code"]:
                say(f"       {c['code']}: {c['title']}", "info")
        if state["blocking"]:
            say("Setup cannot continue until the red items are fixed.", "error")
            return EXIT_BLOCKED
        raw = self._prefill_raw() or {}
        self._accepted = None
        if not self.cfg.answers:
            say("answers.json not found; headless setup needs it.", "error")
            return 11
        self.home = Path(str(raw.get("home_path") or self.home))
        self._started = True
        try:
            for step in bootstrap.bootstrap_steps():
                self._run_bootstrap_step(step)
        except bootstrap.BootstrapError as e:
            title, what = preflight.copy_for(e.code)
            say(f"{e.code}  {title}", "error")
            say(f"    {e.detail}", "error")
            say(f"    Fix: {what}", "error")
            return 12 if e.code in ("NS-E110", "NS-E111") else 14
        out = self.cfg.data_dir / "bootstrap" / "preflight.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(self.pf, indent=2), encoding="utf-8")
        venv_python = self.home / "venvs" / "studio" / "Scripts" / "python.exe"
        python = self.cfg.python if self.cfg.simulate else str(venv_python)
        app = self.run_app_path
        argv = [python, "-X", "utf8", "-m", "installer.stage2.wizard", "--app-path", str(app),
                "--answers", str(self.cfg.answers), "--preflight", str(out)]
        if self.cfg.simulate:
            argv += ["--simulate-downloads", "--no-head-checks", "--non-interactive",
                     "--home", str(self.home)]
        if self.cfg.halt_after:
            argv += ["--halt-after", self.cfg.halt_after]
        env = wizard_env(app)
        env[bootstrap.HOME_VAR] = str(self.home)
        return subprocess.run(argv, cwd=str(app), env=env, check=False).returncode
