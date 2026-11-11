"""Review queue, mark-uploaded, pause and resume (spec sections 10 and 18.10-18.11, component C7)."""

from __future__ import annotations

import errno
import json
import os
import shutil
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from ns_common.db import Db
from ns_common.paths import Paths
from ns_studio.channels.crud import edit_channel, get_channel
from ns_studio.errors import StudioError

_URL_HOSTS = ("youtube.com/", "youtu.be/")


def pending(db: Db, channel: str | None = None) -> list[sqlite3.Row]:
    """Outputs waiting for the owner's decision, oldest first."""
    if channel:
        return db.query(
            "select * from outputs where status='pending' and channel=? order by created_at, output_id",
            (channel,),
        )
    return db.query("select * from outputs where status='pending' order by created_at, output_id")


def get_output(db: Db, output_id: str) -> sqlite3.Row:
    """One `outputs` row; `NS-E326` when it does not exist."""
    row = db.one("select * from outputs where output_id=?", (output_id,))
    if row is None:
        raise StudioError("NS-E326", f"{output_id} is not in the outputs table")
    return row


def describe(paths: Paths, db: Db, output_id: str) -> list[str]:
    """Lines shown by `studio review show`: title, file, story text, policy notes, disclosure advice."""
    row = get_output(db, output_id)
    lines = [
        f"{row['output_id']}  [{row['status']}]  {row['title']}",
        f"file: {row['path']}",
    ]
    if "profile" in row.keys() and row["profile"]:
        lines.append(
            f"TEST OUTPUT ({row['profile']}): made on an engine-test install; it cannot be posted (NS-E329)"
        )
    job_dir = paths.job_dir(row["job_id"])
    story = job_dir / "story.json"
    if story.exists():
        text = json.loads(story.read_text(encoding="utf-8")).get("text", "")
        lines += ["", "story:", str(text)]
    for name in ("policy_text.json", "policy_final.json"):
        f = job_dir / name
        if not f.exists():
            continue
        data = json.loads(f.read_text(encoding="utf-8"))
        lines.append("")
        lines.append(f"policy ({data.get('stage')}): {'passed' if data.get('passed') else 'FAILED'}")
        for chk, res in data.get("checks", {}).items():
            lines.append(f"  {'ok  ' if res.get('passed') else 'FAIL'} {chk}: {res.get('detail', '')}")
        adv = data.get("advice") or {}
        if name == "policy_final.json" and adv:
            lines.append(
                f"disclosure advice: altered/synthetic content = {adv.get('ai_disclosure')}, "
                f"made for kids = {adv.get('made_for_kids')}, age restriction suggested = "
                f"{adv.get('age_restrict_suggested')}"
            )
    if row["review_note"]:
        lines.append(f"note: {row['review_note']}")
    return lines


def approve(db: Db, output_id: str, note: str = "") -> None:
    """pending -> approved."""
    row = get_output(db, output_id)
    if row["status"] != "pending":
        raise StudioError("NS-E326", f"{output_id} is {row['status']}, only pending outputs can be approved")
    db.execute(
        "update outputs set status='approved', review_note=? where output_id=?", (note or None, output_id)
    )


def _destination(paths: Paths, src: Path, row: sqlite3.Row) -> Path:
    try:
        rel = src.relative_to(paths.outbox)
        slug, day, job, *rest = rel.parts
        return paths.rejected_dir(slug, day, job).joinpath(*rest)
    except ValueError:
        today = f"{datetime.now(UTC):%Y-%m-%d}"
        return paths.rejected_dir(row["channel"], today, row["job_id"]) / src.name


#: A file another program holds open is tried this often, this far apart, before the move is given up (an
#: antivirus scan or the Explorer preview pane lets go within a moment; a player does not).
MOVE_TRIES = 5
MOVE_WAIT_S = 0.2


def move_one(src: Path, dst: Path) -> None:
    """Move one file, or leave it exactly where it was (D-57).

    `os.replace` is one step on one drive and never copies. `shutil.move`, used here before, answered ANY
    failed rename with copy-then-delete: a video held open by a player was copied whole into `rejected`, the
    delete then failed, and the video was in both places. Only across drives (the Outbox on another drive
    than the home) is the file copied here, under a `.part` name, and the copy is taken away again when the
    original cannot be removed. A file held open (PermissionError: WinError 32 or 5) is tried `MOVE_TRIES`
    times; every other failure is raised at once."""
    held: OSError | None = None
    for _ in range(MOVE_TRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:
            held = e
            time.sleep(MOVE_WAIT_S)
        except OSError as e:
            if e.errno != errno.EXDEV and getattr(e, "winerror", None) != _ERROR_NOT_SAME_DEVICE:
                raise
            _copy_then_remove(src, dst)
            return
    assert held is not None  # MOVE_TRIES >= 1
    raise held


_ERROR_NOT_SAME_DEVICE = 17  # WinError 17: "The system cannot move the file to a different disk drive"


def _copy_then_remove(src: Path, dst: Path) -> None:
    """The move across drives: a whole copy under `<name>.part`, renamed into place, then the original is
    removed. Whatever fails, exactly one complete file is left: the original (the copy is removed again)."""
    part = dst.with_name(dst.name + ".part")
    try:
        shutil.copy2(src, part)
        os.replace(part, dst)
    except OSError:
        part.unlink(missing_ok=True)
        raise
    held: OSError | None = None
    for _ in range(MOVE_TRIES):
        try:
            src.unlink()
            return
        except PermissionError as e:
            held = e
            time.sleep(MOVE_WAIT_S)
        except OSError as e:
            held = e
            break
    try:
        dst.unlink(missing_ok=True)
    except OSError:
        pass  # both files stay: the original is whole and still the row's path; the next Reject replaces dst
    assert held is not None
    raise held


def _move_together(pairs: list[tuple[Path, Path]], output_id: str) -> None:
    """Move every `(from, to)` pair, or none of them (D-57).

    The files of one output (the video, its sheet, its thumbnails) are rejected together. Before, they were
    moved one by one with nothing behind a failure: a video open in a player stopped the loop with its sheet
    and thumbnails already in `rejected`, the video still in the Outbox and the row unchanged. Now a failure
    puts back what was already moved and raises NS-E331 with the name of the file that is held; the row is
    not touched by the caller. A file that could not be put back either is named in the message (the next
    Reject moves the rest to it)."""
    moved: list[tuple[Path, Path]] = []
    try:
        for a, b in pairs:
            move_one(a, b)
            moved.append((a, b))
    except OSError as e:
        stuck: list[str] = []
        for a, b in reversed(moved):
            try:
                move_one(b, a)
            except OSError:
                stuck.append(b.name)
        held = pairs[len(moved)][0].name
        if stuck:
            state = (
                f"{', '.join(stuck)} could not be put back and stayed in the rejected folder: Reject again "
                "moves the rest there"
            )
        else:
            state = "nothing was moved"
        why = f"{output_id} was not rejected: {held} is held open ({e}); {state}"
        raise StudioError("NS-E331", why) from e


def reject(paths: Paths, db: Db, output_id: str, reason: str) -> Path:
    """Reject an output: record the reason and move its files (same stem, thumbnails) to `rejected\\`.

    All of its files move or none does (`_move_together`); the row is changed only after they moved."""
    if not reason.strip():
        raise StudioError("NS-E326", "a rejection needs a reason")
    row = get_output(db, output_id)
    if row["status"] not in ("pending", "approved"):
        raise StudioError("NS-E326", f"{output_id} is {row['status']} and cannot be rejected")
    src = Path(row["path"])
    new_path = src
    dest = _destination(paths, src, row)
    # `dest` there with `src` gone: an earlier Reject moved the video and could not put it back when a later
    # file was held (its message said so). The row still names `src`; the rest follows the video now.
    cut_short = not src.exists() and dest.is_file()
    if src.exists() or cut_short:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # the video first: it is the file a player or a browser tab holds open, and when it cannot be moved
        # nothing else has been
        siblings = [p for p in src.parent.glob(f"{src.stem}.*") if p.is_file() and p != src]
        if src.exists():
            siblings.insert(0, src)
        if row["kind"] == "long":
            siblings += [p for p in src.parent.glob("thumb_*.jpg") if p.is_file() and p not in siblings]
        _move_together([(p, dest.parent / p.name) for p in siblings], output_id)
        new_path = dest
    db.execute(
        "update outputs set status='rejected', review_note=?, path=? where output_id=?",
        (reason.strip(), str(new_path), output_id),
    )
    return new_path


def mark_uploaded(db: Db, job_id: str, item: str, url: str, now: datetime | None = None) -> str:
    """Record the public URL of an approved output (`studio mark-uploaded <job> <long|s<k>> <url>`)."""
    output_id = f"{job_id}/{item}"
    row = get_output(db, output_id)
    if not any(h in url for h in _URL_HOSTS):
        raise StudioError("NS-E326", "the URL must be a youtube.com or youtu.be link")
    if "profile" in row.keys() and row["profile"]:
        raise StudioError("NS-E329", f"{output_id} was made with the {row['profile']} profile")
    if row["status"] != "approved":
        raise StudioError("NS-E326", f"{output_id} is {row['status']}; approve it in 'studio review' first")
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    db.execute(
        "insert or replace into uploads(output_id, video_url, published_at) values (?,?,?)",
        (output_id, url.strip(), stamp),
    )
    db.execute("update outputs set status='uploaded' where output_id=?", (output_id,))
    return output_id


def open_in_player(path: Path) -> bool:
    """Open `path` in the default player (Windows only); False when that is not possible."""
    starter = getattr(os, "startfile", None)
    if starter is None or not path.exists():
        return False
    starter(str(path))
    return True


# ---------------------------------------------------------------- pause and resume (spec 18.11)
def pause(paths: Paths, db: Db, slug: str, reason: str, now: datetime | None = None) -> None:
    """Pause a channel and record an incident; nightly runs skip paused channels."""
    if not reason.strip():
        raise StudioError("NS-E326", "pausing needs a reason (the notice text)")
    get_channel(paths, slug)
    edit_channel(paths, db, slug, {"status": "paused"})
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    db.execute(
        "insert into incidents(channel, ts, kind, note) values (?,?,?,?)",
        (slug, stamp, "pause", reason.strip()),
    )


def last_outputs(db: Db, slug: str, n: int = 10) -> list[sqlite3.Row]:
    """The last `n` outputs of a channel, newest first (shown before `studio resume`)."""
    return db.query(
        "select output_id, title, status, review_note from outputs where channel=? "
        "order by created_at desc, rowid desc limit ?",
        (slug, n),
    )


def resume(paths: Paths, db: Db, slug: str, now: datetime | None = None) -> None:
    """Resume a paused channel and record the incident."""
    ch = get_channel(paths, slug)
    if ch.status != "paused":
        raise StudioError("NS-E326", f"channel {slug} is not paused")
    edit_channel(paths, db, slug, {"status": "active"})
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    db.execute(
        "insert into incidents(channel, ts, kind, note) values (?,?,?,?)",
        (slug, stamp, "resume", "owner reviewed the last 10 outputs"),
    )
