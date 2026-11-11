"""The GPU kit's two nights (card gpu-kit): ONE record, ONE reader, honoured by the product.

The kit (`dev/kit/gpukit.py`, step K4) writes `run/gpu-kit-nights.json` BEFORE it changes anything for the
nights. The record is the single owner of the kit's nights: for each of the two nights its window and what
it makes (one full story video on the kit's channel, besides what the owner's channels have due), what
Automatic runs were before the kit, the run time it armed, and `until` (the end of night 2's window).

`read` is the only reader. It never raises and answers one of four results (`Nights.kind`):

- `none`: no record. Every route behaves as if the kit had never run.
- `valid`: the nights are armed; `night` is 1 or 2 while `now` is inside that night's window, else None.
- `expired`: past `until`. The night runs exactly as without a record; the nightly then puts back what the
  kit changed (`end_nights`): the kit's channel paused, Automatic runs off when they were off before the kit
  and nobody chose them since, the record removed, a note.
- `unreadable` (a torn file, a time without a time zone, a record from a newer version, a file that cannot
  be opened): every route behaves as if there were no record, and the reason is logged.

Readers: the nightly (`studio run --daily`, `night_start`/`end_nights`), the planner (`cadence.plan_daily`
through `kit_night`: the kit's channel is planned on an armed night only, once a night), the owner's routes
to Automatic runs (Settings, `ns schedule install`, setup's nightly answer: `owner_chose`) and the kit's K7.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from ns_common.atomic import write_text_atomic
from ns_common.logging import NsLogger
from ns_common.paths import Paths

RECORD = "gpu-kit-nights.json"
SCHEMA = 1
#: The field a Settings request carries when the GPU kit itself moves the switch (not the owner).
BY_KIT = "gpu-kit"
#: The kit's own channel (the same slug as `cadence.KIT_CHANNEL` and `dev/kit/gpukit.py:CHANNEL`).
KIT_CHANNEL = "gpu-kit"
#: What each armed night makes for the kit, besides the owner's channels.
KIT_NIGHT_MAKES = "one full story video on the kit's channel"

EXPIRED_NOTE = (
    "The GPU kit's two nights are over. Automatic runs are off again, as they were before the kit, and its "
    "channel is paused. To collect what the two nights did, double-click GPU-KIT.cmd. To make videos every "
    "night, switch Automatic runs on in Settings."
)


@dataclass(frozen=True)
class Nights:
    """What the record says now (`read`)."""

    kind: Literal["none", "valid", "expired", "unreadable"]
    record: Mapping[str, Any] = field(default_factory=dict)
    night: int | None = None  # valid: the armed night `now` is in (1 or 2), else None
    reason: str = ""  # unreadable: why

    @property
    def armed(self) -> bool:
        return self.kind in ("valid", "expired")


#: Said once in the morning notes when a night cannot read the record: no night can then switch Automatic runs
#: off again, so the owner is told where the file is and what to do.
UNREADABLE_NOTE = (
    "The GPU kit's record of its two test nights cannot be read ({file}). Nightshift runs as if the kit "
    "had not switched anything on. If you did not switch Automatic runs on yourself, switch them off in "
    "Settings, "
    "or run GPU-KIT.cmd again: it puts back what it changed."
)


def record_file(paths: Paths) -> Path:
    return paths.home / "run" / RECORD


def _aware(raw: Any, what: str) -> datetime:
    when = datetime.fromisoformat(str(raw))
    if when.tzinfo is None:
        raise ValueError(f"{what} has no time zone ({raw})")
    return when


def read(paths: Paths, now: datetime) -> Nights:
    """THE reader of the record. Never raises; `now` must be aware."""
    try:
        text = record_file(paths).read_text(encoding="utf-8")
    except FileNotFoundError:
        return Nights("none")
    except Exception as e:  # noqa: BLE001 - a folder at its path, a lock, a bad disk: as if no record
        return Nights("unreadable", reason=f"{record_file(paths)} cannot be read ({e})")
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except ValueError as e:
        return Nights("unreadable", reason=f"{record_file(paths)} is torn or not JSON ({e})")
    try:
        if int(data.get("schema", SCHEMA)) > SCHEMA:
            return Nights(
                "unreadable", data, reason=f"written by a newer version (schema {data.get('schema')})"
            )
        until = _aware(data["until"], "until")
        windows = [(_aware(n["from"], "a night's start"), _aware(n["to"], "a night's end"))
                   for n in data["nights"]]  # fmt: skip
        if now >= until:
            return Nights("expired", data)
        night = next((i for i, (lo, hi) in enumerate(windows, 1) if lo <= now < hi), None)
    except Exception as e:  # noqa: BLE001 - a time without a time zone, a missing field, a wrong type
        return Nights(
            "unreadable", data, reason=f"{record_file(paths)} cannot be used ({type(e).__name__}: {e})"
        )
    return Nights("valid", data, night)


def write(paths: Paths, record: Mapping[str, Any]) -> None:
    """The kit's writer (it raises: the kit says so and stops before it changes anything)."""
    text = json.dumps({"schema": SCHEMA, **record}, indent=2, ensure_ascii=False)
    write_text_atomic(record_file(paths), text + "\n")


def remove(paths: Paths, log: NsLogger | None = None) -> bool:
    """Remove the record; False (logged) when it cannot be removed now. Never raises."""
    try:
        record_file(paths).unlink(missing_ok=True)
        return True
    except Exception as e:  # noqa: BLE001
        if log is not None:
            log.warn("gpu_kit_nights_not_removed", str(e))
        return False


def owner_chose(paths: Paths, now: datetime, route: str, log: NsLogger | None = None) -> None:
    """The owner switched Automatic runs themselves (Settings, `ns schedule install`, setup's nightly answer):
    from now on the switch is theirs, recorded once and never undone by the kit or a night. Never raises."""
    try:
        nights = read(paths, now)
        if nights.armed and not nights.record.get("owner_chose_at"):
            write(
                paths,
                {**nights.record, "owner_chose_at": now.isoformat(timespec="seconds"), "owner_route": route},
            )
    except Exception as e:  # noqa: BLE001 - the owner's own click always applies
        if log is not None:
            log.warn("gpu_kit_owner_choice_not_recorded", str(e), route=route)


def kit_night(paths: Paths, now: datetime, made_tonight: Callable[[datetime, datetime], bool]) -> int | None:
    """The armed night (1 or 2) whose kit video the planner adds now, or None: the record is valid, `now` is
    inside that night's window, and no job of the kit's channel was made in it yet (`made_tonight(from, to)`).
    Never raises."""
    try:
        nights = read(paths, now)
        if nights.kind != "valid" or nights.night is None:
            return None
        n = nights.record["nights"][nights.night - 1]
        lo, hi = _aware(n["from"], "start"), _aware(n["to"], "end")
        return None if made_tonight(lo, hi) else nights.night
    except Exception:  # noqa: BLE001 - as if no record
        return None


def night_start(paths: Paths, now: datetime, log: NsLogger | None = None) -> Nights:
    """At the start of a nightly run: the record as read, said in the log once. An expired record pauses the
    kit's channel BEFORE the night (so it neither plans nor continues the kit's job); the night then runs
    exactly as if there were no record. Never raises."""
    nights = read(paths, now)
    if log is not None and nights.kind == "unreadable":
        log.warn(
            "gpu_kit_nights_unreadable", f"{nights.reason}: tonight runs as if the GPU kit had not armed it"
        )
    if nights.kind == "unreadable":
        with contextlib.suppress(Exception):  # a note never stops a night
            _note_once(paths, UNREADABLE_NOTE.format(file=record_file(paths)))
    if log is not None and nights.kind == "valid":
        log.info("gpu_kit_night", night=nights.night, makes=KIT_NIGHT_MAKES if nights.night else None)
    if nights.kind == "expired":
        _pause_kit_channel(paths, log)
    return nights


def end_nights(
    paths: Paths,
    nights: Nights,
    now: datetime,
    log: NsLogger | None = None,
    *,
    switch_off: Callable[[Paths], None] | None = None,
    leave_note: Callable[[Paths, str], object] | None = None,
    is_on: Callable[[Paths], str | None] | None = None,
) -> str | None:
    """After a night that found the record expired: put back what the kit changed (Automatic runs off when
    they were off before the kit, nobody chose them since and they are still on at the armed time), leave
    the note, remove the record. The sentence said, or None. Never raises."""
    if nights.kind != "expired":
        return None
    try:
        rec = nights.record
        on_now = (is_on or _run_time)(paths)
        theirs = rec.get("automatic_runs_before") is not False or bool(rec.get("owner_chose_at"))
        theirs = bool(theirs or on_now is None or (rec.get("run_time") and on_now != rec.get("run_time")))
        if not theirs:
            (leave_note or _note_once)(paths, EXPIRED_NOTE)
            (switch_off or _switch_off)(paths)
        if log is not None:
            # said BEFORE the record goes, and with the record's own `armed_at`: once the record is gone,
            # this line is how the kit's last step knows that Nightshift itself settled these nights (and
            # that Automatic runs still on after it are the owner's, not the kit's to switch off)
            log.info("gpu_kit_nights_ended", switched_off=not theirs, armed_at=rec.get("armed_at"))
        remove(paths, log)
        return None if theirs else EXPIRED_NOTE
    except Exception as e:  # noqa: BLE001 - kept: the next night tries again
        if log is not None:
            log.warn("gpu_kit_nights_put_back_failed", str(e))
        return None


def _run_time(paths: Paths) -> str | None:
    from ns_supervisor.schedule_cfg import read_run_time

    return read_run_time(paths)  # the time of the enabled task, None while Automatic runs are off


def _pause_kit_channel(paths: Paths, log: NsLogger | None) -> None:
    try:
        from ns_common.config import load_channel, save_channel

        ch = load_channel(paths, KIT_CHANNEL)
        if ch.status != "paused":
            save_channel(paths, ch.model_copy(update={"status": "paused"}))
    except FileNotFoundError:
        return
    except Exception as e:  # noqa: BLE001 - a night never plans it anyway (cadence)
        if log is not None:
            log.warn("gpu_kit_channel_not_paused", str(e))


def _note_once(paths: Paths, message: str) -> object:
    """The morning note, said once while it waits."""
    from ns_supervisor.notify import leave_note

    return leave_note(paths, "Nightshift", message,
                      once=lambda notes: any(n["message"] == message for n in notes))  # fmt: skip


def _switch_off(paths: Paths) -> None:
    from ns_supervisor import schedule
    from ns_supervisor.control import app_path

    schedule.set_automatic(paths, app_path(paths), on=False)
