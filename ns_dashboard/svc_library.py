"""Library page operations: idea bank, stories, footage cache and formats (component C9, spec 20.2)."""
# Dense descriptor tables and long signatures: wrapping them hurts readability.
# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ns_dashboard import formats
from ns_dashboard.common import now_iso
from ns_dashboard.errors import ApiError
from ns_dashboard.state import AppState, LockedDb
from ns_dashboard.svc_channels import ChannelsService

IDEA_STATUSES = ("new", "approved", "used", "dropped")
IDEA_COLUMNS = {
    "status": "TEXT NOT NULL DEFAULT 'new'",
    "setting": "TEXT",
    "protagonist": "TEXT",
    "keywords": "TEXT",
    "source": "TEXT",
    "updated_at": "TEXT",
}


def ensure_idea_columns(db: LockedDb) -> None:
    """Add the idea-bank columns to `ideas` when the shared schema does not have them yet (CR-C9-6)."""
    with db.lock:
        have = {r["name"] for r in db.query("PRAGMA table_info(ideas)")}
        for col, decl in IDEA_COLUMNS.items():
            if col not in have:
                db.execute(f"ALTER TABLE ideas ADD COLUMN {col} {decl}")


def like(q: str) -> str:
    """`%q%` with LIKE wildcards in `q` escaped (use with `ESCAPE '\\'`)."""
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def _idea(row: sqlite3.Row) -> dict[str, Any]:
    status = row["status"] or "new"
    if status == "new" and row["used"]:
        status = "used"
    try:
        keywords = json.loads(row["keywords"]) if row["keywords"] else []
    except ValueError:
        keywords = []
    return {
        "idea_id": row["idea_id"],
        "channel": row["channel"],
        "premise": row["premise"],
        "twist": row["twist"],
        "setting": row["setting"],
        "protagonist": row["protagonist"],
        "keywords": keywords,
        "status": status,
        "source": row["source"] or "nightshift",
        "job_id": row["job_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class LibraryService:
    """Reads and writes what Nightshift remembers."""

    def __init__(self, state: AppState, channels: ChannelsService) -> None:
        self.s = state
        self.channels = channels

    # --- ideas ---
    def list_ideas(self, channel: str | None = None, status: str | None = None, q: str | None = None,
                   limit: int = 200) -> list[dict[str, Any]]:
        """Idea bank entries, newest first."""
        if status and status not in IDEA_STATUSES:
            raise ApiError(422, "bad_filter", f"Unknown idea status '{status}'.")
        where, args = [], []
        if channel:
            where.append("channel = ?")
            args.append(channel)
        if status == "used":
            where.append("(status = 'used' OR (COALESCE(status, 'new') = 'new' AND used = 1))")
        elif status == "new":
            where.append("COALESCE(status, 'new') = 'new' AND used = 0")
        elif status:
            where.append("status = ?")
            args.append(status)
        if q:
            where.append("(premise LIKE ? ESCAPE '\\' OR twist LIKE ? ESCAPE '\\')")
            args += [like(q), like(q)]
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self.s.db.query(
            f"SELECT * FROM ideas{clause} ORDER BY idea_id DESC LIMIT ?", [*args, max(1, min(limit, 500))]
        )
        return [_idea(r) for r in rows]

    def add_idea(self, body: dict[str, Any]) -> dict[str, Any]:
        """Add an idea typed by the owner (or the Assistant) for a channel."""
        slug = str(body.get("channel") or "")
        ch = self.channels.load(slug)
        premise = str(body.get("premise") or "").strip()
        if len(premise) < 10 or len(premise) > 600:
            raise ApiError(422, "invalid_idea", "The premise should be one to three sentences (10 to 600 characters).")
        status = str(body.get("status") or "new")
        if status not in ("new", "approved"):
            raise ApiError(422, "invalid_idea", "A new idea starts as 'new' or 'approved'.")
        keywords = body.get("keywords") or []
        if not isinstance(keywords, list) or len(keywords) > 8:
            raise ApiError(422, "invalid_idea", "'keywords' must be a list of at most 8 words.")
        now = now_iso()
        with self.s.db.tx() as db:
            db.execute(
                "INSERT OR IGNORE INTO channels(slug, display_name, created_at, status) VALUES (?, ?, ?, ?)",
                (ch.slug, ch.display_name, ch.created_at or now, ch.status),
            )
            db.execute(
                "INSERT INTO ideas(channel, premise, twist, created_at, used, status, setting, protagonist,"
                " keywords, source, updated_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, 'owner', ?)",
                (
                    slug, premise, str(body.get("twist") or "").strip(), now, status,
                    body.get("setting"), body.get("protagonist"),
                    json.dumps([str(k) for k in keywords]), now,
                ),
            )
            new_id = db.one("SELECT last_insert_rowid() AS id")
        assert new_id is not None
        self.s.log.info("idea_added", idea_id=new_id["id"], channel=slug)
        return self.get_idea(int(new_id["id"]))

    def get_idea(self, idea_id: int) -> dict[str, Any]:
        """One idea or 404."""
        row = self.s.db.one("SELECT * FROM ideas WHERE idea_id = ?", (idea_id,))
        if row is None:
            raise ApiError(404, "not_found", "That idea was not found.")
        return _idea(row)

    def set_idea_status(self, idea_id: int, status: str | None) -> dict[str, Any]:
        """new | approved | used | dropped; approved ideas are drawn first by the `ideas` stage."""
        if status not in IDEA_STATUSES:
            raise ApiError(422, "invalid_idea", f"Status must be one of {', '.join(IDEA_STATUSES)}.")
        self.get_idea(idea_id)
        self.s.db.execute(
            "UPDATE ideas SET status = ?, used = ?, updated_at = ? WHERE idea_id = ?",
            (status, 1 if status == "used" else 0, now_iso(), idea_id),
        )
        return self.get_idea(idea_id)

    # --- stories ---
    def stories(self, q: str | None = None, channel: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Written stories with their outputs and where they were posted."""
        where, args = [], []
        if q:
            where.append(
                "(working_title LIKE ? ESCAPE '\\' OR opening_line LIKE ? ESCAPE '\\' OR text LIKE ? ESCAPE '\\')"
            )
            args += [like(q)] * 3
        if channel:
            where.append("channel = ?")
            args.append(channel)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self.s.db.query(
            "SELECT job_id, channel, format, working_title, opening_line, word_count, critic_avg, revisions,"
            f" created_at FROM stories{clause} ORDER BY created_at DESC LIMIT ?",
            [*args, max(1, min(limit, 200))],
        )
        out = [dict(r) | {"outputs": []} for r in rows]
        by_job = {s["job_id"]: s for s in out}
        if by_job:
            marks = ",".join("?" for _ in by_job)
            outs = self.s.db.query(
                "SELECT o.job_id, o.output_id, o.kind, o.k, o.status, u.video_url FROM outputs o"
                f" LEFT JOIN uploads u ON u.output_id = o.output_id WHERE o.job_id IN ({marks})"
                " ORDER BY o.output_id",
                list(by_job),
            )
            for o in outs:
                by_job[o["job_id"]]["outputs"].append(
                    {k: o[k] for k in ("output_id", "kind", "k", "status", "video_url")}
                )
        return out

    # --- footage ---
    def footage(self, q: str | None = None, limit: int = 60) -> list[dict[str, Any]]:
        """Cached stock clips by keyword, licence and last use."""
        where, args = [], []
        if q:
            where.append(
                "(c.keywords LIKE ? ESCAPE '\\' OR c.clip_id LIKE ? ESCAPE '\\' OR c.source LIKE ? ESCAPE '\\')"
            )
            args += [like(q)] * 3
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self.s.db.query(
            "SELECT c.*, (SELECT MAX(used_at) FROM clip_usage u WHERE u.source = c.source"
            f" AND u.clip_id = c.clip_id) AS last_used FROM clips c{clause} ORDER BY c.fetched_at DESC LIMIT ?",
            [*args, max(1, min(limit, 200))],
        )
        out = []
        for r in rows:
            try:
                kw = json.loads(r["keywords"]) if r["keywords"] else []
            except ValueError:
                kw = []
            out.append(
                {
                    "source": r["source"], "clip_id": r["clip_id"], "url": r["url"], "license": r["license"],
                    "width": r["width"], "height": r["height"], "duration": r["duration"],
                    "local_path": r["local_path"], "keywords": kw, "fetched_at": r["fetched_at"],
                    "last_used": r["last_used"],
                }
            )
        return out

    # --- formats ---
    def list_formats(self) -> list[dict[str, Any]]:
        """Shipped and user formats."""
        return formats.list_formats(self.s.paths)

    def create_format(self, body: dict[str, Any]) -> dict[str, Any]:
        """New format or a duplicate of an existing one."""
        return formats.create_format(self.s.paths, body)

    def update_format(self, fid: str, body: dict[str, Any]) -> dict[str, Any]:
        """Replace or patch a format."""
        return formats.update_format(self.s.paths, fid, body)

    # --- update ---
    def update_check(self) -> dict[str, Any]:
        """Is a newer version on GitHub? (structured result of `ns update --check`)"""
        return self.s.adapters.update.check()

    def update_apply(self) -> dict[str, Any]:
        """Install the newest version (`ns update --github`, rolls back on failure)."""
        if self.s.draining:
            raise ApiError(503, "shutting_down", "Nightshift is shutting down.")
        self.s.log.warn("update_apply", "update started from the dashboard")
        return self.s.adapters.update.apply()
