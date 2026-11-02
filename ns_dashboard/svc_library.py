"""Library page operations: idea bank, stories, footage cache and formats (component C9, spec 20.2)."""
# Dense descriptor tables and long signatures: wrapping them hurts readability.
# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

from ns_dashboard import formats, updater
from ns_dashboard.common import now_iso
from ns_dashboard.errors import ApiError
from ns_dashboard.state import AppState
from ns_dashboard.svc_channels import ChannelsService

#: The names `ns models get` takes: the VoiceDesign talker and the story LLM.
MODEL_NAMES = ("voicedesign", "story_llm")
IDEA_STATUSES = ("new", "approved", "used", "rejected")
IDEA_SOURCES = ("owner", "assistant", "generated")
STATUS_ALIASES = {"dropped": "rejected"}  # the UI says "Dropped"; the shared schema says "rejected"


def like(q: str) -> str:
    """`%q%` with the LIKE wildcards in `q` escaped with a backslash (use with ESCAPE)."""
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def _idea(row: sqlite3.Row) -> dict[str, Any]:
    status = row["status"] or "new"
    if status == "new" and row["used"]:
        status = "used"
    return {
        "idea_id": row["idea_id"],
        "channel": row["channel"],
        "title": row["title"] or row["premise"][:60],
        "premise": row["premise"],
        "twist": row["twist"],
        "status": status,
        "source": row["source"] or "generated",
        "job_id": row["job_id"],
        "created_at": row["created_at"],
    }


def story_status(statuses: list[str]) -> str:
    """One word for a story from the states of its videos: pending, approved, posted, rejected or none."""
    if not statuses:
        return "none"
    if "pending" in statuses:
        return "pending"
    if "uploaded" in statuses:
        return "posted"
    if "approved" in statuses:
        return "approved"
    return "rejected"


class LibraryService:
    """Reads and writes what Nightshift remembers."""

    def __init__(self, state: AppState, channels: ChannelsService) -> None:
        self.s = state
        self.channels = channels

    # --- ideas ---
    def list_ideas(self, channel: str | None = None, status: str | None = None, q: str | None = None,
                   limit: int = 200) -> list[dict[str, Any]]:
        """Idea bank entries, newest first."""
        status = STATUS_ALIASES.get(status or "", status)
        if status and status not in IDEA_STATUSES:
            raise ApiError(422, "bad_filter", f"Unknown idea status '{status}'.")
        where, args = [], []
        if channel:
            where.append("channel = ?")
            args.append(channel)
        if status == "used":
            where.append("(status = 'used' OR (status = 'new' AND used = 1))")
        elif status == "new":
            where.append("status = 'new' AND used = 0")
        elif status:
            where.append("status = ?")
            args.append(status)
        if q:
            where.append("(premise LIKE ? ESCAPE '\\' OR twist LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\')")
            args += [like(q)] * 3
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
        source = str(body.get("source") or "owner")
        if source not in IDEA_SOURCES:
            raise ApiError(422, "invalid_idea", f"'source' must be one of {', '.join(IDEA_SOURCES)}.")
        title = str(body.get("title") or "").strip()[:100] or premise[:60]
        now = now_iso()
        with self.s.db.tx() as db:
            db.execute(
                "INSERT OR IGNORE INTO channels(slug, display_name, created_at, status) VALUES (?, ?, ?, ?)",
                (ch.slug, ch.display_name, ch.created_at or now, ch.status),
            )
            db.execute(
                "INSERT INTO ideas(channel, premise, twist, created_at, used, status, title, source)"
                " VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                (slug, premise, str(body.get("twist") or "").strip(), now, status, title, source),
            )
            new_id = db.one("SELECT last_insert_rowid() AS id")
        assert new_id is not None
        self.s.log.info("idea_added", idea_id=new_id["id"], channel=slug, source=source)
        return self.get_idea(int(new_id["id"]))

    def get_idea(self, idea_id: int) -> dict[str, Any]:
        """One idea or 404."""
        row = self.s.db.one("SELECT * FROM ideas WHERE idea_id = ?", (idea_id,))
        if row is None:
            raise ApiError(404, "not_found", "That idea was not found.")
        return _idea(row)

    def set_idea_status(self, idea_id: int, status: str | None) -> dict[str, Any]:
        """new | approved | used | rejected (`dropped` is accepted); approved ideas are drawn first."""
        status = STATUS_ALIASES.get(status or "", status)
        if status not in IDEA_STATUSES:
            raise ApiError(422, "invalid_idea", f"Status must be one of {', '.join(IDEA_STATUSES)}.")
        self.get_idea(idea_id)
        self.s.db.execute(
            "UPDATE ideas SET status = ?, used = ? WHERE idea_id = ?", (status, 1 if status == "used" else 0, idea_id)
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
        for s in out:  # what the Stories tab shows: a title, a word count and one status for the whole story
            s["title"] = s["working_title"] or s["opening_line"]
            s["words"] = s["word_count"]
            s["status"] = story_status([o["status"] for o in s["outputs"]])
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
            " AND u.clip_id = c.clip_id) AS last_used,"
            " (SELECT COUNT(*) FROM clip_usage n WHERE n.source = c.source AND n.clip_id = c.clip_id) AS uses"
            f" FROM clips c{clause} ORDER BY c.fetched_at DESC LIMIT ?",
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
                    "last_used": r["last_used"], "uses": int(r["uses"]),
                }
            )
        return out

    def delete_footage(self, source: str, clip_id: str) -> dict[str, Any]:
        """Remove a cached stock clip: its rows and its file (only a file inside the Nightshift home)."""
        from pathlib import Path

        from ns_dashboard.security import is_within

        row = self.s.db.one("SELECT local_path FROM clips WHERE source = ? AND clip_id = ?", (source, clip_id))
        if row is None:
            raise ApiError(404, "not_found", "That clip is not in the cache.")
        with self.s.db.tx() as db:
            db.execute("DELETE FROM clip_usage WHERE source = ? AND clip_id = ?", (source, clip_id))
            db.execute("DELETE FROM clips WHERE source = ? AND clip_id = ?", (source, clip_id))
        removed = False
        if row["local_path"]:
            path = Path(row["local_path"])
            if is_within(self.s.paths.home, path) and path.is_file():
                path.unlink(missing_ok=True)
                removed = True
        self.s.log.info("footage_deleted", source=source, clip_id=clip_id, file_removed=removed)
        return {"ok": True, "source": source, "clip_id": clip_id, "file_removed": removed}

    # --- formats ---
    def list_formats(self) -> list[dict[str, Any]]:
        """Shipped and user formats, each with the channels that may use it (an empty `formats_allowed` is all)."""
        found = formats.list_formats(self.s.paths)
        chans = self.channels.load_all()
        for f in found:
            f["channels"] = [c.slug for c in chans if not c.formats_allowed or f["id"] in c.formats_allowed]
        return found

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

    def models_get(self, name: str | None) -> dict[str, Any]:
        """Start downloading a model that is fetched on demand (the voice design model, the story model).

        Answers 501 until `ns models get` exists, so the page can say so instead of pretending.
        """
        if name not in MODEL_NAMES:
            raise ApiError(422, "unknown_model", f"Choose one of: {', '.join(MODEL_NAMES)}.")
        if not self.s.adapters.update.models_available():
            raise ApiError(
                501, "models_unavailable", "Downloading models from here is not available yet.",
                hint="Run the installer's model step again with the option ticked.",
            )
        current = self.s.model_downloads.get(name)
        if current and current["state"] == "running":
            return {"name": name, **current}
        self.s.model_downloads[name] = {"state": "running", "message": "Downloading"}

        def work() -> None:
            try:
                def progress(line: str) -> None:
                    self.s.model_downloads[name] = {"state": "running", "message": line[-200:]}

                code, line = self.s.adapters.update.get_model(name, progress)
                self.s.model_downloads[name] = {"state": "done" if code == 0 else "failed", "message": line}
            except Exception as e:  # a failing download must not leave the page waiting forever
                self.s.model_downloads[name] = {"state": "failed", "message": str(e)}

        started = {"name": name, **self.s.model_downloads[name]}
        threading.Thread(target=work, name=f"ns-model-{name}", daemon=True).start()
        return started

    def models_state(self) -> dict[str, dict[str, str]]:
        """Downloads started in this session: name -> {state, message}."""
        return {k: dict(v) for k, v in self.s.model_downloads.items()}

    def update_state(self) -> dict[str, Any]:
        """Progress of the detached updater (`run/update_state.json`): idle, running, done or failed."""
        return updater.read_update_state(self.s.paths)

    def update_apply(self) -> dict[str, Any]:
        """Install the newest version (`ns update --github`, rolls back on failure)."""
        if self.s.draining:
            raise ApiError(503, "shutting_down", "Nightshift is shutting down.")
        self.s.log.warn("update_apply", "update started from the dashboard")
        res = self.s.adapters.update.apply()
        if res.get("state") == "unavailable":
            raise ApiError(501, "update_unavailable", str(res.get("message") or "Updating from GitHub is not available yet."))
        return res

    # --- series workshop (read-only list plus "Produce next part") ---
    def series(self) -> list[dict[str, Any]]:
        """Workshop stories with per-part status, read from the story folders."""
        return self.s.adapters.series.listing()

    def produce_next_part(self, slug: str) -> dict[str, Any]:
        """Queue the next approved part of `slug` as a `series_part` job and wake the worker."""
        if self.s.draining:
            raise ApiError(503, "shutting_down", "Nightshift is shutting down and not accepting new jobs.")
        queued = self.s.adapters.series.queue_next(slug)
        for item in queued:
            self.s.worker.enqueue(str(item["job_id"]))
            rec = self.s.store.get(str(item["job_id"]))
            if rec is not None:
                self.s.hub.publish("dashboard_job", rec.to_dict())
        self.s.log.info("series_part_queued", slug=slug, parts=[q["part"] for q in queued])
        return {"slug": slug, "queued": queued}
