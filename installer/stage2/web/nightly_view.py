"""Automatic runs as the setup window and the setup summary show them (nightly-optional part b).

Standard library only: the setup window runs on the bare Python. Two things live here, each in one place:

* what Automatic runs are NOW (`current_choice`), read from ``config/schedule.json`` by the rules of
  ``ns_supervisor.schedule_cfg`` (the record of an enabled Windows task; no record is off). The window shows
  it under its switch and sends it back with the owner's answer as ``nightly_seen``, so the scheduler step
  applies the answer only to the state the owner was looking at;
* the one sentence the end of setup says about Automatic runs (`outcome_line`, `kept_line`, `summary`): the
  console summary (s10) and the window's Done screen say the same words.

The record can lag Windows by up to a minute (the dashboard's own look brings it in line); the scheduler step
never decides from it: it asks Windows.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ns_common.atomic import read_text_retry

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
#: ``InstallerAnswers.nightly_seen``: a home with no install (`new`), off, or on at a time.
SEEN_RE = re.compile(r"^(new|off|on@([01]\d|2[0-3]):[0-5]\d)$")
_OFF_MODES = ("disabled", "switched_off")
WHERE = "Settings > Schedule"


def _json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(read_text_retry(path, "utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_state(home: Path) -> tuple[bool, str | None, bool]:
    """(on, time, only while signed in) as ``config/schedule.json`` says. A missing, torn or unreadable
    record, one without a usable time and one in an off mode (a dev install's) all read off."""
    data = _json(Path(home) / "config" / "schedule.json")
    when, mode = data.get("run_time"), data.get("mode")
    if not (isinstance(when, str) and _HHMM.match(when)):
        return False, None, False
    if mode in _OFF_MODES:
        return False, when, False
    return True, when, mode == "interactive_only"


def state_word(home: Path) -> str:
    """``on@HH:MM`` or ``off``: the record in the spelling of `nightly_seen`."""
    on, when, _ = record_state(home)
    return f"on@{when}" if on else "off"


def current_choice(home: Path) -> str:
    """What the window shows as "Now": ``on@HH:MM``, ``off`` (an install is or was here), or ``new`` (no
    install state, flags or record in the folder: nobody chose yet, and the window shows no "Now" line)."""
    word = state_word(home)
    if word != "off":
        return word
    config = Path(home) / "config"
    traces = ("install_state.json", "wizard_flags.json", "schedule.json")
    return "off" if any((config / name).exists() for name in traces) else "new"


def valid_seen(value: Any) -> str | None:
    """`value` when it is a `nightly_seen` word, else None."""
    return value if isinstance(value, str) and SEEN_RE.match(value) else None


def _seen_words(seen: str | None) -> str:
    return f"on at {seen[3:]}" if seen and seen.startswith("on@") else "off"


def _why(reason: Any) -> str:
    text = " ".join(str(reason or "").split())
    return text[:200] or "no reason given"


def _owner_words(o: dict[str, Any], on: bool) -> str:
    """What must be said when the PC's one nightly task belongs, or belonged, to another Nightshift folder."""
    other, took = o.get("other_home"), o.get("took_from")
    if not on and other and o.get("other_off") and o.get("unreachable"):
        return (
            f" A switched-off nightly task names the Nightshift at {other}, which cannot be reached now (its "
            "drive is not there); it was left as it is."
        )
    if not on and other and o.get("other_off"):
        return (
            f" A switched-off nightly task of the Nightshift at {other} is in Task Scheduler; it was left as "
            "it is. Switching Automatic runs on in this one's Settings moves it here."
        )
    if not on and other and o.get("unreachable"):
        return (
            f" The nightly task runs the Nightshift at {other}, which cannot be reached now (its drive is "
            "not there); it was left as it is."
        )
    if not on and other:
        return (
            f" Another Nightshift at {other} owns this PC's nightly task (a PC has one), so this one does "
            f"not run by itself; switching Automatic runs on in this one's {WHERE} moves the task here."
        )
    if on and took and o.get("took_off"):
        return (
            f" This PC has one nightly task: the switched-off one of the Nightshift at {took} now runs this "
            "Nightshift, and that one's Settings still show Automatic runs off."
        )
    if on and took:
        return (
            f" This PC has one nightly task: it now runs this Nightshift and no longer the one at {took}, "
            "whose Settings will show Automatic runs off."
        )
    if on and o.get("owner_unknown") and o.get("case") != "kept":
        return (
            " A nightly task was already on this PC and setup could not tell which Nightshift it belonged "
            "to; it now runs this one."
        )
    return ""


def kept_line(on: bool, when: str | None, signed_in_only: bool = False) -> str:
    """The sentence for a state setup did not change."""
    if not on:
        return f"Automatic runs: off, kept as set in Settings (switch them on in {WHERE})."
    only = " They run only while you are signed in (NS-W205)." if signed_in_only else ""
    return f"Automatic runs: on at {when}, kept as set in Settings.{only}"


def outcome_line(o: dict[str, Any]) -> str:
    """The sentence for what ``ns schedule install --setup`` answered (`schedule.SetupResult` as a dict).
    Every sentence is true of the state it names and says where the owner changes it."""
    case, on, when = o.get("case"), o.get("on"), o.get("run_time")
    only, reason = bool(o.get("signed_in_only")), _why(o.get("reason"))
    gone, was_off = o.get("gone_home"), bool(o.get("was_off"))
    if gone and case == "refused" and was_off:
        return (
            f"Automatic runs: off. A switched-off nightly task left by a deleted Nightshift ({gone}) could "
            f"not be removed ({reason}); it is switched off, so it starts nothing. Delete 'Nightshift "
            f"Daily' in Task Scheduler, or switch Automatic runs on in {WHERE} to run this one every night."
        )
    if gone and case == "refused":
        return (
            f"Automatic runs: off. A nightly task left by a deleted Nightshift ({gone}) could not be removed "
            f"({reason}); it still wakes this PC for nothing. Delete 'Nightshift Daily' in Task Scheduler, "
            f"or switch Automatic runs on in {WHERE} to run this one every night."
        )
    if gone and was_off:
        return (
            f"Automatic runs: off. A switched-off nightly task left by a deleted Nightshift ({gone}) was "
            f"removed. Switch Automatic runs on in {WHERE} to run this one every night."
        )
    if gone:
        return (
            f"Automatic runs: off. A nightly task left by a deleted Nightshift ({gone}) was removed: it woke "
            f"this PC for nothing. Switch Automatic runs on in {WHERE} to run this one every night."
        )
    if case == "unrecorded":
        return (
            f"Automatic runs: Windows runs the nightly task at {when}, but Nightshift could not record it "
            "(NS-W208). Its pages say off, or show the old time, until the folder config can be written; to "
            "stop the nightly run now, delete or disable 'Nightshift Daily' in Task Scheduler."
        )
    if case == "undone":
        still = f"on at {when} as before" if on else "off"
        return (
            f"Automatic runs: {still}. Nightshift could not write config/schedule.json ({reason}), so the "
            f"change was undone; change them in {WHERE} once the folder config can be written."
        )
    if case == "unread":
        if o.get("wanted_on"):
            return f"Automatic runs: not switched on (NS-W206: {reason}); switch them on in {WHERE}."
        return f"Automatic runs: not checked ({reason}), left as they were; Settings shows them."
    if case == "refused":
        if o.get("wanted_on"):
            return (
                f"Automatic runs: off. NS-W206: Windows refused the nightly task ({reason}); try the switch "
                f"in {WHERE}, or run 'ns schedule install' from an elevated PowerShell."
            )
        if o.get("wanted_off"):
            return (
                f"Automatic runs: still on at {when}. Windows refused to remove the nightly task ({reason}); "
                f"switch them off in {WHERE}, or delete 'Nightshift Daily' in Task Scheduler."
            )
        if o.get("was_off"):
            return (
                "Automatic runs: off. A switched-off nightly task of an earlier install of this folder could "
                f"not be removed ({reason}); it is switched off, so it starts nothing. Delete 'Nightshift "
                f"Daily' in Task Scheduler, or switch Automatic runs on in {WHERE}."
            )
        return (
            f"Automatic runs: on at {when}, kept as set in Settings. Windows refused to register the task "
            f"again ({reason}); the task is as it was."
        )
    if case == "stale":
        now = f"on at {when}" if on else "off"
        return (
            f"Automatic runs: left {now}. The setup window showed them {_seen_words(o.get('seen'))} when you "
            f"answered, and they are not that now, so setup changed nothing; {WHERE} changes them."
        ) + _owner_words(o, bool(on))
    if o.get("removed_old"):
        found = "switched-off nightly task" if o.get("was_off") else "nightly task"
        line = (
            f"Automatic runs: off. An older Nightshift's {found} was found and removed; switch them on "
            f"in {WHERE} when you want the nightly run."
        )
    elif case == "kept" and on and o.get("owner_unknown"):
        line = (
            f"Automatic runs: on at {when}. A nightly task was already on this PC and setup could not tell "
            f"which Nightshift it belongs to, so it was left as it is; switch it off in {WHERE} if you do "
            "not want it."
        )
    elif case == "kept":
        line = kept_line(bool(on), when, only)
    elif on:
        how = "every night (the PC wakes itself for it)" if case == "first" else "as you chose in setup"
        if only:
            how = "only while you are signed in (NS-W205)"
        line = f"Automatic runs: on at {when}, {how}."
    elif case == "first":
        line = (
            f"Automatic runs: off. Nothing runs by itself; switch them on in {WHERE} when you want the "
            "nightly run."
        )
    else:
        line = "Automatic runs: off, as you chose in setup."
    line += _owner_words(o, bool(on))
    asked = o.get("time_kept")
    if asked:
        stays = when if when else "as it was"
        line += (
            f" The nightly time stays {stays} as set in Settings. Your answers name {asked}; change it in "
            f"{WHERE} if you want that."
        )
    return line


def summary(home: Path, flags: dict[str, Any]) -> dict[str, Any]:
    """What the end of setup says: ``{"state": on|off|failed|unrecorded, "time", "line"}``.

    ``unrecorded`` is NS-W208 (the scheduler step's ``schedule_mode``): Windows runs the task at ``time`` and
    the record could not be written, so the record's "off" must not be the headline. It holds while the
    record still is what that step left; once the record follows Windows, the state is read from it again.

    The scheduler step leaves its sentence in the flag ``nightly_line`` with the record's state at that
    moment (``nightly_state``). A run in which that step had nothing to do (a Repair with nothing changed)
    still reaches the summary: when the record has moved since (Settings changed it), the stored sentence
    would be untrue, and the state as it is now is said instead.
    """
    on, when, only = record_state(home)
    line = flags.get("nightly_line")
    if not isinstance(line, str) or not line or flags.get("nightly_state") != state_word(home):
        # no decision recorded at all (the scheduler step was skipped) and nothing on: nobody set anything
        undecided = flags.get("schedule") is None and not on
        line = outcome_line({"case": "first", "on": False}) if undecided else kept_line(on, when, only)
    failed = flags.get("schedule") == "failed" and not on
    state = "on" if on else "failed" if failed else "off"
    if flags.get("schedule_mode") == "unrecorded" and flags.get("nightly_state") == state_word(home):
        ran_at = flags.get("nightly_time")
        return {"state": "unrecorded", "time": ran_at if isinstance(ran_at, str) else when, "line": line}
    return {"state": state, "time": when, "line": line}
