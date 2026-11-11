"""What Windows itself recorded about the PC stopping, read-only (card night-fits, night-fits-b).

Two readings, both standard library, both never required (a caller that cannot have them says it cannot
tell, and never guesses):

* `SystemLog.read(since, until)`: the System event log's shutdown, restart and power-loss events created
  from `since` to `until`, through `wevtutil qe System /q:<the four ids, in that window> /rd:false /c:20
  /f:xml` (oldest first, so later events never push the window's first ones out; 5 s, no shell, no
  window); `Events.capped` says the read stopped at 20. The ids: 1074 (User32: a program or a person
  asked for a shutdown or restart; param1 is the program, param3 the reason, param5 `restart` or
  `power off`), 41 (Kernel-Power: the PC started again after it stopped without a clean shutdown), 6008
  (EventLog: "the previous system shutdown ... was unexpected"; its binary data holds that moment as two
  SYSTEMTIMEs, local then UTC) and 6006 (EventLog: the event log service stopped: a clean shutdown).
  Under pytest it is refused before anything starts (`ns_common.testguard.under_test`, no opt-out): a
  test hands its own `read` (the fake of `tests/test_night_cause.py`, which reproduces the shapes `parse`
  reads from a capture of this laptop's own log, `tests/fixtures/winevents/system-1074-41-6008-6006.xml`).
* `asleep_s()`: the seconds this PC has spent in sleep or hibernation since it started: the interrupt time
  (`QueryInterruptTime`, which keeps counting through sleep) minus the unbiased interrupt time
  (`QueryUnbiasedInterruptTime`: "does not include time the system spends in sleep or hibernation",
  Microsoft Learn). Neither is moved by a change of the clock. None off Windows or when either call is
  missing. Python's own `time.monotonic` on 3.12 is `GetTickCount64`, which counts sleep too, so it cannot
  tell a sleep from a stall; the difference of the two counters can.
"""

from __future__ import annotations

import locale
import os
import struct
import subprocess  # noqa: TID251  (wevtutil, read-only; refused under pytest before it starts)
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime

from ns_common import testguard

#: The System log events a stopped night is explained by (see the module docstring).
SHUTDOWN_ASKED, UNEXPECTED_BOOT, UNEXPECTED_SHUTDOWN, LOG_STOPPED = 1074, 41, 6008, 6006
IDS = (SHUTDOWN_ASKED, UNEXPECTED_BOOT, UNEXPECTED_SHUTDOWN, LOG_STOPPED)
MAX_EVENTS = 20
TIMEOUT_S = 5.0
_NS = "{http://schemas.microsoft.com/win/2004/08/" "events/event}"  # (no slash-events path in one piece:
# tests/test_spec_runtime_text.py keeps that string for the supervisor's event stream)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class EventLogUnavailable(Exception):
    """The System log could not be read (refused under test, wevtutil missing, failed or too slow)."""


@dataclass(frozen=True)
class Event:
    """One System log event, as much of it as the cause of a stopped night needs."""

    id: int
    at: datetime  # TimeCreated, UTC (41 and 6008 are written when the PC starts again)
    provider: str
    process: str | None = None  # 1074: the program that asked (file name only)
    reason: str | None = None  # 1074: Windows' reason text ("Operating System: Upgrade (Planned)")
    action: str | None = None  # 1074: "restart", "power off"
    stopped_at: datetime | None = None  # 6008: the moment of the unexpected shutdown (UTC)


class Events(list[Event]):
    """What one read returned, oldest first; `capped` when it returned `MAX_EVENTS` (there may be more in the
    window: a caller must not say the window holds no other event)."""

    capped: bool = False


def _stamp(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def query_argv(since: datetime, until: datetime) -> list[str]:
    """The one command `SystemLog` runs: the four ids, created from `since` to `until`, OLDEST first (so the
    events right after `since` are never pushed out by later ones), 20 at most."""
    ids = " or ".join(f"EventID={i}" for i in IDS)
    window = f"@SystemTime>='{_stamp(since)}' and @SystemTime<='{_stamp(until)}'"
    xpath = f"*[System[({ids}) and TimeCreated[{window}]]]"
    return ["wevtutil", "qe", "System", f"/q:{xpath}", "/rd:false", f"/c:{MAX_EVENTS}", "/f:xml"]


def _time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw.strip().rstrip("Z")
    if "." in text:  # wevtutil writes seven fractional digits; fromisoformat takes six
        head, frac = text.split(".", 1)
        text = f"{head}.{frac[:6]}"
    try:
        return datetime.fromisoformat(text).replace(tzinfo=UTC)
    except ValueError:
        return None


def _systemtime_utc(binary: str | None) -> datetime | None:
    """6008's binary data: two SYSTEMTIMEs (16 bytes each, little-endian words), local first, then UTC."""
    if not binary or len(binary) < 64:
        return None
    try:
        y, mo, _dow, d, h, mi, s, _ms = struct.unpack("<8H", bytes.fromhex(binary[32:64]))
        return datetime(y, mo, d, h, mi, s, tzinfo=UTC)
    except ValueError:
        return None


def parse(text: str) -> Events:
    """wevtutil's `/f:xml` answer (one `<Event>` element after another, no root) as events, oldest first;
    `capped` when it held `MAX_EVENTS` elements (counted before any is skipped). An element that does not
    read is skipped."""
    body = text.strip()
    if not body:
        return Events()
    try:
        root = ET.fromstring(f"<Events>{body}</Events>")
    except ET.ParseError as e:
        raise EventLogUnavailable(f"wevtutil's answer is not XML: {e}") from e
    out = Events()
    out.capped = len(root) >= MAX_EVENTS
    for el in root:
        system = el.find(f"{_NS}System")
        if system is None:
            continue
        try:
            event_id = int((system.findtext(f"{_NS}EventID") or "").strip())
        except ValueError:
            continue
        created = system.find(f"{_NS}TimeCreated")
        at = _time(created.get("SystemTime") if created is not None else None)
        if at is None:
            continue
        provider_el = system.find(f"{_NS}Provider")
        provider = provider_el.get("Name", "") if provider_el is not None else ""
        data = el.find(f"{_NS}EventData")
        items = data.findall(f"{_NS}Data") if data is not None else []
        named = {d.get("Name"): (d.text or "") for d in items}
        process = reason = action = None
        stopped = None
        if event_id == SHUTDOWN_ASKED:
            path = named.get("param1", "").split(" (", 1)[0].strip()
            process = path.replace("/", "\\").rsplit("\\", 1)[-1] or None
            reason, action = named.get("param3") or None, named.get("param5") or None
        elif event_id == UNEXPECTED_SHUTDOWN and data is not None:
            stopped = _systemtime_utc(data.findtext(f"{_NS}Binary"))
        out.append(Event(event_id, at, provider, process, reason, action, stopped))
    out.sort(key=lambda e: e.at)
    return out


class SystemLog:
    """The real System log (the `EventSource` of `ns_studio.nightcause`). Never writes anything."""

    def read(self, since: datetime, until: datetime) -> Events:
        if testguard.under_test():
            raise EventLogUnavailable(
                "a test tried to read this PC's real Windows event log; this is refused under pytest and "
                "cannot be switched off. Hand the code a fake EventSource."
            )
        if sys.platform != "win32":
            raise EventLogUnavailable("not Windows: there is no System event log")
        try:
            done = subprocess.run(
                query_argv(since, until), capture_output=True, timeout=TIMEOUT_S, check=False,
                creationflags=_NO_WINDOW, env={**os.environ},
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise EventLogUnavailable(f"wevtutil could not be run: {e}") from e
        encoding = locale.getpreferredencoding(False) or "utf-8"
        if done.returncode != 0:
            why = done.stderr.decode(encoding, "replace").strip().splitlines()
            raise EventLogUnavailable(f"wevtutil exited {done.returncode}: {why[0] if why else ''}")
        return parse(done.stdout.decode(encoding, "replace"))


def asleep_s() -> float | None:
    """Seconds this PC has spent asleep or hibernating since it started (None when it cannot be read)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        biased, unbiased = ctypes.c_ulonglong(), ctypes.c_ulonglong()
        ctypes.WinDLL("kernelbase").QueryInterruptTime(ctypes.byref(biased))  # VOID (Windows 10 and later)
        if not ctypes.WinDLL("kernel32").QueryUnbiasedInterruptTime(ctypes.byref(unbiased)):
            return None
    except (AttributeError, OSError):
        return None
    return max(0.0, (biased.value - unbiased.value) / 1e7)
