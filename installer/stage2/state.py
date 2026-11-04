"""Resume state for the wizard (component C1): config/install_state.json through ns_common.config."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ns_common.config import load_install_state, save_install_state
from ns_common.models import InstallState, StepState
from ns_common.paths import Paths


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class StateStore:
    """Reads and writes install_state.json after every change so a crash or reboot resumes at the step."""

    def __init__(self, paths: Paths, app_path: Path, clock: Callable[[], str] = _now) -> None:
        self.paths = paths
        self.app_path = app_path
        self.clock = clock
        self.state: InstallState = load_install_state(paths)

    def begin(self, profile: str | None, dev_mode: bool = False) -> None:
        """Record app path, start time, profile and dev mode; keeps the original start time on resume."""
        self.state.app_path = str(self.app_path)
        self.state.dev_mode = dev_mode
        self.state.started_at = self.state.started_at or self.clock()
        self.state.completed_at = None
        if profile == "gpu" or profile == "cpu-dev":
            self.state.profile = profile
        self.save()

    def save(self) -> None:
        """Write the file."""
        save_install_state(self.paths, self.state)

    def status(self, step_id: str) -> str:
        """pending, done, failed or skipped."""
        st = self.state.steps.get(step_id)
        return st.status if st else "pending"

    def is_done(self, step_id: str) -> bool:
        """True when the step finished earlier."""
        return self.status(step_id) == "done"

    def any_done(self) -> bool:
        """True when a previous run finished at least one step (this run is a resume)."""
        return any(
            s.status in ("done", "skipped") for step_id, s in self.state.steps.items()
            if not step_id.startswith("b0")    # the setup window's bootstrap steps are not wizard progress
        )

    def mark(self, step_id: str, status: str, error: str | None = None) -> None:
        """Set a step's status and persist."""
        finished = self.clock() if status in ("done", "skipped", "failed") else None
        self.state.steps[step_id] = StepState.model_validate(
            {"status": status, "error": error, "finished_at": finished}
        )
        self.save()

    def complete(self) -> None:
        """Stamp completed_at."""
        self.state.completed_at = self.clock()
        self.save()


def flags_path(paths: Paths) -> Path:
    """Where wizard-internal flags live (not part of the install_state contract)."""
    return paths.config / "wizard_flags.json"


def load_flags(paths: Paths) -> dict[str, Any]:
    """Read wizard flags (empty when absent or unreadable)."""
    try:
        data = json.loads(flags_path(paths).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_flags(paths: Paths, flags: dict[str, Any]) -> None:
    """Write wizard flags."""
    f = flags_path(paths)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(flags, indent=2, sort_keys=True) + "\n", encoding="utf-8")
