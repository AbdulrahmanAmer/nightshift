"""What stopped a nightly run that died, from evidence the product and Windows recorded (card night-fits,
night-fits-b, 4b.3).

`cause(paths, run_id, events, boot)` is the one function. It is asked when a dead nightly run is closed
(`ownership.recover`: the dashboard's start, `studio`'s start, the next night's `_close_dead_work`) and its
answer replaces "power cut, crash or restart" in the morning note, the job's Queue card and the run's line.

The evidence, all of it optional:
- the run's own ledger lines (`ledger.py`: `night_start` with the task's start and limit, `cut_expected`
  written two minutes before Windows' limit, and the time of every line);
- the night's beat (`run/night-beat.json`, rewritten every `nightclock.BEAT_S` = 30 s while the run lives):
  the last moment the run was known alive, to within 30 s;
- Windows' System log after that moment (`events`, an `EventSource`: `ns_common.winevents.SystemLog` on a
  real install, a fake in tests), read only, bounded (5 s, 20 events), never required;
- the time the PC last started (`boot`).

Each cause has its own sentence and is asserted only on its evidence (`Cause.said`, no full stop: the
callers add ", while <channel>'s video was at <stage>"):
- `power_loss`: a 6008 whose recorded moment of the unexpected shutdown lies within `NEAR_S` of the last
  beat ("... at about <hh:mm>"), or a 41 after the last beat with no such 6008 ("... some time after");
- `update_restart`: a 1074 within `NEAR_S` of the last beat whose program is Windows Update's
  (`UPDATE_PROCESSES`) or whose reason names an update, and the PC started again after the last beat; a
  second one within `REPEAT_DAYS` adds the NS-W212 sentence, once in that window (`Cause.extra`);
- `shutdown`: any other 1074 (with its program) or a 6006 within `NEAR_S`, and the PC started again;
- `time_limit`: a `cut_expected` line of the run and a last beat between that line and the limit plus
  `NEAR_S`, with nothing in Windows' log that explains it otherwise;
- `unknown`: everything else. With a beat or a ledger line the sentence says when the run was last known
  alive and exactly what is missing (the log could not be read, or names nothing; whether the PC started
  again); with no evidence at all (a night of an older version) it is the old sentence, word for word.
Only a beat is precise enough to tie a Windows event to the run's end: with ledger lines alone the time is
said as "some time after" and no event is asserted.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from ns_common.atomic import read_text_retry
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_common.winevents import (
    LOG_STOPPED,
    SHUTDOWN_ASKED,
    UNEXPECTED_BOOT,
    UNEXPECTED_SHUTDOWN,
    Event,
)
from ns_studio import ledger

TIME_LIMIT, UPDATE_RESTART, POWER_LOSS, SHUTDOWN, UNKNOWN = (
    "time_limit", "update_restart", "power_loss", "shutdown", "unknown",
)
#: A Windows event explains the run's end only this close to its last beat (a beat every 30 s, a shutdown that
#: takes up to a minute and a half to end the programs).
NEAR_S = 120
#: How often the night's beat is written (`nightclock.BEAT_S`, repeated: this module imports no clock).
BEAT_S = 30
#: A second restart for updates within this many days adds the NS-W212 sentence (once in such a window).
REPEAT_DAYS = 14
#: Windows Update's programs that ask for a restart (event 1074, param1).
UPDATE_PROCESSES = frozenset({"trustedinstaller.exe", "mousocoreworker.exe", "usoclient.exe"})
#: Words of a 1074 reason (param3) that name an update.
UPDATE_WORDS = ("update", "upgrade", "service pack", "hotfix", "security fix")
#: The words of a night that left no evidence (a night of 0.1.15-rc4 or older): unchanged.
OLD_WORDS = "The night's run was interrupted (power cut, crash or restart)"
CAUSE_LINE = "night_cause"


class EventSource(Protocol):
    """Windows' System log: shutdown, restart and power-loss events from `since` on (raises when it cannot
    be read)."""

    def read(self, since: datetime) -> Sequence[Event]: ...


@dataclass(frozen=True)
class Cause:
    kind: str
    said: str  # the sentence, no full stop
    at: datetime | None = None
    extra: str | None = None  # NS-W212's sentence (a second restart for updates within REPEAT_DAYS)
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        """The sentence names what stopped the run, or says exactly what is not known (not the old words)."""
        return self.said != OLD_WORDS


def hhmm(when: datetime) -> str:
    """`03:40`: a moment as the owner reads it, in the PC's own time zone."""
    return when.astimezone().strftime("%H:%M")


def _parse(raw: Any) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def read_beat(paths: Paths) -> dict[str, Any] | None:
    """The night's last beat (`run/night-beat.json`), or None."""
    try:
        data = json.loads(read_text_retry(paths.home / "run" / "night-beat.json"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _limit_s(start: Mapping[str, Any] | None) -> int | None:
    limit = (start or {}).get("limit")
    return int(limit) if isinstance(limit, int | float) and not isinstance(limit, bool) else None


def _within(at: datetime | None, last: datetime) -> bool:
    return at is not None and abs((at - last).total_seconds()) <= NEAR_S


def _is_update(e: Event) -> bool:
    if (e.process or "").lower() in UPDATE_PROCESSES:
        return True
    reason = (e.reason or "").lower()
    return any(w in reason for w in UPDATE_WORDS)


def _repeat(
    lines: Sequence[Mapping[str, Any]], run_id: str, at: datetime, start: Mapping[str, Any] | None
) -> str | None:
    """NS-W212's sentence when another restart for updates ended a night within `REPEAT_DAYS` before `at`
    and the sentence was not said in that window yet."""
    since = at - timedelta(days=REPEAT_DAYS)
    earlier = [x for x in lines if x.get("kind") == CAUSE_LINE and x.get("run_id") != run_id
               and (t := _parse(x.get("cause_at"))) is not None and since <= t <= at]
    if not any(x.get("cause") == UPDATE_RESTART for x in earlier):
        return None
    if any(x.get("w212") for x in earlier):
        return None
    task = _parse((start or {}).get("task_start"))
    when = f" and include {hhmm(task)}, when the nightly run starts" if task is not None else (
        " so that they include the time the nightly run starts")
    return ("Windows did this twice in two weeks. To keep it away from the night: Settings > Windows Update "
            f"> Advanced options > Active hours{when} (NS-W212)")


def cause(
    paths: Paths,
    run_id: str,
    events: EventSource | None,
    boot: datetime | None,
    *,
    lines: Sequence[Mapping[str, Any]] | None = None,
    beat: Mapping[str, Any] | None = None,
    log: NsLogger | None = None,
) -> Cause:
    """What stopped the dead nightly run `run_id` (module docstring). Never raises."""
    if lines is None:
        try:
            lines = ledger.read(paths)
        except Exception:  # noqa: BLE001 - no ledger: no evidence from it
            lines = []
    if beat is None:
        beat = read_beat(paths)
    own = [x for x in lines if x.get("run_id") == run_id]
    start = next((x for x in own if x.get("kind") == "night_start"), None)
    beat_at = _parse(beat.get("at")) if beat and beat.get("run_id") == run_id else None
    line_times = [t for x in own if (t := _parse(x.get("at"))) is not None]
    last_line = max(line_times) if line_times else None
    if beat_at is None and last_line is None:
        return Cause(UNKNOWN, OLD_WORDS, evidence={"basis": "none"})
    last = max(t for t in (beat_at, last_line) if t is not None)
    # the beat bounds the end to within a beat or two, also when a ledger line came just after it; a beat
    # older than that stopped being written (a write failure) and bounds nothing
    precise = beat_at is not None and (last - beat_at).total_seconds() <= 2 * BEAT_S
    restarted = None if boot is None else boot > last
    evidence: dict[str, Any] = {"basis": "beat" if precise else "ledger", "last_alive": last.isoformat(),
                                "boot": boot.isoformat() if boot else None}
    found: list[Event] = []
    unreadable: str | None = None
    if precise:
        try:
            if events is None:
                raise LookupError("no event source")
            found = list(events.read(last - timedelta(seconds=NEAR_S)))
        except Exception as e:  # noqa: BLE001 - never required: the sentence says it was not read
            unreadable = str(e) or type(e).__name__
            if log is not None:
                log.warn("event_log_unreadable", unreadable, run_id=run_id)
    evidence["events"] = [f"{e.id}@{e.at.isoformat(timespec='seconds')}" for e in found]
    evidence["event_log"] = "not read: no beat" if not precise else (unreadable or "read")
    if precise and restarted is not False:
        explained = _from_events(found, last, own, lines, run_id, start, evidence)
        if explained is not None:
            return explained
    cut = next((x for x in own if x.get("kind") == "cut_expected"), None)
    task = _parse((start or {}).get("task_start"))
    limit = _limit_s(start)
    # Windows' own stop at the limit, once a restart or a power loss at that moment is ruled out: the PC did
    # not start again since, or the log was read and names none near the last beat
    ruled_out = restarted is False or unreadable is None
    if precise and ruled_out and cut is not None and task is not None and limit is not None:
        limit_at = task + timedelta(seconds=limit)
        cut_at = _parse(cut.get("at"))
        if cut_at is not None and cut_at <= last <= limit_at + timedelta(seconds=NEAR_S):
            from ns_studio.nightclock import limit_adjective

            return Cause(
                TIME_LIMIT,
                f"Windows stopped the night's run at {hhmm(limit_at)}, at the {limit_adjective(limit)} limit "
                "it allows the nightly task",
                limit_at, evidence=evidence,
            )
    return Cause(UNKNOWN, _unknown_words(last, precise, boot, restarted, unreadable), None, evidence=evidence)


def _unknown_words(
    last: datetime, precise: bool, boot: datetime | None, restarted: bool | None, unreadable: str | None
) -> str:
    """The sentence for a run whose end no evidence explains: when it was last known alive, and exactly what
    is missing. A PC that did not start again since then kept running: the run's own process ended."""
    when = f"at about {hhmm(last)}" if precise else f"some time after {hhmm(last)}"
    again = f"; the PC was started again at {hhmm(boot)}" if restarted and boot else ""
    if restarted is False:
        detail = ("the PC did not restart, so the run's own process was ended: a crash, a person or a "
                  "program ended it")
    elif not precise:
        detail = "the run's last beat was not kept, so its end cannot be matched with what Windows recorded"
    elif unreadable is not None:
        detail = "Windows' event log could not be read"
    else:
        detail = "Windows' event log names no shutdown, restart or power loss at that time"
    return f"The night's run was interrupted {when}, and what stopped it cannot be told: {detail}{again}"


def _from_events(
    found: Sequence[Event],
    last: datetime,
    own: Sequence[Mapping[str, Any]],
    lines: Sequence[Mapping[str, Any]],
    run_id: str,
    start: Mapping[str, Any] | None,
    evidence: dict[str, Any],
) -> Cause | None:
    """The first Windows event near the run's last beat that explains its end (module docstring)."""
    near: list[tuple[datetime, Event]] = []
    for e in found:
        moment = e.stopped_at if e.id == UNEXPECTED_SHUTDOWN else e.at
        if moment is not None and e.id in (SHUTDOWN_ASKED, LOG_STOPPED, UNEXPECTED_SHUTDOWN) and _within(
            moment, last
        ):
            near.append((moment, e))
    near.sort(key=lambda t: t[0])
    if near:
        at, e = near[0]
        if e.id == UNEXPECTED_SHUTDOWN:
            return Cause(POWER_LOSS, f"The PC lost power or stopped unexpectedly at about {hhmm(at)}, during "
                         "the night's run", at, evidence=evidence)
        if e.id == SHUTDOWN_ASKED and _is_update(e):
            return Cause(UPDATE_RESTART, f"Windows restarted the PC at {hhmm(at)} to finish installing "
                         "updates, during the night's run", at, _repeat(lines, run_id, at, start), evidence)
        asked = next((x for _t, x in near if x.id == SHUTDOWN_ASKED), None)
        verbs = {"restart": "restarted", "power off": "shut down"}
        verb = verbs.get((asked.action or "").lower(), "shut down or restarted") if asked else (
            "shut down or restarted")
        by = f" (asked by {asked.process})" if asked is not None and asked.process else ""
        return Cause(SHUTDOWN, f"The PC was {verb} at {hhmm(at)}{by}, during the night's run", at,
                     evidence=evidence)
    if any(e.id in (UNEXPECTED_BOOT, UNEXPECTED_SHUTDOWN) and e.at > last for e in found):
        return Cause(POWER_LOSS, "The PC lost power or stopped unexpectedly some time after "
                     f"{hhmm(last)}, during the night's run", None, evidence=evidence)
    return None


def record(paths: Paths, run_id: str, c: Cause, *, now: datetime, log: NsLogger | None = None) -> None:
    """The `night_cause` ledger line (what `_repeat` counts): written once, when the run is closed."""
    line = {"kind": CAUSE_LINE, "at": now.isoformat(timespec="seconds"), "run_id": run_id,
            "cause": c.kind, "cause_at": c.at.isoformat(timespec="seconds") if c.at else None,
            "w212": c.extra is not None, "said": c.said, "evidence": c.evidence}
    try:
        ledger.write(paths, line, when=now, log=log)
    except Exception as e:  # noqa: BLE001 - the ledger never stops a recovery
        if log is not None:
            log.warn("ledger_write_failed", str(e), kind=CAUSE_LINE)
