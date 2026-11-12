"""Orientation tools: system_map, find, where_is, search_docs, explain_error (component C9, spec 20.3a)."""
# Dense descriptor tables and long signatures: wrapping them hurts readability.
# ruff: noqa: E501

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ns_common.errors import describe_shown as describe  # an answer shown now (FN-3)
from ns_dashboard.assistant import docs_index
from ns_dashboard.assistant.registry import ToolContext
from ns_dashboard.logs_tail import tail
from ns_dashboard.svc_library import like
from ns_studio.media.outbox import OUTBOX_LAYOUT, REJECTED_LAYOUT, REMOVED_LAYOUT

PAGES: list[tuple[str, str]] = [
    ("Queue", "get finished videos onto YouTube: approve, copy title/description/tags, mark posted"),
    ("Create", "start a story video or clips from your own video now"),
    ("Library", "idea bank, stories, voices, formats, cached footage"),
    ("Assistant", "talk to Nightshift; it can run the other pages for you"),
    ("Channels", "set up each YouTube channel; pause or resume"),
    ("Results", "what works, what changes next, import the YouTube Studio export"),
    ("Settings", "run time, notifications, keys, update, diagnostics, advanced"),
]
FOLDERS: dict[str, str] = {
    "bin": "llama.cpp, qwentts, opencode programs",
    "models": "llm, embed, tts (voices), whisper model files",
    "venvs": "python environments: studio, supervisor, tts",
    "config": "runtime.json, install_state.json, dashboard.json, channels/<slug>.yaml, formats/",
    "data": "studio.db, assistant.db, jobs/<job_id>/ working files, backup/",
    "cache": "clips (stock footage), search, dashboard (thumbnails, previews)",
    "imports": "clipper/<source_id>/ uploaded videos, analytics/ CSV exports",
    "outbox": OUTBOX_LAYOUT,
    "rejected": REJECTED_LAYOUT,
    "removed": REMOVED_LAYOUT,
    "logs": "<component>-YYYYMMDD.jsonl, one JSON line per event",
    "drafts": "<id>.md story drafts the Assistant writes part by part; the owner can edit them",
}
SERVICES: dict[str, str] = {
    "llama": "8080 language model (GPU)",
    "embed": "8081 text embeddings (CPU)",
    "tts": "8091 voice engine (GPU)",
    "whisper": "in-process speech recognition (GPU)",
    "supervisor": "8099 starts/stops services, owns the GPU lock",
    "dashboard": "8100 this app",
}
ERROR_RANGES = "E1xx install, E2xx runtime/GPU lock, E3xx text/LLM, E4xx media/QC, E5xx stock/network, E6xx policy gate, E7xx update"
MAX_MAP_CHARS = 6000  # about 1,500 tokens


def system_map(ctx: ToolContext, _args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compact map of pages, folders, services, channels, formats, error ranges and engine state."""
    dash = ctx.dash
    channels = [f"{c.slug}({c.status})" for c in dash.channels.load_all()]
    fmts = [f["id"] for f in dash.library.list_formats()]
    engine = dash.engine.engine()
    pending = dash.state.db.one("SELECT COUNT(*) AS n FROM outputs WHERE status = 'pending'")
    out: dict[str, Any] = {
        "pages": {n: d for n, d in PAGES},
        "home": str(dash.state.paths.home),
        "folders": FOLDERS,
        "services": SERVICES,
        "channels": channels,
        "formats": fmts,
        "errors": ERROR_RANGES,
        "engine": f"{engine['state']}: {engine['detail']}",
        "videos_waiting_for_review": int(pending["n"]) if pending else 0,
    }
    # keep the map inside its token budget: trim the variable parts first
    while len(json.dumps(out, separators=(",", ":"))) > MAX_MAP_CHARS and (out["formats"] or out["channels"]):
        key = "formats" if len(out["formats"]) >= len(out["channels"]) else "channels"
        out[key] = out[key][:-1]
        out[f"{key}_truncated"] = True
    return out


def compact_map(ctx: ToolContext) -> str:
    """`system_map` as a one-line JSON string for the system prompt."""
    return json.dumps(system_map(ctx), separators=(",", ":"), ensure_ascii=False)


def docs(ctx: ToolContext) -> docs_index.DocsIndex:
    """The index the Assistant may quote: README, operator guide, error catalogue and the generated capability list."""
    from ns_dashboard.assistant.tools import NO_TOOL  # tools.py imports this module

    def chunks() -> list[docs_index.Chunk]:
        return docs_index.capability_chunks((t.spec for t in ctx.dash.registry.all()), NO_TOOL)

    return docs_index.get_index(ctx.dash.state.paths, chunks)


def search_docs(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Ranked passages from the owner-facing docs and the generated capability list."""
    hits = docs(ctx).search(str(args.get("query", "")), int(args.get("limit", 5)))
    return {"query": args.get("query", ""), "hits": hits}


def explain_error(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Message and hint for an `NS-` code, plus the latest log lines that mention it."""
    code = str(args.get("code", "")).strip().upper()
    try:
        d = describe(code)
    except KeyError:
        return {"code": code, "known": False, "message": "Not a Nightshift error code.",
                "hint": "Codes look like NS-E203 (error) or NS-W112 (warning)."}
    lines = tail(ctx.dash.state.paths.logs, n=400)["lines"]
    mine = [r for r in lines if code in json.dumps(r, default=str)][-5:]
    return {"code": code, "known": True, "kind": d.kind, "message": d.message, "hint": d.hint, "recent_log_lines": mine}


def _home_path(ctx: ToolContext, *parts: str) -> str:
    return str(Path(ctx.dash.state.paths.home, *parts))


def find(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Search outputs, stories, ideas, channels, jobs, logs and docs; typed hits with ids and places."""
    q = str(args.get("query", "")).strip()
    if not q:
        return {"query": q, "hits": []}
    dash, db = ctx.dash, ctx.dash.state.db
    pat = like(q)
    hits: list[dict[str, Any]] = []
    for r in db.query(
        "SELECT output_id, title, channel, kind, status, path FROM outputs WHERE title LIKE ? ESCAPE '\\'"
        " OR output_id LIKE ? ESCAPE '\\' OR channel LIKE ? ESCAPE '\\' ORDER BY created_at DESC LIMIT 8",
        (pat, pat, pat),
    ):
        hits.append({"type": "output", "id": r["output_id"], "title": r["title"], "status": r["status"],
                     "channel": r["channel"], "kind": r["kind"], "path": r["path"], "page": "Queue"})
    for r in db.query(
        "SELECT job_id, channel, working_title FROM stories WHERE working_title LIKE ? ESCAPE '\\'"
        " OR opening_line LIKE ? ESCAPE '\\' OR text LIKE ? ESCAPE '\\' ORDER BY created_at DESC LIMIT 5",
        (pat, pat, pat),
    ):
        hits.append({"type": "story", "id": r["job_id"], "title": r["working_title"], "channel": r["channel"],
                     "page": "Library > Stories"})
    for r in db.query(
        "SELECT idea_id, channel, premise FROM ideas WHERE premise LIKE ? ESCAPE '\\' OR twist LIKE ? ESCAPE '\\'"
        " ORDER BY idea_id DESC LIMIT 5",
        (pat, pat),
    ):
        hits.append({"type": "idea", "id": r["idea_id"], "title": r["premise"][:120], "channel": r["channel"],
                     "page": "Library > Ideas"})
    low = q.lower()
    for c in dash.channels.load_all():
        if low in c.slug or low in c.display_name.lower() or low in c.niche.lower():
            hits.append({"type": "channel", "id": c.slug, "title": c.display_name, "status": c.status,
                         "path": str(dash.state.paths.channel_file(c.slug)), "page": "Channels"})
    for r in db.query(
        "SELECT job_id, kind, channel, status, message FROM dashboard_jobs WHERE job_id LIKE ? ESCAPE '\\'"
        " OR message LIKE ? ESCAPE '\\' OR channel LIKE ? ESCAPE '\\' ORDER BY rowid DESC LIMIT 5",
        (pat, pat, pat),
    ):
        hits.append({"type": "job", "id": r["job_id"], "title": f"{r['kind']} {r['status']}: {r['message']}",
                     "channel": r["channel"], "page": "Create"})
    for rec in tail(dash.state.paths.logs, n=600)["lines"]:
        if low in json.dumps(rec, default=str).lower():
            hits.append({"type": "log", "id": rec.get("ts"), "title": f"{rec.get('component')}: {rec.get('event')}",
                         "snippet": str(rec.get("msg", ""))[:160], "page": "Settings > Advanced"})
            if sum(1 for h in hits if h["type"] == "log") >= 4:
                break
    for d in docs(ctx).search(q, 3):
        hits.append({"type": "doc", "id": f"{d['file']}:{d['line']}", "title": d["heading"] or d["file"],
                     "snippet": d["text"][:200]})
    return {"query": q, "hits": hits}


def where_is(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Absolute path or page for an output, job, channel, model, log or setting."""
    thing = str(args.get("thing", "")).strip()
    dash, paths = ctx.dash, ctx.dash.state.paths
    low = thing.lower()
    row = dash.state.db.one("SELECT path, channel FROM outputs WHERE output_id = ?", (thing,))
    if row:
        return {"kind": "output", "path": row["path"], "folder": str(Path(row["path"]).parent), "page": "Queue"}
    job = dash.state.db.one("SELECT job_id FROM dashboard_jobs WHERE job_id = ?", (thing,))
    if job:
        return {"kind": "job", "page": "Create", "work_folder": str(paths.job_dir(thing))}
    if paths.channel_file(low).exists():
        return {"kind": "channel", "path": str(paths.channel_file(low)), "page": "Channels"}
    named: dict[str, tuple[str, str | Path]] = {
        "llm": ("models", paths.models_llm), "language model": ("models", paths.models_llm),
        "embed": ("models", paths.models_embed), "tts": ("models", paths.models_tts),
        "voices": ("models", paths.models_tts_voices), "whisper": ("models", paths.models_whisper),
        "logs": ("logs", paths.logs), "log": ("logs", paths.logs), "outbox": ("outbox", paths.outbox),
        "rejected": ("rejected", paths.rejected), "database": ("data", paths.db_path),
        "config": ("config", paths.config), "runtime": ("config", paths.runtime_file),
        "backups": ("data", paths.backup_dir), "analytics": ("imports", paths.imports_analytics),
        "footage": ("cache", paths.cache_clips), "clips": ("cache", paths.cache_clips),
        "footage cache": ("cache", paths.cache_clips), "stock footage": ("cache", paths.cache_clips),
        "stock clips": ("cache", paths.cache_clips), "clip cache": ("cache", paths.cache_clips),
    }
    if low in named:
        kind, p = named[low]
        return {"kind": kind, "path": str(p)}
    settings_words = {"schedule", "run time", "notifications", "keys", "update", "diagnostics"}
    if low in settings_words:
        return {"kind": "setting", "page": "Settings"}
    for name, desc in PAGES:
        if low == name.lower():
            return {"kind": "page", "page": name, "purpose": desc}
    if low.startswith("ns-"):
        return {"kind": "error code", "tool": "explain_error"}
    return {"kind": "unknown", "hint": "Try find(query) for a wider search.", "home": _home_path(ctx)}


def static_map() -> str:
    """The fixed part of the system prompt: pages, folders, error ranges. No live state, so it never changes."""
    pages = "\n".join(f"- {n}: {d}" for n, d in PAGES)
    folders = "\n".join(f"- {n}: {d}" for n, d in FOLDERS.items())
    return f"Pages:\n{pages}\nFolders under the Nightshift home:\n{folders}\nError codes: {ERROR_RANGES}"
