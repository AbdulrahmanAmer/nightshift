"""Console output and prompts for the wizard (component C1).

Every line the installer shows goes through ``Ui``, so tests can replace it with a scripted fake and the
rest of the code never calls ``print``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
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


PLAIN_INTERVAL_S = 60.0     # without a terminal: one total line at most this often (card download-outage N5)
GIB = 1024**3
MIB = 1024**2
PLAIN_OUTCOMES = {"downloaded": "downloaded", "copied": "copied from your folder", "skipped": "already here",
                  "missing_optional": "not available, setup goes on without it", "failed": "failed"}


def _gb(n: float) -> str:
    return f"{n / GIB:.1f} GB"


def _took(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60} s"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min"


class RichReporter:
    """Per-file and overall progress bars with speed and ETA (rich). Use as a context manager.

    When the console is not a terminal (unattended setup with its output in a file) no bars are drawn: plain
    lines instead, each written whole and flushed (N5). A line when a file starts (name, size), one when it
    ends (outcome, size, time taken) and a total line at most once per ``PLAIN_INTERVAL_S``: GB of GB,
    percent, MB/s over the last interval, files done of all. Waits and retries are a line on both routes.
    """

    def __init__(
        self, console: Console, total_bytes: int = 0, *, files: int = 0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._console = console
        self._plain = not console.is_terminal
        self._lock = threading.Lock()
        self._tasks: dict[str, TaskID] = {}
        self._done: dict[str, int] = {}
        self._total_bytes = total_bytes
        # plain lines: the clock, the file count of the plan, when each file started, what ended
        self._clock = clock
        self._files = files
        self._sizes: dict[str, int | None] = {}
        self._began: dict[str, float] = {}
        self._ended: dict[str, str] = {}
        self._moved = 0                         # bytes that arrived while this reporter watched
        self._line_t = clock()
        self._line_moved = 0
        self._progress: Progress | None = None
        if not self._plain:
            self._progress = Progress(
                TextColumn("{task.description}", justify="left"),
                BarColumn(bar_width=30),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=console,
            )
            self._overall = self._progress.add_task("[bold]Overall", total=total_bytes or None)

    def __enter__(self) -> RichReporter:
        if self._progress is not None:
            self._progress.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._progress is not None:
            self._progress.stop()

    def _line(self, text: str) -> None:
        """One whole line: above the bars on a terminal; written and flushed as is into a file."""
        if self._progress is not None:
            self._progress.console.print(text, markup=False, highlight=False)
            return
        out = self._console.file
        out.write(text + "\n")
        out.flush()

    def start(self, name: str, total: int | None) -> None:
        """Add a bar for a file (the same bar when a later pass starts it again), or say that it starts."""
        with self._lock:
            again = name in self._began
            self._began[name] = self._clock()
            self._sizes[name] = total
            self._ended.pop(name, None)
            self._done.pop(name, None)          # the first report of a start is a baseline, not speed
            if self._progress is not None:
                task = self._tasks.get(name)
                if task is None:
                    self._tasks[name] = self._progress.add_task(name[-38:], total=total)
                else:
                    self._progress.update(task, description=name[-38:], total=total)
                return
            size = _gb(total) if total else "size not known yet"
            self._line(f"{name}: {'starting again' if again else 'starting'}, {size}")

    def update(self, name: str, done: int, total: int | None) -> None:
        """Advance a file bar and the overall bar; without a terminal, a total line now and then."""
        with self._lock:
            if self._progress is not None:
                task = self._tasks.get(name)
                if task is None:
                    return
                self._done[name] = done
                self._progress.update(task, completed=done, total=total)
                self._progress.update(self._overall, completed=sum(self._done.values()))
                return
            before = self._done.get(name)
            if before is not None and done > before:
                self._moved += done - before
            self._done[name] = done
            if total:
                self._sizes[name] = total
            now = self._clock()
            if now - self._line_t >= PLAIN_INTERVAL_S:
                self._total_line(now)

    def _total_line(self, now: float) -> None:
        got = sum(self._done.values())
        total = self._total_bytes
        pct = f" ({min(100, int(got * 100 / total))}%)" if total else ""
        speed = (self._moved - self._line_moved) / max(now - self._line_t, 1e-9) / MIB
        ended = sum(1 for o in self._ended.values() if o != "failed")
        files = max(self._files, len(self._began))
        of_total = f" of {_gb(total)}" if total else ""
        self._line(f"Downloaded {_gb(got)}{of_total}{pct}, {speed:.1f} MB/s, {ended} of {files} files done")
        self._line_t, self._line_moved = now, self._moved

    def finish(self, name: str, outcome: str) -> None:
        """Mark a file finished, or say how it ended (outcome, size, time taken)."""
        with self._lock:
            self._ended[name] = outcome
            if self._progress is not None:
                task = self._tasks.get(name)
                if task is not None:
                    self._progress.update(task, description=f"{name[-30:]} [{outcome}]")
                return
            size = self._sizes.get(name) or self._done.get(name)
            took = _took(self._clock() - self._began.get(name, self._clock()))
            what = PLAIN_OUTCOMES.get(outcome, outcome)
            self._line(f"{name}: {what}, {_gb(size) if size else 'size not known'}, {took}")

    def retrying(self, name: str, attempt: int, reason: str, wait_s: float) -> None:
        """A transfer waits to be tried again (reason stall, network, offline, hash or later): one line."""
        from installer.stage2.downloader import retry_text

        with self._lock:
            self._line(f"{name[-38:]}: {retry_text(reason, wait_s)}")

    def site_wait(self, name: str, host: str, status: int, wait_s: float, left_s: float) -> None:
        """A download site answered 5xx or 429: print the wait above the bars (host, status, time left)."""
        from installer.stage2.downloader import site_wait_text

        with self._lock:
            self._line(f"{name[-38:]}: {site_wait_text(host, status, wait_s, left_s)}")


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

    _plan_files = 0

    def plan(self, items: Sequence[Any]) -> None:
        """A download plan is about to run (``DownloadItem`` objects): its file count is kept for the plain
        total line ("3 of 13 files done")."""
        self._plan_files = len(items)

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
        return RichReporter(self.console, total_bytes, files=self._plan_files)

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
