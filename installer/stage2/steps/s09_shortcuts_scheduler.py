"""s09_shortcuts_scheduler: desktop shortcuts and the nightly task (component C1, spec 15.9).

Shortcuts point at the launcher scripts under ``<app>/launchers``; launchers that are not in this build are
reported and skipped. The scheduled task is registered by ``ns schedule install``.

Toasts: ``ns_supervisor.notify`` sends them as AppUserModelID ``Nightshift``, which Windows ignores unless
it is registered. This step writes the registry key (DisplayName and IconUri) and, best effort, stamps the
same ID on the Desktop dashboard shortcut through IPropertyStore. If only the registry part works, toasts
still show; the flag ``aumid`` records what worked (``registry+shortcut``, ``registry`` or ``failed``).
"""

from __future__ import annotations

import json
from pathlib import Path

from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.errors import make_error
from installer.stage2.winenv import (
    create_shortcut,
    desktop_dir,
    register_aumid,
    set_shortcut_aumid,
)
from ns_common.atomic import write_text_atomic
from ns_common.paths import Paths
from ns_supervisor.notify import AUMID
from ns_supervisor.schedule import INTERACTIVE_MARK

# The brand icon ships with the dashboard build; without it a shortcut keeps PowerShell's own icon.
ICON_RELATIVE = Path("ns_dashboard") / "static" / "brand" / "nightshift.ico"

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

#: The shortcut that carries the AppUserModelID.
AUMID_SHORTCUT = "Nightshift Dashboard"


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


def shortcut_workdir(ctx: StepContext) -> Path:
    """Working directory of every shortcut: the install home, never the app folder.

    A process whose current directory is the app folder blocks the folder swap of ``ns update`` (WinError 32).
    """
    return ctx.paths.home


def shortcut_icon(ctx: StepContext) -> Path | None:
    """The brand .ico under the app folder, or None when this build does not ship it."""
    icon = ctx.app_path / ICON_RELATIVE
    return icon if icon.is_file() else None


def remember_run_time(paths: Paths, run_time: str) -> None:
    """Keep the nightly time the owner chose in ``config\\dashboard.json`` (the dashboard's own settings).

    ``config\\schedule.json`` is the time of a *registered* task: ``ns schedule install`` writes it after
    Windows accepted the task, and the engine and the dashboard read it as "a run is coming". When Windows
    refused the task (NS-W206) the chosen time is only a preference.
    """
    target = paths.config / "dashboard.json"
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["run_time"] = run_time
    write_text_atomic(target, json.dumps(data, indent=2) + "\n")


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
    clears_on_run = ("schedule", "schedule_mode", "aumid", "shortcuts_app")

    def check(self, ctx: StepContext) -> bool:
        """Shortcuts exist and the task decision is recorded."""
        if ctx.flag("schedule") not in ("installed", "unavailable", "failed") or ctx.flag("aumid") is None:
            return False
        # INSTALL.cmd over an older install copies the program to <home>\app: the shortcuts and the task
        # still point at the folder the zip was extracted to (which the owner may delete) until s09 re-runs.
        if ctx.flag("shortcuts_app") != str(ctx.app_path):
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
        icon = shortcut_icon(ctx)
        for name, launcher in shortcuts:
            args = f'-NoProfile -ExecutionPolicy Bypass -File "{launcher}"'
            ok = create_shortcut(
                ctx.runner, folder / f"{name}.lnk", "powershell.exe", args, shortcut_workdir(ctx),
                f"Nightshift: {name}", icon,
            )
            if not ok:
                raise make_error("NS-E110", f"could not create the shortcut '{name}'")
            ctx.ui.say(f"  shortcut: {name}")
        self._register_toast_identity(ctx, folder, icon)

        run_time = ctx.ensure_answers().run_time
        argv = ["schedule", "install", "--time", run_time, *(["--disabled"] if ctx.dev_mode else [])]
        res = ctx.run_tool("ns", argv, timeout=300)
        if res.ok and ctx.dev_mode:
            # docs/DEV_MODE.md: the task exists (`ns schedule show` and the uninstall find it) but it is
            # disabled, so it never wakes or runs the development PC
            ctx.set_flag("schedule", "installed")
            ctx.set_flag("schedule_mode", "disabled")
            remember_run_time(ctx.paths, run_time)      # the chosen time stays a preference
            ctx.ui.say(f"Dev mode: nightly task 'Nightshift Daily' registered for {run_time} but DISABLED.")
        elif res.ok:
            ctx.set_flag("schedule", "installed")
            ctx.ui.say(f"Nightly task 'Nightshift Daily' registered for {run_time} (wakes the PC).")
            if INTERACTIVE_MARK in res.text:      # "...only while you are logged on"
                ctx.set_flag("schedule_mode", "interactive")
                ctx.warn("NS-W205", f"registered for {run_time}")
            else:
                ctx.set_flag("schedule_mode", "s4u")
        elif "No such command" in res.text or res.returncode == 2:
            ctx.set_flag("schedule", "unavailable")
            ctx.ui.say("The nightly task is not part of this version yet; nothing was scheduled.", "yellow")
            ctx.log.warn("schedule_unavailable", res.text[-200:])
        else:
            # Never block the install on the task: the PC works without it, the owner can run it by hand.
            ctx.set_flag("schedule", "failed")
            ctx.log.warn("schedule_failed", f"exit {res.returncode}: {res.text[-300:]}")
            remember_run_time(ctx.paths, run_time)       # a preference only: no schedule.json without a task
            ctx.warn("NS-W206", res.text.strip()[-200:])
        ctx.set_flag("shortcuts_app", str(ctx.app_path))     # the app folder the shortcuts and the task use

    @staticmethod
    def _register_toast_identity(ctx: StepContext, folder: Path, icon: Path | None) -> None:
        """Registry key first (what makes toasts show), then the property on the dashboard shortcut."""
        if not register_aumid(ctx.runner, AUMID, "Nightshift", icon):
            ctx.set_flag("aumid", "failed")
            ctx.log.warn("aumid_registry_failed", AUMID)
            ctx.ui.say("  Windows notifications could not be registered; Nightshift works without.", "yellow")
            return
        lnk = folder / f"{AUMID_SHORTCUT}.lnk"
        if lnk.exists() and set_shortcut_aumid(ctx.runner, lnk, AUMID):
            ctx.set_flag("aumid", "registry+shortcut")
        else:
            ctx.set_flag("aumid", "registry")
            ctx.log.warn("aumid_shortcut_failed", str(lnk))
        ctx.ui.say("  notifications: registered")

    def verify(self, ctx: StepContext) -> None:
        """Every expected shortcut file exists and the task decision is recorded."""
        folder = desktop(ctx)
        for name, _ in wanted_shortcuts(ctx):
            if not (folder / f"{name}.lnk").exists():
                raise make_error("NS-E110", f"shortcut '{name}' was not created")
        if ctx.flag("schedule") not in ("installed", "unavailable", "failed"):
            raise make_error("NS-E204", "the nightly task decision was not recorded")
