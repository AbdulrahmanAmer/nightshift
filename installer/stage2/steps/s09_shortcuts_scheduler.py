"""s09_shortcuts_scheduler: desktop shortcuts and the nightly task (component C1, spec 15.9).

Shortcuts point at the launcher scripts under ``<app>/launchers``; launchers that are not in this build are
reported and skipped. The scheduled task is registered by ``ns schedule install``.
"""

from __future__ import annotations

from pathlib import Path

from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.errors import make_error
from installer.stage2.winenv import create_shortcut, desktop_dir

# (shortcut name, launcher script, only with Nightshift Code)
SHORTCUTS = (
    ("Nightshift Dashboard", "nightshift-dashboard.ps1", False),
    ("Nightshift Studio", "nightshift-studio.ps1", False),
    ("Nightshift Outbox", "open-outbox.ps1", False),
    ("Nightshift - Stop All", "stop-all.ps1", False),
    ("Nightshift - Update", "update.ps1", False),
    ("Nightshift - Diagnostics", "diagnostics.ps1", False),
    ("Nightshift Code", "nightshift-code.ps1", True),
)


def wanted_shortcuts(ctx: StepContext) -> list[tuple[str, Path]]:
    """(name, launcher path) for the launchers that exist in this build."""
    code = ctx.ensure_answers().install_code
    out: list[tuple[str, Path]] = []
    for name, script, needs_code in SHORTCUTS:
        if needs_code and not code:
            continue
        path = ctx.app_path / "launchers" / script
        if path.is_file():
            out.append((name, path))
    return out


def desktop(ctx: StepContext) -> Path:
    """Desktop folder of the current user."""
    found = ctx.desktop_override or desktop_dir(ctx.runner)
    if found is None:
        raise make_error("NS-E110", "could not find the Desktop folder")
    return found


class S09ShortcutsScheduler(Step):
    """Create the shortcuts and register the nightly task."""

    id = "s09_shortcuts_scheduler"
    title = "Shortcuts and nightly task"

    def check(self, ctx: StepContext) -> bool:
        """Shortcuts exist and the task decision is recorded."""
        if ctx.flag("schedule") not in ("installed", "unavailable"):
            return False
        folder = desktop(ctx)
        return all((folder / f"{name}.lnk").exists() for name, _ in wanted_shortcuts(ctx))

    def run(self, ctx: StepContext) -> None:
        """Make the .lnk files, then call ``ns schedule install``."""
        folder = desktop(ctx)
        shortcuts = wanted_shortcuts(ctx)
        known = {script for _, script, _ in SHORTCUTS}
        have = {p.name for _, p in shortcuts}
        for script in sorted(known - have):
            ctx.ui.say(f"  launcher {script} is not part of this version yet; no shortcut for it.", "dim")
        for name, launcher in shortcuts:
            args = f'-NoProfile -ExecutionPolicy Bypass -File "{launcher}"'
            ok = create_shortcut(
                ctx.runner, folder / f"{name}.lnk", "powershell.exe", args, ctx.app_path,
                f"Nightshift: {name}",
            )
            if not ok:
                raise make_error("NS-E110", f"could not create the shortcut '{name}'")
            ctx.ui.say(f"  shortcut: {name}")

        run_time = ctx.ensure_answers().run_time
        res = ctx.run_tool("ns", ["schedule", "install", "--time", run_time], timeout=300)
        if res.ok:
            ctx.set_flag("schedule", "installed")
            ctx.ui.say(f"Nightly task 'Nightshift Daily' registered for {run_time} (wakes the PC).")
        elif "No such command" in res.text or res.returncode == 2:
            ctx.set_flag("schedule", "unavailable")
            ctx.ui.say("The nightly task is not part of this version yet; nothing was scheduled.", "yellow")
            ctx.log.warn("schedule_unavailable", res.text[-200:])
        else:
            detail = f"ns schedule install failed (exit {res.returncode}): {res.text[-200:]}"
            raise make_error("NS-E202", detail)

    def verify(self, ctx: StepContext) -> None:
        """Every expected shortcut file exists and the task decision is recorded."""
        folder = desktop(ctx)
        for name, _ in wanted_shortcuts(ctx):
            if not (folder / f"{name}.lnk").exists():
                raise make_error("NS-E110", f"shortcut '{name}' was not created")
        if ctx.flag("schedule") not in ("installed", "unavailable"):
            raise make_error("NS-E202", "the nightly task was not registered")
