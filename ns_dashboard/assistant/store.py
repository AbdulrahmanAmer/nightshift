"""Assistant transcripts and pending actions in `data/assistant.db` (component C9).

The shared schema (`contracts/schema.sql`) provides `assistant_messages`, a flat transcript that every message
is also written to. Conversations, pending actions and the busy queue need more than that table offers, so
they live in the same schema file (CR-C9-9); `Db.migrate()` creates them.
"""
# Dense descriptor tables and long signatures: wrapping them hurts readability.
# ruff: noqa: E501

from __future__ import annotations

import json
import secrets
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from ns_common.db import Db
from ns_common.paths import Paths


#: A claim this old was left by a caller that died while answering; another caller may take it over.
CLAIM_STALE_S = 900.0


def _now() -> str:
    return datetime.now(UTC).isoformat()


class AssistantStore:
    """Thread-safe access to the assistant database."""

    def __init__(self, paths: Paths) -> None:
        self.db = Db(paths.assistant_db)
        self._lock = threading.RLock()
        with self._lock:
            self.db.migrate()  # contracts/schema.sql: assistant_messages, conversations, messages, actions, queued_messages
            self._add_claim_columns()

    def _add_claim_columns(self) -> None:
        """`queued_messages.claimed_at` and `claimed_by` (an older file has neither; `assistant.db` is ours alone)."""
        have = {r["name"] for r in self.db.query("PRAGMA table_info(queued_messages)")}
        for column in ("claimed_at", "claimed_by"):
            if column not in have:
                self.db.execute(f"ALTER TABLE queued_messages ADD COLUMN {column} TEXT")

    # --- conversations / messages ---
    def new_conversation(self, title: str | None = None) -> str:
        """Create a conversation and return its id."""
        cid = secrets.token_hex(6)
        now = _now()
        with self._lock:
            self.db.execute(
                "INSERT INTO conversations(conversation_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (cid, title, now, now),
            )
        return cid

    def has_conversation(self, cid: str) -> bool:
        """True when `cid` exists."""
        with self._lock:
            return self.db.one("SELECT 1 FROM conversations WHERE conversation_id = ?", (cid,)) is not None

    def add_message(self, cid: str, role: str, content: str | None = None, *, tool_calls: Any = None,
                    tool_call_id: str | None = None, name: str | None = None, context: Any = None) -> int:
        """Append a message; returns its id."""
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO messages(conversation_id, role, content, tool_calls_json, tool_call_id, name,"
                " context_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cid, role, content,
                    json.dumps(tool_calls) if tool_calls else None,
                    tool_call_id, name,
                    json.dumps(context) if context else None,
                    _now(),
                ),
            )
            self.db.execute("UPDATE conversations SET updated_at = ? WHERE conversation_id = ?", (_now(), cid))
            self.db.execute(
                "INSERT INTO assistant_messages(ts, role, content, tool_calls_json) VALUES (?, ?, ?, ?)",
                (_now(), role, content or "", json.dumps(tool_calls) if tool_calls else None),
            )
            return int(cur.lastrowid or 0)

    def set_title(self, cid: str, title: str) -> None:
        """Name a conversation after its first question."""
        with self._lock:
            self.db.execute(
                "UPDATE conversations SET title = ? WHERE conversation_id = ? AND title IS NULL", (title[:80], cid)
            )

    def messages(self, cid: str) -> list[dict[str, Any]]:
        """All messages of a conversation in order, as chat-completions dicts."""
        with self._lock:
            rows = self.db.query(
                "SELECT role, content, tool_calls_json, tool_call_id, name, created_at FROM messages"
                " WHERE conversation_id = ? ORDER BY msg_id",
                (cid,),
            )
        out: list[dict[str, Any]] = []
        for r in rows:
            m: dict[str, Any] = {"role": r["role"], "content": r["content"] or "", "created_at": r["created_at"]}
            if r["tool_calls_json"]:
                m["tool_calls"] = json.loads(r["tool_calls_json"])
            if r["tool_call_id"]:
                m["tool_call_id"] = r["tool_call_id"]
            if r["name"]:
                m["name"] = r["name"]
            out.append(m)
        return out

    def conversations(self, limit: int = 30) -> list[dict[str, Any]]:
        """Recent conversations, newest first."""
        with self._lock:
            rows = self.db.query(
                "SELECT conversation_id, title, created_at, updated_at,"
                " (SELECT COUNT(*) FROM queued_messages q WHERE q.conversation_id = c.conversation_id"
                "  AND q.status = 'queued') AS queued FROM conversations c"
                " ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            )
        return [dict(r) for r in rows]

    # --- actions ---
    def add_action(self, cid: str, tool: str, args: dict[str, Any], confirm_text: str, context: Any) -> str:
        """Record a pending side-effect action and return its id."""
        aid = secrets.token_hex(8)
        with self._lock:
            self.db.execute(
                "INSERT INTO actions(action_id, conversation_id, tool, args_json, confirm_text, context_json,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (aid, cid, tool, json.dumps(args), confirm_text, json.dumps(context or {}), _now()),
            )
        return aid

    def get_action(self, aid: str) -> dict[str, Any] | None:
        """The action row as a dict (args and context decoded) or None."""
        with self._lock:
            row = self.db.one("SELECT * FROM actions WHERE action_id = ?", (aid,))
        if row is None:
            return None
        d = dict(row)
        d["args"] = json.loads(d.pop("args_json"))
        d["context"] = json.loads(d.pop("context_json") or "{}")
        d["result"] = json.loads(d["result_json"]) if d.get("result_json") else None
        return d

    def claim_action(self, aid: str) -> bool:
        """Atomically move `pending` -> `running` so an action cannot run twice."""
        with self._lock:
            cur = self.db.execute(
                "UPDATE actions SET status = 'running' WHERE action_id = ? AND status = 'pending'", (aid,)
            )
            return cur.rowcount == 1

    def finish_action(self, aid: str, status: str, result: Any) -> None:
        """Record the outcome (`done`, `failed` or `dismissed`)."""
        with self._lock:
            self.db.execute(
                "UPDATE actions SET status = ?, result_json = ?, decided_at = ? WHERE action_id = ?",
                (status, json.dumps(result, default=str), _now(), aid),
            )

    def pending_actions(self, cid: str) -> list[dict[str, Any]]:
        """Actions of a conversation still waiting for Yes/No."""
        with self._lock:
            rows = self.db.query(
                "SELECT action_id FROM actions WHERE conversation_id = ? AND status = 'pending' ORDER BY created_at",
                (cid,),
            )
        return [a for a in (self.get_action(r["action_id"]) for r in rows) if a]

    # --- queue (engine busy) ---
    def queue_message(self, cid: str, text: str, context: Any) -> int:
        """Remember a request to answer once the engine is free."""
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO queued_messages(conversation_id, text, context_json, created_at) VALUES (?, ?, ?, ?)",
                (cid, text, json.dumps(context or {}), _now()),
            )
            return int(cur.lastrowid or 0)

    def queued(self) -> list[dict[str, Any]]:
        """Queued requests, oldest first."""
        with self._lock:
            rows = self.db.query("SELECT * FROM queued_messages WHERE status = 'queued' ORDER BY queue_id")
        return [dict(r) | {"context": json.loads(r["context_json"] or "{}")} for r in rows]

    def claim_queued(self, queue_id: int, by: str, *, stale_s: float = CLAIM_STALE_S) -> bool:
        """Take a saved request for answering, in one statement; False when another caller has it.

        A claim older than `stale_s` belongs to a caller that died mid-answer and may be taken over.
        """
        now = datetime.now(UTC)
        cutoff = (now - timedelta(seconds=stale_s)).isoformat()
        with self._lock:
            cur = self.db.execute(
                "UPDATE queued_messages SET claimed_at = ?, claimed_by = ? WHERE queue_id = ? AND status = 'queued'"
                " AND (claimed_at IS NULL OR claimed_at < ?)",
                (now.isoformat(), by, queue_id, cutoff),
            )
            return cur.rowcount == 1

    def release_queued(self, queue_id: int, by: str) -> None:
        """Give a claimed request back (the engine was busy, or answering failed) so it is tried again later."""
        with self._lock:
            self.db.execute(
                "UPDATE queued_messages SET claimed_at = NULL, claimed_by = NULL WHERE queue_id = ? AND claimed_by = ?"
                " AND status = 'queued'",
                (queue_id, by),
            )

    def mark_queued_done(self, queue_id: int) -> None:
        """Mark a queued request as handled."""
        with self._lock:
            self.db.execute("UPDATE queued_messages SET status = 'done' WHERE queue_id = ?", (queue_id,))

    def close(self) -> None:
        """Close the database."""
        with self._lock:
            self.db.close()
