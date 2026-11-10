"""s09_shortcuts_scheduler: desktop shortcuts and the nightly task (component C1, spec 15.9).

Shortcuts point at the launcher scripts under ``<app>/launchers``; launchers that are not in this build are
reported and skipped.

The nightly task is the owner's choice (nightly-optional part b). This step registers it only on a first
install whose answers say yes, or for the setup window's switch when Windows still has the state the window
showed; a Repair and a reinstall over an install leave Automatic runs exactly as Windows has them. The
decision is made by ``ns schedule install --setup`` (``ns_supervisor.schedule.setup_apply``: it reads the
task, under the schedule lock) and this step only passes the facts and words the answer. Its sequence, in
this order, so a run killed at any point is repeated safely:

1. Windows: ``ns schedule install --setup``. Killed after it: the rerun passes the same facts; a first
   install's yes finds the task already on at that time and runs no second script.
2. the flags, ``schedule`` first: the decision is recorded, ``first_install`` becomes False. Killed after it:
   the rerun is no first install any more and keeps what step 1 made.
3. the answers ``nightly`` and ``nightly_seen`` are removed from ``config/answers.json``: they were about
   this run and are never read again. Killed between 2 and 3: step 2 also wrote down which answer it acted on
   and the state that made (flag ``nightly_done``); the rerun finds its answer there with that state still in
   place, so it does not present the answer to Windows a second time (the window's answer would no longer
   match the state it was made against, and the last line would deny setup's own change) and says the first
   run's sentence. The flag goes after step 3; left behind by a kill there, it is dropped by the next run
   that has no answer, and it never matches an answer made against another state.

Toasts: ``ns_supervisor.notify`` sends them as AppUserModelID ``Nightshift``, which Windows ignores unless
it is registered. This step writes the registry key (DisplayName and IconUri) and, best effort, stamps the
same ID on the Desktop dashboard shortcut through IPropertyStore. If only the registry part works, toasts
still show; the flag ``aumid`` records what worked (``registry+shortcut``, ``registry`` or ``failed``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from installer.stage2.answers import forget_setup_input
from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.errors import make_error
from installer.stage2.web import nightly_view
from installer.stage2.winenv import (
    create_shortcut,
    desktop_dir,
    register_aumid,
    set_shortcut_aumid,
)
from ns_supervisor.notify import AUMID
from ns_supervisor.schedule_cfg import remember_run_time

#: The line ``ns schedule install --setup`` ends with (``ns_supervisor.cli.SETUP_PREFIX``).
SETUP_PREFIX = "setup: "

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
    # `schedule_time` is not cleared: it is the time the answers had when this step last finished, which
    # the next run needs to tell a time the answers changed from one Settings changed (D-14)
    clears_on_run = ("schedule", "schedule_mode", "aumid", "shortcuts_app")

    def check(self, ctx: StepContext) -> bool:
        """Shortcuts exist and the task decision is recorded."""
        if ctx.flag("schedule") not in ("installed", "unavailable", "failed") or ctx.flag("aumid") is None:
            return False
        # INSTALL.cmd over an older install copies the program to <home>\app: the shortcuts and the task
        # still point at the folder the zip was extracted to (which the owner may delete) until s09 re-runs.
        if ctx.flag("shortcuts_app") != str(ctx.app_path):
            return False
        ans = ctx.ensure_answers()
        if ans.nightly is not None or ans.nightly_seen is not None:
            return False                    # an answer about Automatic runs that nobody acted on yet
        asked = ans.run_time
        if ctx.flag("schedule_time", asked) != asked:
            return False                    # the answers ask for another time than the task has (D-14)
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

        ans = ctx.ensure_answers()
        run_time = ans.run_time
        if ctx.dev_mode:
            self._dev_task(ctx, run_time)
        else:
            self._automatic_runs(ctx, ans.nightly, ans.nightly_seen, run_time)
        ctx.set_flag("schedule_time", run_time)             # the time the answers name, as of this run
        ctx.set_flag("shortcuts_app", str(ctx.app_path))     # the app folder the shortcuts and the task use
        ctx.set_flag("first_install", False)                 # setup has decided: later runs keep the choice
        # last: the answers were about this run. A kill before this line repeats the step with them.
        if forget_setup_input(ctx.paths.config / "answers.json"):
            ctx.log.info("setup_input_consumed", keys=["nightly", "nightly_seen"])
        ctx.answers = ans.model_copy(update={"nightly": None, "nightly_seen": None})
        ctx.clear_flags("nightly_done")

    @staticmethod
    def _dev_task(ctx: StepContext, run_time: str) -> None:
        """docs/DEV_MODE.md: the task exists (`ns schedule show` and the uninstall find it) but it is
        disabled, so it never wakes or runs the development PC. Unchanged by the nightly question."""
        res = ctx.run_tool("ns", ["schedule", "install", "--time", run_time, "--disabled"], timeout=300)
        if res.ok:
            ctx.set_flag("schedule", "installed")
            ctx.set_flag("schedule_mode", "disabled")
            remember_run_time(ctx.paths, run_time)      # the chosen time stays a preference
            ctx.ui.say(f"Dev mode: nightly task 'Nightshift Daily' registered for {run_time} but DISABLED.")
        elif "No such command" in res.text or res.returncode == 2:
            ctx.set_flag("schedule", "unavailable")
            ctx.ui.say("The nightly task is not part of this version yet; nothing was scheduled.", "yellow")
            ctx.log.warn("schedule_unavailable", res.text[-200:])
        else:
            ctx.set_flag("schedule", "failed")
            ctx.log.warn("schedule_failed", f"exit {res.returncode}: {res.text[-300:]}")
            remember_run_time(ctx.paths, run_time)
            ctx.warn("NS-W206", res.text.strip()[-200:])

    @staticmethod
    def _setup_result(text: str) -> dict[str, Any] | None:
        """The JSON the last `setup: ` line of the command's output carries; None when there is none."""
        for line in reversed(text.splitlines()):
            if line.startswith(SETUP_PREFIX):
                try:
                    data = json.loads(line[len(SETUP_PREFIX):])
                except ValueError:
                    return None
                return data if isinstance(data, dict) and isinstance(data.get("case"), str) else None
        return None

    def _automatic_runs(
        self, ctx: StepContext, nightly: bool | None, seen: str | None, run_time: str
    ) -> None:
        """Steps 1 and 2 of the module docstring for a real install. Never raises for the task: the PC works
        without it, and every outcome ends in one true sentence (the summary and the Done screen repeat
        it)."""
        first = ctx.flag("first_install") is True
        was = ctx.flag("schedule_time")
        answer = {"nightly": nightly, "seen": seen}
        done = ctx.flag("nightly_done")
        acted = (
            nightly is not None and isinstance(done, dict) and not first
            and {k: done.get(k) for k in answer} == answer
            and done.get("state") == nightly_view.state_word(ctx.paths.home)
        )
        earlier_line = ctx.flag("nightly_line") if acted else None
        if acted:           # an earlier run of this step already acted on this answer and was killed after it
            nightly = seen = None
        argv = ["schedule", "install", "--time", run_time, "--setup", "first" if first else "again"]
        if nightly is not None:
            argv += ["--nightly", "yes" if nightly else "no"]
        if seen:
            argv += ["--seen", seen]
        if isinstance(was, str) and was:
            argv += ["--was", was]
        res = ctx.run_tool("ns", argv, timeout=300)
        out = self._setup_result(res.stdout) if res.ok else None
        if out is None and ("No such command" in res.text or res.returncode == 2):
            ctx.set_flag("schedule", "unavailable")
            line = (
                "Automatic runs: off. The nightly task is not part of this version yet; nothing was "
                "scheduled."
            )
            ctx.log.warn("schedule_unavailable", res.text[-200:])
        elif out is None:
            # Never block the install on the task: the owner can switch Automatic runs on in Settings. Only
            # what is known is said: a first install's yes was not carried out (NS-W206); in every other case
            # nothing was asked of Windows, so what it had is unchanged, whatever that is.
            wanted = first and nightly is True
            ctx.set_flag("schedule", "failed" if wanted else "installed")
            ctx.log.warn("schedule_failed", f"exit {res.returncode}: {res.text[-300:]}")
            detail = res.text.strip()[-200:] or "ns schedule install gave no answer"
            if wanted:
                remember_run_time(ctx.paths, run_time)   # a preference only: no schedule.json without a task
                ctx.warn("NS-W206", detail)
            line = nightly_view.outcome_line({"case": "unread", "wanted_on": wanted, "reason": detail})
        else:
            line = nightly_view.outcome_line(out)
            case, on = out["case"], out.get("on") is True
            if acted and case == "kept" and isinstance(earlier_line, str) and earlier_line:
                line = earlier_line         # the sentence of the run that made this state
            wanted_on = bool(out.get("wanted_on"))
            ctx.set_flag("schedule", "failed" if wanted_on and case in ("refused", "unread") else "installed")
            only = on and bool(out.get("signed_in_only"))
            mode = ("interactive" if only else "s4u") if on else "off"
            if case == "unrecorded":
                mode = "unrecorded"
            ctx.set_flag("schedule_mode", mode)
            ctx.log.info(
                "automatic_runs_setup", line, first=first, nightly=nightly, seen=seen, case=case,
                on=out.get("on"), run_time=out.get("run_time"), time_kept=out.get("time_kept"),
            )
            if case == "unrecorded":
                ctx.warn("NS-W208", f"at {out.get('run_time')}")
            elif wanted_on and case in ("refused", "unread"):
                ctx.warn("NS-W206", str(out.get("reason") or "")[:200])
            elif only and case in ("first", "applied"):
                ctx.warn("NS-W205", f"registered for {out.get('run_time')}")
        ctx.set_flag("nightly_line", line)
        ctx.set_flag("nightly_state", nightly_view.state_word(ctx.paths.home))
        ctx.set_flag("nightly_time", (out or {}).get("run_time"))
        if answer["nightly"] is not None:   # which answer was acted on, and the state that made
            ctx.set_flag("nightly_done", {**answer, "state": nightly_view.state_word(ctx.paths.home)})
        ctx.ui.say(line)

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
