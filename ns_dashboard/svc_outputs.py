"""Review queue operations: list, detail, approve, reject, mark uploaded, media paths (component C9)."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from ns_common import ffmpeg
from ns_dashboard import media
from ns_dashboard.common import OUTPUT_KINDS, OUTPUT_STATUSES, now_iso, output_item
from ns_dashboard.errors import AdapterUnavailable, ApiError
from ns_dashboard.security import is_within
from ns_dashboard.state import AppState

URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
EXCERPT_CHARS = 1500


def _read_json(f: Path) -> Any:
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_text(f: Path) -> str | None:
    try:
        return f.read_text(encoding="utf-8")
    except OSError:
        return None


def policy_notes(policy: Any) -> list[str]:
    """Plain-English lines for the Review page: problems first, then the checks that passed."""
    if not isinstance(policy, dict) or not isinstance(policy.get("checks"), dict):
        return []
    problems: list[str] = []
    passed: list[str] = []
    for name, chk in policy["checks"].items():
        label = str(name).replace("_", " ")
        detail = str(chk.get("detail", "")).strip() if isinstance(chk, dict) else ""
        if isinstance(chk, dict) and chk.get("passed") is False:
            problems.append(f"Problem: {label}" + (f" - {detail}" if detail else ""))
        else:
            passed.append(f"OK: {label}")
    return [*problems, *passed]


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
        row = self.s.db.one("SELECT * FROM outputs WHERE output_id = ?", (output_id,))
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
        if status in ("", "all"):
            status = None
        if status and status not in OUTPUT_STATUSES:
            raise ApiError(422, "bad_filter", f"Unknown status '{status}'.")
        if kind and kind not in OUTPUT_KINDS:
            raise ApiError(422, "bad_filter", f"Unknown kind '{kind}'.")
        where, args = [], []
        for col, val in (("status", status), ("kind", kind), ("channel", channel)):
            if val:
                where.append(f"{col} = ?")
                args.append(val)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self.s.db.query(
            f"SELECT * FROM outputs{clause} ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
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
        item.update(
            {
                "description": flat.get("description", ""),
                "tags": flat.get("tags", []),
                "hashtags": flat.get("hashtags", []),
                "publish_after": flat.get("publish_after"),
                "ai_disclosure": adv.get("ai_disclosure", flat.get("ai_disclosure")),
                "made_for_kids": adv.get("made_for_kids", flat.get("made_for_kids")),
                "age_restrict_suggested": adv.get(
                    "age_restrict_suggested", flat.get("age_restrict_suggested")
                ),
                "chapters": flat.get("chapters"),
                "related_video_instruction": flat.get("related_video_instruction"),
                "thumbnails": flat.get("thumbnails"),
                "policy_notes": policy_notes(policy),
                "sheet": sheet_dict,
                "sheet_text": _read_text(video.with_suffix(".txt")),
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
        """pending/rejected -> approved."""
        row = self._row(output_id)
        if row["status"] == "uploaded":
            raise ApiError(409, "already_uploaded", "This video is already marked as uploaded.")
        self.s.db.execute("UPDATE outputs SET status = 'approved' WHERE output_id = ?", (output_id,))
        self.s.log.info("output_approved", output_id=output_id)
        return output_item(self._row(output_id))

    def reject(self, output_id: str, reason: str | None) -> dict[str, Any]:
        """Reject with a required reason; the note feeds the next run's critic bar."""
        text = (reason or "").strip()
        if not text:
            raise ApiError(400, "reason_required", "Please say why you are rejecting this video.")
        row = self._row(output_id)
        if row["status"] == "uploaded":
            raise ApiError(409, "already_uploaded", "This video is already marked as uploaded.")
        self.s.db.execute(
            "UPDATE outputs SET status = 'rejected', review_note = ? WHERE output_id = ?",
            (text[:500], output_id),
        )
        self.s.log.info("output_rejected", output_id=output_id, reason=text[:200])
        return output_item(self._row(output_id))

    def mark_uploaded(self, output_id: str, url: str | None) -> dict[str, Any]:
        """Record the YouTube URL (so analytics can be matched later) and set status `uploaded`."""
        link = (url or "").strip()
        if not URL_RE.match(link):
            raise ApiError(400, "bad_url", "Paste the full YouTube link, starting with https://")
        row = self._row(output_id)
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

    def thumb_path(self, output_id: str) -> Path:
        """Cached first-frame JPEG (generated on first request)."""
        video = self.media_path(output_id)
        if not ffmpeg.available():
            raise ApiError(503, "ffmpeg_missing", "FFmpeg is not installed, so thumbnails are unavailable.")
        return media.thumbnail(self.s.paths, output_id, video, self.s.log)
