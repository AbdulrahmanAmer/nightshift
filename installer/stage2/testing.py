"""Test doubles for the installer (component C1): a scripted UI and a ready-made step context.

Lives in the package (not under tests/) so ``--simulate-downloads`` runs and the C10 gate can reuse it.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console

from installer.stage2.answers import build_answers
from installer.stage2.context import StepContext
from installer.stage2.runner import Runner
from installer.stage2.secrets import MemoryStore, SecretStore
from installer.stage2.simulate import FakeRunner
from installer.stage2.state import StateStore
from installer.stage2.sysinfo import sample_preflight
from installer.stage2.ui import Ui
from ns_common.config import load_manifest
from ns_common.logging import NsLogger
from ns_common.models import Manifest
from ns_common.paths import Paths, repo_root


class NullReporter:
    """Progress reporter that shows nothing (context manager + Reporter protocol)."""

    def __enter__(self) -> NullReporter:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def start(self, name: str, total: int | None) -> None:
        """Ignore."""

    def update(self, name: str, done: int, total: int | None) -> None:
        """Ignore."""

    def finish(self, name: str, outcome: str) -> None:
        """Ignore."""


class FakeUi(Ui):
    """UI that records output and answers prompts from a script.

    ``script`` maps a substring of the prompt text to an answer (a string, or a list consumed in order).
    A prompt with no scripted answer returns its default; with no default the test fails loudly.
    """

    def __init__(self, script: dict[str, str | list[str]] | None = None, *, interactive: bool = True) -> None:
        super().__init__(Console(file=io.StringIO(), width=120), interactive=interactive)
        self.script = script or {}
        self.said: list[str] = []
        self.tables: list[tuple[str, list[list[str]]]] = []
        self.asked: list[str] = []
        self.played: list[Path] = []
        self.markdowns: list[str] = []

    @property
    def text(self) -> str:
        """Everything said so far."""
        return "\n".join(self.said)

    def say(self, msg: str = "", style: str | None = None) -> None:
        """Record a line."""
        self.said.append(msg)

    def rule(self, title: str) -> None:
        """Record a title."""
        self.said.append(f"== {title}")

    def markdown(self, text: str) -> None:
        """Record markdown text."""
        self.markdowns.append(text)

    def panel(self, body: str, title: str = "", style: str = "red") -> None:
        """Record a boxed message."""
        self.said.append(f"[{title}] {body}")

    def table(self, title: str, columns: Sequence[str], rows: Sequence[Sequence[str]],
              status_col: int | None = None) -> None:
        """Record a table."""
        self.tables.append((title, [list(r) for r in rows]))

    def reporter(self, total_bytes: int = 0) -> Any:  # type: ignore[override]
        """No progress bars."""
        return NullReporter()

    def _scripted(self, prompt: str) -> str | None:
        self.asked.append(prompt)
        for key, val in self.script.items():
            if key in prompt:
                if isinstance(val, list):
                    return val.pop(0) if val else None
                return val
        return None

    def ask(self, prompt: str, default: str | None = None, *, secret: bool = False) -> str:
        """Scripted answer, else the default."""
        got = self._scripted(prompt)
        if got is not None:
            return got
        if default is not None:
            return default
        raise AssertionError(f"unscripted prompt: {prompt}")

    def confirm(self, prompt: str, default: bool = True) -> bool:
        """Scripted yes/no ('y'/'n'), else the default."""
        got = self._scripted(prompt)
        return default if got is None else got.lower().startswith("y")

    def choose(self, prompt: str, options: Sequence[str], default: str | None = None) -> str:
        """Scripted choice, else the default."""
        got = self._scripted(prompt)
        if got is not None:
            return got
        if default is not None:
            return default
        raise AssertionError(f"unscripted choice: {prompt}")

    def pause(self, prompt: str = "Press Enter to continue") -> None:
        """Record only."""
        self.asked.append(prompt)

    def play(self, wav: Path) -> None:
        """Record instead of playing."""
        self.played.append(wav)


def not_found_http() -> httpx.Client:
    """HTTP client whose every answer is 404 (no test touches the internet)."""
    return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404)))


def make_test_context(
    tmp_path: Path,
    *,
    raw_answers: dict[str, Any] | None = None,
    ui: FakeUi | None = None,
    runner: Runner | None = None,
    store: SecretStore | None = None,
    manifest: Manifest | None = None,
    app_path: Path | None = None,
    preflight: dict[str, Any] | None | str = "sample",
    http: Callable[[], httpx.Client] | None = None,
    simulate: bool = True,
    unattended: bool = True,
    home_forced: bool = True,
    resolve: bool = False,
) -> StepContext:
    """Build a StepContext rooted in ``tmp_path`` with fakes for everything that touches the machine."""
    home = tmp_path / "home"
    paths = Paths(home)
    paths.ensure()
    app = app_path or repo_root()
    pf = sample_preflight(home, app) if preflight == "sample" else preflight
    ctx = StepContext(
        app_path=app,
        paths=paths,
        ui=ui or FakeUi(),
        runner=runner or FakeRunner(tmp_path / "desktop"),
        store=store or MemoryStore(),
        manifest=manifest or load_manifest(),
        log=NsLogger("installer", paths.logs, echo=False),
        state=StateStore(paths, app),
        simulate=simulate,
        unattended=unattended,
        raw_answers=dict(raw_answers or {}),
        preflight=pf if isinstance(pf, dict) else None,
        home_forced=home_forced,
        sleep=lambda _s: None,
        head_checks=False,
        free_gb_fn=lambda _p: 500.0,
        http_factory=http or not_found_http,
    )
    if resolve and raw_answers and "channel" in raw_answers:
        ctx.answers = build_answers(dict(raw_answers))
    return ctx


def example_answers(**over: Any) -> dict[str, Any]:
    """A complete, valid answers dict (no secrets) for tests."""
    data: dict[str, Any] = {
        "llm_tier": "small",
        "channel": {"display_name": "After Dark Tales", "niche": "horror fiction", "language": "en",
                    "seed_keywords": ["haunted house"]},
        "voice": {"mode": "preset", "speaker": "Ryan"},
        "masters_per_week": 1,
        "run_time": "03:00",
        "install_code": True,
        "allow_cpu": False,
    }
    data.update(over)
    return data
