"""A paused channel's queued and scheduled story jobs wait (P47).

Pausing a channel (the Channels switch, `studio pause`, the Assistant) used to stop only NEW work. A story job
already in the queue, or scheduled with Create > Pick a time, still started. The job worker now asks
`make_channel_hold` about every queued job before it starts one: for a story job of a paused channel the
answer is the plain reason, the job stays `queued` and shows the reason as its message, and it starts by
itself once the channel is resumed. The profile file is the truth (that is what `studio pause` writes),
read each time.

Held kinds are the story kinds: `run` (what `_run_params` already refuses for a paused channel) and
`series_part` (a Series Workshop part is a story video of the channel). A Clipper job is not held, because a
Clipper request for a paused channel is accepted on purpose (it cuts the owner's own video).
"""

from __future__ import annotations

from datetime import datetime

import yaml
from pydantic import ValidationError

from ns_common.config import load_channel
from ns_common.paths import Paths
from ns_dashboard.jobs import HELD_PREFIX, Hold, JobRecord
from ns_dashboard.state import LockedDb

HELD_KINDS = frozenset({"run", "series_part"})


def paused_since(db: LockedDb, slug: str) -> str | None:
    """The time of the channel's latest pause incident as local `Mon 09 Nov 21:30`, or None when unknown
    (a profile edited by hand has no incident)."""
    row = db.one(
        "SELECT ts FROM incidents WHERE channel = ? AND kind = 'pause' ORDER BY rowid DESC LIMIT 1", (slug,)
    )
    if row is None:
        return None
    try:
        when = datetime.fromisoformat(str(row["ts"]))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.astimezone()
    return f"{when.astimezone():%a %d %b %H:%M}"


def hold_text(display_name: str, since: str | None) -> str:
    """The message of a held job."""
    when = f" since {since}" if since else ""
    return f"{HELD_PREFIX}{display_name} is paused{when}; resume it to release this job"


def make_channel_hold(paths: Paths, db: LockedDb) -> Hold:
    """The `hold` of the job worker: why a queued story job must not start (its channel is paused)."""

    def hold(rec: JobRecord) -> str | None:
        if rec.kind not in HELD_KINDS or not rec.channel:
            return None
        try:
            ch = load_channel(paths, rec.channel)
        except (OSError, ValueError, ValidationError, yaml.YAMLError):
            return None  # no profile to ask: the run itself reports it, as before
        if ch.status != "paused":
            return None
        return hold_text(ch.display_name, paused_since(db, rec.channel))

    return hold
