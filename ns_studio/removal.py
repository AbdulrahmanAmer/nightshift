"""Remove what the owner does not want: a voice he made, an idea, a story, a cached footage clip (G-36, G-38).

One code for the Dashboard routes and the `studio remove` / `studio voice remove` commands. A removal is
never a silent delete:

* everything that goes is MOVED into the home's `removed\\<kind>\\<stamp>-<name>\\` folder, and a
  `record.json` there says what it was: the database rows (as they were), where each file or folder went
  from and to, the night-ledger lines of a story, and when. Putting a thing back is a file move plus the rows
  of the record; nothing here automates that.
* the order is: look (refuse what must not go), write the record, move the files, delete the rows in ONE
  transaction, then take the story's ledger lines out. A move that fails puts back what it had moved and
  removes the record; a failing transaction does the same. So after any failure the thing is whole where it
  was, and after a success it is wholly in `removed`.
* a refusal is a `RemovalError` whose `reason` says why: `not_found`, `in_use` (a voice a channel speaks with,
  a story that is being made right now), `busy` (a file held open by another program), `invalid` (a name that
  cannot be one), `failed` (a disk error).

What stays: an idea that produced a removed story (its `job_id` is only a note), the footage clips a removed
story used (they are cached footage of their own; remove them one by one), and the voice's preview cache.
"""

from __future__ import annotations

import contextlib
import errno
import glob
import json
import os
import re
import shutil
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ns_common.atomic import write_text_atomic
from ns_common.models import VOICE_NAME_PATTERN, VOICE_NAME_RULE
from ns_common.paths import Paths
from ns_studio import ledger, ownership
from ns_studio.errors import StudioError

REMOVED = "removed"
_NAME_RE = re.compile(VOICE_NAME_PATTERN)
_WIN_NOT_SAME_DEVICE = 17
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class RemovalError(StudioError):
    """A removal that was refused or did not complete; nothing was changed (see the module note)."""

    def __init__(self, reason: str, message: str, hint: str = "") -> None:
        super().__init__("NS-E326")
        self.reason = reason
        self.message = message
        self.hint = hint
        self.args = (str(self),)

    def __str__(self) -> str:
        return self.message


def removed_root(paths: Paths) -> Path:
    """`<home>\\removed`: where removed things wait, with their records."""
    return paths.home / REMOVED


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%d-%H%M%S")


def _slot(paths: Paths, kind: str, name: str, now: datetime | None) -> Path:
    """A new folder `removed/<kind>/<stamp>-<name>` (a `-2`, `-3` ... when it is taken); created."""
    safe = _UNSAFE.sub("_", name).strip("._") or "item"
    base = removed_root(paths) / kind
    n = 1
    while True:
        suffix = "" if n == 1 else f"-{n}"
        slot = base / f"{_stamp(now)}-{safe[:60]}{suffix}"
        try:
            slot.mkdir(parents=True, exist_ok=False)
            return slot
        except FileExistsError:
            n += 1
        except OSError as e:
            raise RemovalError("failed", f"the folder for removed things could not be made ({e})") from e


def _row(row: Mapping[str, Any] | Any) -> dict[str, Any]:
    """A database row as plain JSON: blobs (embeddings) are left out, the record does not need them."""
    d = dict(row) if isinstance(row, Mapping) else {k: row[k] for k in row.keys()}
    return {k: v for k, v in d.items() if not isinstance(v, bytes | bytearray | memoryview)}


def _rows(db: Any, sql: str, args: Sequence[Any] = ()) -> list[dict[str, Any]]:
    return [_row(r) for r in db.query(sql, args)]


@contextmanager
def _tx(db: Any) -> Iterator[Any]:
    """One transaction on a `LockedDb` (its own `tx`) or a plain `ns_common.db.Db`."""
    if hasattr(db, "tx"):
        with db.tx() as inner:
            yield inner
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


def _put(src: Path, dst: Path) -> None:
    """Move one file or folder to `dst` (made if need be) in one step; across drives a copy then a delete."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
    except PermissionError as e:
        raise RemovalError(
            "busy",
            f"{src.name} is held open by another program ({e})",
            "Close whatever has it open (a video player, the File Explorer preview pane, a virus scan) and "
            "try again. Nothing was changed.",
        ) from e
    except OSError as e:
        if e.errno != errno.EXDEV and getattr(e, "winerror", None) != _WIN_NOT_SAME_DEVICE:
            raise RemovalError("failed", f"{src} could not be moved ({e})") from e
        try:
            shutil.move(str(src), str(dst))
        except (OSError, shutil.Error) as e2:
            raise RemovalError("failed", f"{src} could not be moved to another drive ({e2})") from e2


class _Mover:
    """Moves things into one slot and can put every one of them back."""

    def __init__(self, slot: Path) -> None:
        self.slot = slot
        self.done: list[tuple[Path, Path]] = []

    def take(self, src: Path, label: str) -> None:
        if not src.exists():
            return
        dst = self.slot / "files" / label
        n = 2
        while dst.exists():
            dst = self.slot / "files" / f"{label}-{n}"
            n += 1
        _put(src, dst)
        self.done.append((src, dst))

    def undo(self) -> None:
        for src, dst in reversed(self.done):
            with contextlib.suppress(RemovalError, OSError):
                _put(dst, src)
        self.done.clear()

    def moved(self) -> list[dict[str, str]]:
        return [{"from": str(a), "to": str(b)} for a, b in self.done]


def _dump(record: dict[str, Any]) -> str:
    return json.dumps(record, indent=2, ensure_ascii=False, default=str) + "\n"


def _write_record(slot: Path, record: dict[str, Any]) -> None:
    try:
        write_text_atomic(slot / "record.json", _dump(record))
    except OSError as e:
        raise RemovalError("failed", f"the record of the removal could not be written ({e})") from e


def _abandon(slot: Path) -> None:
    """The removal did not happen: its record folder goes (what it held was put back by the caller)."""
    shutil.rmtree(slot, ignore_errors=True)


def _remove_with_record(
    paths: Paths,
    db: Any,
    kind: str,
    name: str,
    record: dict[str, Any],
    take_files: Callable[[_Mover], None],
    delete_rows: Callable[[Any], None],
    now: datetime | None,
) -> dict[str, Any]:
    """The common sequence: record, files, rows in one transaction; undone as a whole when a step fails."""
    when = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
    slot = _slot(paths, kind, name, now)
    mover = _Mover(slot)
    record = {"kind": kind, "id": name, "removed_at": when, "state": "started", **record}
    try:
        _write_record(slot, record)
        take_files(mover)
        record["moved"] = mover.moved()
        record["state"] = "moved"
        _write_record(slot, record)  # before the rows go: from here on the record says where everything is
        if db is not None:
            with _tx(db) as tx:
                delete_rows(tx)
    except RemovalError:
        mover.undo()
        _abandon(slot)
        raise
    except Exception as e:  # a database error, a disk error: whole again, then said
        mover.undo()
        _abandon(slot)
        raise RemovalError("failed", f"{kind} {name} was not removed: {type(e).__name__}: {e}") from e
    record["state"] = "removed"  # the rows are gone; a record that cannot be updated still says "moved"
    with contextlib.suppress(RemovalError):
        _write_record(slot, record)
    return {"ok": True, "kind": kind, "id": name, "record": str(slot / "record.json"), "folder": str(slot)}


# ---------------------------------------------------------------- voices
def voice_users(paths: Paths, name: str) -> list[str]:
    """The channels (by display name) whose profile names `name` as their designed or cloned voice."""
    from ns_common.config import list_channels, load_channel

    out: list[str] = []
    for slug in list_channels(paths):
        with contextlib.suppress(Exception):  # an unreadable profile uses no voice
            ch = load_channel(paths, slug)
            if ch.voice.mode != "preset" and ch.voice.name == name:
                out.append(ch.display_name)
    return out


def remove_voice(paths: Paths, name: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Move a designed or cloned voice (`models/tts/voices/<name>`) to `removed/voices`.

    Refused: a name that is not a voice name, a built-in preset, a voice that does not exist, and a voice
    that a channel's profile still names (`in_use`, the channels are named in the message)."""
    from ns_tts.voices import PRESETS

    name = str(name or "").strip()
    if not _NAME_RE.fullmatch(name):
        raise RemovalError("invalid", f"'{name}' is not a voice name: {VOICE_NAME_RULE}.")
    if name.lower() in {p.lower() for p in PRESETS}:
        raise RemovalError("invalid", f"{name} is a built-in voice; only voices you made can be removed.")
    root = paths.models_tts_voices
    folder = root / name
    try:
        known = {f.name: f for f in root.iterdir() if f.is_dir()} if root.is_dir() else {}
    except OSError as e:
        raise RemovalError("failed", f"the voice folder cannot be read ({e})") from e
    match = next((f for n, f in known.items() if n.lower() == name.lower()), None)
    if match is None:
        raise RemovalError("not_found", f"There is no voice called '{name}'.")
    folder = match
    users = voice_users(paths, folder.name)
    if users:
        raise RemovalError(
            "in_use",
            f"The voice {folder.name} is used by {', '.join(users)}.",
            "Give those channels another voice (Dashboard > Channels), then remove it.",
        )
    meta: Any = None
    with contextlib.suppress(OSError, ValueError):
        meta = json.loads((folder / "voice.json").read_text(encoding="utf-8"))
    return _remove_with_record(
        paths, None, "voices", folder.name,
        {"voice": meta, "folder": str(folder)},
        lambda m: m.take(folder, folder.name),
        lambda tx: None,
        now,
    )


# ---------------------------------------------------------------- ideas
def remove_idea(paths: Paths, db: Any, idea_id: int, *, now: datetime | None = None) -> dict[str, Any]:
    """Move an idea out of the idea bank: its row goes into the record. A story it produced is not touched."""
    rows = _rows(db, "SELECT * FROM ideas WHERE idea_id = ?", (idea_id,))
    if not rows:
        raise RemovalError("not_found", "That idea was not found.")
    return _remove_with_record(
        paths, db, "ideas", f"idea-{idea_id}",
        {"rows": {"ideas": rows}},
        lambda m: None,
        lambda tx: tx.execute("DELETE FROM ideas WHERE idea_id = ?", (idea_id,)),
        now,
    )


# ---------------------------------------------------------------- footage
def remove_footage(
    paths: Paths, db: Any, source: str, clip_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """Move a cached stock clip out: its `clips` row and the `clip_usage` rows go into the record, and its
    file moves to `removed/footage` (only a file inside the Nightshift home; any other path is left alone)."""
    rows = _rows(db, "SELECT * FROM clips WHERE source = ? AND clip_id = ?", (source, clip_id))
    if not rows:
        raise RemovalError("not_found", "That clip is not in the cache.")
    usage = _rows(db, "SELECT * FROM clip_usage WHERE source = ? AND clip_id = ?", (source, clip_id))
    local = rows[0].get("local_path")
    file = Path(str(local)) if local else None
    inside = False
    if file is not None:
        with contextlib.suppress(OSError):
            home = paths.home.resolve()
            inside = home in file.resolve().parents and file.is_file()

    def take(m: _Mover) -> None:
        if inside and file is not None:
            m.take(file, file.name)

    def delete(tx: Any) -> None:
        tx.execute("DELETE FROM clip_usage WHERE source = ? AND clip_id = ?", (source, clip_id))
        tx.execute("DELETE FROM clips WHERE source = ? AND clip_id = ?", (source, clip_id))

    out = _remove_with_record(
        paths, db, "footage", f"{source}-{clip_id}",
        {"rows": {"clips": rows, "clip_usage": usage}, "file_inside_home": inside},
        take, delete, now,
    )
    out["file_removed"] = inside
    out.update({"source": source, "clip_id": clip_id})
    return out


# ---------------------------------------------------------------- stories
def _story_folders(paths: Paths, job_id: str, outputs: list[dict[str, Any]]) -> tuple[list[Path], list[Path]]:
    """(whole folders, single video files) that belong to the story's outputs.

    A whole folder is an Outbox story folder whose marker holds this job's id. Every other output gets its own
    files (the stem's sheet and thumbnails beside the video), taken one by one."""
    from ns_studio.media.outbox import MARKER

    outbox = paths.outbox.resolve()
    folders: list[Path] = []
    loose: list[Path] = []
    for out in outputs:
        video = Path(str(out["path"]))
        found: Path | None = None
        with contextlib.suppress(OSError, ValueError):
            for parent in video.resolve().parents:
                if parent == outbox or outbox not in parent.parents:
                    break
                marker = parent / MARKER
                if marker.is_file() and marker.read_text(encoding="utf-8").strip() == job_id:
                    found = parent
                    break
        if found is not None:
            if found not in folders:
                folders.append(found)
        elif video.parent.is_dir():
            for p in sorted(video.parent.glob(f"{glob.escape(video.stem)}.*")):
                if p.is_file() and p not in loose:
                    loose.append(p)
            if out.get("kind") == "long":
                loose += [p for p in video.parent.glob("thumb_*.jpg") if p.is_file() and p not in loose]
    return folders, loose


def _prune_parents(paths: Paths, start: Path) -> None:
    """Drop the empty folders a removal left in the Outbox and `rejected` (never the channel's own folder)."""
    for root in (paths.outbox, paths.rejected):
        try:
            base, here = root.resolve(), start.resolve()
        except OSError:
            continue
        while here != base and here.parent != base and base in here.parents:
            try:
                here.rmdir()
            except OSError:
                break
            here = here.parent


def remove_story(paths: Paths, db: Any, job_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Move a story out of the Library: its rows (story, parts, videos, uploads, analytics, combos and the
    clip use of that job), its Outbox and `rejected` files, its job folder, its owner record and its ledger
    lines. Refused while the job is being made."""
    job_id = str(job_id or "").strip()
    if not job_id or re.search(r"[\\/]|\.\.", job_id):
        raise RemovalError("invalid", "That is not a story id.")
    stories = _rows(db, "SELECT * FROM stories WHERE job_id = ?", (job_id,))
    outputs = _rows(db, "SELECT * FROM outputs WHERE job_id = ?", (job_id,))
    if not stories and not outputs:
        raise RemovalError("not_found", "That story was not found.")
    if ownership.liveness(paths, job_id) == "live":
        raise RemovalError(
            "in_use",
            f"The story {job_id} is being made right now.",
            "Cancel it in the Queue, then remove it.",
        )
    ids = [str(o["output_id"]) for o in outputs]
    marks = ",".join("?" for _ in ids)
    tables: dict[str, list[dict[str, Any]]] = {
        "stories": stories,
        "parts": _rows(db, "SELECT * FROM parts WHERE job_id = ?", (job_id,)),
        "outputs": outputs,
        "uploads": _rows(db, f"SELECT * FROM uploads WHERE output_id IN ({marks})", ids) if ids else [],
        "analytics": _rows(db, f"SELECT * FROM analytics WHERE output_id IN ({marks})", ids) if ids else [],
        "combos": _rows(db, "SELECT * FROM combos WHERE job_id = ?", (job_id,)),
        "clip_usage": _rows(db, "SELECT * FROM clip_usage WHERE job_id = ?", (job_id,)),
    }
    folders, loose = _story_folders(paths, job_id, outputs)
    pattern = f"*/*/{glob.escape(job_id)}"
    rejected_dirs = sorted(paths.rejected.glob(pattern)) if paths.rejected.is_dir() else []
    job_dir = paths.job_dir(job_id)

    def take(m: _Mover) -> None:
        for i, f in enumerate(folders, 1):
            m.take(f, f"outbox-story-{i}")
        for i, d in enumerate(rejected_dirs, 1):
            m.take(d, f"rejected-{i}")
        for i, p in enumerate(loose, 1):
            m.take(p, f"video-{i}-{p.name}")
        m.take(job_dir, "job-folder")
        m.take(ownership.record_path(paths, job_id), "owner-record.json")
        m.take(ownership.lock_path(paths, job_id), "owner.lock")

    def delete(tx: Any) -> None:
        if ids:
            tx.execute(f"DELETE FROM analytics WHERE output_id IN ({marks})", ids)
            tx.execute(f"DELETE FROM uploads WHERE output_id IN ({marks})", ids)
        tx.execute("DELETE FROM outputs WHERE job_id = ?", (job_id,))
        tx.execute("DELETE FROM parts WHERE job_id = ?", (job_id,))
        tx.execute("DELETE FROM combos WHERE job_id = ?", (job_id,))
        tx.execute("DELETE FROM clip_usage WHERE job_id = ?", (job_id,))
        tx.execute("DELETE FROM stories WHERE job_id = ?", (job_id,))

    out = _remove_with_record(
        paths, db, "stories", job_id,
        {"rows": tables, "channel": (stories or outputs)[0].get("channel")},
        take, delete, now,
    )
    for p in [*folders, *rejected_dirs, *loose]:
        _prune_parents(paths, p.parent)
    ledger_taken = ledger.take_job(paths, job_id)
    if ledger_taken:  # the record is whole already; the ledger lines are added to it
        rec_file = Path(out["record"])
        with contextlib.suppress(OSError, ValueError):
            rec = json.loads(rec_file.read_text(encoding="utf-8"))
            rec["ledger_lines"] = ledger_taken
            write_text_atomic(rec_file, _dump(rec))
    out["ledger_lines"] = len(ledger_taken)
    out["videos"] = len(outputs)
    return out
