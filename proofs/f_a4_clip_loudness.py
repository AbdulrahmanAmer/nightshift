"""F-A4 (behaviour part): boost-then-limit loudness on a quiet, peaky clip source.

F-A4 itself is a merge/process box (PR #94 merged with an opus review and CI zero/zero); this proof covers
what the merged code does on an install. A quiet, peaky speech-like video (about -20 LUFS, true peak about
-0.6 dBTP: a plain gain to -14 would clip) is generated at run time, given a transcript sidecar for the dev
transcriber and cut into a clip through POST /api/jobs/clip. The render report must say the clip was lifted
and limited (`mode: limited`), and FFmpeg's ebur128 on the clip in the outbox, independent of the product's
own numbers, must read -14 +- 1 LUFS and a true peak of -1 dBTP or lower. Ported from runs/qa/fd16.py
(0.1.13: -14.1 / -1.3).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from proofs._lib import db_rows, ebur128, ffmpeg_path, pauses_proof_channels, proof_channel, short, wait_job

PROOF = {
    "id": "f-a4-clip-loudness",
    "boxes": ["F-A4"],
    "install": "dev",
    "window": False,
    "needs": ["engine"],
    "timeout_s": 900,
    "fakes": [],
}

CHANNEL = "proof-loudness"
FIXTURE = Path(__file__).parent / "_fixtures" / "loudness" / "talk.transcript.json"
STEM = "proof-loudness-talk"

# Quiet speech-like tone (two syllable-rate tremolos) plus a 3 ms burst every 2 s that makes the peaks:
# measured on the generated file at about -20.3 LUFS / -0.6 dBTP, like the quiet podcast of finding 8.
AUDIO_EXPR = (
    "0.22*sin(2*PI*140*t)*(0.5+0.5*sin(2*PI*3*t))"
    "+0.11*sin(2*PI*420*t)*(0.5+0.5*sin(2*PI*2.3*t))"
    "+0.8*sin(2*PI*900*t)*lt(mod(t\\,2)\\,0.003)"
)


def _make_source(ctx: Any, exe: str, duration_s: float) -> Path:
    """The source video in ctx.scratch, made by an FFmpeg helper process; done when it writes progress=end."""
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
            f"aevalsrc={AUDIO_EXPR}:s=22050:d={duration_s}",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x305070:s=640x360:r=30:d={duration_s}",
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
        name="proof-loudness-source",
    )

    def finished() -> bool:
        return progress.exists() and "progress=end" in progress.read_text(encoding="utf-8", errors="replace")

    ctx.wait(finished, 120, every=1, what="FFmpeg to finish the generated source video")
    return out


def _sidecar(ctx: Any, video: Path) -> float:
    """`<stem>.transcript.json` next to the video (read by the dev transcriber); returns its duration."""
    tr = json.loads(FIXTURE.read_text(encoding="utf-8"))
    tr["source"] = video.name
    side = video.with_name(f"{video.stem}.transcript.json")
    side.write_text(json.dumps(tr), encoding="utf-8")
    ctx.step(
        "transcript sidecar written", file=str(side), words=len(tr["words"]), duration_s=tr["duration_s"]
    )
    return float(tr["duration_s"])


@pauses_proof_channels
def run(ctx: Any) -> None:
    exe = ffmpeg_path()
    if not exe:
        ctx.skip("FFmpeg is not on PATH: the quiet source video cannot be generated")
    assert exe is not None
    duration = float(json.loads(FIXTURE.read_text(encoding="utf-8"))["duration_s"])
    video = _make_source(ctx, exe, duration)
    ctx.check(video.is_file() and video.stat().st_size > 0, "source video generated", file=str(video))
    _sidecar(ctx, video)

    src = ebur128(ctx, video)
    quiet_and_peaky = (
        src["I_LUFS"] is not None
        and src["TP_dBTP"] is not None
        and src["I_LUFS"] <= -17.0
        and src["TP_dBTP"] + (-14.0 - src["I_LUFS"]) > -1.0
    )
    ctx.check(
        quiet_and_peaky,
        "the source is quiet and peaky: a plain gain to -14 LUFS would put its peak over -1 dBTP",
        source_lufs=src["I_LUFS"],
        source_tp=src["TP_dBTP"],
    )

    proof_channel(ctx, CHANNEL)
    status, job = ctx.api(
        "POST",
        "/api/jobs/clip",
        {
            "channel": CHANNEL,
            "path": str(video),
            "rights_confirmed": True,  # the proof's own generated video
            "max_clips": 1,
            "min_s": 10,
            "max_s": 40,
            "template": "auto",
        },
    )
    ctx.check(
        status in (200, 201, 202) and isinstance(job, dict) and bool(job.get("job_id")),
        "clip job accepted by POST /api/jobs/clip",
        status=status,
        answer=short(job),
    )
    done = wait_job(ctx, str(job["job_id"]), timeout=600)
    ctx.check(
        done.get("status") == "done",
        "the clip job is done (QC passed on the rendered clip)",
        job_id=done.get("job_id"),
        status=done.get("status"),
        message=done.get("message"),
    )

    result = done.get("result") or {}
    oids = [str(o) for o in result.get("output_ids") or []]
    ctx.check(bool(oids), "the job made a clip output", output_ids=oids)
    oid = oids[0]
    rows = db_rows(ctx, "select job_id, kind, path from outputs where output_id=?", (oid,))
    ctx.check(
        len(rows) == 1 and rows[0][1] == "clip" and Path(rows[0][2]).is_file(),
        "outputs row points at the clip file in the outbox",
        rows=rows,
    )
    studio_job, clip_path = str(rows[0][0]), Path(rows[0][2])
    job_dir = ctx.home / "data" / "jobs" / studio_job
    item = oid.split("/")[-1]

    report_file = job_dir / "render" / "report.json"
    ctx.check(report_file.is_file(), "render/report.json exists in the job folder", file=str(report_file))
    loud = (json.loads(report_file.read_text(encoding="utf-8")).get(item) or {}).get("loudness") or {}
    ctx.step("render report loudness", item=item, **{k: loud.get(k) for k in sorted(loud)})
    ctx.check(
        loud.get("mode") == "limited",
        "render report: the clip was lifted and limited first (mode limited, not plain loudnorm)",
        mode=loud.get("mode"),
    )
    ctx.check(
        isinstance(loud.get("boost_db"), (int, float))
        and loud["boost_db"] > 0
        and isinstance(loud.get("ceiling_db"), (int, float))
        and loud["ceiling_db"] < 0,
        "render report: a positive boost and a limiter ceiling under 0 dBFS are recorded",
        boost_db=loud.get("boost_db"),
        ceiling_db=loud.get("ceiling_db"),
    )
    li, lt = loud.get("limited_i"), loud.get("limited_tp")
    ctx.check(
        isinstance(li, (int, float)) and isinstance(lt, (int, float)) and lt + (-14.0 - li) <= -1.0,
        "render report: after the limiter a plain gain to -14 LUFS keeps the peak at -1 dBTP or lower",
        limited_i=li,
        limited_tp=lt,
    )

    qc_file = job_dir / "qc" / f"{item}.json"
    qc = json.loads(qc_file.read_text(encoding="utf-8")) if qc_file.is_file() else {}
    qc_loud = (qc.get("checks") or {}).get("loudness") or {}
    ctx.check(
        qc_loud.get("passed") is True,
        "the product's QC loudness check passed",
        qc_value=qc_loud.get("value"),
        qc_limit=qc_loud.get("limit"),
    )

    got = ebur128(ctx, clip_path)
    ctx.check(
        got["I_LUFS"] is not None and abs(got["I_LUFS"] - (-14.0)) <= 1.0,
        "independent ebur128 on the clip: integrated loudness is -14 +- 1 LUFS",
        lufs=got["I_LUFS"],
        source_lufs=src["I_LUFS"],
    )
    ctx.check(
        got["TP_dBTP"] is not None and got["TP_dBTP"] <= -1.0,
        "independent ebur128 on the clip: true peak is -1 dBTP or lower",
        true_peak_dbtp=got["TP_dBTP"],
        source_tp=src["TP_dBTP"],
    )
