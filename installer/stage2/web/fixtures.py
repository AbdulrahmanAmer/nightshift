"""One frozen SetupState per screen, for design review (docs/design/installer-ui.md 5.7).

``python -m installer.stage2.web --simulate --fixture downloading --no-window --port 8765`` serves one of them
unchanged, so the UI can be reviewed (``npm run dev`` proxies ``/api`` to it) without running a setup. The
states are built from a simulated controller instead of being stored as files, so they cannot drift from the
real state shape.
"""

from __future__ import annotations

import copy
import tempfile
import threading
from pathlib import Path
from typing import Any

import ns_common.diskneed as diskneed
from installer.stage2.web import plan
from installer.stage2.web.controller import Config, SetupController
from installer.stage2.web.fakes import fake_effects
from installer.stage2.web.model import STEP_ORDER

NAMES = (
    "welcome", "welcome-resume", "checks-ok", "checks-blocking", "choices", "downloading", "paused",
    "installing", "verifying", "done", "failed",
)
GB = 1024 ** 3
DRAFT = {
    "ownerName": "Abdo",
    "channel": {"displayName": "After Dark Tales", "niche": "horror fiction", "language": "en"},
    "homePath": "D:\\Nightshift", "llmTier": "small", "voice": {"speaker": "Ryan"},
    "modelsSource": {"kind": "internet", "path": None}, "runTime": "03:00", "mastersPerWeek": 1,
    "nightly": True, "nightlySeen": "new",
    "installCode": True, "theme": "dark", "pexelsKey": "demo-pexels-key", "pixabayKey": "demo-pixabay-key",
}


def _set_steps(state: dict[str, Any], done_until: str | None, running: str | None = None) -> None:
    reached = done_until is None
    for st in state["steps"]:
        if st["id"] == running:
            st["status"] = "running"
            st["startedAt"] = "2026-11-02T21:41:00"
            st["detail"] = "Trying the story model at different settings"
        elif not reached:
            st["status"] = "done"
            st["finishedAt"] = "2026-11-02T21:30:00"
        else:
            st["status"] = "pending"
        if st["id"] == done_until:
            reached = True


def _download_rows(state: dict[str, Any], *, paused: bool = False) -> None:
    items = state["downloads"]["items"]
    for r in items:
        if r["via"] in ("winget", "uv", "present"):
            r.update(status="done", note="already here" if r["via"] == "present" else "installed")
        elif r["group"] == "programs":
            r.update(status="done", bytesDone=r["bytesTotal"] or 0)
    models = [r for r in items if r["group"] == "models"]
    if models:
        first = models[0]
        total = first["bytesTotal"] or 9 * GB
        first.update(status="paused" if paused else "active", bytesDone=int(total * 0.23), bytesTotal=total,
                     speedBps=None if paused else 11.4e6, etaS=None if paused else 600)
    state["downloads"]["paused"] = paused
    done = sum(r["bytesDone"] for r in items)
    total = sum(r["bytesTotal"] or 0 for r in items)
    state["downloads"]["overall"] = {
        "bytesTotal": total, "bytesDone": done, "filesTotal": len(items),
        "filesDone": sum(1 for r in items if r["status"] == "done"),
        "speedBps": None if paused else 11.4e6, "etaS": None if paused else 2460,
    }


def build(name: str, app_path: Path | None = None) -> dict[str, Any]:
    """The state for fixture ``name``."""
    if name not in NAMES:
        raise KeyError(f"unknown fixture {name}; choose one of {', '.join(NAMES)}")
    app = app_path or Path(__file__).resolve().parents[3]
    tmp = Path(tempfile.mkdtemp(prefix="ns-fixture-"))
    fx, _cmd = fake_effects(tmp)
    ctl = SetupController(Config(app_path=app, data_dir=tmp, simulate=True, home=tmp / "home"), fx)
    state = ctl.snapshot()
    state["machine"]["drives"] = [{"letter": "D", "freeGb": 412.0, "type": "SSD"},
                                  {"letter": "C", "freeGb": 97.0, "type": "SSD"}]
    if name == "welcome":
        pass
    elif name == "welcome-resume":
        state["resumed"] = True
        state["uiStep"] = "welcome"
        _set_steps(state, "s02_download_bins")
    elif name == "checks-ok":
        state["uiStep"] = "checks"
    elif name == "checks-blocking":
        state["uiStep"] = "checks"
        need = plan.home_need(ctl.manifest, tmp / "home", "small")  # what the real row says (H-6)
        for c in state["checks"]:
            if c["id"] == "disk":
                c.update(status="fail", blocking=True, code="NS-E104",
                         value=need.sentence(31 * diskneed.GIB, "C:", subject="This install"),
                         title="Not enough free disk space",
                         whatToDo=f"Free {need.gb} GB on this drive, or pick another drive below",
                         fix={"kind": "choose-drive", "label": "Pick another drive"})
        state["blocking"] = True
    elif name == "choices":
        state["uiStep"] = "choices"
    else:
        ctl_ok = ctl.submit_answers(copy.deepcopy(DRAFT))
        state = ctl.snapshot()
        if not ctl_ok["ok"]:
            raise RuntimeError(f"fixture draft was rejected: {ctl_ok['errors']}")
        state["uiStep"] = None
        state["startedAt"] = "2026-11-02T21:04:00"
        if name in ("downloading", "paused"):
            _set_steps(state, "s02_download_bins", None)
            state["steps"][5]["status"] = "running"
            state["run"] = "paused" if name == "paused" else "running"
            _download_rows(state, paused=name == "paused")
        elif name == "installing":
            _set_steps(state, "s03_download_models", "s06_tune")
            state["run"] = "running"
            _download_rows(state)
        elif name == "verifying":
            _set_steps(state, "s07_configs", "s08_smoke")
            state["run"] = "running"
            state["verify"] = {"checks": [
                {"name": "llm", "label": "Story model", "status": "passed", "value": "works",
                 "note": None},
                {"name": "embed", "label": "Story memory", "status": "passed", "value": "ready",
                 "note": None},
                {"name": "tts", "label": "Voice engine", "status": "passed", "value": "Ryan, 0.31 RTF",
                 "note": None},
                {"name": "whisper", "label": "Captions", "status": "passed", "value": "graphics card",
                 "note": None},
                {"name": "nvenc", "label": "Video encoder", "status": "passed", "value": "NVENC",
                 "note": None},
            ], "voice": None, "render": None, "renderNote": None, "reportReady": True}
        elif name == "done":
            _set_steps(state, STEP_ORDER[-1])
            state["run"] = "finished"
            state["result"] = {
                "home": "D:\\Nightshift", "outbox": "D:\\Nightshift\\outbox", "appPath": str(app),
                "installed": [{"label": "Story model (Small)", "detail": "9.0 GB"},
                              {"label": "Voices: presets and your own", "detail": "4.4 GB"},
                              {"label": "Captions, memory, faces", "detail": "1.7 GB"},
                              {"label": "Nightshift Code", "detail": "yes"},
                              {"label": "Night run", "detail": "03:00"},
                              {"label": "Everything else", "detail": "D:\\Nightshift"}],
                "shortcuts": ["Nightshift Dashboard", "Nightshift Outbox", "Nightshift - Diagnostics"],
                "runTime": "03:00", "scheduled": True, "nightly": "on",
                "nightlyLine": "Automatic runs: on at 03:00, every night (the PC wakes itself for it).",
                "diagnosticsZip": "C:\\Users\\Abdo\\Desktop\\nightshift-diagnostics-20261102.zip",
                "stockKeyMissing": True, "durationS": 3480, "warnings": [],
            }
        elif name == "failed":
            _set_steps(state, "s02_download_bins")
            state["steps"][5]["status"] = "failed"
            state["run"] = "waiting"
            state["failure"] = {
                "step": "s03_download_models", "stepTitle": "Download AI models", "code": "NS-E109",
                "title": "A downloaded file is damaged",
                "whatToDo": "Press Retry. Setup downloads that file again. If the same error comes back, "
                            "send "
                            "the diagnostics zip",
                "detail": "qwen-talker-1.7b-base-Q8_0.gguf: expected 4b9a33a2..., got 0c1d...",
                "canSkip": False,
                "attempts": 3, "at": "21:52",
            }
            _download_rows(state)
    state["phase"] = {
        "welcome": "checks", "welcome-resume": "choices", "checks-ok": "checks", "checks-blocking": "checks",
        "choices": "choices", "downloading": "downloading", "paused": "downloading",
        "installing": "installing",
        "verifying": "verifying", "done": "done", "failed": "failed",
    }[name]
    state["rev"] = 1
    assert all(s["id"] in STEP_ORDER for s in state["steps"])
    return state


class FixtureController:
    """Answers the API from one frozen state; actions change nothing."""

    def __init__(self, name: str, app_path: Path | None = None) -> None:
        self.state = build(name, app_path)
        self.shutdown = threading.Event()
        self.version = self.state["version"]

    def snapshot(self) -> dict[str, Any]:
        """The frozen state."""
        return copy.deepcopy(self.state)

    def wait_state(self, since: int | None, timeout: float = 15.0) -> dict[str, Any]:
        """Long poll: nothing ever changes, so wait out the timeout when the client is up to date."""
        if since is not None and since >= self.state["rev"]:
            self.shutdown.wait(min(timeout, 15.0))
        return self.snapshot()

    def set_ui_step(self, step: str) -> tuple[int, dict[str, Any]]:
        """Pretend."""
        return 200, {"ok": True, "message": None, "state": self.snapshot()}

    def submit_answers(self, draft: dict[str, Any]) -> dict[str, Any]:
        """Accept anything."""
        return {"ok": True, "answers": self.state["answers"], "plan": self.state["plan"]}

    def check_key(self, service: str, key: str) -> dict[str, Any]:
        """Any non-empty key works."""
        return {"ok": bool(key), "code": None if key else "NS-E501", "message": None}

    def disk(
        self, path: str, tier: str, source: bool, custom: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """A roomy drive."""
        return {"path": path, "exists": True, "drive": path[:1].upper() or "D", "freeGb": 412.0,
                "type": "SSD",
                "needGb": plan.required_free_gb(tier), "ok": True, "message": "412 GB free on D: (SSD)",
                "modelsFound": None}

    def do_action(self, name: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        """Accept everything, change nothing (close-setup ends the server)."""
        if name == "close-setup":
            self.shutdown.set()
        return 200, {"ok": True, "message": None, "state": self.snapshot()}

    def file_path(self, name: str) -> Path | None:
        """No sample files."""
        return None

