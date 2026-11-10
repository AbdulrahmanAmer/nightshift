"""Task-shaped read tools: one call does what used to take three or four thin ones (component C9).

A small local model fails at chaining tools, not at reading one good answer. Each tool here collects what a
question needs from tables and files the studio already writes, says how sure it is (`n`, `confidence`) and
ends with a `next` hint the model can copy into a confirm card. All of them only read, apart from `open_path`
which opens a folder inside the Nightshift home in Explorer. None adds a route: they are orientation-style tools.
"""
# Dense descriptor tables and long signatures: wrapping them hurts readability.
# ruff: noqa: E501

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ns_common.errors import describe_shown as describe  # an answer shown now (FN-3)
from ns_dashboard.assistant import orientation
from ns_dashboard.assistant.registry import ToolCall, ToolContext, ToolDef, ToolSpec, obj
from ns_dashboard.channel_hold import HELD_KINDS
from ns_dashboard.errors import AdapterUnavailable, ApiError
from ns_dashboard.jobs import JobRecord
from ns_dashboard.logs_tail import LEVELS, tail
from ns_dashboard.security import is_within

NS_CODE = re.compile(r"NS-[EW]\d{3}")
MAX_LOG_LINES = 12


def _s(desc: str) -> dict[str, Any]:
    return {"type": "string", "description": desc}


def _i(desc: str, **extra: Any) -> dict[str, Any]:
    return {"type": "integer", "description": desc, **extra}


def _confidence(n: int) -> str:
    """A word for how much to trust an average over `n` videos (the prompt forbids claims at `low`)."""
    return "low" if n < 5 else "medium" if n < 10 else "high"


def _read_json(f: Path) -> Any:
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _scalars(d: Any, limit: int = 8) -> dict[str, Any]:
    """The first few plain values of a dict (a summary of a file whose layout varies)."""
    if not isinstance(d, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in d.items():
        if v is None or isinstance(v, str | int | float | bool):
            out[str(k)] = v[:200] if isinstance(v, str) else v
        if len(out) >= limit:
            break
    return out


def _channel_slug(c: ToolContext, a: dict[str, Any]) -> str:
    slug = str(a.get("channel") or c.context.get("channel") or "").strip()
    if slug:
        return slug
    slugs = [ch.slug for ch in c.dash.channels.load_all()]
    if len(slugs) == 1:
        return slugs[0]
    raise ApiError(422, "which_channel", "Which channel? Give its slug.", hint="Channels: " + ", ".join(slugs))


# --------------------------------------------------------------------------- diagnose


def diagnose(c: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    """Why did a job or the last nightly run fail: job, stage, code meaning, log lines, policy retries, next step."""
    ref = str(a.get("job_id") or "last").strip() or "last"
    store = c.dash.state.store
    if ref.lower() == "last":
        failed = store.list_jobs("failed", None, 1)
        rec = failed[0] if failed else None
    else:
        rec = store.get(ref)
        if rec is None:
            raise ApiError(404, "not_found", "That job was not found.", hint="Use list_jobs to see job ids.")
    out: dict[str, Any] = {}
    ids: set[str] = set()
    codes: list[str] = []
    if rec is not None:
        err = (rec.result or {}).get("error")
        code = str(err.get("code")) if isinstance(err, dict) and err.get("code") else None
        out["job"] = {
            "job_id": rec.job_id, "kind": rec.kind, "channel": rec.channel, "status": rec.status,
            "message": rec.message[:300], "finished_at": rec.finished_at, "failed_stage": rec.failed_stage(), "code": code,
        }
        ids.add(rec.job_id)
        studio = (rec.result or {}).get("studio_job_ids") or [(rec.result or {}).get("studio_job_id")]
        ids.update(str(s) for s in studio if s)
        for text in (code or "", rec.message):
            codes += NS_CODE.findall(text)
    run = c.dash.state.db.one("SELECT run_id, started_at, finished_at, mode, summary_json FROM runs ORDER BY started_at DESC LIMIT 1")
    if run is not None:
        summary = _read_summary(run["summary_json"])
        out["last_run"] = {"run_id": run["run_id"], "mode": run["mode"], "started_at": run["started_at"],
                           "finished_at": run["finished_at"], "summary": _scalars(summary)}
        codes += NS_CODE.findall(run["summary_json"] or "")
    lines = _log_lines(c, ids, codes)
    for ln in lines:
        codes += NS_CODE.findall(json.dumps(ln, default=str))
    out["log_lines"] = lines
    meanings = []
    for code in dict.fromkeys(codes):
        try:
            d = describe(code)
        except KeyError:
            continue
        meanings.append({"code": code, "kind": d.kind, "message": d.message, "hint": d.hint})
        if len(meanings) >= 3:
            break
    out["meaning"] = meanings
    attempts = []
    for sid in sorted(ids):
        data = _read_json(c.dash.state.paths.job_dir(sid) / "policy_attempts.json")
        if isinstance(data, dict):
            attempts.append({"job": sid, **_scalars(data, 4)})
    if attempts:
        out["policy_attempts"] = attempts
    if rec is None:
        out["note"] = "No failed job is recorded. The log lines (if any) are the latest warnings and errors."
    out["next"] = _diagnose_next(rec, meanings)
    return out


def _read_summary(raw: Any) -> Any:
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _log_lines(c: ToolContext, ids: set[str], codes: list[str]) -> list[dict[str, Any]]:
    records = tail(c.dash.state.paths.logs, n=600)["lines"]
    bad = [r for r in records if LEVELS.get(str(r.get("level", "")).upper(), 20) >= LEVELS["WARN"]]
    mine = [r for r in bad if ids and any(i in json.dumps(r, default=str) for i in ids)]
    if not mine and codes:
        mine = [r for r in bad if any(k in json.dumps(r, default=str) for k in codes)]
    chosen = (mine or bad)[-MAX_LOG_LINES:]
    return [{k: (str(r.get(k, ""))[:200]) for k in ("ts", "level", "component", "event", "msg")} for r in chosen]


def _diagnose_next(rec: Any, meanings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    if rec is not None and rec.kind == "run" and rec.channel:
        steps.append({"tool": "start_story_job", "args": {"channel": rec.channel},
                      "why": "Make it again; this version starts the job from the beginning."})
    steps.append({"tool": "start_verify", "args": {"quick": True}, "why": "Run the self-check if the hint does not fix it."})
    if not meanings:
        steps.append({"tool": "make_diagnostics", "why": "No known code matched: make a report for the developer."})
    return steps[:2]


# --------------------------------------------------------------------------- review_brief


def review_brief(c: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    """What to check before posting one video, and what the owner rejected before on the same channel and options."""
    oid = str(a["output_id"])
    d = c.dash.outputs.detail(oid)
    db = c.dash.state.db
    brief: dict[str, Any] = {
        "output_id": oid, "title": d.get("title"), "channel": d.get("channel"), "kind": d.get("kind"),
        "status": d.get("status"), "duration_s": d.get("duration_s"), "file_exists": d.get("file_exists"),
        "policy_notes": list(d.get("policy_notes") or [])[:8],
        "disclosure": {"ai_disclosure": d.get("ai_disclosure"), "made_for_kids": d.get("made_for_kids"),
                       "age_restrict_suggested": d.get("age_restrict_suggested")},
    }
    qc = _read_json(c.dash.state.paths.job_dir(str(d.get("job_id"))) / "qc" / "report.json")
    if qc is not None:
        brief["qc"] = _scalars(qc, 6)
    row = db.one("SELECT combo_id FROM outputs WHERE output_id = ?", (oid,))
    combo = db.one("SELECT hook, structure, pov_tone, title_formula FROM combos WHERE combo_id = ?", (row["combo_id"],)) if row and row["combo_id"] else None
    if combo is not None:
        brief["options"] = {k: combo[k] for k in combo.keys() if combo[k] is not None}
    channel = str(d.get("channel") or "")
    recent = db.query(
        "SELECT output_id, title, review_note, combo_id FROM outputs WHERE channel = ? AND status = 'rejected'"
        " ORDER BY created_at DESC, rowid DESC LIMIT 10", (channel,),
    )
    brief["recent_rejections"] = [
        {"output_id": r["output_id"], "title": r["title"], "reason": r["review_note"] or "no reason given"} for r in recent[:3]
    ]
    repeats: list[str] = []
    if combo is not None:
        for field_name in ("hook", "structure", "pov_tone", "title_formula"):
            value = combo[field_name]
            if value is None:
                continue
            n = db.one(
                f"SELECT COUNT(*) AS n FROM outputs o JOIN combos c ON c.combo_id = o.combo_id"
                f" WHERE o.channel = ? AND o.status = 'rejected' AND c.{field_name} = ?", (channel, value),
            )
            if n and int(n["n"]) >= 2:
                repeats.append(f"You rejected {int(n['n'])} earlier videos on {channel} with {field_name.replace('_', ' ')} '{value}'.")
    brief["repeat_rejections"] = repeats
    brief["next"] = {"page": "Queue", "why": "Only you approve or reject: open the Queue page and press A or R."}
    return brief


# --------------------------------------------------------------------------- what_worked


def what_worked(c: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    """Which hooks, structures, tones and so on score best on a channel, with the number of videos behind each."""
    from ns_studio.learn import reweight
    from ns_studio.variation.draw import POOL_FIELD

    slug = _channel_slug(c, a)
    days = int(a.get("days") or 0)
    db = c.dash.state.db
    with db.lock:
        scored = reweight.scored_outputs(db.db, slug)
    info = {r["output_id"]: r for r in db.query("SELECT output_id, title, kind, created_at FROM outputs WHERE channel = ?", (slug,))}
    if days > 0:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        scored = [s for s in scored if s[0] in info and str(info[s[0]]["created_at"]) >= cutoff]
    pools: dict[str, list[dict[str, Any]]] = {}
    for pool, fld in POOL_FIELD.items():
        groups: dict[str, list[float]] = {}
        for _oid, score, fields in scored:
            if fld in fields:
                groups.setdefault(fields[fld], []).append(score)
        rows = [{"option": o, "mean_score": round(sum(v) / len(v), 3), "n": len(v), "confidence": _confidence(len(v))}
                for o, v in groups.items()]
        if rows:
            pools[pool] = sorted(rows, key=lambda r: -r["mean_score"])[:3]
    ranked = sorted(scored, key=lambda s: -s[1])

    def perf(item: tuple[str, float, dict[str, str]]) -> dict[str, Any]:
        oid, score, fields = item
        return {"output_id": oid, "title": info[oid]["title"] if oid in info else oid, "score": round(score, 3),
                "hook": fields.get("hook"), "structure": fields.get("structure")}

    n = len(scored)
    out: dict[str, Any] = {
        "channel": slug, "days": days or "all", "n": n, "confidence": _confidence(n), "best_options": pools,
        "top": [perf(s) for s in ranked[:3]], "bottom": [perf(s) for s in ranked[-3:]] if n > 3 else [],
        "last_import_changes": reweight.load_snapshot(c.dash.state.paths, slug).get("changes", {}),
    }
    if n < 5:
        out["note"] = f"Only {n} video(s) have analytics: too few to say what works. Do not make claims."
        out["next"] = {"tool": "import_analytics", "why": "Import the YouTube Studio CSV so there is more to learn from."}
    return out


# --------------------------------------------------------------------------- tonight_plan


def _start_of(job: JobRecord, held_by: dict[str, str]) -> str:
    """When a queued job starts: its set time, or as soon as possible; a story job of a paused channel waits for
    the resume (`channel_hold.HELD_KINDS`), also past its set time."""
    name = held_by.get(job.channel or "") if job.kind in HELD_KINDS else None
    if name is None:
        return job.schedule_at or "as soon as possible"
    if job.schedule_at:
        return f"{job.schedule_at} if {name} is resumed by then, else when {name} is resumed"
    return f"when {name} is resumed"


def automatic_runs(c: ToolContext) -> dict[str, Any]:
    """Whether the nightly run starts by itself, in the words the pages use, and where the owner changes it
    (the Assistant has no switch of its own)."""
    from ns_dashboard.state import load_dashboard_settings, schedule_is_registered, status_schedule_mode

    paths = c.dash.state.paths
    on, when = schedule_is_registered(paths), load_dashboard_settings(paths)["run_time"]
    if not on:
        state = f"off: nothing runs by itself (the kept time is {when})"
    elif status_schedule_mode(paths) == "interactive_only":
        state = f"on at {when}, only while you are signed in to Windows (NS-W205)"
    else:
        state = f"on at {when} every night"
    return {"on": on, "time": when, "state": state, "where": "Settings > Schedule > Automatic runs (a switch)"}


def tonight_plan(c: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    """What the nightly run will make: channels, counts and the governor's reasons, run time, queued jobs.

    `automatic_runs` says whether that run starts by itself; when it is off, `plan` is what a run would make
    if the owner started one."""
    from ns_studio.channels import cadence

    db = c.dash.state.db
    with db.lock:
        due = cadence.plan_daily(c.dash.state.paths, db.db, datetime.now(UTC))
    chans = c.dash.channels.load_all()
    planned = {d.channel.slug for d in due}
    nxt = c.dash.engine.next_run()
    queued = c.dash.state.store.list_jobs("queued", None, 10)
    # the job worker holds a paused channel's story jobs until the resume (P47, `channel_hold`)
    held_by = {ch.slug: ch.display_name for ch in chans if ch.status == "paused"}
    return {
        "next_run": nxt.isoformat(timespec="minutes") if nxt else None,
        "automatic_runs": automatic_runs(c),
        "plan": [
            {"channel": d.channel.slug, "format": d.fmt, "count": d.count, "stage": d.plan.stage,
             "masters_per_week": d.plan.masters_per_week, "shorts_per_day": d.plan.shorts_per_day,
             "quality_drop": d.plan.quality_drop, "reasons": d.plan.reasons}
            for d in due
        ],
        "active_but_nothing_due": [ch.slug for ch in chans if ch.status == "active" and ch.slug not in planned],
        "paused": [ch.slug for ch in chans if ch.status != "active"],
        "queued_jobs": [{"job_id": j.job_id, "kind": j.kind, "channel": j.channel, "start": _start_of(j, held_by)}
                        for j in queued],
        "next": {"tool": "start_story_job", "why": "To add a video outside the plan, use start_story_job (it asks first)."},
    }


# --------------------------------------------------------------------------- video_stats


def video_stats(c: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    """Imported YouTube numbers for one video and where it ranks among the channel's videos of the same kind."""
    oid = str(a["output_id"])
    db = c.dash.state.db
    o = db.one("SELECT output_id, title, channel, kind FROM outputs WHERE output_id = ?", (oid,))
    if o is None:
        raise ApiError(404, "not_found", "That video was not found.")
    rows = db.query(
        "SELECT date, views, viewed_pct, avg_pct_viewed, avg_view_duration_s, subs_gained FROM analytics"
        " WHERE output_id = ? ORDER BY date DESC LIMIT 14", (oid,),
    )
    if not rows:
        return {"output_id": oid, "title": o["title"], "has_analytics": False,
                "next": {"tool": "import_analytics", "why": "No numbers are imported for this video yet."}}
    mine = int(rows[0]["views"] or 0)
    peers = db.query(
        "SELECT a.views AS v FROM analytics a JOIN outputs x ON x.output_id = a.output_id WHERE x.channel = ? AND x.kind = ?"
        " AND a.date = (SELECT MAX(date) FROM analytics WHERE output_id = a.output_id)", (o["channel"], o["kind"]),
    )
    better = sum(1 for p in peers if int(p["v"] or 0) > mine)
    return {
        "output_id": oid, "title": o["title"], "has_analytics": True, "latest": dict(rows[0]),
        "days_of_data": len(rows), "history": [dict(r) for r in rows[1:6]],
        "rank": f"{better + 1} of {len(peers)} {o['kind']} videos on {o['channel']} by views", "n": len(peers),
        "confidence": _confidence(len(peers)),
    }


# --------------------------------------------------------------------------- get_story


def _job_of(c: ToolContext, a: dict[str, Any]) -> str:
    job = str(a.get("job_id") or "").strip()
    if not job:
        oid = str(a.get("output_id") or "").strip()
        job = oid.split("/", 1)[0]
    if not job:
        raise ApiError(422, "invalid_options", "Give a job_id or an output_id.")
    return job


def get_story(c: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    """A written story: title, opening, idea premise and twist, parts, the videos made from it, and its text."""
    job = _job_of(c, a)
    db = c.dash.state.db
    s = db.one("SELECT job_id, channel, format, working_title, opening_line, word_count, critic_avg, text FROM stories WHERE job_id = ?", (job,))
    if s is None:
        raise ApiError(404, "not_found", "No story is stored for that job.", hint="Use search_stories to find it.")
    parts = db.query("SELECT n, hook, word_count, est_seconds, text FROM parts WHERE job_id = ? ORDER BY n", (job,))
    want = a.get("part")
    text = str(s["text"])
    if want is not None:
        match = next((p for p in parts if int(p["n"]) == int(want)), None)
        if match is None:
            raise ApiError(404, "not_found", f"The story has no part {want}.")
        text = str(match["text"])
    idea = db.one("SELECT premise, twist FROM ideas WHERE job_id = ? ORDER BY idea_id DESC LIMIT 1", (job,))
    outs = db.query("SELECT output_id, kind, status, title FROM outputs WHERE job_id = ? ORDER BY output_id", (job,))
    return {
        "job_id": job, "channel": s["channel"], "title": s["working_title"], "format": s["format"],
        "opening_line": s["opening_line"], "word_count": s["word_count"], "critic_avg": s["critic_avg"],
        "idea": dict(idea) if idea else None,
        "parts": [{"n": p["n"], "hook": p["hook"], "words": p["word_count"], "seconds": p["est_seconds"]} for p in parts],
        "videos": [dict(o) for o in outs],
        "text_excerpt": text[:1500], "text_cut": len(text) > 1500,
    }


# --------------------------------------------------------------------------- open_path


def open_path(c: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    """Open a folder (an output's folder, the logs, the outbox of a channel...) in Explorer; only inside the home."""
    thing = str(a["thing"]).strip()
    paths = c.dash.state.paths
    found = orientation.where_is(c, {"thing": thing})
    target = found.get("folder") or found.get("path") or found.get("work_folder")
    low = thing.lower()
    if not target and "outbox" in low:
        for ch in c.dash.channels.load_all():
            if ch.slug in low or ch.slug.replace("-", " ") in low:
                target = str(paths.outbox / ch.slug)
                break
    if not target:
        raise ApiError(404, "not_found", "I do not know where that is.", hint="Try where_is or find first.")
    p = Path(str(target))
    folder = p if p.is_dir() else p.parent
    if not is_within(paths.home, folder):
        raise ApiError(403, "outside_home", "That folder is outside the Nightshift folder, so I will not open it.")
    if not folder.exists():
        raise ApiError(404, "folder_missing", "That folder does not exist yet.")
    try:
        c.dash.state.adapters.folder.open(folder)
    except AdapterUnavailable as e:
        raise ApiError(501, "open_folder_unavailable", str(e)) from e
    return {"ok": True, "opened": str(folder)}


# --------------------------------------------------------------------------- table


def _def(name: str, topic: str, desc: str, call: ToolCall, props: dict[str, Any], required: list[str],
         summary: tuple[str, ...]) -> ToolDef:
    spec = ToolSpec(name=name, description=desc, args_schema=obj(props, required), side_effect=False,
                    topic=topic, summary_fields=summary)
    return ToolDef(spec=spec, call=call)


def tools() -> list[ToolDef]:
    """The composite read tools, ready for the registry."""
    out_id = {"output_id": _s("output id such as after-dark-20261101-ab12/long")}
    return [
        _def("diagnose", "diagnostics",
             "Why a job or the last night failed: job, stage, plain meaning of the code, log lines and what to do next.",
             diagnose, {"job_id": _s("job id, or 'last' (default) for the latest failed job")}, [],
             ("job", "meaning", "next", "last_run")),
        _def("review_brief", "queue",
             "What to check before posting one video: policy notes, disclosure, earlier rejections on the same options.",
             review_brief, out_id, ["output_id"], ("title", "channel", "policy_notes", "disclosure", "repeat_rejections", "next")),
        _def("what_worked", "results",
             "Which hooks, structures and tones score best on a channel, with how many videos each average rests on.",
             what_worked, {"channel": _s("channel slug"), "days": _i("only videos from the last N days", minimum=1, maximum=365)}, [],
             ("channel", "n", "confidence", "best_options", "top", "bottom")),
        _def("tonight_plan", "create",
             "What the nightly run will make: channels, how many, why, whether automatic runs are on, the run time and queued jobs.",
             tonight_plan, {}, [], ("next_run", "automatic_runs", "plan", "paused", "queued_jobs")),
        _def("video_stats", "results",
             "YouTube numbers imported for one video and its rank among the channel's videos of the same kind.",
             video_stats, out_id, ["output_id"], ("title", "latest", "rank", "n", "confidence")),
        _def("get_story", "library",
             "One written story: title, opening, idea, parts, videos made from it and its text (or one part).",
             get_story, {"job_id": _s("story job id"), **out_id, "part": _i("only this part's text", minimum=1, maximum=50)}, [],
             ("title", "channel", "opening_line", "idea", "parts", "videos", "text_excerpt")),
        _def("open_path", "orientation",
             "Open a folder in Explorer: an output's folder, 'logs', 'outbox' or 'after-dark outbox'. Only inside the Nightshift folder.",
             open_path, {"thing": _s("e.g. an output id, 'logs', 'outbox', 'after-dark outbox'")}, ["thing"], ("ok", "opened")),
    ]

