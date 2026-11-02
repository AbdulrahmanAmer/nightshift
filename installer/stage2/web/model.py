"""Names and shapes shared by the setup server (docs/design/installer-ui.md 5.6). Standard library only.

The HTTP JSON uses camelCase keys, exactly as ``installer/ui/src/api/types.ts``. States are plain dicts; the
TypedDicts here document the parts the Python code builds.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

SCHEMA = 1

# Run order of every step the window shows. b01 to b03 run in the server (bootstrap.py), the rest in the wizard.
STEP_ORDER: tuple[str, ...] = (
    "b01_tools", "b02_home", "b03_setup_env",
    "s01_questions", "s02_download_bins", "s03_download_models",
    "s04_venvs", "s05_opencode_prewarm", "s06_tune", "s07_configs",
    "s08_smoke", "v01_samples", "s09_shortcuts_scheduler", "s10_summary",
)
BOOTSTRAP_STEPS: tuple[str, ...] = STEP_ORDER[:3]
ENGINE_STEPS: tuple[str, ...] = STEP_ORDER[3:]

# The screen each step belongs to (the first unfinished step decides the phase).
STEP_PHASE: dict[str, str] = {
    "b01_tools": "downloading", "b02_home": "downloading", "b03_setup_env": "downloading",
    "s01_questions": "downloading", "s02_download_bins": "downloading", "s03_download_models": "downloading",
    "s04_venvs": "installing", "s05_opencode_prewarm": "installing", "s06_tune": "installing",
    "s07_configs": "installing",
    "s08_smoke": "verifying", "v01_samples": "verifying", "s09_shortcuts_scheduler": "verifying",
    "s10_summary": "verifying",
}

PHASES = ("checks", "choices", "downloading", "installing", "verifying", "done", "failed")
RUN_STATES = ("idle", "running", "pausing", "paused", "waiting", "stopping", "stopped", "finished")
TIERS = ("small", "balanced", "best")
PRESET_VOICES = ("Ryan", "Aiden", "Serena", "Vivian")
UI_STEPS = ("welcome", "checks", "choices")
ACTIONS = (
    "start", "pause", "resume", "retry", "skip", "cancel", "recheck", "open-dashboard",
    "diagnostics", "reveal", "elevate-longpaths", "close-setup",
)
FILE_NAMES = ("voice-sample.wav", "render-test.mp4", "render-test.jpg", "verify-report.json")

Phase = Literal["checks", "choices", "downloading", "installing", "verifying", "done", "failed"]


class FieldError(TypedDict):
    """One problem with one field of the Choices form."""

    field: str
    message: str
    code: str | None


class LogLine(TypedDict):
    """One line of the log shown in the window."""

    t: str
    level: Literal["info", "warn", "error"]
    text: str


class FailureView(TypedDict):
    """What the Failed screen shows."""

    step: str
    stepTitle: str
    code: str
    title: str
    whatToDo: str
    detail: str
    canSkip: bool
    attempts: int
    at: str


class Notice(TypedDict):
    """A warning (NS-W code) raised so far."""

    code: str
    title: str
    whatToDo: str
    step: str | None


JsonDict = dict[str, Any]
