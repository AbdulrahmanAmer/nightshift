"""The GPU kit's two nights (card gpu-kit): a record the product honours, so nothing the kit switched on
outlives its two nights even when the kit is never run again.

The kit (`dev/kit/gpukit.py`, step K4) writes `run/gpu-kit-nights.json` BEFORE it switches Automatic runs on:
what they were (`automatic_runs_before`), the run time it armed, and `until` (the second night's run time plus
the task's limit). Two places read it:

- the nightly run (`studio run --daily`, `expire_if_due`): a night that starts after `until`, with Automatic
  runs off before the kit and the run time still the armed one, is the kit's third night. It leaves a morning
  note, switches Automatic runs off as they were, removes the record and does no work. The nights before
  `until` run as usual.
- the Settings switch (`owner_changed`): the owner who switches Automatic runs on or off themselves decides;
  the record keeps the moment and neither the night nor the kit switches anything back.

Nothing here changes the kit's channel: the night never plans it (`cadence.KIT_CHANNEL`).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ns_common.atomic import write_text_atomic
from ns_common.logging import NsLogger
from ns_common.paths import Paths

RECORD = "gpu-kit-nights.json"
#: The field a request carries when the GPU kit itself moves the switch (not the owner).
BY_KIT = "gpu-kit"

EXPIRED_NOTE = (
    "The GPU kit had switched Automatic runs on for two nights. Those two nights are over, so Automatic runs "
    "are off again, as they were before the kit, and tonight's run made nothing. To collect what the two "
    "nights did, double-click GPU-KIT.cmd. To make videos every night, switch Automatic runs on in Settings."
)


def record_file(paths: Paths) -> Path:
    return paths.home / "run" / RECORD


def read(paths: Paths) -> dict[str, Any] | None:
    """The record, or None when there is none (or it cannot be read: then nothing is switched by it)."""
    try:
        data = json.loads(record_file(paths).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write(paths: Paths, record: dict[str, Any]) -> None:
    write_text_atomic(record_file(paths), json.dumps(record, indent=2, ensure_ascii=False) + "\n")


def remove(paths: Paths) -> None:
    record_file(paths).unlink(missing_ok=True)


def owner_changed(paths: Paths, now: datetime) -> None:
    """The owner moved the switch in Settings: from now on the switch is theirs (recorded, never undone)."""
    rec = read(paths)
    if rec is not None and not rec.get("owner_changed_at"):
        write(paths, {**rec, "owner_changed_at": now.isoformat(timespec="seconds")})


def _note_once(paths: Paths, message: str) -> object:
    """The morning note, said once while it waits (a night that retries the switch-off repeats none)."""
    from ns_supervisor.notify import leave_note

    return leave_note(paths, "Nightshift", message,
                      once=lambda notes: any(n["message"] == message for n in notes))


def _switch_off(paths: Paths) -> None:
    from ns_supervisor import schedule
    from ns_supervisor.control import app_path

    schedule.set_automatic(paths, app_path(paths), on=False)


def expire_if_due(
    paths: Paths,
    now: datetime,
    log: NsLogger | None = None,
    *,
    switch_off: Callable[[Paths], None] = _switch_off,
    leave_note: Callable[[Paths, str], object] | None = None,
) -> str | None:
    """Called at the start of a nightly run. The sentence to print when this night is the kit's third (it
    then does no work), else None (the night runs as usual)."""
    rec = read(paths)
    if rec is None:
        return None
    try:
        until = datetime.fromisoformat(str(rec["until"]))
    except (KeyError, ValueError):
        return None
    if now < until:
        return None
    from ns_supervisor.schedule_cfg import read_run_time

    on_now = read_run_time(paths)  # the time of the enabled task, None while Automatic runs are off
    if rec.get("automatic_runs_before") is not False or rec.get("owner_changed_at") or on_now is None or (
        rec.get("run_time") and on_now != rec.get("run_time")
    ):  # theirs before the kit, theirs since, already off, or the time moved by the owner: nothing to undo
        remove(paths)
        return None
    (leave_note or _note_once)(paths, EXPIRED_NOTE)  # first: a switch-off ending this process still leaves it
    write(paths, {**rec, "expired_at": now.isoformat(timespec="seconds")})
    try:
        switch_off(paths)
    except Exception as e:  # noqa: BLE001 - kept: the next night does no work either and tries again
        if log is not None:
            log.warn("gpu_kit_nights_switch_off_failed", str(e))
        return EXPIRED_NOTE
    remove(paths)
    if log is not None:
        log.info("gpu_kit_nights_expired", until=str(until))
    return EXPIRED_NOTE
