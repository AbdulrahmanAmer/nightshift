"""Engine bridge, the wizard side (component C1; docs/design/installer-ui.md section 1.7).

With ``--ui-bridge stdio`` the wizard (P2) builds a ``BridgeUi`` instead of the rich console ``Ui``. It writes
one JSON object per line to stdout (UTF-8, flushed) and reads commands from stdin on a daemon thread, so the
setup server (P1) can show progress, ask for a decision after a failure, pause the downloads and stop the
run. In bridge mode stdout carries nothing else: stray rich output goes to stderr.

Events out: hello, step, say, table, plan, file, warn, failure, verify, sample, bundle, bye.
Commands in: keys (always the first line), decision, pause, resume, stop.
A ``file`` event is start, progress, retry (a stalled or failed transfer waits to be tried again) or finish.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from rich.console import Console

from installer.stage2.ui import Ui

PROGRESS_INTERVAL_S = 0.25  # at most 4 progress events per second per file
GATE_POLL_S = 0.2
DECISIONS = ("retry", "skip", "quit")
FILE_OUTCOMES = {
    "downloaded": "downloaded", "copied": "copied", "skipped": "skipped", "missing_optional": "missing",
    "missing": "missing", "failed": "failed",
}


class SetupStopped(BaseException):
    """The setup window pressed Stop. A BaseException (like KeyboardInterrupt) so no step swallows it."""


class BridgeReporter:
    """Download reporter: forwards per-file progress to P1 and honours pause and stop."""

    def __init__(self, ui: BridgeUi, total_bytes: int = 0) -> None:
        self._ui = ui
        self.total_bytes = total_bytes
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def __enter__(self) -> BridgeReporter:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def start(self, name: str, total: int | None) -> None:
        """A file started."""
        self._ui.emit({"type": "file", "key": name, "event": "start", "total": total})

    def update(self, name: str, done: int, total: int | None) -> None:
        """Bytes done so far. Blocks while paused (the next 1 MB chunk waits here) and raises on stop."""
        self._ui.checkpoint()
        now = time.monotonic()
        with self._lock:
            last = self._last.get(name, 0.0)
            final = total is not None and done >= total
            if not final and now - last < PROGRESS_INTERVAL_S:
                return
            self._last[name] = now
        self._ui.emit({"type": "file", "key": name, "event": "progress", "done": done, "total": total})

    def finish(self, name: str, outcome: str) -> None:
        """A file ended."""
        self._ui.emit(
            {"type": "file", "key": name, "event": "finish", "outcome": FILE_OUTCOMES.get(outcome, outcome)}
        )

    def retrying(self, name: str, attempt: int, reason: str, wait_s: float) -> None:
        """A transfer failed (reason stall, network or hash) and is tried again after ``wait_s`` seconds."""
        self._ui.emit({"type": "file", "key": name, "event": "retry", "attempt": attempt, "reason": reason,
                       "waitS": wait_s})


class BridgeUi(Ui):
    """The wizard's UI when a setup window drives it."""

    bridge = True

    def __init__(self, out: TextIO | None = None, inp: TextIO | None = None) -> None:
        super().__init__(Console(stderr=True), interactive=False)
        self._out = out if out is not None else sys.stdout
        self._in = inp if inp is not None else sys.stdin
        self._lock = threading.Lock()
        self.gate = threading.Event()
        self.gate.set()                     # set = running, cleared = paused
        self.stop = threading.Event()
        self._decisions: queue.Queue[str] = queue.Queue()
        self._keys: queue.Queue[dict[str, Any]] = queue.Queue()
        self._reader: threading.Thread | None = None

    # ---- wire ----
    def emit(self, event: dict[str, Any]) -> None:
        """Write one event line."""
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str)
        with self._lock:
            self._out.write(line + "\n")
            self._out.flush()

    def start_reader(self) -> None:
        """Read commands on a daemon thread (end of input counts as stop)."""
        if self._reader is not None:
            return
        self._reader = threading.Thread(target=self._read_loop, name="bridge-commands", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        try:
            for raw in self._in:
                text = raw.strip()
                if not text:
                    continue
                try:
                    cmd = json.loads(text)
                except ValueError:
                    continue
                if isinstance(cmd, dict):
                    self.handle(cmd)
        except (OSError, ValueError):
            pass
        self.handle({"cmd": "stop"})        # P1 went away: stop cleanly

    def handle(self, cmd: dict[str, Any]) -> None:
        """Apply one command from P1."""
        name = cmd.get("cmd")
        if name == "keys":
            self._keys.put(cmd)
        elif name == "decision":
            action = str(cmd.get("action", ""))
            if action in DECISIONS:
                self._decisions.put(action)
        elif name == "pause":
            self.gate.clear()
        elif name == "resume":
            self.gate.set()
        elif name == "stop":
            self.stop.set()
            self.gate.set()                 # wake anything waiting at the pause gate
            self._decisions.put("quit")

    def wait_keys(self, timeout: float = 60.0) -> dict[str, str | None]:
        """The first command: the stock footage keys (None keeps a stored key)."""
        try:
            cmd = self._keys.get(timeout=timeout)
        except queue.Empty:
            return {}
        return {k: (str(cmd[k]) if cmd.get(k) else None) for k in ("pexels_api_key", "pixabay_api_key")}

    def checkpoint(self) -> None:
        """Called from the download progress hook: wait while paused, raise SetupStopped after Stop."""
        while not self.gate.wait(GATE_POLL_S):
            if self.stop.is_set():
                break
        if self.stop.is_set():
            raise SetupStopped()

    # ---- output ----
    def say(self, msg: str = "", style: str | None = None) -> None:
        """A log line."""
        self.emit({"type": "say", "text": msg, "style": style})

    def rule(self, title: str) -> None:
        """A section title."""
        self.emit({"type": "say", "text": title, "style": "rule"})

    def markdown(self, text: str) -> None:
        """Markdown text."""
        self.emit({"type": "say", "text": text, "style": "markdown"})

    def panel(self, body: str, title: str = "", style: str = "red") -> None:
        """A boxed message."""
        self.emit({"type": "say", "text": f"{title}: {body}" if title else body, "style": "panel"})

    def table(self, title: str, columns: Sequence[str], rows: Sequence[Sequence[str]],
              status_col: int | None = None) -> None:
        """A table."""
        self.emit({"type": "table", "title": title, "columns": list(columns),
                   "rows": [[str(c) for c in r] for r in rows]})

    def reporter(self, total_bytes: int = 0) -> BridgeReporter:  # type: ignore[override]
        """The download reporter."""
        return BridgeReporter(self, total_bytes)

    # ---- input: never blocks (the answers are complete, a prompt means a bug) ----
    def ask(self, prompt: str, default: str | None = None, *, secret: bool = False) -> str:
        """Return the default and report the prompt."""
        self.emit({"type": "warn", "code": "prompt", "detail": prompt})
        return default or ""

    def confirm(self, prompt: str, default: bool = True) -> bool:
        """Return the default and report the prompt."""
        self.emit({"type": "warn", "code": "prompt", "detail": prompt})
        return default

    def choose(self, prompt: str, options: Sequence[str], default: str | None = None) -> str:
        """Return the default and report the prompt."""
        self.emit({"type": "warn", "code": "prompt", "detail": prompt})
        return default if default is not None else options[0]

    def pause(self, prompt: str = "Press Enter to continue") -> None:
        """No-op."""

    def play(self, wav: Path) -> None:
        """No-op (the window plays samples)."""

    # ---- hooks ----
    def step_status(self, step_id: str, status: str, title: str, detail: str | None = None) -> None:
        """A step changed."""
        self.emit({"type": "step", "id": step_id, "status": status, "title": title, "detail": detail})

    def decide_failure(self, step_id: str, failure: Any, optional: bool, attempt: int) -> str | None:
        """Tell P1 about the failure and block until it answers retry, skip or quit."""
        while True:
            try:
                self._decisions.get_nowait()    # drop stale answers
            except queue.Empty:
                break
        self.emit({
            "type": "failure", "step": step_id, "code": failure.code, "message": failure.message,
            "hint": failure.hint, "detail": failure.detail, "optional": optional, "attempt": attempt,
        })
        while True:
            try:
                action = self._decisions.get(timeout=0.5)
            except queue.Empty:
                if self.stop.is_set():
                    return "quit"
                continue
            return action                   # the wizard turns a skip of a required step into quit

    def notice(self, code: str, detail: str = "") -> None:
        """A warning code was raised."""
        self.emit({"type": "warn", "code": code, "detail": detail})

    def plan(self, items: Sequence[Any]) -> None:
        """The download plan about to run."""
        rows = [{"key": i.name, "file": Path(i.dest).name, "bytes": i.size, "via": "download"} for i in items]
        self.emit({"type": "plan", "items": rows, "totalBytes": sum(int(i.size or 0) for i in items)})

    def verify_checks(self, checks: Sequence[dict[str, Any]]) -> None:
        """The ns verify rows."""
        rows = [{"name": str(c.get("name", "")), "passed": bool(c.get("passed")),
                 "value": None if c.get("value") is None else str(c.get("value")),
                 "limit": None if c.get("limit") is None else str(c.get("limit"))} for c in checks]
        self.emit({"type": "verify", "checks": rows})

    def sample(self, kind: str, path: Path, seconds: float | None = None) -> None:
        """A test sample was written."""
        self.emit({"type": "sample", "kind": kind, "path": str(path), "seconds": seconds})

    def bundle(self, path: Path | None) -> None:
        """The diagnostics zip."""
        self.emit({"type": "bundle", "path": None if path is None else str(path)})
