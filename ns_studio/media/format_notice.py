"""One-time notice about owner-edited formats that now shape videos (Head decision D2, package P10a).

Until this update no render read the Short, Full video or Square presets, so an owner's edited copy changed
nothing. From now on a format the owner edited or added applies as written. On the first start after the
update (the first job a studio process starts) one morning note names the formats that differ from the
shipped ones and what changes. The note is the Queue notice: the dashboard's Queue page shows the morning
notes (`run/morning_note.json`, `ns_supervisor.notify`). A marker file keeps it to once per kind of format.

Pure helpers (`describe_differences`, `owner_format_changes`) build the text; `notify_owner_formats`
writes it.
"""

from __future__ import annotations

import contextlib
import importlib
import json
from pathlib import Path
from typing import Any

from ns_common.atomic import read_text_retry, write_text_atomic
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_studio.media import formats as F

NOTICE_FILE = "formats_notice.json"
#: Kinds whose renders draw from their format. A later package that makes another kind real adds it here, and
#: the owner gets one more note for it.
NOTICE_KINDS: tuple[str, ...] = ("short", "square")
TITLE = "Nightshift"


def marker_path(paths: Paths) -> Path:
    """`run/formats_notice.json`: which kinds the owner has been told about."""
    return paths.home / "run" / NOTICE_FILE


def _get(raw: dict[str, Any], dotted: str) -> Any:
    cur: Any = raw
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def describe_differences(mine: dict[str, Any], shipped: dict[str, Any] | None) -> list[str]:
    """Plain sentences for what an owner's format file says that the shipped one does not.

    `shipped` None means a format of the owner's own: nothing to compare, it is simply usable now.
    """
    if shipped is None:
        return []
    out: list[str] = []

    def differs(dotted: str) -> bool:
        return _get(mine, dotted) != _get(shipped, dotted) and _get(mine, dotted) is not None

    if differs("canvas.w") or differs("canvas.h"):
        out.append(f"the frame is {_get(mine, 'canvas.w')}x{_get(mine, 'canvas.h')}")
    if _get(mine, "layout") != _get(shipped, "layout"):
        out.append("the picture is laid out as the format says")
    if differs("captions.anchor_y_pct"):
        out.append(f"captions are centred at {_get(mine, 'captions.anchor_y_pct'):g} % of the height")
    if differs("captions.words_on_screen"):
        out.append(f"at most {_get(mine, 'captions.words_on_screen')} words on screen at once")
    if differs("captions.styles_allowed"):
        out.append(f"caption styles are limited to {', '.join(_get(mine, 'captions.styles_allowed') or [])}")
    if differs("safe_margins.top") or differs("safe_margins.bottom"):
        out.append(
            f"captions keep {_get(mine, 'safe_margins.top') or 0} px from the top and "
            f"{_get(mine, 'safe_margins.bottom') or 0} px from the bottom"
        )
    if differs("shots.min_s") or differs("shots.max_s"):
        out.append(f"shots last {_get(mine, 'shots.min_s')} to {_get(mine, 'shots.max_s')} s")
    if differs("canvas.fps"):
        out.append(f"{_get(mine, 'canvas.fps')} frames per second")
    if differs("encoder.preset"):
        out.append(f"encoder preset {_get(mine, 'encoder.preset')}")
    return out


def owner_format_changes(
    paths: Paths, kinds: tuple[str, ...] = NOTICE_KINDS
) -> list[tuple[str, str, list[str]]]:
    """[(format id, `edited` or `yours`, what changes)] for the owner's formats of the given kinds."""
    shipped = F._load_dir(str(F.FORMATS_DIR))
    mine = F._load_dir(str(paths.formats_dir))
    out: list[tuple[str, str, list[str]]] = []
    for fid, fmt in sorted(mine.formats.items()):
        if fmt.kind not in kinds:
            continue
        if fid in shipped.formats:
            changes = describe_differences(mine.raw[fid], shipped.raw[fid])
            if changes:
                out.append((fid, "edited", changes))
        else:
            out.append((fid, "yours", []))
    return out


def notice_text(changes: list[tuple[str, str, list[str]]]) -> str:
    """The morning note: every format that differs and what will now change."""
    parts: list[str] = []
    for fid, how, what in changes:
        if how == "edited":
            parts.append(f"{fid} (edited): {'; '.join(what)}")
        else:
            parts.append(f"{fid} (yours): used when a channel lists it first in its allowed formats")
    return (
        "Formats you edited or added in Library > Formats now shape your videos, as written. "
        + " | ".join(parts)
        + ". Open Library > Formats to change them."
    )


def _read_marker(paths: Paths) -> dict[str, Any] | None:
    try:
        data = json.loads(read_text_retry(marker_path(paths)))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _leave_note(paths: Paths, message: str) -> bool:
    try:
        notify = importlib.import_module("ns_supervisor.notify")
        notify.leave_note(paths, TITLE, message)
    except (ImportError, AttributeError, OSError):
        return False
    return True


def notify_owner_formats(paths: Paths, log: NsLogger | None = None) -> bool:
    """Leave the one-time note when the owner has formats that now apply; True when a note was written.

    The marker lists the kinds already covered, so a later package that makes another kind real notes it
    once more. A first start with nothing to say still records the marker; one whose note could not be
    written (the morning note file is locked) does not, so the next job tries again. Never raises: a notice
    must not stop a job.
    """
    try:
        marker = _read_marker(paths)
        done = [str(k) for k in (marker or {}).get("kinds", [])]
        todo = tuple(k for k in NOTICE_KINDS if k not in done)
        if not todo:
            return False
        changes = owner_format_changes(paths, todo)
        wrote = False
        if changes:
            wrote = _leave_note(paths, notice_text(changes))
            if log is not None:
                log.info(
                    "format_notice",
                    f"{len(changes)} owner formats noted",
                    formats=[c[0] for c in changes],
                    note_written=wrote,
                )
        if changes and not wrote:
            return False  # the note could not be left (a locked file): no marker, the next job tries again
        target = marker_path(paths)
        target.parent.mkdir(parents=True, exist_ok=True)
        kinds = sorted({*done, *todo})
        write_text_atomic(target, json.dumps({"version": 1, "kinds": kinds}) + "\n")
        return wrote
    except Exception as e:  # a notice must never fail a job
        if log is not None:
            with contextlib.suppress(Exception):
                log.warn("format_notice_failed", str(e)[:200])
        return False
