"""Review queue operations: list, detail, approve, reject, mark uploaded, media paths (component C9)."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from ns_common import ffmpeg
from ns_common.atomic import read_text_retry
from ns_common.errors import NsError, describe
from ns_dashboard import media
from ns_dashboard.common import OUTPUT_KINDS, OUTPUT_STATUSES, now_iso, output_item
from ns_dashboard.errors import AdapterUnavailable, ApiError
from ns_dashboard.security import is_within
from ns_dashboard.state import AppState
from ns_studio.errors import StudioError
from ns_studio.media.outbox import disclosure_for
from ns_studio.media.youtube_rules import read_notes

URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
EXCERPT_CHARS = 1500
WARNING_CODE_RE = re.compile(r"\bNS-W\d{3}\b")
#: An output row with the YouTube link of its upload (NULL until it is marked as posted).
OUTPUT_SELECT = (
    "SELECT o.*, u.video_url AS video_url FROM outputs o"
    " LEFT JOIN uploads u ON u.output_id = o.output_id"
)


def _read_json(f: Path) -> Any:
    try:
        return json.loads(read_text_retry(f))
    except (OSError, ValueError):
        return None


def _read_text(f: Path) -> str | None:
    try:
        return f.read_text(encoding="utf-8")
    except OSError:
        return None


def policy_notes(policy: Any) -> list[str]:
    """Plain-English lines for the Review page: problems, then warnings, then the checks that passed.

    A check that passed with a warning (its detail names an `NS-W...` code, like the stock-footage reuse
    NS-W405) is shown with its text instead of a bare OK.
    """
    if not isinstance(policy, dict) or not isinstance(policy.get("checks"), dict):
        return []
    problems: list[str] = []
    warned: list[str] = []
    passed: list[str] = []
    for name, chk in policy["checks"].items():
        label = str(name).replace("_", " ")
        detail = str(chk.get("detail", "")).strip() if isinstance(chk, dict) else ""
        if isinstance(chk, dict) and chk.get("passed") is False:
            problems.append(f"Problem: {label}" + (f" - {detail}" if detail else ""))
        elif WARNING_CODE_RE.search(detail):
            warned.append(f"Warning: {label} - {detail}")
        else:
            passed.append(f"OK: {label}")
    return [*problems, *warned, *passed]


def _same_file(a: Path, b: Path) -> bool:
    """True when both files have the same size and the same SHA-256 (size alone can match another file)."""
    if a.stat().st_size != b.stat().st_size:
        return False
    digests = []
    for f in (a, b):
        h = hashlib.sha256()
        with f.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        digests.append(h.digest())
    return digests[0] == digests[1]


class OutputsService:
    """Everything the Review and Clipper pages do with `outputs` rows."""

    def __init__(self, state: AppState) -> None:
        self.s = state

    # --- queries ---
    def counts(self) -> dict[str, int]:
        """Number of outputs per status (all four keys present)."""
        out: dict[str, int] = dict.fromkeys(OUTPUT_STATUSES, 0)
        for r in self.s.db.query("SELECT status, COUNT(*) AS n FROM outputs GROUP BY status"):
            out[r["status"]] = int(r["n"])
        return out

    def _row(self, output_id: str) -> sqlite3.Row:
        row = self.s.db.one(
            OUTPUT_SELECT + " WHERE o.output_id = ?",
            (output_id,),
        )
        if row is None:
            raise ApiError(404, "not_found", "That video was not found.")
        return row

    def list_outputs(
        self,
        status: str | None = None,
        kind: str | None = None,
        channel: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Filtered outputs, newest first (a bare list)."""
        statuses = [s.strip() for s in (status or "").split(",") if s.strip()]
        if statuses == ["all"]:
            statuses = []
        for s in statuses:
            if s not in OUTPUT_STATUSES:
                raise ApiError(422, "bad_filter", f"Unknown status '{s}'.")
        if kind and kind not in OUTPUT_KINDS:
            raise ApiError(422, "bad_filter", f"Unknown kind '{kind}'.")
        where: list[str] = []
        args: list[Any] = []
        if statuses:
            where.append(f"o.status IN ({','.join('?' for _ in statuses)})")
            args.extend(statuses)
        for col, val in (("kind", kind), ("channel", channel)):
            if val:
                where.append(f"o.{col} = ?")
                args.append(val)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self.s.db.query(
            OUTPUT_SELECT
            + f"{clause} ORDER BY o.created_at DESC, o.rowid DESC LIMIT ? OFFSET ?",
            [*args, max(1, min(int(limit), 500)), max(0, int(offset))],
        )
        return [output_item(r) for r in rows]

    def detail(self, output_id: str) -> dict[str, Any]:
        """Output item plus the upload sheet, policy notes and excerpts for the Review page."""
        row = self._row(output_id)
        item = output_item(row)
        video = Path(row["path"])
        sheet = _read_json(video.with_suffix(".json"))
        sheet_dict = sheet if isinstance(sheet, dict) else None
        job_dir = self.s.paths.job_dir(row["job_id"])
        policy = None
        for name in ("policy_final.json", "policy_text.json"):
            policy = _read_json(job_dir / name)
            if isinstance(policy, dict):
                break
            policy = None
        advice = policy.get("advice") if isinstance(policy, dict) else None
        if advice is None and sheet_dict is not None:
            advice = {
                k: sheet_dict[k]
                for k in ("ai_disclosure", "made_for_kids", "age_restrict_suggested")
                if k in sheet_dict
            } or None
        up = self.s.db.one("SELECT video_url, published_at FROM uploads WHERE output_id = ?", (output_id,))
        flat = sheet_dict or {}
        adv = advice or {}
        sheet_text = _read_text(video.with_suffix(".txt"))
        answer = adv.get("ai_disclosure", flat.get("ai_disclosure"))
        told = None
        if answer in ("yes", "no"):  # the answer for the voice the job spoke with, and why (rule R8)
            told = disclosure_for(self.s.paths, row["job_id"], row["channel"], row["kind"], str(answer))
        item.update(
            {
                "description": flat.get("description", ""),
                "tags": flat.get("tags", []),
                "hashtags": flat.get("hashtags", []),
                "publish_after": flat.get("publish_after"),
                "ai_disclosure": told.answer if told else answer,
                "ai_disclosure_reason": told.reason if told else None,
                "ai_disclosure_note": (told.note or None) if told else None,
                "youtube_notes": read_notes(sheet_text).owner_lines(),
                "made_for_kids": adv.get("made_for_kids", flat.get("made_for_kids")),
                "age_restrict_suggested": adv.get(
                    "age_restrict_suggested", flat.get("age_restrict_suggested")
                ),
                "chapters": flat.get("chapters"),
                "related_video_instruction": flat.get("related_video_instruction"),
                "thumbnails": flat.get("thumbnails"),
                "policy_notes": policy_notes(policy),
                "sheet": sheet_dict,
                "sheet_text": sheet_text,
                "policy": policy,
                "advice": advice,
                "story_excerpt": self._story_excerpt(row),
                "transcript_excerpt": self._transcript_excerpt(row, sheet_dict),
                "folder": str(video.parent),
                "upload": dict(up) if up else None,
                "file_exists": video.exists(),
            }
        )
        return item

    def _story_excerpt(self, row: sqlite3.Row) -> str | None:
        if row["kind"] == "clip":
            return None
        if row["kind"] == "short" and row["k"] is not None:
            sql = "SELECT text FROM parts WHERE job_id = ? AND n = ?"
            part = self.s.db.one(sql, (row["job_id"], row["k"]))
            if part:
                return str(part["text"])[:EXCERPT_CHARS]
        story = self.s.db.one("SELECT text FROM stories WHERE job_id = ?", (row["job_id"],))
        return str(story["text"])[:EXCERPT_CHARS] if story else None

    def _transcript_excerpt(self, row: sqlite3.Row, sheet: dict[str, Any] | None) -> str | None:
        if row["kind"] != "clip" or not row["source_id"]:
            return None
        src_dir = self.s.paths.home / "imports" / "clipper" / str(row["source_id"])
        data = _read_json(src_dir / "transcript.json")
        if not isinstance(data, dict):
            return None
        segs = [s for s in data.get("segments", []) if isinstance(s, dict)]
        start, end = (sheet or {}).get("start_s"), (sheet or {}).get("end_s")
        if isinstance(start, int | float) and isinstance(end, int | float):
            segs = [s for s in segs if s.get("end", 0) >= start and s.get("start", 0) <= end]
        text = " ".join(str(s.get("text", "")).strip() for s in segs[:40])
        return text[:EXCERPT_CHARS] or None

    # --- actions ---
    def approve(self, output_id: str) -> dict[str, Any]:
        """pending/rejected -> approved; a rejected video's files go back to the Outbox first."""
        with self.s.db.lock:  # as _move_to_rejected: a second approve waits, then sees the row already moved
            row = self._row(output_id)
            if row["status"] == "uploaded":
                raise ApiError(409, "already_uploaded", "This video is already marked as uploaded.")
            moved = self._move_back_to_outbox(output_id, row)  # raises NsError, the row stays rejected
            if moved is None:
                self.s.db.execute("UPDATE outputs SET status = 'approved' WHERE output_id = ?", (output_id,))
        self.s.log.info("output_approved", output_id=output_id, moved_to=str(moved or ""))
        return output_item(self._row(output_id))

    def reject(self, output_id: str, reason: str | None) -> dict[str, Any]:
        """Reject with a required reason; the note feeds the next run's critic bar."""
        text = (reason or "").strip()
        if not text:
            raise ApiError(400, "reason_required", "Please say why you are rejecting this video.")
        row = self._row(output_id)
        if row["status"] == "uploaded":
            raise ApiError(409, "already_uploaded", "This video is already marked as uploaded.")
        moved = None
        if row["status"] != "rejected":  # a second rejection only changes the note; the files are away
            moved = self._move_to_rejected(output_id, text[:500])
        if moved is None:
            self.s.db.execute(
                "UPDATE outputs SET status = 'rejected', review_note = ? WHERE output_id = ?",
                (text[:500], output_id),
            )
        self.s.log.info("output_rejected", output_id=output_id, reason=text[:200], moved_to=str(moved or ""))
        return output_item(self._row(output_id))

    def _move_to_rejected(self, output_id: str, reason: str) -> Path | None:
        """Take the video's files out of the Outbox, as `studio review reject` does (same code).

        They go to the `rejected` folder of the Nightshift home, under channel, day and job (the video, its
        sheet and thumbnails), and the row keeps the new path, so a rejected video is not left among the
        ones waiting to be posted. None when the files could not be moved (the row is then rejected in
        place and the failure is logged).
        """
        from ns_studio import review

        try:
            with self.s.db.lock:
                return review.reject(self.s.paths, self.s.db, output_id, reason)  # type: ignore[arg-type]
        except (OSError, StudioError) as e:
            self.s.log.warn("reject_move_failed", str(e), output_id=output_id)
            return None

    def _move_back_to_outbox(self, output_id: str, row: sqlite3.Row) -> Path | None:
        """The mirror of `_move_to_rejected`: put a rejected video's files back where they came from.

        The video, its sheet and thumbnails go from `<home>\\rejected\\<rel>` to `<home>\\outbox\\<rel>` (the
        same relative path the reject took them from), and the row is set to `approved` with the new path
        in one step. None when the row's file is not under `rejected` (nothing to move, so approving a
        pending or already approved video changes the status only). Running it again after a move that was
        cut short finishes the move. A video that is gone from `rejected` (and not already back in the
        Outbox), or an Outbox file that would be overwritten, is an `NsError` NS-E326 and nothing changes.
        """
        paths = self.s.paths
        src = Path(row["path"])
        if not is_within(paths.rejected, src) or src.resolve() == paths.rejected.resolve():
            return None
        dest = paths.outbox / src.resolve().relative_to(paths.rejected.resolve())
        if not src.is_file() and not dest.is_file():
            raise NsError(
                "NS-E326",
                f"the files of this video are no longer in the rejected folder ({src}), "
                "so they cannot be moved back to the Outbox",
            )
        siblings = [p for p in src.parent.glob(f"{src.stem}.*") if p.is_file()]
        if row["kind"] == "long":
            siblings += [p for p in src.parent.glob("thumb_*.jpg") if p.is_file()]
        for p in siblings:  # check everything first, so a conflict leaves all files where they are
            target = dest.parent / p.name
            if target.exists() and not _same_file(target, p):
                raise NsError(
                    "NS-E326",
                    f"{target} is already in the Outbox and is a different file, "
                    f"so {p.name} was not moved back",
                )
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            for p in siblings:
                target = dest.parent / p.name
                if target.exists():  # the same file is already back (an earlier move was cut short)
                    p.unlink()
                else:
                    shutil.move(str(p), str(target))
            self.s.db.execute(
                "UPDATE outputs SET status = 'approved', path = ? WHERE output_id = ?",
                (str(dest), output_id),
            )
        except OSError as e:
            raise NsError("NS-E326", f"the files could not be moved back to the Outbox: {e}") from e
        parent = src.parent  # drop the folders the reject made, now they are empty
        base = paths.rejected.resolve()
        while parent.resolve() != base and base in parent.resolve().parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return dest

    def mark_uploaded(self, output_id: str, url: str | None) -> dict[str, Any]:
        """Record the YouTube URL (so analytics can be matched later) and set status `uploaded`."""
        link = (url or "").strip()
        if not URL_RE.match(link):
            raise ApiError(400, "bad_url", "Paste the full YouTube link, starting with https://")
        row = self._row(output_id)
        if "profile" in row.keys() and row["profile"]:
            known = describe("NS-E329")
            raise ApiError(409, "NS-E329", known.message, hint=known.hint)
        if row["status"] == "rejected":
            raise ApiError(409, "rejected", "Approve this video before marking it as uploaded.")
        with self.s.db.tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO uploads(output_id, video_url, published_at) VALUES (?, ?, ?)",
                (output_id, link, now_iso()),
            )
            db.execute("UPDATE outputs SET status = 'uploaded' WHERE output_id = ?", (output_id,))
        self.s.log.info("output_uploaded", output_id=output_id)
        return output_item(self._row(output_id))

    def open_folder(self, output_id: str) -> dict[str, Any]:
        """Open the output's folder in Explorer."""
        folder = Path(self._row(output_id)["path"]).parent
        if not folder.exists():
            raise ApiError(404, "folder_missing", "The folder for this video no longer exists.")
        try:
            self.s.adapters.folder.open(folder)
        except AdapterUnavailable as e:
            raise ApiError(501, "open_folder_unavailable", str(e)) from e
        return {"ok": True, "folder": str(folder)}

    # --- media ---
    def media_path(self, output_id: str) -> Path:
        """File to stream; 404 when missing and 403 when it lies outside the Nightshift home."""
        path = Path(self._row(output_id)["path"])
        if not is_within(self.s.paths.home, path):
            raise ApiError(403, "forbidden", "That file is outside the Nightshift folder.")
        if not path.is_file():
            raise ApiError(404, "file_missing", "The video file is missing from disk.")
        return path

    def thumb_choice_path(self, output_id: str, n: int) -> Path:
        """`thumb_<n>.jpg` (1 to 3) that the studio put next to a long video in the Outbox."""
        if n not in (1, 2, 3):
            raise ApiError(404, "not_found", "There are three thumbnails, numbered 1 to 3.")
        video = self.media_path(output_id)
        path = video.parent / f"thumb_{n}.jpg"
        if not path.is_file():
            raise ApiError(404, "file_missing", "That thumbnail was not made for this video.")
        return path

    def thumb_path(self, output_id: str) -> Path:
        """Cached first-frame JPEG (generated on first request)."""
        video = self.media_path(output_id)
        if not ffmpeg.available():
            raise ApiError(503, "ffmpeg_missing", "FFmpeg is not installed, so thumbnails are unavailable.")
        return media.thumbnail(self.s.paths, output_id, video, self.s.log)
