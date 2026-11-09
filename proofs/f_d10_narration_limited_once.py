"""F-D10: the narration is limited once, at the voice stage; the Short only lifts it.

One Short on the fake engine, narrated with the preset voice Ryan (proof_channel forces that voice, never the
template channel's designed one). The voice stage must leave each narration item at -16 LUFS with its
peak held under the Short's ceiling (about -4 dBTP), and the Short's loudness stage must then add no limiter
of its own: the assembled narration (`shorts/s1/audio_raw.wav`) and the delivered audio (`audio.wav`) differ
by a plain gain, so the true peak rises by the same number of dB as the loudness does. A second limiter would
bend that relation (the peak would rise less than the loudness). Every number is measured with FFmpeg's
ebur128, not read from a product report. The delivered Short must read -14 +- 1 LUFS and -1 dBTP or lower.
The unit proofs are tests/test_audio_loudnorm.py and tests/test_voice_stage.py (CI is the record); this is
the same claim on an install. Related run: runs/qa/fd16.py (clip path).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from proofs._lib import db_rows, ebur128, ffmpeg_path, pauses_proof_channels, proof_channel, run_short

PROOF = {
    "id": "f-d10-narration-limited-once",
    "boxes": ["F-D10"],
    "install": "dev",
    "window": False,
    "needs": ["engine"],
    "timeout_s": 900,
    "fakes": [],
}

CHANNEL = "proof-narration"
VOICE_LUFS = -16.0  # what the voice stage leaves each item at (spec 7.5)
VOICE_PEAK_MAX = -3.5  # the Short's -1.5 ceiling less the 2 dB lift, with half a dB of measuring slack
MAX_ITEMS = 6


@pauses_proof_channels
def run(ctx: Any) -> None:
    if not ffmpeg_path():
        ctx.skip("FFmpeg is not on PATH: the narration cannot be measured")
    proof_channel(ctx, CHANNEL)
    job = run_short(ctx, CHANNEL)
    ctx.check(
        job.get("status") == "done",
        "the Short job is done",
        job_id=job.get("job_id"),
        status=job.get("status"),
        message=job.get("message"),
    )
    oids = [str(o) for o in (job.get("result") or {}).get("output_ids") or []]
    ctx.check(bool(oids), "the job made a Short output", output_ids=oids)
    oid = oids[0]
    rows = db_rows(ctx, "select job_id, path from outputs where output_id=?", (oid,))
    ctx.check(
        len(rows) == 1 and Path(rows[0][1]).is_file(),
        "outputs row points at the Short file in the outbox",
        rows=rows,
    )
    studio_job, short_path = str(rows[0][0]), Path(rows[0][1])
    job_dir = ctx.home / "data" / "jobs" / studio_job
    item = oid.split("/")[-1]

    manifest_file = job_dir / "audio" / "manifest.json"
    ctx.check(manifest_file.is_file(), "audio/manifest.json exists", file=str(manifest_file))
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    ids = sorted(manifest)[:MAX_ITEMS]
    ctx.check(bool(ids), "the voice stage made narration items", items=ids)
    for nid in ids:
        wav = Path(manifest[nid]["path"])
        ctx.check(wav.is_file(), f"narration item {nid} exists", file=str(wav))
        m = ebur128(ctx, wav)
        ctx.check(
            m["I_LUFS"] is not None and abs(m["I_LUFS"] - VOICE_LUFS) <= 1.0,
            f"voice stage: {nid} is at -16 +- 1 LUFS",
            lufs=m["I_LUFS"],
        )
        ctx.check(
            m["TP_dBTP"] is not None and m["TP_dBTP"] <= VOICE_PEAK_MAX,
            f"voice stage: {nid} peak is held under {VOICE_PEAK_MAX} dBTP (ceiling less the lift)",
            true_peak_dbtp=m["TP_dBTP"],
        )

    report_file = job_dir / "render" / "report.json"
    ctx.check(report_file.is_file(), "render/report.json exists", file=str(report_file))
    entry = json.loads(report_file.read_text(encoding="utf-8")).get(item) or {}
    final_wav = Path(str(entry.get("audio") or ""))
    raw_wav = final_wav.with_name("audio_raw.wav")
    ctx.check(
        final_wav.is_file() and raw_wav.is_file(),
        "the Short kept its assembled narration (audio_raw.wav) and its delivered audio (audio.wav)",
        raw=str(raw_wav),
        final=str(final_wav),
    )
    raw, final = ebur128(ctx, raw_wav), ebur128(ctx, final_wav)
    lift = None if raw["I_LUFS"] is None or final["I_LUFS"] is None else final["I_LUFS"] - raw["I_LUFS"]
    peak_rise = None
    if raw["TP_dBTP"] is not None and final["TP_dBTP"] is not None:
        peak_rise = final["TP_dBTP"] - raw["TP_dBTP"]
    ctx.check(
        lift is not None and 1.0 <= lift <= 3.5,
        "the Short lifts the narration by about 2 dB (-16 to -14 LUFS)",
        raw_lufs=raw["I_LUFS"],
        final_lufs=final["I_LUFS"],
        lift_db=None if lift is None else round(lift, 2),
    )
    ctx.check(
        lift is not None and peak_rise is not None and abs(peak_rise - lift) <= 0.5,
        "the peak rises by as much as the loudness: a plain gain, no second limiter",
        lift_db=None if lift is None else round(lift, 2),
        peak_rise_db=None if peak_rise is None else round(peak_rise, 2),
        raw_tp=raw["TP_dBTP"],
        final_tp=final["TP_dBTP"],
    )

    got = ebur128(ctx, short_path)
    ctx.check(
        got["I_LUFS"] is not None and abs(got["I_LUFS"] - (-14.0)) <= 1.0,
        "the delivered Short is -14 +- 1 LUFS",
        lufs=got["I_LUFS"],
    )
    ctx.check(
        got["TP_dBTP"] is not None and got["TP_dBTP"] <= -1.0,
        "the delivered Short's true peak is -1 dBTP or lower",
        true_peak_dbtp=got["TP_dBTP"],
    )
