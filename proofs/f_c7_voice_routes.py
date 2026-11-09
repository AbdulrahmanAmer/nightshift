"""F-C7: the voice routes on the fake engine, and Library > Voices "Create it now".

Preview (a WAV the product caches), design and clone (the voice folder, `voice.json` and `preview.wav` under
models/tts/voices/, the voice in GET /api/voices), their coded refusals, then the pending voice: a request is
queued, `GET /api/voices/pending` shows it, `POST /api/voices/apply-pending` queues an `apply_voice` job that
creates it and forgets the request, and the same on the Library > Voices page by clicking "Create it now"
(Playwright on the page itself). Ported from runs/qa/croutes/croutes.py box F-C7 and runs/qa/fc7.py
(evidence log 2026-11-09, 0.1.12 and 0.1.14).

The pending voice is the proof's OWN. Only setup queues one (installer s07 `queue_voice` writes
config/pending_voice.json as {channel, mode, name, description}); the proof writes that file in the same shape
for a voice named `proof-...` on its own channel `proof-voices`, and only when the install holds no pending
voice at all. When one is there (the setup voice, which F-E12 needs) the proof does not touch it: the pending
part ends as skipped, after the parts that need no pending voice.

The product has no voice delete route, so the proof's voices are named from the install's version
(`proof-design-0-1-15`, `proof-clone-...`, `proof-pending-...`, `proof-click-...`): at most four proof voices
per version remain. A run on a version whose voice exists from an earlier run still checks what can be shown
(the voice is listed, its files exist, a second design or clone answers 409 voice_exists, preview works),
then ends as skipped, naming the voices it did not create, because making them is the thing to show.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import struct
import uuid
import wave
from hashlib import sha1
from pathlib import Path
from typing import Any

from proofs._lib import db_rows, pauses_proof_channels, proof_channel, short, wait_job

PROOF = {
    "id": "f-c7-voice-routes",
    "boxes": ["F-C7"],
    "install": "dev",
    "window": True,
    "needs": ["engine"],
    "timeout_s": 900,
    "fakes": [],
}

CHANNEL = "proof-voices"  # the channel the proof's pending voice names (setup's request carries one)
DESCRIPTION = "A calm, low, warm narrator voice."
TEST_LINE = "The lamp was still burning when the boat arrived."


def _error(answer: Any) -> dict[str, Any]:
    err = answer.get("error") if isinstance(answer, dict) else None
    return err if isinstance(err, dict) else {}


def _speech_wav(path: Path, seconds: float, sr: int = 24000) -> Path:
    """A voiced-syllable-like mono 16-bit WAV that passes the clone sample check (3-30 s, loud, gaps)."""
    rnd = random.Random(7)
    syll, gap = int(0.18 * sr), int(0.07 * sr)
    period = syll + gap
    phase = 0.0
    frames = bytearray()
    for i in range(int(sr * seconds)):
        pos = i % period
        if pos < syll:
            t = i / sr
            env = math.sin(math.pi * pos / syll)
            phase += 2 * math.pi * (130 + 25 * math.sin(2 * math.pi * 3 * t)) / sr
            v = 0.22 * env * (math.sin(phase) + 0.5 * math.sin(2 * phase) + 0.25 * math.sin(3 * phase))
        else:
            v = rnd.uniform(-1, 1) * 0.0003
        frames += struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32767))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return path


def _is_wav(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 1000 and path.read_bytes()[:4] == b"RIFF"


def _voices(ctx: Any) -> dict[str, Any]:
    """GET /api/voices as {name: mode}."""
    code, voices = ctx.api("GET", "/api/voices")
    ctx.check(code == 200 and isinstance(voices, list), "GET /api/voices answers a list", status=code)
    return {v.get("name"): v.get("mode") for v in voices}


def _voice_dir(ctx: Any, name: str) -> Path:
    return ctx.home / "models" / "tts" / "voices" / name


def _queue_pending(ctx: Any, name: str, channel: str) -> Path:
    """Write config/pending_voice.json as setup does (installer s07 `queue_voice`: channel, mode, name and,
    for a designed voice, description; indent 2, a closing newline) for the proof's own voice and channel."""
    f = ctx.home / "config" / "pending_voice.json"
    tmp = f.with_name(f.name + ".proof-tmp")
    request = {"channel": channel, "mode": "design", "name": name, "description": DESCRIPTION}
    tmp.write_text(json.dumps(request, indent=2) + "\n", "utf-8")
    os.replace(tmp, f)
    ctx.step(
        "queued the proof's own pending voice (what setup's queue_voice writes)",
        voice=name,
        channel=channel,
        file=str(f.relative_to(ctx.home)),
        request=request,
    )
    return f


def _preview_part(ctx: Any, tag: str) -> None:
    voices = _voices(ctx)
    voice = "Ryan" if "Ryan" in voices else str(next(iter(voices)))
    text = f"Proof preview {tag}: the lamp was still burning."
    key = sha1(f"{voice}|1.0|{text}".encode()).hexdigest()[:16]
    cache = ctx.home / "cache" / "dashboard" / "voice" / f"{key}.wav"
    rel = str(cache.relative_to(ctx.home))
    ctx.check(not cache.exists(), "no cached preview for this text yet", file=rel)
    code, _ = ctx.api("POST", "/api/voices/preview", {"voice": voice, "text": text}, timeout=300)
    ctx.check(
        code == 200 and _is_wav(cache),
        "POST /api/voices/preview answers 200 and the product stored a WAV (RIFF) for the text",
        voice=voice,
        status=code,
        bytes=cache.stat().st_size if cache.exists() else None,
    )
    code, answer = ctx.api("POST", "/api/voices/preview", {"voice": "", "text": "x"})
    ctx.check(
        code == 400 and _error(answer).get("code") == "no_voice",
        "preview without a voice answers 400 no_voice",
        status=code,
        answer=short(answer),
    )
    code, answer = ctx.api("POST", "/api/voices/preview", {"voice": voice, "text": "x", "speed": 2.5})
    ctx.check(
        code == 422 and _error(answer).get("code") == "invalid_options",
        "preview with speed 2.5 answers 422 invalid_options",
        status=code,
        answer=short(answer),
    )


def _design_part(ctx: Any, tag: str, reused: list[str]) -> None:
    name = f"proof-design-{tag}"
    preview = _voice_dir(ctx, name) / "preview.wav"
    if name in _voices(ctx):
        reused.append(name)
        ctx.step("this version's design voice exists from an earlier run: not made again", voice=name)
        ctx.check(
            _voices(ctx).get(name) == "design"
            and (_voice_dir(ctx, name) / "voice.json").is_file()
            and _is_wav(preview),
            "GET /api/voices lists it as a design voice; voice.json and preview.wav (RIFF) exist",
            voice=name,
            preview_bytes=preview.stat().st_size if preview.exists() else None,
        )
    else:
        code, made = ctx.api(
            "POST",
            "/api/voices/design",
            {"name": name, "description": DESCRIPTION, "test_line": TEST_LINE},
            timeout=300,
        )
        ctx.check(
            code == 200
            and made.get("voice_id") == name
            and Path(str(made.get("preview_wav_path"))).name == "preview.wav",
            "POST /api/voices/design answers the voice id and the preview path",
            status=code,
            answer=short(made),
        )
        ctx.check(
            (_voice_dir(ctx, name) / "voice.json").is_file() and _is_wav(preview),
            "the voice folder holds voice.json and a preview.wav (RIFF)",
            preview_bytes=preview.stat().st_size if preview.exists() else None,
        )
        ctx.check(
            _voices(ctx).get(name) == "design",
            "GET /api/voices lists the new voice as a design voice",
            voice=name,
        )
    for what, body, status, code_name in (
        ("a name with a space and a mark", {"name": "bad name!"}, 422, "invalid_voice"),
        ("an empty description", {"name": f"proof-nodesc-{tag}", "description": " "}, 422, "invalid_voice"),
        ("the name of a built-in voice", {"name": "Ryan"}, 409, "voice_exists"),
        ("the name of the voice just made", {"name": name}, 409, "voice_exists"),
    ):
        full = {"description": DESCRIPTION, "test_line": TEST_LINE, **body}
        code, answer = ctx.api("POST", "/api/voices/design", full)
        ctx.check(
            code == status and _error(answer).get("code") == code_name,
            f"design with {what} answers {status} {code_name}",
            status=code,
            answer=short(answer),
        )
    ctx.check(
        not _voice_dir(ctx, f"proof-nodesc-{tag}").exists() and not _voice_dir(ctx, "bad name!").exists(),
        "the refused designs left no voice folder",
    )


def _clone_part(ctx: Any, tag: str, reused: list[str]) -> None:
    name = f"proof-clone-{tag}"
    refused = f"proof-clonebad-{tag}"  # the refusals use a name that is never made
    sample = _speech_wav(ctx.scratch / "sample-6s.wav", 6.0)
    short_wav = _speech_wav(ctx.scratch / "sample-1s.wav", 1.0)
    base = {"name": refused, "sample_path": str(sample), "test_line": TEST_LINE}
    code, answer = ctx.api("POST", "/api/voices/clone", {**base, "consent": False})
    err = _error(answer)
    ctx.check(
        code == 422 and err.get("code") == "NS-E313" and err.get("hint"),
        "clone without consent answers 422 NS-E313 with a hint",
        status=code,
        answer=short(answer),
    )
    for what, body, status, code_name in (
        ("no recording", {"sample_path": ""}, 400, "no_sample"),
        ("a path that is no file", {"sample_path": str(ctx.scratch / "missing.wav")}, 400, "bad_path"),
        ("a 1 s recording", {"sample_path": str(short_wav)}, 422, "NS-E312"),
    ):
        code, answer = ctx.api("POST", "/api/voices/clone", {**base, "consent": True, **body})
        ctx.check(
            code == status and _error(answer).get("code") == code_name,
            f"clone with {what} answers {status} {code_name}",
            status=code,
            answer=short(answer),
        )
    ctx.check(not _voice_dir(ctx, refused).exists(), "the refused clones left no voice folder")
    preview = _voice_dir(ctx, name) / "preview.wav"
    if name in _voices(ctx):
        reused.append(name)
        ctx.step("this version's clone voice exists from an earlier run: not made again", voice=name)
        ctx.check(
            _voices(ctx).get(name) == "clone"
            and (_voice_dir(ctx, name) / "voice.json").is_file()
            and _is_wav(preview),
            "GET /api/voices lists it as a clone; the folder holds voice.json and a preview.wav (RIFF)",
            voice=name,
            preview_bytes=preview.stat().st_size if preview.exists() else None,
        )
    else:
        body = {**base, "name": name, "consent": True}
        code, made = ctx.api("POST", "/api/voices/clone", body, timeout=300)
        ctx.check(
            code == 200 and made.get("voice_id") == name and _is_wav(preview),
            "POST /api/voices/clone with consent answers the voice id and a preview.wav (RIFF) exists",
            status=code,
            answer=short(made),
        )
        ctx.check(
            (_voice_dir(ctx, name) / "voice.json").is_file() and _voices(ctx).get(name) == "clone",
            "voice.json exists and GET /api/voices lists the new voice as a clone",
            voice=name,
        )
    code, answer = ctx.api("POST", "/api/voices/clone", {**base, "name": name, "consent": True})
    ctx.check(
        code == 409 and _error(answer).get("code") == "voice_exists",
        "clone with the name of the voice just made answers 409 voice_exists",
        status=code,
        answer=short(answer),
    )


def _apply_job_rows(ctx: Any) -> list[Any]:
    return db_rows(ctx, "select job_id, status from dashboard_jobs where kind='apply_voice' order by job_id")


def _pending_route_part(ctx: Any, name: str, channel: str) -> None:
    """Queue the proof's own pending voice and create it through GET pending + POST apply-pending."""
    before = _apply_job_rows(ctx)
    code, answer = ctx.api("POST", "/api/voices/apply-pending")
    ctx.check(
        code == 200
        and answer == {"created": None, "pending": None, "job_id": None}
        and _apply_job_rows(ctx) == before,
        "apply-pending with nothing pending queues no job and says so",
        status=code,
        answer=short(answer),
    )
    pending_file = _queue_pending(ctx, name, channel)
    try:
        code, answer = ctx.api("GET", "/api/voices/pending")
        pend = (answer or {}).get("pending") or {}
        ctx.check(
            code == 200
            and pend.get("name") == name
            and pend.get("mode") == "design"
            and pend.get("channel") == channel
            and pend.get("description") == DESCRIPTION,
            "GET /api/voices/pending shows the queued voice: name, mode, channel and description",
            status=code,
            pending=short(pend),
        )
        code, queued = ctx.api("POST", "/api/voices/apply-pending")
        job_id = str(queued.get("job_id") or "")
        ctx.check(
            code == 200
            and job_id.startswith("apply_voice-")
            and (queued.get("pending") or {}).get("name") == name,
            "POST /api/voices/apply-pending queues an apply_voice job for it",
            status=code,
            answer=short(queued),
        )
        code, again = ctx.api("POST", "/api/voices/apply-pending")
        again_id = again.get("job_id")
        ctx.check(
            code == 200 and again_id in (job_id, None),
            "asking again answers the same job (or none once it ended), never a second job",
            first=job_id,
            second=again_id,
        )
        job = wait_job(ctx, job_id, timeout=300)
        ctx.check(
            job.get("status") == "done" and (job.get("result") or {}).get("created") == name,
            "the apply_voice job is done and reports the voice it created",
            status=job.get("status"),
            message=job.get("message"),
            result=short(job.get("result")),
        )
        voice_json = _voice_dir(ctx, name) / "voice.json"
        ctx.check(
            voice_json.is_file() and not pending_file.exists(),
            "the voice exists (voice.json) and config/pending_voice.json is gone",
            voice_json=voice_json.is_file(),
            pending_file=pending_file.exists(),
        )
        code, after = ctx.api("GET", "/api/voices/pending")
        ctx.check(
            code == 200 and after == {"pending": None} and _voices(ctx).get(name) == "design",
            "GET /api/voices/pending is empty and GET /api/voices lists the voice (design)",
            answer=short(after),
        )
        new_ids = sorted({r[0] for r in _apply_job_rows(ctx)} - {r[0] for r in before})
        ctx.check(new_ids == [job_id], "exactly one apply_voice job row was made", new_jobs=new_ids)
        row = db_rows(ctx, "select channel from dashboard_jobs where job_id=?", (job_id,))
        ctx.check(row == [(channel,)], "the apply_voice job is on the channel the request named", rows=row)
    finally:
        _drop_own(ctx, pending_file, name)


def _drop_own(ctx: Any, pending_file: Path, name: str) -> None:
    """Remove the proof's own request when a failed run left it; a request that is not the proof's stays."""
    try:
        if pending_file.is_file() and json.loads(pending_file.read_text("utf-8")).get("name") == name:
            pending_file.unlink()
            ctx.step("removed the proof's own unused pending request", voice=name)
    except (OSError, ValueError):
        pass


def _listed_without_card(page: Any, name: str) -> bool:
    text = page.inner_text("body")
    return name in text and "Pending voice from setup" not in text


def _click_part(ctx: Any, name: str, channel: str) -> None:
    """Library > Voices: the pending card names the proof's voice and Create it now makes it."""
    pending_file = _queue_pending(ctx, name, channel)
    try:
        code, answer = ctx.api("GET", "/api/voices/pending")
        ctx.check(
            code == 200
            and ((answer or {}).get("pending") or {}).get("name") == name
            and ((answer or {}).get("pending") or {}).get("channel") == channel,
            "the only pending voice is the proof's own, on its own channel (checked before the click)",
            answer=short(answer),
        )
        page = ctx.page()
        posts: list[tuple[str, str]] = []
        page.on("request", lambda r: posts.append((r.method, r.url)) if r.method == "POST" else None)
        page.goto(f"http://127.0.0.1:{ctx.ports['dashboard']}/#/library?tab=voices", wait_until="load")
        card = page.get_by_text("Pending voice from setup")
        card.wait_for(timeout=30000)
        text = page.inner_text("body")
        ctx.check(
            name in text and DESCRIPTION in text and "Create it now" in text,
            "Library > Voices shows the pending card with the proof's voice, its description and the button",
            card=text[max(0, text.find("Pending voice")) :][:300].replace("\n", " / "),
        )
        page.screenshot(path=str(ctx.scratch / "voices-pending.png"), full_page=True)
        page.get_by_role("button", name="Create it now").click()
        ctx.wait(
            lambda: ctx.api("GET", "/api/voices/pending")[1] == {"pending": None},
            300,
            every=2,
            what="the pending voice to be created from the page",
        )
        ctx.wait(
            lambda: _listed_without_card(page, name),
            60,
            every=2,
            what=f"the page to list {name} and drop the pending card",
        )
        page.screenshot(path=str(ctx.scratch / "voices-after.png"), full_page=True)
        sent = [u.split("/api/", 1)[1] for m, u in posts if "/api/" in u]
        ctx.check(
            "voices/apply-pending" in sent,
            "the click sent POST /api/voices/apply-pending",
            posts=sent,
        )
        job = db_rows(
            ctx,
            "select status from dashboard_jobs where kind='apply_voice' and params_json like ?",
            (f'%"{name}"%',),
        )
        ctx.check(
            job == [("done",)]
            and (_voice_dir(ctx, name) / "voice.json").is_file()
            and not pending_file.exists()
            and _voices(ctx).get(name) == "design",
            "its apply_voice job is done, the voice exists and the pending request is gone",
            job=job,
        )
    finally:
        _drop_own(ctx, pending_file, name)


@pauses_proof_channels
def run(ctx: Any) -> None:
    tag = re.sub(r"[^A-Za-z0-9]+", "-", ctx.version).strip("-")  # 0.1.15 -> 0-1-15
    ctx.step("the proof's voices are named proof-* from this version", version=ctx.version, tag=tag)
    reused: list[str] = []
    _preview_part(ctx, uuid.uuid4().hex[:6])  # a preview is cached by its text: a new text every run
    _design_part(ctx, tag, reused)
    _clone_part(ctx, tag, reused)

    # the pending parts: only on an install that holds no pending voice at all
    pending_name, click_name = f"proof-pending-{tag}", f"proof-click-{tag}"
    todo: list[str] = []
    for name in (pending_name, click_name):
        if name in _voices(ctx):
            reused.append(name)
            ctx.check(
                _voices(ctx).get(name) == "design" and (_voice_dir(ctx, name) / "voice.json").is_file(),
                "this version's voice from an earlier run is listed as a design voice, voice.json exists",
                voice=name,
            )
        else:
            todo.append(name)
    held_reason = ""
    if todo:
        code, answer = ctx.api("GET", "/api/voices/pending")
        held = (answer or {}).get("pending")
        stray = ctx.home / "config" / "pending_voice.json"
        if code != 200 or held is not None or stray.exists():
            held_reason = (
                "the install holds a pending voice that is not the proof's"
                f" ({(held or {}).get('name')!r}); the proof never presses Create it now on setup's voice"
            )
        else:
            channel = proof_channel(ctx, CHANNEL)
            if pending_name in todo:
                _pending_route_part(ctx, pending_name, channel)
            if click_name in todo:
                _click_part(ctx, click_name, channel)
    reasons: list[str] = []
    if reused:
        reasons.append(
            f"creating {', '.join(reused)}: already created by an earlier run on this version; "
            "the product has no voice delete route"
        )
    if held_reason:
        reasons.append(held_reason)
    if reasons:
        ctx.skip("; ".join(reasons))
