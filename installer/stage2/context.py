"""Shared context passed to every wizard step (component C1)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from installer.stage2.answers import InstallerAnswers, build_answers, find_answers, load_raw
from installer.stage2.downloader import DownloadItem, PlanResult, pending, run_plan, total_bytes
from installer.stage2.errors import describe_code, tail_lines
from installer.stage2.runner import Runner, RunResult
from installer.stage2.secrets import SecretStore
from installer.stage2.state import (
    INSTALL_KEY,
    StateStore,
    is_stale,
    load_flags,
    reconcile_flag_file,
    save_flags,
)
from installer.stage2.sysinfo import free_gb
from installer.stage2.ui import Ui
from installer.stage2.winenv import HOME_VAR
from ns_common.logging import NsLogger
from ns_common.models import LlmCustom, Manifest
from ns_common.paths import Paths
from ns_common.uv import find_uv, uv_env


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
    # `INSTALL.cmd --dev` (docs/DEV_MODE.md): the real steps with stub models, fake engines, no GPU packages
    dev_mode: bool = False
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
    #: The PC's list of Nightshift folders (`shortcut_owner.LIST_NAME`); None is the one under LOCALAPPDATA.
    installs_list: Path | None = None
    warnings: list[tuple[str, str]] = field(default_factory=list)
    # sentences a step wants on its own row and in the install report (it finished, but the owner must know
    # something): the wizard takes them when the step ends (``note_step`` / ``take_notes``)
    notes: list[str] = field(default_factory=list)
    # setup window (bridge mode): the pause gate (set = running) and the stop request
    gate: threading.Event | None = None
    stop_requested: threading.Event = field(default_factory=threading.Event)
    # the ``files`` folder beside INSTALL.cmd (the bundle setup was started from), None when there is none:
    # ``download`` looks there for every pinned file before the internet (D-17)
    files_dir: Path | None = None

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
        self.ui.notice(code, detail)

    def note_step(self, text: str) -> None:
        """Keep a sentence for the running step's row: it shows there and in install-report.json."""
        self.notes.append(text)

    def take_notes(self) -> list[str]:
        """The sentences kept since the last call (the wizard asks when a step ends)."""
        taken, self.notes = self.notes, []
        return taken

    # ---- flags (wizard-internal, persisted in config/wizard_flags.json) ----
    @property
    def install_id(self) -> str | None:
        """Names this install: when its setup started (``install_state.started_at``); None before ``begin``.

        The uninstaller removes install_state.json, so a later setup gets a new id and the flags of the
        install that was removed no longer apply to it (see ``state.reconcile_flag_file``).
        """
        return self.state.state.started_at

    def flag(self, name: str, default: Any = None) -> Any:
        """Read a persisted flag; a flag written by another install reads as unset."""
        flags = load_flags(self.paths)
        if is_stale(flags, self.install_id):
            return default
        return flags.get(name, default)

    def set_flag(self, name: str, value: Any) -> None:
        """Persist a flag, stamped with the install it describes."""
        flags = load_flags(self.paths)
        if is_stale(flags, self.install_id):
            flags = {}
        flags[name] = value
        if self.install_id is not None:
            flags[INSTALL_KEY] = self.install_id
        save_flags(self.paths, flags)

    def clear_flags(self, *names: str) -> list[str]:
        """Forget flags (a step does this before it starts work that makes them untrue). Returns the names
        that were set."""
        flags = load_flags(self.paths)
        if is_stale(flags, self.install_id):
            flags = {}
        gone = [n for n in names if n in flags]
        for n in gone:
            del flags[n]
        if gone:
            save_flags(self.paths, flags)
        return gone

    def reconcile_flags(self) -> list[str]:
        """At the start of a run: drop the flags of an install that is not this one. Returns their names."""
        if self.install_id is None:
            return []
        return reconcile_flag_file(self.paths, self.install_id, resumed=self.resumed)

    # ---- answers ----
    def ensure_answers(self) -> InstallerAnswers:
        """Return the resolved answers, loading config/answers.json when s01 did not run in this session."""
        if self.answers is None:
            found = find_answers(self.app_path, self.paths, self.answers_path)
            candidate = self.paths.config / "answers.json"
            src = candidate if candidate.is_file() else found
            if src is None:
                raise RuntimeError("answers are not available yet; step s01_questions has not completed")
            self.answers = self.constrain(build_answers(load_raw(src), saved=True))
        return self.answers

    def constrain(self, ans: InstallerAnswers) -> InstallerAnswers:
        """The answers a dev install really uses: the Small model only, no Nightshift Code (OpenCode is not
        downloaded), no voice design model, no models folder. A real install gets ``ans`` back unchanged."""
        if not self.dev_mode:
            return ans
        return ans.model_copy(update={
            "install_code": False, "include_voicedesign": False, "llm_tier": "small", "llm_custom": None,
            "models_source": None,
        })

    @property
    def profile(self) -> str:
        """gpu or cpu-dev (a dev install is always cpu-dev)."""
        if self.dev_mode:
            return "cpu-dev"
        if self.preflight and self.preflight.get("profile") in ("gpu", "cpu-dev"):
            return str(self.preflight["profile"])
        return self.state.state.profile or "gpu"

    @property
    def fake_voice(self) -> bool:
        """Engine test (docs/DEV_MODE.md): a real install whose voice engine is the fake. A dev install fakes
        the voice anyway and its consumers read ``dev_mode``, so the answer counts for a real install only."""
        return not self.dev_mode and self.ensure_answers().fake_voice

    @property
    def fake_footage(self) -> bool:
        """Engine test (docs/DEV_MODE.md): a real install whose clips come from the fake source. A dev install
        already fakes its footage through ``dev_mode``, so the answer counts for a real install only."""
        return not self.dev_mode and self.ensure_answers().fake_footage

    @property
    def tiny_story(self) -> bool:
        """Engine test (docs/DEV_MODE.md): a real install whose text stages use a tiny model's thresholds.
        A dev install runs a fake language model on fixtures, so the answer counts for a real install only."""
        return not self.dev_mode and self.ensure_answers().tiny_story

    @property
    def tier(self) -> str:
        """LLM tier from the answers (small, balanced, best)."""
        return self.ensure_answers().llm_tier

    @property
    def llm_custom(self) -> LlmCustom | None:
        """The story model the owner named (a Hugging Face GGUF with no pinned hash), or None."""
        return self.ensure_answers().llm_custom

    # ---- locations of tools ----
    def venv_python(self, name: str) -> Path:
        """Python of the named venv under NIGHTSHIFT_HOME\\venvs."""
        return self.paths.venvs / name / "Scripts" / "python.exe"

    def uv(self) -> str:
        """uv executable: the one stage 1 put under bin\\uv, else PATH, else plain ``uv``."""
        return find_uv(self.paths) or "uv"

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
        timeout: float | None = 3600, capture: bool = True,
    ) -> RunResult:
        """Run uv with its cache on the install drive (``UV_CACHE_DIR``) and its output captured.

        The output goes to ``logs`` and a failure carries its last lines (``errors.uv_error``), so the Failed
        screen shows why uv stopped instead of "site unreachable".
        """
        res = self.runner.run(
            [self.uv(), *args], cwd=self.app_path, env=uv_env(self.paths.home), timeout=timeout,
            capture=capture, tag=tag, meta=meta,
        )
        if capture and res.text:
            self.log.info("uv", f"{tag}: exit {res.returncode}", output=tail_lines(res.text, 60))
        return res

    # ---- downloads ----
    def download(
        self, items: list[DownloadItem], *, models_source: Path | str | None = None, programs: bool = False,
    ) -> PlanResult:
        """Run a download plan with progress bars (simulated in --simulate-downloads mode).

        A pinned file is taken from the ``files`` folder beside INSTALL.cmd first, then from
        ``models_source``, then from its URL (``run_plan``); a simulated run reads no folder. ``programs``:
        the plan is pinned programs, so a site that cannot be reached also names the files folder.
        """
        from installer.stage2.simulate import simulated_fetch

        fetch = None
        self.ui.plan(items)
        client = self.http_factory()
        if self.simulate:
            fetch = simulated_fetch(items, head_checks=self.head_checks, log=self.log)
        if any(i.stub for i in items):          # a dev install: the models are written, not downloaded
            from installer.stage2.dev_stubs import stub_fetch

            fetch = stub_fetch(items, fetch)
        try:
            with self.ui.reporter(total_bytes(pending(items))) as rep:
                return run_plan(
                    items, client=client, models_source=models_source, reporter=rep, sleep=self.sleep,
                    fetch=fetch, gate=self.gate, log=self.log, say=self.ui.say, kit_hint=programs,
                    sources=[self.files_dir] if self.files_dir is not None and not self.simulate else [],
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
