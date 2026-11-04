"""Phase derivation and action preconditions (docs/design/installer-ui.md 1.5 and 5.6). Pure functions."""

from __future__ import annotations

from typing import Any

from installer.stage2.web.model import STEP_ORDER, STEP_PHASE

DONE_STATUSES = ("done", "skipped", "warning")


def first_unfinished(steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The first step, in run order, whose status is neither done nor skipped."""
    for s in steps:
        if s["status"] not in DONE_STATUSES:
            return s
    return None


def derive_phase(
    steps: list[dict[str, Any]],
    *,
    ui_step: str | None,
    answers_accepted: bool,
    started: bool,
    run: str,
    failure: dict[str, Any] | None,
    answers_locked: bool = False,
) -> str:
    """The screen to show. Never stored on its own: always derived from the rest of the state.

    A setup server started over an install whose questions step is done (the window was closed, the PC
    slept or rebooted, INSTALL.cmd was run again) has ``started`` False but ``answers_locked`` True: the
    phase then follows the steps, like a running install, so the window shows the Welcome resume screen
    (Continue starts the run) instead of a locked Choices screen whose Install button re-sends answers the
    server refuses.
    """
    if failure is not None and run == "waiting":
        return "failed"
    if not started and not answers_locked:
        if answers_accepted:
            return "choices"
        return "choices" if ui_step == "choices" else "checks"
    nxt = first_unfinished(steps)
    if nxt is None:
        return "done"
    return STEP_PHASE.get(nxt["id"], "downloading")


def finalize(state: dict[str, Any], *, started: bool) -> None:
    """Recompute everything derived from the rest of the state, in place."""
    state["blocking"] = any(bool(c.get("blocking")) for c in state["checks"])
    if started:
        state["uiStep"] = None
    state["phase"] = derive_phase(
        state["steps"],
        ui_step=state.get("uiStep"),
        answers_accepted=state.get("answers") is not None,
        started=started,
        run=state["run"],
        failure=state.get("failure"),
        answers_locked=bool(state.get("answersLocked")),
    )


def check(action: str, state: dict[str, Any], path: str | None = None) -> str | None:
    """None when ``action`` is allowed now, else the reason it is not (the server answers 409 with it)."""
    run = state["run"]
    phase = state["phase"]
    if action == "start":
        if state.get("answers") is None:
            return "Choose your settings first."
        if state["blocking"]:
            return "Fix the red items on the PC check first."
        if run not in ("idle", "stopped"):
            return "Setup is already running."
    elif action == "pause":
        if not (run == "running" and phase == "downloading"):
            return "Downloads are not running."
    elif action == "resume":
        if run not in ("paused", "pausing", "stopped"):
            return "Setup is not paused."
    elif action == "retry":
        if run != "waiting":
            return "Nothing is waiting for a decision."
    elif action == "skip":
        failure = state.get("failure")
        if run != "waiting" or not failure or not failure.get("canSkip"):
            return "This step cannot be skipped."
    elif action == "cancel":
        if run not in ("running", "pausing", "paused", "waiting"):
            return "Setup is not running."
    elif action == "recheck":
        if run not in ("idle", "stopped") or state.get("checking"):
            return "Setup is busy."
    elif action == "open-dashboard":
        if phase != "done":
            return "Setup is not finished."
    elif action == "reveal":
        if not path or path not in revealable(state):
            return "That folder cannot be opened from here."
    elif action == "elevate-longpaths":
        if not any(c["id"] == "longpaths" and c["status"] == "warn" for c in state["checks"]):
            return "Long file paths are already on."
    elif action == "close-setup":
        if run not in ("idle", "stopped", "finished"):
            return "Setup is still running."
    elif action != "diagnostics":
        return f"Unknown action {action}."
    return None


def revealable(state: dict[str, Any]) -> set[str]:
    """Paths the window may ask Explorer to open."""
    out: set[str] = set()
    result = state.get("result")
    if result:
        out.update(p for p in (result.get("outbox"), result.get("home"), result.get("diagnosticsZip")) if p)
    answers = state.get("answers")
    if answers and answers.get("homePath"):
        out.add(answers["homePath"].rstrip("\\") + "\\logs")
    return out


def step_index(step_id: str) -> int:
    """Position of a step in the run order (unknown ids sort last)."""
    return STEP_ORDER.index(step_id) if step_id in STEP_ORDER else len(STEP_ORDER)
