"""Shared context passed to every wizard step (component C1)."""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from installer.stage2.answers import InstallerAnswers, build_answers, find_answers, load_raw
from installer.stage2.downloader import DownloadItem, PlanResult, pending, run_plan, total_bytes
from installer.stage2.errors import describe_code
from installer.stage2.runner import Runner, RunResult
from installer.stage2.secrets import SecretStore
from installer.stage2.state import StateStore, load_flags, save_flags
from installer.stage2.sysinfo import free_gb
from installer.stage2.ui import Ui
from installer.stage2.winenv import HOME_VAR
from ns_common.logging import NsLogger
from ns_common.models import Manifest
from ns_common.paths import Paths


def default_http() -> httpx.Client:
    """HTTP client used for downloads, HEAD checks and key validation."""
    return httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0))


@dataclass
class StepContext:
    """Everything a step may touch. Collaborators are injectable so CI can run every step."""

    app_path: Path
    paths: Paths
    ui: Ui
    runner: Runner
    store: SecretStore
    manifest: Manifest
    log: NsLogger
    state: StateStore
    simulate: bool = False
    unattended: bool = False
    answers_path: Path | None = None
    raw_answers: dict[str, Any] = field(default_factory=dict)
    answers: InstallerAnswers | None = None
    preflight: dict[str, Any] | None = None
    home_forced: bool = False
    http_factory: Callable[[], httpx.Client] = default_http
    sleep: Callable[[float], None] = time.sleep
    head_checks: bool = True
    resumed: bool = False
    free_gb_fn: Callable[[Path], float] = free_gb
    desktop_override: Path | None = None
    warnings: list[tuple[str, str]] = field(default_factory=list)

    # ---- messages ----
    def warn(self, code: str, detail: str = "") -> None:
        """Show and record a warning (NS-W code); warnings never stop the install."""
        info = describe_code(code)
        text = f"{code}: {info.message}" + (f" ({detail})" if detail else "")
        self.ui.say(f"  ! {text}", style="yellow")
        if info.hint:
            self.ui.say(f"    {info.hint}", style="dim")
        self.log.warn("warning", text, code=code)
        self.warnings.append((code, detail))

    # ---- flags (wizard-internal, persisted in config/wizard_flags.json) ----
    def flag(self, name: str, default: Any = None) -> Any:
        """Read a persisted flag."""
        return load_flags(self.paths).get(name, default)

    def set_flag(self, name: str, value: Any) -> None:
        """Persist a flag."""
        flags = load_flags(self.paths)
        flags[name] = value
        save_flags(self.paths, flags)

    # ---- answers ----
    def ensure_answers(self) -> InstallerAnswers:
        """Return the resolved answers, loading config/answers.json when s01 did not run in this session."""
        if self.answers is None:
            found = find_answers(self.app_path, self.paths, self.answers_path)
            candidate = self.paths.config / "answers.json"
            src = candidate if candidate.is_file() else found
            if src is None:
                raise RuntimeError("answers are not available yet; step s01_questions has not completed")
            self.answers = build_answers(load_raw(src))
        return self.answers

    @property
    def profile(self) -> str:
        """gpu or cpu-dev."""
        if self.preflight and self.preflight.get("profile") in ("gpu", "cpu-dev"):
            return str(self.preflight["profile"])
        return self.state.state.profile or "gpu"

    @property
    def tier(self) -> str:
        """LLM tier from the answers (small, balanced, best)."""
        return self.ensure_answers().llm_tier

    # ---- locations of tools ----
    def venv_python(self, name: str) -> Path:
        """Python of the named venv under NIGHTSHIFT_HOME\\venvs."""
        return self.paths.venvs / name / "Scripts" / "python.exe"

    def uv(self) -> str:
        """uv executable: the one stage 1 put under bin\\uv, else PATH, else plain ``uv``."""
        local = self.paths.home / "bin" / "uv" / "uv.exe"
        if local.exists():
            return str(local)
        return shutil.which("uv") or "uv"

    def tool_argv(self, tool: str, args: list[str]) -> list[str]:
        """Command line for the ``ns`` or ``studio`` console script of the studio venv."""
        exe = self.paths.venvs / "studio" / "Scripts" / f"{tool}.exe"
        if exe.exists():
            return [str(exe), *args]
        module = {"ns": "ns_supervisor.cli", "studio": "ns_studio.cli"}[tool]
        return [str(self.venv_python("studio")), "-m", module, *args]

    def run_tool(
        self, tool: str, args: list[str], *, timeout: float | None = None, capture: bool = True
    ) -> RunResult:
        """Run ``ns`` / ``studio`` from the studio venv with the app folder as working directory."""
        env = {"PYTHONUTF8": "1", HOME_VAR: str(self.paths.home)}
        return self.runner.run(
            self.tool_argv(tool, args), cwd=self.app_path, env=env, timeout=timeout, capture=capture,
            tag=f"{tool} {' '.join(args)}",
        )

    def run_uv(
        self, args: list[str], *, tag: str, meta: dict[str, str] | None = None,
        timeout: float | None = 3600, capture: bool = False,
    ) -> RunResult:
        """Run uv (output goes to the terminal so the operator sees progress)."""
        env = {"UV_LINK_MODE": "copy"}
        return self.runner.run(
            [self.uv(), *args], cwd=self.app_path, env=env, timeout=timeout, capture=capture, tag=tag,
            meta=meta,
        )

    # ---- downloads ----
    def download(self, items: list[DownloadItem], *, models_source: Path | str | None = None) -> PlanResult:
        """Run a download plan with progress bars (simulated in --simulate-downloads mode)."""
        from installer.stage2.simulate import simulated_fetch

        fetch = None
        client = self.http_factory()
        if self.simulate:
            fetch = simulated_fetch(items, head_checks=self.head_checks, log=self.log)
        try:
            with self.ui.reporter(total_bytes(pending(items))) as rep:
                return run_plan(
                    items, client=client, models_source=models_source, reporter=rep, sleep=self.sleep,
                    fetch=fetch,
                )
        finally:
            client.close()

    # ---- home folder ----
    def relocate(self, home: Path) -> None:
        """Switch to another install folder (only before anything was installed), keeping step state."""
        if Path(home) == self.paths.home:
            return
        old_state = self.state.state
        self.paths = Paths(Path(home))
        self.paths.ensure()
        self.state = StateStore(self.paths, self.app_path)
        self.state.state = old_state
        self.state.save()
        self.log = NsLogger("installer", self.paths.logs, echo=False)
