"""Console output and prompts for the wizard (component C1).

Every line the installer shows goes through ``Ui``, so tests can replace it with a scripted fake and the
rest of the code never calls ``print``.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.prompt import Confirm, Prompt
from rich.table import Table

STATUS_STYLE = {"ok": "green", "warn": "yellow", "fail": "red", "done": "green", "skipped": "dim",
                "failed": "red", "pending": "yellow"}


class RichReporter:
    """Per-file and overall progress bars with speed and ETA (rich). Use as a context manager."""

    def __init__(self, console: Console, total_bytes: int = 0) -> None:
        self._progress = Progress(
            TextColumn("{task.description}", justify="left"),
            BarColumn(bar_width=30),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
        )
        self._lock = threading.Lock()
        self._tasks: dict[str, TaskID] = {}
        self._done: dict[str, int] = {}
        self._overall = self._progress.add_task("[bold]Overall", total=total_bytes or None)

    def __enter__(self) -> RichReporter:
        self._progress.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._progress.stop()

    def start(self, name: str, total: int | None) -> None:
        """Add a bar for a file."""
        with self._lock:
            self._tasks[name] = self._progress.add_task(name[-38:], total=total)

    def update(self, name: str, done: int, total: int | None) -> None:
        """Advance a file bar and the overall bar."""
        with self._lock:
            task = self._tasks.get(name)
            if task is None:
                return
            self._progress.update(task, completed=done, total=total)
            self._done[name] = done
            self._progress.update(self._overall, completed=sum(self._done.values()))

    def finish(self, name: str, outcome: str) -> None:
        """Mark a file finished."""
        with self._lock:
            task = self._tasks.get(name)
            if task is not None:
                self._progress.update(task, description=f"{name[-30:]} [{outcome}]")

    def site_wait(self, name: str, host: str, status: int, wait_s: float, left_s: float) -> None:
        """A download site answered 5xx or 429: print the wait above the bars (host, status, time left)."""
        from installer.stage2.downloader import site_wait_text

        self._progress.console.print(f"{name[-38:]}: {site_wait_text(host, status, wait_s, left_s)}",
                                     markup=False, highlight=False)


class Ui:
    """Rich-based console for the wizard."""

    def __init__(self, console: Console | None = None, *, interactive: bool = True) -> None:
        self.console = console or Console()
        self.interactive = interactive

    # ---- bridge hooks (no-ops here; installer.stage2.bridge.BridgeUi forwards them to the setup window) ----
    bridge = False

    def step_status(self, step_id: str, status: str, title: str, detail: str | None = None) -> None:
        """A step started or ended (running, done, skipped, failed, warning, pending)."""

    def decide_failure(self, step_id: str, failure: Any, optional: bool, attempt: int) -> str | None:
        """Return retry, skip or quit to decide a failure for the caller; None keeps the console menu."""
        return None

    def notice(self, code: str, detail: str = "") -> None:
        """A warning (NS-W code) was raised."""

    def plan(self, items: Sequence[Any]) -> None:
        """A download plan is about to run (``DownloadItem`` objects)."""

    def verify_checks(self, checks: Sequence[dict[str, Any]]) -> None:
        """The ``ns verify`` result rows (name, passed, value, limit)."""

    def sample(self, kind: str, path: Path, seconds: float | None = None) -> None:
        """A test sample (voice, render, thumb) was written."""

    def bundle(self, path: Path | None) -> None:
        """The diagnostics zip was written (None when it could not be)."""

    # ---- output ----
    def say(self, msg: str = "", style: str | None = None) -> None:
        """Print a line."""
        self.console.print(msg, style=style, highlight=False)

    def rule(self, title: str) -> None:
        """Print a section title."""
        self.console.rule(f"[bold]{title}")

    def markdown(self, text: str) -> None:
        """Render markdown text (the welcome screen)."""
        self.console.print(Markdown(text))

    def panel(self, body: str, title: str = "", style: str = "red") -> None:
        """Print a boxed message (errors)."""
        self.console.print(Panel(body, title=title, border_style=style))

    def table(self, title: str, columns: Sequence[str], rows: Sequence[Sequence[str]],
              status_col: int | None = None) -> None:
        """Print a table; ``status_col`` colours that column by ok/warn/fail/done/skipped."""
        t = Table(title=title, show_lines=False)
        for c in columns:
            t.add_column(c)
        for row in rows:
            cells = list(row)
            if status_col is not None:
                style = STATUS_STYLE.get(cells[status_col].strip().lower())
                if style:
                    cells[status_col] = f"[{style}]{cells[status_col]}[/{style}]"
            t.add_row(*cells)
        self.console.print(t)

    def reporter(self, total_bytes: int = 0) -> RichReporter:
        """A progress reporter for downloads."""
        return RichReporter(self.console, total_bytes)

    # ---- input ----
    def _eof(self) -> None:
        """Standard input ended (no console, a closed console, an empty redirected stdin): from now on
        every prompt takes its default instead of raising EOFError out of a step (first dev-mode run)."""
        self.interactive = False
        self.say("No keyboard input is available; setup continues with the default answers.", "yellow")

    def ask(self, prompt: str, default: str | None = None, *, secret: bool = False) -> str:
        """Ask for a line of text; Enter accepts ``default``."""
        if not self.interactive:
            return default or ""
        try:
            if default is not None:
                return Prompt.ask(prompt, default=default, console=self.console, password=secret)
            return Prompt.ask(prompt, console=self.console, password=secret)
        except EOFError:
            self._eof()
            return default or ""

    def confirm(self, prompt: str, default: bool = True) -> bool:
        """Yes/no question."""
        if not self.interactive:
            return default
        try:
            return Confirm.ask(prompt, default=default, console=self.console)
        except EOFError:
            self._eof()
            return default

    def choose(self, prompt: str, options: Sequence[str], default: str | None = None) -> str:
        """Pick one of ``options`` (case-insensitive match; Enter takes ``default``)."""
        if not self.interactive:
            return default if default is not None else options[0]
        try:
            return Prompt.ask(prompt, choices=list(options), default=default, console=self.console,
                              case_sensitive=False)
        except EOFError:
            self._eof()
            return default if default is not None else options[0]

    def pause(self, prompt: str = "Press Enter to continue") -> None:
        """Wait for Enter (no-op when not interactive)."""
        if not self.interactive:
            return
        try:
            Prompt.ask(prompt, default="", console=self.console, show_default=False)
        except EOFError:
            self._eof()

    def play(self, wav: Path) -> None:
        """Play a WAV file through the speakers (Windows only; silently skipped elsewhere)."""
        try:
            import winsound  # Windows only

            winsound.PlaySound(str(wav), winsound.SND_FILENAME)
        except (ImportError, RuntimeError):
            self.say(f"(cannot play audio here; sample saved at {wav})", style="dim")
