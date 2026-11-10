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

Each shortcut starts the launcher through ``<home>/nightshift-launch.ps1`` (a copy of
``installer/launch.ps1``, card 206-carried): that file is beside the program folder, not in it, so when an
interrupted update or rollback has left no ``<home>/app`` the shortcut still opens a window that says so and
says to run INSTALL.cmd from the zip, instead of flashing and closing. A build without that file keeps the
direct shortcut. An install made before this keeps its direct shortcuts until setup runs again (``check``
asks for a re-run while the copy is missing).

A PC has one Desktop, one 'Nightshift Daily' task, one toast registration and one NIGHTSHIFT_HOME, and
may have several Nightshift folders (card second-install-takeover). The invariant of this step: it changes
each of them only when it is this install's own, belongs to no folder that exists (deleted, absent), or
the owner said yes in this run (the answer ``shortcuts_here``); and it says what it found. Its sequence for
the Desktop, in this order, so a run killed at any point is repeated safely:

0. this install's full set of shortcuts is written into ``<home>/Shortcuts`` (always: "kept" never leaves
   an install without a way to start), and with a yes over another install's Desktop the folder they opened
   is noted in the flag ``shortcuts_taking``;
1. the Desktop: the canonical shortcuts are read (``shortcuts_view.look``). Another install's shortcut
   among them and no yes: the Desktop is left as it is, whole (nothing of this install is mixed in beside
   it; only a shortcut that already is this install's is refreshed). Otherwise every one is written, except
   a file under such a name that Nightshift did not make. Killed after it: the rerun finds its own
   shortcuts and, from the flag of step 0, still says where they pointed before;
2. the flags ``shortcuts_owner`` (``mine`` or ``other:<folder>``), ``shortcuts_line`` (the sentence the
   summary and the Done screen repeat), ``home_var``, ``shortcuts_app``. Killed after it: ``check`` is true
   apart from the answer, and the rerun writes the same shortcuts again;
3. the answer ``shortcuts_here`` is removed from ``config/answers.json`` with the other setup input, and
   the flags of step 0 go. A later Repair never repeats a yes.

A dev install never takes another install's nightly task, with or without a yes (its task is always
disabled: taking it would switch the other install's nightly run off): ``ns schedule install --disabled``
reads the task first and leaves it (``schedule.DEV_LEFT``).

Toasts: ``ns_supervisor.notify`` sends them as AppUserModelID ``Nightshift``, which Windows ignores unless
it is registered. This step writes the registry key (DisplayName and IconUri) and, best effort, stamps the
same ID on the Desktop dashboard shortcut through IPropertyStore. If only the registry part works, toasts
still show; the flag ``aumid`` records what worked (``registry+shortcut``, ``registry`` or ``failed``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import ns_common.testguard as testguard
from installer.stage2 import shortcut_owner
from installer.stage2.answers import forget_setup_input
from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.errors import make_error
from installer.stage2.web import nightly_view, shortcuts_view
from installer.stage2.winenv import (
    HOME_VAR,
    create_shortcut,
    desktop_dir,
    eval_ps_text,
    ps_quote,
    register_aumid_keeping,
    set_shortcut_aumid,
    set_user_env,
)
from ns_supervisor import schedule
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

#: The script every shortcut starts (in the program folder), and the name of its copy in the install home.
STUB_SOURCE = shortcut_owner.STUB_SOURCE
STUB_NAME = shortcut_owner.STUB_NAME

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


def launch_stub(ctx: StepContext) -> Path | None:
    """Copy ``installer/launch.ps1`` to ``<home>/nightshift-launch.ps1`` and return the copy; None when this
    build has no such file or it cannot be copied (the shortcuts then start the launchers directly). A
    copy of a higher format that a newer version left in the home is kept (`shortcut_owner.install_stub`)."""
    if not (ctx.app_path / STUB_SOURCE).is_file():
        return None
    try:
        return shortcut_owner.install_stub(ctx.app_path, ctx.paths.home)
    except OSError as e:
        ctx.ui.say(f"  could not copy {STUB_NAME} to {ctx.paths.home} ({e}); shortcuts start directly", "dim")
        return None


def shortcut_arguments(launcher: Path, stub: Path | None) -> str:
    """What a shortcut passes to powershell.exe: the launcher through the stub, or the launcher itself."""
    return shortcut_owner.arguments_for(launcher, stub)


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


#: What the flag `schedule` holds once this step decided: `other` is a dev install that found the PC's
#: nightly task running another Nightshift folder and left it.
SCHEDULE_FLAGS = ("installed", "unavailable", "failed", "other")
_DEV_LEFT = re.compile(re.escape(schedule.DEV_LEFT) + r"([^;\r\n]+);")


def installs_list(ctx: StepContext) -> Path | None:
    """Where this setup records its home for the uninstaller of another install. A test run writes nothing
    under this PC's LOCALAPPDATA: it records only into a list the test hands in."""
    if ctx.installs_list is not None:
        return ctx.installs_list
    return None if testguard.under_test() else shortcut_owner.default_list()


def own_folder(ctx: StepContext) -> Path:
    """``<home>/Shortcuts``: this install's own full set (the uninstaller removes the folder)."""
    return ctx.paths.home / shortcuts_view.FOLDER


def home_var_now(ctx: StepContext) -> tuple[str, str | None]:
    """Whose NIGHTSHIFT_HOME (the user variable) is, seen from this install: (`mine` | `none` | `gone` |
    `other` | `unreachable` | `unread`, the folder it names). `unread`: Windows did not answer."""
    expr = f"[Environment]::GetEnvironmentVariable('{ps_quote(HOME_VAR)}', 'User')"
    named = eval_ps_text(ctx.runner, expr, tag="ps:get-env", meta={"name": HOME_VAR})
    if named is None:
        return "unread", None
    if not named.strip():
        return "none", None
    return shortcut_owner.folder_state(named.strip(), ctx.paths.home), named.strip()


class S09ShortcutsScheduler(Step):
    """Create the shortcuts and register the nightly task."""

    id = "s09_shortcuts_scheduler"
    title = "Shortcuts and nightly task"
    # `schedule_time` is not cleared: it is the time the answers had when this step last finished, which
    # the next run needs to tell a time the answers changed from one Settings changed (D-14)
    # (`shortcuts_taking` and `home_var_taking` are not cleared either: they are what a run killed after
    # the Desktop was written still has to say; the end of `run` removes them)
    clears_on_run = (
        "schedule", "schedule_mode", "aumid", "shortcuts_app", "shortcuts_owner", "shortcuts_line",
        "shortcuts_names", "home_var", "home_var_line", "dev_task_line",
    )

    def check(self, ctx: StepContext) -> bool:
        """This install's shortcuts exist, the Desktop and NIGHTSHIFT_HOME are as this step last left
        them (or another install's, as it recorded), and the task decision is recorded."""
        if ctx.flag("schedule") not in SCHEDULE_FLAGS or ctx.flag("aumid") is None:
            return False
        # INSTALL.cmd over an older install copies the program to <home>\app: the shortcuts and the task
        # still point at the folder the zip was extracted to (which the owner may delete) until s09 re-runs.
        if ctx.flag("shortcuts_app") != str(ctx.app_path):
            return False
        if (ctx.app_path / STUB_SOURCE).is_file() and not (ctx.paths.home / STUB_NAME).is_file():
            return False                    # shortcuts made before the stub existed (or the copy is gone)
        ans = ctx.ensure_answers()
        if ans.nightly is not None or ans.nightly_seen is not None or ans.shortcuts_here is not None:
            return False                    # an answer about this run that nobody acted on yet
        asked = ans.run_time
        if ctx.flag("schedule_time", asked) != asked:
            return False                    # the answers ask for another time than the task has (D-14)
        folder = desktop(ctx)
        names = [name for name, _ in wanted_shortcuts(ctx)]
        own = own_folder(ctx)
        if not all((own / f"{name}.lnk").is_file() for name in names):
            return False                    # this install's own set lost one (or was never made)
        seen = shortcuts_view.look(folder, ctx.paths.home, ctx.app_path, names)
        if ctx.flag("shortcuts_owner") != shortcuts_view.owner_flag(seen):
            return False                    # the Desktop changed hands since: say so, or take what was left
        ours = (shortcut_owner.MINE, shortcut_owner.FOREIGN)
        if seen["state"] not in shortcuts_view.KEPT and any(o not in ours for o, _ in seen["files"].values()):
            return False                    # a shortcut is missing, or opens a deleted Nightshift
        state, named = home_var_now(ctx)
        if state == "unread":
            return True                     # Windows did not say: nothing is changed on a guess
        if state == shortcut_owner.MINE:
            return ctx.flag("home_var") == shortcut_owner.MINE
        # another install's: fine while that is what this step recorded; nobody's (unset, a deleted
        # folder): work to do
        return state in shortcuts_view.KEPT and ctx.flag("home_var") == f"other:{named}"

    def run(self, ctx: StepContext) -> None:
        """Make the .lnk files, then call ``ns schedule install``."""
        folder = desktop(ctx)
        ans = ctx.ensure_answers()
        yes = ans.shortcuts_here is True
        shortcuts = wanted_shortcuts(ctx)
        known = {script for _, script, _ in SHORTCUTS}
        have = {p.name for _, p in shortcuts}
        for script in sorted(known - have):
            ctx.ui.say(f"  launcher {script} is not part of this version yet; no shortcut for it.", "dim")
        icon = shortcut_icon(ctx)
        stub = launch_stub(ctx)
        if not yes:
            ctx.clear_flags("shortcuts_taking", "home_var_taking")    # notes of a yes that is not here now
        own = own_folder(ctx)
        own.mkdir(parents=True, exist_ok=True)
        for name, launcher in shortcuts:    # step 0: this install's full set, whatever the Desktop holds
            self._write(ctx, own / f"{name}.lnk", name, launcher, stub, icon)
        names = [name for name, _ in shortcuts]
        seen = shortcuts_view.look(folder, ctx.paths.home, ctx.app_path, names)
        kept = seen["state"] in shortcuts_view.KEPT and not yes
        line = self._desktop(ctx, folder, shortcuts, stub, icon, seen, yes)
        dashboard = seen["files"].get(AUMID_SHORTCUT, ("", None))[0]
        on_desktop = dashboard == shortcut_owner.MINE or (not kept and dashboard != shortcut_owner.FOREIGN)
        stamp = (folder if on_desktop else own) / f"{AUMID_SHORTCUT}.lnk"
        self._register_toast_identity(ctx, stamp, icon, keep_other=not yes)
        var_line = self._home_var(ctx, yes)

        run_time = ans.run_time
        if ctx.dev_mode:
            self._dev_task(ctx, run_time)
        else:
            self._automatic_runs(ctx, ans.nightly, ans.nightly_seen, run_time)
        ctx.set_flag("schedule_time", run_time)             # the time the answers name, as of this run
        # step 2: whose the Desktop is now, in the words the summary repeats
        ctx.set_flag("shortcuts_owner", "other:" + "|".join(seen["paths"]) if kept else shortcut_owner.MINE)
        ctx.set_flag("shortcuts_line", line)
        ctx.set_flag("shortcuts_names", names)
        ctx.set_flag("home_var_line", var_line)
        ctx.set_flag("shortcuts_app", str(ctx.app_path))     # the app folder the shortcuts and the task use
        shortcut_owner.record_home(ctx.paths.home, installs_list(ctx))   # the PC's list of installs
        ctx.set_flag("first_install", False)                 # setup has decided: later runs keep the choice
        # last: the answers were about this run. A kill before this line repeats the step with them.
        if forget_setup_input(ctx.paths.config / "answers.json"):
            ctx.log.info("setup_input_consumed", keys=["nightly", "nightly_seen", "shortcuts_here"])
        ctx.answers = ans.model_copy(update={"nightly": None, "nightly_seen": None, "shortcuts_here": None})
        ctx.clear_flags("nightly_done", "shortcuts_taking", "home_var_taking")

    @staticmethod
    def _write(
        ctx: StepContext, lnk: Path, name: str, launcher: Path, stub: Path | None, icon: Path | None
    ) -> None:
        ok = create_shortcut(
            ctx.runner, lnk, "powershell.exe", shortcut_arguments(launcher, stub), shortcut_workdir(ctx),
            f"Nightshift: {name}", icon,
        )
        if not ok:
            raise make_error("NS-E110", f"could not create the shortcut '{name}' in {lnk.parent}")

    def _desktop(
        self, ctx: StepContext, folder: Path, shortcuts: list[tuple[str, Path]], stub: Path | None,
        icon: Path | None, seen: dict[str, Any], yes: bool,
    ) -> str:
        """Step 1 of the module docstring; the sentence about it. NS-W119 when the Desktop was kept."""
        others = seen["state"] in shortcuts_view.KEPT
        kept = others and not yes
        taking = ctx.flag("shortcuts_taking")
        if others and yes:
            ctx.set_flag("shortcuts_taking", list(seen["paths"]))   # before the first write (a kill)
        for name, launcher in shortcuts:
            owner = seen["files"][name][0]
            if owner == shortcut_owner.FOREIGN or (kept and owner != shortcut_owner.MINE):
                continue
            self._write(ctx, folder / f"{name}.lnk", name, launcher, stub, icon)
            ctx.ui.say(f"  shortcut: {name}")
        if kept:
            line = shortcuts_view.kept_line(seen, ctx.paths.home)
            ctx.warn("NS-W119", " and ".join(seen["paths"]))
        elif others:
            line = shortcuts_view.took_line(seen["paths"])
        elif yes and isinstance(taking, list) and taking:
            line = shortcuts_view.took_line([str(p) for p in taking])    # the run before this was killed
        elif seen["state"] == shortcut_owner.GONE:
            line = shortcuts_view.gone_line(seen["gone"])
        else:
            line = shortcuts_view.MINE_LINE
        ctx.log.info(
            "desktop_shortcuts", line, state=seen["state"], yes=yes, others=seen["paths"],
            foreign=seen["foreign"],
        )
        ctx.ui.say(line)
        for name in seen["foreign"]:
            ctx.ui.say(shortcuts_view.foreign_line(name))
        return line

    @staticmethod
    def _home_var(ctx: StepContext, yes: bool) -> str | None:
        """NIGHTSHIFT_HOME by the same rule: set when it names this folder, nothing, or a folder that is
        gone; left when it names another folder that exists (or whose drive is not there) unless the owner
        said yes. The sentence to say, if any. When Windows does not say what it holds, nothing is set."""
        state, named = home_var_now(ctx)
        took = ctx.flag("home_var_taking")
        line: str | None = None
        if state == "unread":
            ctx.log.warn("home_var_unread", "NIGHTSHIFT_HOME could not be read; left as it is")
            ctx.set_flag("home_var", "unread")
            return None
        if state in shortcuts_view.KEPT and not yes:
            ctx.set_flag("home_var", f"other:{named}")
            line = shortcuts_view.var_kept_line(str(named))
        else:
            if state in shortcuts_view.KEPT:
                ctx.set_flag("home_var_taking", str(named))     # before the change (a kill after it)
                took = str(named)
            if state != shortcut_owner.MINE and not set_user_env(ctx.runner, HOME_VAR, str(ctx.paths.home)):
                raise make_error("NS-E121", "could not set the NIGHTSHIFT_HOME user variable")
            ctx.set_flag("home_var", shortcut_owner.MINE)
            if yes and isinstance(took, str) and took:
                line = shortcuts_view.var_took_line(took)
        if line:
            ctx.log.info("home_var", line, state=state, yes=yes)
            ctx.ui.say(line)
        return line

    @staticmethod
    def _dev_task(ctx: StepContext, run_time: str) -> None:
        """docs/DEV_MODE.md: the task exists (`ns schedule show` and the uninstall find it) but it is
        disabled, so it never wakes or runs the development PC. Unchanged by the nightly question.
        Another install's task is left exactly as it is (`schedule.install` reads it first)."""
        res = ctx.run_tool("ns", ["schedule", "install", "--time", run_time, "--disabled"], timeout=300)
        left = _DEV_LEFT.search(res.text) if res.ok else None
        if left is not None:
            line = shortcuts_view.dev_task_line(left.group(1).strip())
            ctx.set_flag("schedule", "other")
            ctx.set_flag("dev_task_line", line)
            ctx.log.info("dev_task_left", line)
            ctx.ui.say(line)
        elif res.ok:
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
    def _register_toast_identity(ctx: StepContext, lnk: Path, icon: Path | None, *, keep_other: bool) -> None:
        """Registry key first (what makes toasts show), then the property on this install's dashboard
        shortcut `lnk` (the Desktop's when that is this install's, else the one in its own folder). A key
        another install uses is left as it is unless the owner said yes (`keep_other` False)."""
        before = ctx.flag("aumid_icon")     # the icon this install registered last (its program folder moves)
        state = register_aumid_keeping(
            ctx.runner, AUMID, "Nightshift", icon, keep_other=keep_other,
            also_mine=[before] if isinstance(before, str) and before else [],
        )
        if state == "registered" and icon is not None:
            ctx.set_flag("aumid_icon", str(icon))
        if state == "failed":
            ctx.set_flag("aumid", "failed")
            ctx.log.warn("aumid_registry_failed", AUMID)
            ctx.ui.say("  Windows notifications could not be registered; Nightshift works without.", "yellow")
            return
        if state.startswith("kept:"):
            ctx.set_flag("aumid", "other")
            ctx.log.info("aumid_kept", state[5:])
            ctx.ui.say(
                f"  notifications: the registration that names {state[5:]} is not this install's and was "
                "left as it is; this install's notifications show under it."
            )
            return
        if lnk.exists() and set_shortcut_aumid(ctx.runner, lnk, AUMID):
            ctx.set_flag("aumid", "registry+shortcut")
        else:
            ctx.set_flag("aumid", "registry")
            ctx.log.warn("aumid_shortcut_failed", str(lnk))
        ctx.ui.say("  notifications: registered")

    def verify(self, ctx: StepContext) -> None:
        """Every expected shortcut file exists (in this install's folder, and on the Desktop unless that
        was kept for another install) and the task decision is recorded."""
        folder = desktop(ctx)
        on_desktop = ctx.flag("shortcuts_owner") == shortcut_owner.MINE
        for name, _ in wanted_shortcuts(ctx):
            if not (own_folder(ctx) / f"{name}.lnk").exists():
                raise make_error("NS-E110", f"shortcut '{name}' was not created in {own_folder(ctx)}")
            if on_desktop and not (folder / f"{name}.lnk").exists():
                raise make_error("NS-E110", f"shortcut '{name}' was not created")
        if ctx.flag("schedule") not in SCHEDULE_FLAGS:
            raise make_error("NS-E204", "the nightly task decision was not recorded")
