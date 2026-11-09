"""F-C11: the Assistant's tools, each called once through the chat with the dev fake's tool cue.

Every message is exactly `tool: <name> {json args}`, which the dev install's fake language model turns into
that one call (ns_supervisor/devfake.py). A read-only tool answers on the turn (a `tool_result` event) and its
answer is compared with what the plain API says. A tool that changes something shows a confirm card first;
the proof checks the card carries the arguments, presses Yes (POST /api/assistant/confirm), reads the result
on the card ("Done: ...") and then reads the effect where the product keeps it: a channel file, a database
row, a format file, a voice folder, a job that ends done, a zip. Everything the proof makes is named
`proof-...` or `proof_...`, and what has no delete route is left paused, rejected or cancelled.

Tools whose Yes cannot be pressed on a laptop install without side effects the proof cannot undo are not run;
the proof ends as skipped, after all the others, naming them: open_path and open_folder (Windows Explorer
windows), set_schedule (registers the shared Windows scheduled task), shutdown_engine (ends the engine the
other proofs need) and resume_job when the install holds no failed story job to resume (a proof cannot make
one honestly). Ported from runs/qa/fc11.py, fc11b.py and fc11c.py (evidence log 2026-11-09, 0.1.14).
"""

from __future__ import annotations

import contextlib
import json
import math
import shutil
import struct
import urllib.request
import uuid
import wave
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from proofs._lib import (
    DesktopGuard,
    api_no_deadline,
    db_rows,
    ffmpeg_path,
    free_diag_minute,
    pauses_proof_channels,
    proof_channel,
    short,
    wait_job,
)

PROOF = {
    "id": "f-c11-assistant-tools",
    "boxes": ["F-C11"],
    "install": "dev",
    "window": False,
    "needs": ["engine"],
    "timeout_s": 1800,
    "fakes": [],
}

CHANNEL = "proof-assistant"
ASSISTANT = "dashboard:assistant"  # the GPU owner name the Assistant acquires llama under (assistant/llm.py)
FIXTURE = Path(__file__).parent / "_fixtures" / "f_c11_assistant_tools" / "talk.transcript.json"
SCRIPT = (
    "The lighthouse keeper had counted the steps every night for thirty years. There were ninety-one, and "
    "never one more. On the first night of the autumn storm he counted ninety-two. He stopped on the stair "
    "and listened. The sea was loud, but under it there was a second sound, slow and even, like someone "
    "climbing behind him. He turned the lamp up and went on. At the top the log book lay open, and in his "
    "own handwriting it said that he had been there since the morning. He had not written it. He sat down "
    "and waited for the sound to reach the door. It stopped just outside. Nothing knocked. In the morning "
    "the stair had ninety-one steps again, and the book was closed, and the lamp was still warm."
)
AUDIO_EXPR = (
    "0.22*sin(2*PI*140*t)*(0.5+0.5*sin(2*PI*3*t))"
    "+0.11*sin(2*PI*420*t)*(0.5+0.5*sin(2*PI*2.3*t))"
    "+0.8*sin(2*PI*900*t)*lt(mod(t\\,2)\\,0.003)"
)
CSV = (
    "﻿Content,Video title,Video publish time,Duration,Views,Stayed to watch (%),"
    "Average percentage viewed (%),Average view duration,Likes,Comments added,Shares,Subscribers\n"
    'PROOFfc11vid01,Proof FC11 unmatched title,"Nov 9, 2026",30,100,50.0,50.0,0:00:15,1,0,0,0\n'
)


# ------------------------------------------------------------------------------------------------ the chat
def _sse(ctx: Any, message: str) -> list[tuple[str, dict[str, Any]]]:
    """POST /api/assistant/chat as a fresh conversation; the frames `data: {kind, payload}` as pairs."""
    url = f"http://127.0.0.1:{ctx.ports['dashboard']}/api/assistant/chat"
    body = json.dumps({"message": message, "conversation_id": None, "context": None}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    events: list[tuple[str, dict[str, Any]]] = []
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=300) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line.startswith("data:"):
                continue
            try:
                obj = json.loads(line[5:].strip())
            except ValueError:
                continue
            if isinstance(obj, dict) and "kind" in obj:
                payload = obj.get("payload")
                events.append((str(obj["kind"]), payload if isinstance(payload, dict) else {}))
    return events


def _engine_idle(ctx: Any) -> dict[str, Any] | None:
    """GET /api/status read as "nothing is using the engine": no running job, no queued job that is due, and
    the GPU held by nobody or by the Assistant itself. Returns what was seen when idle, else None."""
    status, st = ctx.api("GET", "/api/status")
    if status != 200 or not isinstance(st, dict):
        return None
    queue = st.get("queue") or {}
    # a job held for later (scheduled) waits; it does not run
    due = int(queue.get("queued") or 0) - int(queue.get("scheduled") or 0)
    owner = (st.get("engine") or {}).get("vram_owner")
    running = st.get("job") is not None or int(queue.get("running") or 0) > 0
    idle = not running and due <= 0 and owner in (None, "", ASSISTANT)
    return {"queue": queue, "vram_owner": owner} if idle else None


def _chat(ctx: Any, tool: str, args: dict[str, Any] | None) -> list[tuple[str, dict[str, Any]]]:
    """The cue for `tool`, sent once, after waiting until the engine is idle.

    A busy answer is saved by the product as a queued request that the dashboard replays later (a write cue
    would then come back as a fresh confirm card for the owner), so the proof never re-sends: it waits first,
    and a busy answer after that fails the proof with the answer it got."""
    message = f"tool: {tool} {json.dumps(args)}" if args else f"tool: {tool}"
    seen = ctx.wait(lambda: _engine_idle(ctx), 600, every=5, what=f"the engine to be idle before {tool}")
    events = _sse(ctx, message)
    busy = [p for kind, p in events if kind == "busy"]
    ctx.check(
        not busy,
        f"{tool}: the engine answered the cue (not busy) after it was seen idle",
        idle=seen,
        busy=short(busy),
    )
    problems = [p for kind, p in events if kind == "error"]
    ctx.check(not problems, f"{tool}: the turn ran without an error event", errors=short(problems))
    return events


def _read(ctx: Any, tool: str, args: dict[str, Any] | None = None) -> tuple[bool, Any]:
    """A read-only tool through the chat: (ok, result) of its `tool_result` event."""
    events = _chat(ctx, tool, args)
    done = next((p for k, p in events if k == "tool_result" and p.get("tool") == tool), None)
    ctx.check(
        done is not None and not any(k == "confirm" for k, _ in events),
        f"{tool}: routed as named, answered on the turn with no card",
        kinds=[k for k, _ in events],
    )
    assert done is not None
    ctx.step(f"{tool} answered", ok=done.get("ok"), result=short(done.get("result", done.get("error"))))
    return bool(done.get("ok")), done.get("result")


def _write(ctx: Any, tool: str, args: dict[str, Any], before: Any = None) -> Any:
    """A changing tool: the card, `before()` (nothing has happened yet), Yes, the result on the card."""
    events = _chat(ctx, tool, args)
    card = next((p for k, p in events if k == "confirm" and p.get("tool") == tool), None)
    ctx.check(
        card is not None
        and bool(card.get("text"))
        and all(card["args"].get(k) == v for k, v in args.items()),
        f"{tool}: routed as named, a confirm card shows its text and carries the arguments",
        kinds=[k for k, _ in events],
        card=short(card),
    )
    assert card is not None
    if before is not None:
        before()
    status, answer = ctx.api("POST", "/api/assistant/confirm", {"action_id": card["action_id"]}, timeout=300)
    ctx.check(
        status == 200
        and answer.get("status") == "done"
        and answer.get("tool") == tool
        and str(answer.get("message", "")).startswith("Done:"),
        f"{tool}: Yes runs it and the card reports Done with the result",
        status=status,
        message=short(answer.get("message")),
        result=short(answer.get("result")),
    )
    return answer.get("result")


# ------------------------------------------------------------------------------------------------ helpers
def _later(days: int = 20) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")


def _ids(rows: Any, key: str) -> list[Any]:
    return [r.get(key) for r in rows] if isinstance(rows, list) else []


def _speech_wav(path: Path, seconds: float = 10.0, sr: int = 24000) -> Path:
    """A voiced-syllable-like mono 16-bit WAV that passes the clone sample check (3-30 s, loud, gaps)."""
    syll, gap = int(0.18 * sr), int(0.07 * sr)
    phase, frames = 0.0, bytearray()
    for i in range(int(sr * seconds)):
        pos = i % (syll + gap)
        if pos < syll:
            phase += 2 * math.pi * (130 + 25 * math.sin(2 * math.pi * 3 * i / sr)) / sr
            v = 0.22 * math.sin(math.pi * pos / syll) * (math.sin(phase) + 0.5 * math.sin(2 * phase))
        else:
            v = 0.0003 * math.sin(i)
        frames += struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32767))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return path


def _make_source(ctx: Any, exe: str) -> Path:
    """A speech-like source video in ctx.scratch (FFmpeg helper process) with its transcript sidecar."""
    tr = json.loads(FIXTURE.read_text(encoding="utf-8"))
    duration = float(tr["duration_s"])
    out = ctx.scratch / "proof-fc11-talk.mp4"
    progress = ctx.scratch / "proof-fc11-talk.progress.txt"
    progress.unlink(missing_ok=True)
    out.unlink(missing_ok=True)
    ctx.spawn(
        [
            exe,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc={AUDIO_EXPR}:s=22050:d={duration}",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x305070:s=640x360:r=30:d={duration}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-shortest",
            "-progress",
            progress.as_posix(),
            "-nostats",
            out.as_posix(),
        ],
        name="proof-fc11-source",
    )
    ctx.wait(
        lambda: (
            progress.exists() and "progress=end" in progress.read_text(encoding="utf-8", errors="replace")
        ),
        120,
        every=1,
        what="FFmpeg to finish the generated source video",
    )
    tr["source"] = out.name
    out.with_name("proof-fc11-talk.transcript.json").write_text(json.dumps(tr), encoding="utf-8")
    return out


def _job(ctx: Any, job_id: Any, timeout: float = 900) -> dict[str, Any]:
    job = wait_job(ctx, str(job_id), timeout=timeout)
    ctx.check(job.get("status") == "done", f"job {job_id} ends done", message=job.get("message"))
    return job


# ------------------------------------------------------------------------------------------------ read tools
def _read_only_part(ctx: Any) -> None:
    """Tools that only read: each answer is compared with the plain API's answer for the same thing."""
    ok, res = _read(ctx, "system_map")
    ctx.check(ok and isinstance(res, dict) and bool(res), "system_map: a non-empty map", keys=short(res))

    ok, res = _read(ctx, "find", {"query": CHANNEL})
    ctx.check(
        ok and isinstance(res.get("hits"), list) and bool(res["hits"]),
        "find: hits for the proof channel's name",
        hits=len(res.get("hits") or []),
    )
    ok, res = _read(ctx, "search_docs", {"query": "how do I approve a video", "limit": 3})
    ctx.check(
        ok and isinstance(res.get("hits"), list) and 0 < len(res["hits"]) <= 3,
        "search_docs: up to 3 ranked passages",
        hits=len(res.get("hits") or []),
    )

    ok, res = _read(ctx, "list_channels")
    _, plain = ctx.api("GET", "/api/channels")
    ctx.check(
        ok and CHANNEL in _ids(res, "slug") and sorted(_ids(res, "slug")) == sorted(_ids(plain, "slug")),
        "list_channels: the same channels as GET /api/channels, the proof's among them",
        slugs=_ids(res, "slug"),
    )
    ok, res = _read(ctx, "get_channel", {"slug": CHANNEL})
    ctx.check(
        ok and (res.get("profile") or {}).get("slug") == CHANNEL,
        "get_channel: the proof channel's profile",
        slug=(res.get("profile") or {}).get("slug"),
    )
    ok, res = _read(ctx, "validate_channel", {"slug": CHANNEL})
    ctx.check(ok and isinstance(res, dict), "validate_channel: the saved profile is checked", res=short(res))

    ok, res = _read(ctx, "get_settings")
    _, plain = ctx.api("GET", "/api/settings")
    ctx.check(
        ok
        and res.get("schedule_time") == plain.get("schedule_time")
        and res.get("home") == plain.get("home")
        and res.get("profile") == plain.get("profile"),
        # GET /api/settings has no `version` key (it is in GET /api/status), so it is not compared here
        "get_settings: the run time, home and engine profile GET /api/settings has",
        schedule_time=res.get("schedule_time"),
        home=res.get("home"),
        profile=res.get("profile"),
    )
    ok, res = _read(ctx, "list_formats")
    _, plain = ctx.api("GET", "/api/library/formats")
    ctx.check(
        ok and sorted(_ids(res, "id")) == sorted(_ids(plain, "id")) and bool(res),
        "list_formats: the same format ids as GET /api/library/formats",
        count=len(res or []),
    )
    ok, res = _read(ctx, "list_templates")
    _, plain = ctx.api("GET", "/api/templates")
    ctx.check(
        ok and sorted(_ids(res, "id")) == sorted(_ids(plain, "id")) and "auto" in _ids(res, "id"),
        "list_templates: the same layouts as GET /api/templates",
        ids=_ids(res, "id"),
    )
    ok, res = _read(ctx, "list_voices")
    _, plain = ctx.api("GET", "/api/voices")
    ctx.check(
        ok and sorted(_ids(res, "name")) == sorted(_ids(plain, "name")) and bool(res),
        "list_voices: the same voices as GET /api/voices",
        count=len(res or []),
    )
    ok, res = _read(ctx, "list_queue", {"status": "pending", "limit": 5})
    _, plain = ctx.api("GET", "/api/outputs?status=pending&limit=5")
    ctx.check(
        ok and _ids(res, "output_id") == _ids(plain, "output_id"),
        "list_queue: the same pending videos as GET /api/outputs",
        ids=_ids(res, "output_id"),
    )
    ok, res = _read(ctx, "list_jobs", {"limit": 5})
    _, plain = ctx.api("GET", "/api/jobs?limit=5")
    ctx.check(
        ok and _ids(res, "job_id") == _ids(plain, "job_id"),
        "list_jobs: the same jobs as GET /api/jobs",
        ids=_ids(res, "job_id"),
    )
    ok, res = _read(ctx, "list_ideas", {"limit": 5})
    _, plain = ctx.api("GET", "/api/library/ideas?limit=5")
    ctx.check(
        ok and _ids(res, "idea_id") == _ids(plain, "idea_id"),
        "list_ideas: the same ideas as GET /api/library/ideas",
        ids=_ids(res, "idea_id"),
    )
    ok, res = _read(ctx, "list_runs", {"limit": 5})
    _, plain = ctx.api("GET", "/api/runs?limit=5")
    ctx.check(
        ok and isinstance(res, list) and len(res) == len(plain),
        "list_runs: as many runs as GET /api/runs",
        runs=len(res or []),
    )
    ok, res = _read(ctx, "summary_results")
    ctx.check(ok and isinstance(res, dict), "summary_results: the learning summary", result=short(res))
    ok, res = _read(ctx, "tail_logs", {"n": 10, "level": "INFO"})
    ctx.check(ok and res is not None, "tail_logs: log lines come back", result=short(res))
    # The weekly check asks GitHub, so offline it answers a coded error; either way the tool ran and answered.
    ok, res = _read(ctx, "check_update")
    ctx.check(
        (ok and "update_available" in (res or {})) or (not ok and bool((res or {}).get("error"))),
        "check_update: GitHub's answer, or a coded error when it cannot be reached",
        ok=ok,
        result=short(res),
    )


# ------------------------------------------------------------------------------------------------ writes
def _channel_part(ctx: Any, tag: str) -> str:
    slug = f"proof-fc11-{tag}"
    _, got = ctx.api("GET", f"/api/channels/{CHANNEL}")
    profile = {k: v for k, v in got["profile"].items() if k not in ("created_at", "updated_at")}
    profile.update({"slug": slug, "display_name": f"Proof FC11 {tag}", "status": "active"})
    file = ctx.home / "config" / "channels" / f"{slug}.yaml"

    def nothing_yet() -> None:
        code, _ = ctx.api("GET", f"/api/channels/{slug}")
        ctx.check(code == 404 and not file.exists(), "before Yes the channel does not exist", status=code)

    _write(ctx, "create_channel", {"profile": profile}, before=nothing_yet)
    code, got = ctx.api("GET", f"/api/channels/{slug}")
    ctx.check(
        code == 200 and got["profile"]["slug"] == slug and file.is_file(),
        "create_channel: GET /api/channels/{slug} reads it and config/channels/<slug>.yaml exists",
        status=code,
        file=file.name,
    )
    _write(ctx, "edit_channel", {"slug": slug, "changes": {"voice_notes": "proof fc11 edit"}})
    code, got = ctx.api("GET", f"/api/channels/{slug}")
    ctx.check(
        got["profile"]["voice_notes"] == "proof fc11 edit" and "proof fc11 edit" in file.read_text("utf-8"),
        "edit_channel: the profile and its file hold the change",
        voice_notes=got["profile"]["voice_notes"],
    )
    return slug


def _idea_part(ctx: Any, slug: str) -> None:
    premise = "Proof FC11: a night watchman finds a door in the lighthouse that was not there before."
    res = _write(ctx, "add_idea", {"channel": slug, "title": "proof fc11 idea", "premise": premise})
    idea = int(res["idea_id"])
    rows = db_rows(ctx, "select channel, status, source, premise from ideas where idea_id=?", (idea,))
    ctx.check(
        rows == [(slug, "new", "assistant", premise)],
        "add_idea: the idea row is in the bank with the Assistant as its source",
        rows=rows,
    )
    _write(ctx, "set_idea_status", {"idea_id": idea, "status": "rejected"})
    rows = db_rows(ctx, "select status from ideas where idea_id=?", (idea,))
    ctx.check(rows == [("rejected",)], "set_idea_status: the idea row is rejected", rows=rows)


def _format_part(ctx: Any, tag: str) -> None:
    _, fmts = ctx.api("GET", "/api/library/formats")
    base = next(f["id"] for f in fmts if f.get("valid", True))
    new = f"proof_fc11_{tag}"
    file = ctx.home / "config" / "formats" / f"{new}.yaml"
    try:
        _write(
            ctx,
            "duplicate_format",
            {"duplicate_of": base, "id": new},
            before=lambda: ctx.check(not file.exists(), "before Yes the format file does not exist"),
        )
        _, after = ctx.api("GET", "/api/library/formats")
        ctx.check(
            new in _ids(after, "id") and file.is_file(),
            "duplicate_format: the copy is listed and config/formats/<id>.yaml exists",
            copy_of=base,
        )
        _write(ctx, "edit_format", {"id": new, "changes": {"name": "Proof FC11 renamed"}})
        ctx.check(
            "Proof FC11 renamed" in file.read_text("utf-8"),
            "edit_format: the format file holds the new name",
            file=file.name,
        )
    finally:
        file.unlink(missing_ok=True)
        ctx.step("the proof's own format file removed", file=file.name)


def _held_job_part(ctx: Any, slug: str) -> None:
    res = _write(
        ctx,
        "start_story_job",
        {"channel": slug, "format": "shorts_only", "count": 1, "schedule_at": _later()},
    )
    job_id = res["job_id"]
    _, job = ctx.api("GET", f"/api/jobs/{job_id}")
    ctx.check(
        job.get("status") == "queued" and job.get("channel") == slug,
        "start_story_job: a queued job held for later is in the job list",
        job_id=job_id,
        status=job.get("status"),
    )
    _write(ctx, "cancel_job", {"job_id": job_id})
    _, job = ctx.api("GET", f"/api/jobs/{job_id}")
    ctx.check(job.get("status") == "cancelled", "cancel_job: the job is cancelled", status=job.get("status"))


def _script_part(ctx: Any) -> None:
    res = _write(
        ctx,
        "start_script_job",
        {"channel": CHANNEL, "format": "shorts_only", "script": SCRIPT, "title": "Proof FC11 script"},
    )
    job = _job(ctx, res["job_id"])
    oids = [str(o) for o in (job.get("result") or {}).get("output_ids") or []]
    sql = "select job_id, kind, status, path from outputs where output_id=?"
    rows = [db_rows(ctx, sql, (o,)) for o in oids]
    ctx.check(
        bool(oids) and all(r and Path(r[0][3]).is_file() for r in rows),
        "start_script_job: the job made a video whose file is in the outbox",
        output_ids=oids,
    )
    studio_job, oid = rows[0][0][0], oids[0]
    ok, got = _read(ctx, "get_job", {"job_id": res["job_id"]})
    ctx.check(
        ok and got.get("job_id") == res["job_id"] and got.get("status") == "done",
        "get_job: the done job",
        status=got.get("status"),
    )
    ok, got = _read(ctx, "get_output", {"output_id": oid})
    ctx.check(
        ok and got.get("output_id") == oid and got.get("file_exists") is True,
        "get_output: the video's card, its file exists",
        output_id=oid,
    )
    ok, got = _read(ctx, "get_story", {"job_id": studio_job})
    ctx.check(
        ok
        and got.get("job_id") == studio_job
        and oid in [v.get("output_id") for v in got.get("videos") or []],
        "get_story: the story of the job, the video among its videos",
        title=got.get("title"),
    )
    ok, got = _read(ctx, "search_stories", {"channel": CHANNEL, "limit": 3})
    ctx.check(
        ok and studio_job in _ids(got, "job_id"),
        "search_stories: the story is found by channel",
        jobs=_ids(got, "job_id"),
    )


def _clip_part(ctx: Any, skipped: list[str]) -> None:
    exe = ffmpeg_path()
    if not exe:
        skipped.append(
            "start_clip_job and rerun_clips (a clip job needs a source video and FFmpeg is not on PATH)"
        )
        return
    source = _make_source(ctx, str(exe))
    res = _write(
        ctx,
        "start_clip_job",
        {
            "channel": CHANNEL,
            "path": str(source),
            "template": "auto",
            "max_clips": 1,
            "min_s": 10,
            "max_s": 40,
        },
    )
    first = _job(ctx, res["job_id"])
    ids = [str(o) for o in (first.get("result") or {}).get("output_ids") or []]
    ctx.check(bool(ids), "start_clip_job: the job made a clip", output_ids=ids)
    res = _write(ctx, "rerun_clips", {"job_id": first["job_id"], "template": "center_crop"})
    second = _job(ctx, res["job_id"])
    ctx.check(
        (second.get("params") or {}).get("rerun_of") == first["job_id"]
        and (second.get("params") or {}).get("template") == "center_crop",
        "rerun_clips: a new clip job with rerun_of the first and the new layout, done",
        params=short(second.get("params")),
    )


def _verify_diag_import_part(ctx: Any, tag: str) -> None:
    res = _write(ctx, "start_verify", {"quick": True})
    job = _job(ctx, res["job_id"])
    ctx.check((job.get("result") or {}).get("passed") is True, "start_verify: the self-check passed")

    # The product writes the zip to the Desktop (C:) when Yes is pressed. The guard opens just before that Yes
    # (after the card is shown) and moves whatever the product wrote into scratch on the way out, pass or
    # fail. The zip is named by the minute, so free_diag_minute runs inside the guard, immediately before the
    # Yes: the product never overwrites (and the guard never moves) an owner's zip of that minute.
    guards: list[DesktopGuard] = []
    with contextlib.ExitStack() as stack:

        def arm() -> None:
            guards.append(stack.enter_context(DesktopGuard(ctx, "nightshift-diagnostics-*.zip", "f-c11")))
            free_diag_minute(ctx)

        res = _write(ctx, "make_diagnostics", {}, before=arm)
        # The zip the product just made goes into scratch BEFORE any check on it.
        kept = guards[0].take(Path(str(res["path"])))
        ctx.check(
            kept.is_file() and kept.read_bytes()[:2] == b"PK",
            "make_diagnostics: the zip the card names is a zip (read from scratch)",
            bytes=kept.stat().st_size if kept.exists() else None,
        )

    csv = ctx.scratch / f"proof-fc11-analytics-{tag}.csv"
    csv.write_text(CSV, encoding="utf-8")
    _, before = ctx.api("GET", "/api/jobs?kind=learn&limit=50")
    _write(ctx, "import_analytics", {"path": str(csv)})
    _, after = ctx.api("GET", "/api/jobs?kind=learn&limit=50")
    new = [j for j in after if j.get("job_id") not in _ids(before, "job_id")]
    stored = sorted((ctx.home / "imports" / "analytics").glob(f"*{csv.name}"))
    ctx.check(
        len(new) == 1 and new[0].get("status") == "done" and len(stored) == 1,
        "import_analytics: a learn job ended done and the CSV is stored under imports/analytics",
        jobs=short(new),
        stored=[p.name for p in stored],
    )


def _engine_voice_part(ctx: Any, tag: str) -> None:
    _write(ctx, "warmup_engine", {"service": "llama"})

    def llama_state() -> Any:
        _, st = ctx.api("GET", "/api/status")
        return ((st.get("services") or {}).get("llama") or {}).get("state")

    state = ctx.wait(lambda: llama_state() == "ready" and "ready", 180, every=3, what="llama to be ready")
    ctx.check(state == "ready", "warmup_engine: /api/status shows llama ready", state=state)

    voices = ctx.home / "models" / "tts" / "voices"
    design, clone = f"proof-fc11-d-{tag}", f"proof-fc11-c-{tag}"
    try:
        _write(
            ctx,
            "design_voice",
            {"name": design, "description": "a calm low test voice", "test_line": "This is a test."},
        )
        sample = _speech_wav(ctx.scratch / "proof-fc11-sample.wav")
        _write(
            ctx,
            "clone_voice",
            {"name": clone, "sample_path": str(sample), "test_line": "This is a test of the clone."},
        )
        _, listed = ctx.api("GET", "/api/voices")
        names = _ids(listed, "name")
        ctx.check(
            design in names
            and clone in names
            and (voices / design / "voice.json").is_file()
            and (voices / clone / "voice.json").is_file(),
            "design_voice and clone_voice: both voices are listed and have their voice.json",
            listed=[n for n in names if str(n).startswith("proof-")],
        )
    finally:
        for name in (design, clone):
            if (voices / name).is_dir() and (voices / name).resolve().is_relative_to(ctx.home.resolve()):
                shutil.rmtree(voices / name, ignore_errors=True)
        ctx.step("the proof's own voice folders removed", voices=[design, clone])


def _resume_part(ctx: Any, skipped: list[str]) -> None:
    """resume_job on a failed story job of a proof channel, when the install holds one the tool accepts."""
    _, failed = ctx.api("GET", "/api/jobs?status=failed&kind=run&limit=50")
    mine = [j for j in failed or [] if str(j.get("channel", "")).startswith("proof-")]
    for job in mine[:3]:
        events = _chat(ctx, "resume_job", {"job_id": job["job_id"]})
        card = next((p for k, p in events if k == "confirm" and p.get("tool") == "resume_job"), None)
        if card is None:
            ctx.step("the Assistant refuses this job before a card", job_id=job["job_id"])
            continue
        status, answer = ctx.api(
            "POST", "/api/assistant/confirm", {"action_id": card["action_id"]}, timeout=300
        )
        ctx.check(
            status == 200 and answer.get("status") == "done",
            "resume_job: Yes continues the failed story job",
            result=short(answer.get("result")),
        )
        _job(ctx, answer["result"]["job_id"])
        return
    skipped.append("resume_job (the install holds no failed story job of a proof channel to continue)")


@pauses_proof_channels
def run(ctx: Any) -> None:
    tag = uuid.uuid4().hex[:6]
    proof_channel(ctx, CHANNEL)
    skipped: list[str] = []
    slug = ""
    try:
        _read_only_part(ctx)
        slug = _channel_part(ctx, tag)
        _idea_part(ctx, slug)
        _format_part(ctx, tag)
        _held_job_part(ctx, slug)
        _script_part(ctx)
        _clip_part(ctx, skipped)
        _verify_diag_import_part(ctx, tag)
        _engine_voice_part(ctx, tag)
        _resume_part(ctx, skipped)
    finally:
        if slug:
            try:
                code, _ = api_no_deadline(
                    ctx, "POST", f"/api/channels/{slug}/pause", {"note": "proof FC11: left paused"}
                )
                ctx.step("the channel the Assistant made is left paused", channel=slug, status=code)
            except Exception as exc:  # noqa: BLE001  (a cleanup error must not hide the proof's own result)
                ctx.step("pausing the channel the Assistant made failed", channel=slug, error=str(exc))
    skipped += [
        "open_path and open_folder (each opens a Windows Explorer window the proof cannot close)",
        "set_schedule (Yes registers the shared 'Nightshift Daily' scheduled task, which a proof cannot "
        "restore on a dev install)",
        "shutdown_engine (Yes ends the engine; it must be the runner's last act, then a restart)",
    ]
    ctx.skip(
        "not run: " + "; ".join(skipped) + ". The runner would need a fake folder opener (or a window it can "
        "close), a scheduler stand-in, and a last-step engine shutdown with a restart after it"
    )
