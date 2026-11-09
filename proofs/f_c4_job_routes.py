"""F-C4: the job routes: verify, clip re-run, and the status of a chunked upload.

1. POST /api/jobs/verify {quick: true} answers 201 with a queued `verify` job and the job ends done, passed.
2. Settings > Quick check (Playwright on the page itself) sends POST /api/jobs/verify {"quick": true}; the
   answer is 201 with a new verify job, and the Self-check row reads "Passed ... Everything answered".
3. A chunked upload read with GET /api/uploads/{id} after each of three chunks: the offset grows to the bytes
   sent, the status is "uploading", and the `.part` file on disk is that long; a resent chunk is refused 409
   offset_mismatch with the true offset; complete answers the stored path and the sha256 of the bytes sent; an
   unknown upload is 404 not_found.
4. A finished clip job (max_clips 1) re-cut with POST /api/jobs/clip/rerun (max_clips 2, layout center_crop):
   the re-run is a different job that carries `rerun_of`, ends done, and its result names a clip output the
   first job did not have. That clip's file exists at another path than the first clip's, has other bytes
   (sha256) and a sidecar naming the layout; its `outputs` row belongs to the studio job the re-run ran in.
   The product re-cuts inside the same studio job (the same job folder and the same output ids for a clip
   that is cut again), so "new" means an output id the first job's result did not list. An unknown job is
   404 not_found and an unknown layout is 422 invalid_options. Needs FFmpeg to make the source video;
   without it the part ends as skipped, last.

Ported from runs/qa/croutes/croutes.py box F-C4, runs/qa/fc4.py and runs/qa/fsettings.py (evidence log
2026-11-09, 0.1.14).
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import shutil
import struct
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import Any

from proofs._lib import db_rows, ffmpeg_path, pauses_proof_channels, proof_channel, short, wait_job

PROOF = {
    "id": "f-c4-job-routes",
    "boxes": ["F-C4"],
    "install": "dev",
    "window": True,
    "needs": ["engine"],
    "timeout_s": 1500,
    "fakes": [],
}

CHANNEL = "proof-rerun"
FIXTURE = Path(__file__).parent / "_fixtures" / "f_c4_clip_rerun" / "talk.transcript.json"
STEM = "proof-rerun-talk"
PASSED = re.compile(r"Passed[^\n]*Everything answered")

# Speech-like tone, normal level: two syllable-rate tremolos (the clip QC passes on this source).
AUDIO_EXPR = (
    "0.22*sin(2*PI*140*t)*(0.5+0.5*sin(2*PI*3*t))"
    "+0.11*sin(2*PI*420*t)*(0.5+0.5*sin(2*PI*2.3*t))"
    "+0.8*sin(2*PI*900*t)*lt(mod(t\\,2)\\,0.003)"
)


def _raw(
    ctx: Any, method: str, path: str, data: bytes | None = None, headers: dict[str, str] | None = None
) -> tuple[int, Any]:
    """A dashboard request with a raw body; (status, parsed JSON or text). No proxy."""
    url = f"http://127.0.0.1:{ctx.ports['dashboard']}{path}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=120) as resp:
            status, body = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
    try:
        return status, json.loads(body.decode("utf-8"))
    except ValueError:
        return status, body.decode("utf-8", "replace")


def _err(answer: Any) -> dict[str, Any]:
    err = answer.get("error") if isinstance(answer, dict) else None
    return err if isinstance(err, dict) else {}


def _wav_bytes(seconds: float = 6.0, sr: int = 24000) -> bytes:
    """A 6 s mono 16-bit tone (288,044 bytes) for the chunked upload."""
    frames = b"".join(
        struct.pack("<h", int(9000 * math.sin(2 * math.pi * 220 * i / sr))) for i in range(int(sr * seconds))
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(frames)
    return buf.getvalue()


def _verify_route(ctx: Any) -> None:
    status, job = ctx.api("POST", "/api/jobs/verify", {"quick": True})
    ctx.check(
        status == 201 and isinstance(job, dict) and job.get("kind") == "verify" and bool(job.get("job_id")),
        "POST /api/jobs/verify {quick: true} answers 201 with a verify job",
        status=status,
        answer=short(job),
    )
    done = wait_job(ctx, str(job["job_id"]), timeout=600, every=3)
    ctx.check(
        done.get("status") == "done" and (done.get("result") or {}).get("passed") is True,
        "the self-check job ends done, passed",
        status=done.get("status"),
        message=done.get("message"),
        passed=(done.get("result") or {}).get("passed"),
    )


def _quick_check_click(ctx: Any) -> None:
    """Settings > Quick check: the page sends the route, and the Self-check row reads Passed."""
    status, before = ctx.api("GET", "/api/jobs?kind=verify&limit=50")
    known = {j.get("job_id") for j in before} if isinstance(before, list) else set()
    page = ctx.page()
    page.goto(f"http://127.0.0.1:{ctx.ports['dashboard']}/#/settings", wait_until="load")
    button = page.get_by_role("button", name="Quick check")
    button.wait_for(timeout=30000)
    with page.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith("/api/jobs/verify"), timeout=120000
    ) as seen:
        button.click(timeout=120000)
    response = seen.value
    sent = response.request.post_data
    answer = response.json()
    ctx.check(
        response.status == 201 and json.loads(sent or "null") == {"quick": True} and answer.get("job_id"),
        "the Quick check button sent POST /api/jobs/verify {quick: true} and got 201 with a job",
        sent=sent,
        status=response.status,
        job_id=answer.get("job_id"),
    )
    ctx.check(answer.get("job_id") not in known, "it is a new verify job, not an earlier one")
    done = wait_job(ctx, str(answer["job_id"]), timeout=600, every=3)
    ctx.check(done.get("status") == "done", "the button's job ends done", message=done.get("message"))
    status, newest = ctx.api("GET", "/api/jobs?kind=verify&limit=1")
    ctx.check(
        status == 200 and newest and newest[0].get("job_id") == answer["job_id"],
        "the page's source for the row (newest verify job) is the button's job",
        status=status,
    )
    page.reload(wait_until="load")
    ctx.wait(
        lambda: PASSED.search(page.inner_text("body")),
        120,
        every=2,
        what="the Settings Self-check row to read Passed ... Everything answered",
    )
    row = PASSED.search(page.inner_text("body"))
    ctx.check(
        row is not None,
        "the Self-check row reads Passed ... Everything answered",
        row=row.group(0) if row else None,
    )
    page.screenshot(path=str(ctx.scratch / "settings-selfcheck.png"))


def _upload_part(ctx: Any) -> None:
    data = _wav_bytes()
    third = -(-len(data) // 3)
    chunks = [data[i : i + third] for i in range(0, len(data), third)]
    status, up = ctx.api("POST", "/api/uploads", {"filename": "proof-chunked.wav", "size": len(data)})
    ctx.check(
        status == 201 and isinstance(up, dict) and bool(up.get("upload_id")) and up.get("offset") == 0,
        "POST /api/uploads starts an upload at offset 0",
        status=status,
        answer=short(up),
        size=len(data),
        chunks=len(chunks),
    )
    uid = str(up["upload_id"])
    folder = ctx.home / "imports" / "clipper" / uid
    try:
        sent = 0
        for i, chunk in enumerate(chunks, 1):
            status, put = _raw(
                ctx,
                "PUT",
                f"/api/uploads/{uid}?offset={sent}",
                chunk,
                {"Content-Type": "application/octet-stream"},
            )
            sent += len(chunk)
            ctx.check(status == 200, f"chunk {i} of {len(chunks)} accepted", status=status, answer=short(put))
            status, st = ctx.api("GET", f"/api/uploads/{uid}")
            part = folder / "source.wav.part"
            on_disk = part.stat().st_size if part.exists() else None
            ctx.check(
                status == 200
                and st.get("offset") == sent
                and st.get("size") == len(data)
                and st.get("status") == "uploading"
                and on_disk == sent,
                f"GET /api/uploads/{{id}} after chunk {i}: offset {sent} of {len(data)}, uploading, "
                "the .part file is that long",
                status=status,
                offset=st.get("offset"),
                size=st.get("size"),
                upload_status=st.get("status"),
                part_bytes=on_disk,
            )
            if i == 1:
                status, again = _raw(
                    ctx,
                    "PUT",
                    f"/api/uploads/{uid}?offset=0",
                    chunks[0],
                    {"Content-Type": "application/octet-stream"},
                )
                ctx.check(
                    status == 409
                    and _err(again).get("code") == "offset_mismatch"
                    and (_err(again).get("details") or {}).get("offset") == sent,
                    "a resent chunk 1 is refused 409 offset_mismatch with the true offset",
                    status=status,
                    answer=short(again),
                )
        status, done = ctx.api("POST", f"/api/uploads/{uid}/complete")
        final = Path(str(done.get("path"))) if isinstance(done, dict) else Path("missing")
        want = hashlib.sha256(data).hexdigest()
        ctx.check(
            status == 200
            and done.get("status") == "complete"
            and done.get("sha256") == want
            and final.is_file()
            and hashlib.sha256(final.read_bytes()).hexdigest() == want
            and not (folder / "source.wav.part").exists(),
            "complete answers the stored path and the sha256 of the bytes sent; the file on disk has it",
            status=status,
            answer=short(done),
        )
        status, st = ctx.api("GET", f"/api/uploads/{uid}")
        ctx.check(
            status == 200 and st.get("status") == "complete" and st.get("offset") == len(data),
            "GET /api/uploads/{id} after complete: complete, offset = size",
            answer=short(st),
        )
    finally:
        if folder.is_dir() and folder.resolve().is_relative_to(ctx.home.resolve()):
            shutil.rmtree(folder, ignore_errors=True)
            ctx.step("the proof's own upload folder removed", folder=str(folder.relative_to(ctx.home)))
    status, answer = ctx.api("GET", "/api/uploads/src-00000000-0000")
    ctx.check(
        status == 404 and _err(answer).get("code") == "not_found",
        "an unknown upload is 404 not_found",
        status=status,
        answer=short(answer),
    )


def _make_source(ctx: Any, exe: str) -> Path:
    """The source video in ctx.scratch (FFmpeg as a helper process) with its transcript sidecar."""
    tr = json.loads(FIXTURE.read_text(encoding="utf-8"))
    duration = float(tr["duration_s"])
    out = ctx.scratch / f"{STEM}.mp4"
    progress = ctx.scratch / f"{STEM}.progress.txt"
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
        name="proof-rerun-source",
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
    out.with_name(f"{STEM}.transcript.json").write_text(json.dumps(tr), encoding="utf-8")
    ctx.step("source video and transcript sidecar made", file=str(out), duration_s=duration)
    return out


def _clip_rows(ctx: Any, ids: list[str]) -> list[dict[str, Any]]:
    """The `outputs` rows of `ids` with the size and sha256 of the file each one points at (None when the
    file is missing)."""
    out: list[dict[str, Any]] = []
    for oid in ids:
        rows = db_rows(
            ctx, "select output_id, job_id, channel, kind, path from outputs where output_id=?", (oid,)
        )
        if not rows:
            continue
        output_id, job_id, channel, kind, path = rows[0]
        file = Path(str(path))
        out.append(
            {
                "output_id": output_id,
                "job_id": job_id,
                "channel": channel,
                "kind": kind,
                "path": str(path),
                "bytes": file.stat().st_size if file.is_file() else None,
                "sha256": hashlib.sha256(file.read_bytes()).hexdigest() if file.is_file() else None,
            }
        )
    return out


def _rerun_part(ctx: Any) -> None:
    exe = ffmpeg_path()
    if not exe:
        ctx.skip(
            "POST /api/jobs/clip/rerun needs a finished clip job, which needs a source video: FFmpeg is "
            "not on PATH, so the proof cannot generate one (hold the laptop lock, or install FFmpeg)"
        )
    source = _make_source(ctx, str(exe))
    proof_channel(ctx, CHANNEL)
    status, job = ctx.api(
        "POST",
        "/api/jobs/clip",
        {
            "channel": CHANNEL,
            "path": str(source),
            "rights_confirmed": True,  # the proof's own generated video
            "max_clips": 1,
            "min_s": 10,
            "max_s": 40,
            "template": "auto",
        },
    )
    ctx.check(
        status in (200, 201) and isinstance(job, dict) and bool(job.get("job_id")),
        "a clip job is accepted by POST /api/jobs/clip",
        status=status,
        answer=short(job),
    )
    first = wait_job(ctx, str(job["job_id"]), timeout=600)
    ctx.check(first.get("status") == "done", "the first clip job is done", message=first.get("message"))

    status, answer = ctx.api(
        "POST", "/api/jobs/clip/rerun", {"job_id": "clip-00000000-dead", "template": "center_crop"}
    )
    ctx.check(
        status == 404 and _err(answer).get("code") == "not_found",
        "re-run of an unknown job: 404 not_found",
        status=status,
        answer=short(answer),
    )
    status, answer = ctx.api(
        "POST", "/api/jobs/clip/rerun", {"job_id": first["job_id"], "template": "no_such_layout"}
    )
    ctx.check(
        status == 422 and _err(answer).get("code") == "invalid_options",
        "re-run with an unknown layout: 422 invalid_options",
        status=status,
        answer=short(answer),
    )

    ids_first = [str(i) for i in (first.get("result") or {}).get("output_ids") or []]
    before = _clip_rows(ctx, ids_first)
    ctx.check(
        len(ids_first) == 1 and len(before) == 1 and Path(before[0]["path"]).is_file(),
        "the first clip job (max_clips 1) made one clip whose file exists",
        output_ids=ids_first,
        rows=[{k: v for k, v in r.items() if k != "sha256"} for r in before],
    )
    studio_first = (first.get("result") or {}).get("studio_job_id")

    layout = "center_crop"
    status, rerun = ctx.api(
        "POST", "/api/jobs/clip/rerun", {"job_id": first["job_id"], "template": layout, "max_clips": 2}
    )
    params = rerun.get("params") if isinstance(rerun, dict) else {}
    ctx.check(
        status == 201
        and rerun.get("kind") == "clip"
        and rerun.get("job_id") != first["job_id"]
        and (params or {}).get("rerun_of") == first["job_id"]
        and (params or {}).get("template") == layout
        and (params or {}).get("max_clips") == 2,
        f"POST /api/jobs/clip/rerun ({layout}, max_clips 2): 201, a new clip job with rerun_of the first",
        status=status,
        answer=short(rerun),
    )
    second = wait_job(ctx, str(rerun["job_id"]), timeout=600)
    result = second.get("result") or {}
    ctx.check(
        second.get("status") == "done",
        "the re-run clip job is done",
        job_id=second.get("job_id"),
        first_job_id=first["job_id"],
        message=second.get("message"),
    )
    ids_second = [str(i) for i in result.get("output_ids") or []]
    new_ids = [i for i in ids_second if i not in ids_first]
    ctx.check(
        bool(new_ids),
        "the re-run's result names a clip output the first job did not have",
        first_output_ids=ids_first,
        rerun_output_ids=ids_second,
        new_output_ids=new_ids,
    )
    studio_second = result.get("studio_job_id")
    rows = _clip_rows(ctx, new_ids)
    old_paths = {r["path"] for r in before}
    old_hashes = {r["sha256"] for r in before}
    ctx.check(
        len(rows) == len(new_ids)
        and all(
            r["kind"] == "clip"
            and r["channel"] == CHANNEL
            and r["job_id"] == studio_second
            and (params or {}).get("studio_job_id") == r["job_id"]
            and r["output_id"].startswith(f"{r['job_id']}/")
            for r in rows
        ),
        "the outputs rows of the new clips belong to the studio job the re-run ran in",
        rerun_studio_job=studio_second,
        first_studio_job=studio_first,
        rows=[{k: v for k, v in r.items() if k != "sha256"} for r in rows],
    )
    for r in rows:
        sidecar = Path(r["path"]).with_suffix(".json")
        meta = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.is_file() else {}
        ctx.check(
            r["sha256"] is not None
            and r["path"] not in old_paths
            and r["sha256"] not in old_hashes
            and meta.get("template") == layout
            and meta.get("job_id") == r["job_id"],
            f"new clip {r['output_id']}: its file exists at another path, has other bytes, cut as {layout}",
            path=r["path"],
            first_paths=sorted(old_paths),
            sha256=r["sha256"],
            first_sha256=sorted(old_hashes),
            bytes=r["bytes"],
            sidecar_template=meta.get("template"),
            sidecar_job_id=meta.get("job_id"),
        )
    kept = _clip_rows(ctx, ids_first)
    ctx.step(
        "the first clip after the re-run (the product keeps a clip it cuts again in place)",
        rows=[{k: v for k, v in r.items() if k != "sha256"} for r in kept],
        same_file_bytes=[r["sha256"] for r in kept] == [r["sha256"] for r in before],
    )


@pauses_proof_channels
def run(ctx: Any) -> None:
    _verify_route(ctx)
    _quick_check_click(ctx)
    _upload_part(ctx)
    _rerun_part(ctx)
