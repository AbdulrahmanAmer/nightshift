"""F-B5: a clip job from a web link.

A generated speech-like video is served over a local http link (the install's pinned yt-dlp downloads it with
its generic extractor). Three things are shown, each at its effect:

1. `file://` is refused by design: POST /api/jobs/clip answers 400 `bad_url` and no clip job is queued.
2. The same link served slowly and cancelled while the `.part` file is growing: the job ends `cancelled` and
   the `_downloads/dl-*` folder, the `.part` file and the source folder are gone (D-9).
3. The link served at full speed: the clip job ends `done`, the `outputs` row and the clip file exist, the
   `sources` row names the proof channel and the stored source is byte for byte what the link served.

Ported from runs/qa/fb5.py (evidence log 2026-11-09, 0.1.14). The box also names a real YouTube link, which
needs the internet and a third party: after all the checks above the proof ends as skipped for that part,
never as a pass.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from proofs._lib import db_rows, ffmpeg_path, proof_channel, short, wait_job

PROOF = {
    "id": "f-b5-url-clip",
    "boxes": ["F-B5"],
    "install": "dev",
    "window": False,
    "needs": ["engine"],
    "timeout_s": 900,
    "fakes": [],
}

CHANNEL = "proof-clip"
DURATION_S = 40
SEG_S = 8
CHUNK = 16384
SENTENCES = [
    "The keeper climbed the stairs every evening to light the great lamp above the sea.",
    "Nobody in the village remembered who had built the tower or why it faced the open water.",
    "On the coldest night of the winter the lamp went dark and the whole coast fell silent.",
    "A small boat came out of the fog and the keeper held up a lantern to guide it home.",
    "By morning the fog had cleared and the tower stood quiet against a pale and empty sky.",
]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _sidecar() -> dict[str, Any]:
    """A transcript in the product's Transcript schema; the words fall where the generated tone is audible."""
    segments: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []
    for i, sentence in enumerate(SENTENCES):
        tokens = sentence.split()
        start = i * SEG_S + 0.3
        slot = (SEG_S - 1.3) / len(tokens)
        for j, token in enumerate(tokens):
            w_start = start + j * slot
            w_end = w_start + 0.85 * slot
            words.append({"w": token, "start": round(w_start, 3), "end": round(w_end, 3), "seg": i})
        segments.append({"i": i, "start": round(start, 3), "end": words[-1]["end"], "text": sentence})
    return {
        "source": "source.mp4",
        "duration_s": float(DURATION_S),
        "language": "en",
        "segments": segments,
        "words": words,
    }


def _make_video(ctx: Any, out: Path, token: str) -> None:
    """A 40 s 1280x720 test pattern with a tone that goes silent at the end of every 8 s segment. The comment
    tag is unique per run, so the file (and the product's content-hash source id) is new every time."""
    tone = (
        f"sine=frequency=440:sample_rate=48000:duration={DURATION_S},volume=4,"
        f"volume=0:enable='gte(mod(t,{SEG_S}),7.2)'"
    )
    proc = ctx.spawn(
        [
            str(ffmpeg_path()),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=1280x720:rate=12:duration={DURATION_S}",
            "-f",
            "lavfi",
            "-i",
            tone,
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-t",
            str(DURATION_S),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "12",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "48000",
            "-ac",
            "1",
            "-metadata",
            f"comment=proof-f-b5-{token}",
            "-movflags",
            "+faststart",
            str(out),
        ],
        name="ffmpeg-make-video",
    )
    code = proc.wait(timeout=300)
    ctx.check(
        code == 0 and out.is_file() and out.stat().st_size > 0,
        "the proof's source video was generated with FFmpeg",
        exit_code=code,
        file=str(out),
        bytes=out.stat().st_size if out.is_file() else None,
    )


def _serve(data: bytes) -> tuple[http.server.ThreadingHTTPServer, dict[str, float]]:
    """A local http server that sends `data` at `state['bps']` bytes per second (changed between phases)."""
    state: dict[str, float] = {"bps": 50_000_000.0, "requests": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            state["requests"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            for i in range(0, len(data), CHUNK):
                try:
                    self.wfile.write(data[i : i + CHUNK])
                    self.wfile.flush()
                except OSError:  # the client (yt-dlp) went away: the cancel worked
                    return
                time.sleep(CHUNK / state["bps"])

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


def _clip_body(url: str) -> dict[str, Any]:
    return {
        "channel": CHANNEL,
        "url": url,
        "rights_confirmed": True,
        "max_clips": 1,
        "min_s": 10,
        "max_s": 30,
        "template": "auto",
    }


def _clip_jobs(ctx: Any) -> int:
    return int(db_rows(ctx, "select count(*) from dashboard_jobs where kind='clip'")[0][0])


def _part_bytes(folder: Path) -> int:
    """Bytes of the `.part` files under `folder` (0 when the folder or the files are not there yet)."""
    if not folder.is_dir():
        return 0
    return sum(f.stat().st_size for f in folder.rglob("*.part") if f.is_file())


def _size(path: str) -> int | None:
    p = Path(path)
    return p.stat().st_size if p.is_file() else None


def _error_code(answer: Any) -> Any:
    err = answer.get("error") if isinstance(answer, dict) else None
    return err.get("code") if isinstance(err, dict) else None


def run(ctx: Any) -> None:
    if ffmpeg_path() is None:
        ctx.skip("FFmpeg is not on PATH: the proof generates its source video with it")
    clipper = ctx.home / "imports" / "clipper"
    proof_channel(ctx, CHANNEL)

    video = ctx.scratch / "proof-f-b5-source.mp4"
    _make_video(ctx, video, uuid.uuid4().hex[:10])
    data = video.read_bytes()
    digest = _sha256(video)
    source_id = f"src-{digest[:10]}"
    src_dir = clipper / source_id
    ctx.check(
        not src_dir.exists() and not db_rows(ctx, "select 1 from sources where sha256=?", (digest,)),
        "the generated video is new to the install (no source folder, no sources row for its hash)",
        source_id=source_id,
    )

    # 1. file:// is refused by design
    queued_before = _clip_jobs(ctx)
    file_url = "file:///" + video.as_posix()
    status, answer = ctx.api("POST", "/api/jobs/clip", _clip_body(file_url))
    ctx.check(
        status == 400 and _error_code(answer) == "bad_url",
        "a file:// link is refused: 400 bad_url",
        status=status,
        answer=short(answer),
    )
    ctx.check(
        _clip_jobs(ctx) == queued_before,
        "no clip job was queued by the refused link",
        clip_jobs=_clip_jobs(ctx),
    )

    server, state = _serve(data)
    try:
        link = f"http://127.0.0.1:{server.server_address[1]}/proof-f-b5-{digest[:8]}.mp4"
        link_hash = hashlib.sha1(link.encode(), usedforsecurity=False).hexdigest()[:10]
        dl = clipper / "_downloads" / f"dl-{link_hash}"
        ctx.step("local link served by the proof", link=link, bytes=len(data), source_id=source_id)

        # 2. cancel mid-download
        state["bps"] = max(len(data) / 45, 5000.0)
        status, job = ctx.api("POST", "/api/jobs/clip", _clip_body(link))
        ctx.check(
            status == 201 and isinstance(job, dict) and job.get("job_id"),
            "slow link: POST /api/jobs/clip accepted (201)",
            status=status,
            answer=short(job),
        )
        cancel_id = job["job_id"]
        ctx.wait(lambda: _part_bytes(dl) > 0, 180, every=0.25, what="the .part file to start growing")
        part_at_cancel = _part_bytes(dl)
        ctx.check(
            0 < part_at_cancel < len(data),
            "the download is part way when the proof cancels it",
            part_bytes=part_at_cancel,
            video_bytes=len(data),
            percent=round(100 * part_at_cancel / len(data), 1),
        )
        status, answer = ctx.api("POST", f"/api/jobs/{cancel_id}/cancel")
        ctx.check(status == 200, "cancel answers 200", status=status, answer=short(answer))
        ended = wait_job(ctx, cancel_id, timeout=120, every=0.5)
        ctx.check(ended.get("status") == "cancelled", "the job ends cancelled", job=short(ended))
        ctx.wait(lambda: not dl.exists(), 60, every=0.5, what=f"{dl.name} to be removed")
        ctx.check(
            not dl.exists() and _part_bytes(dl) == 0,
            "the download folder and its .part file are gone",
            folder=str(dl),
        )
        ctx.check(
            not src_dir.exists() and not db_rows(ctx, "select 1 from sources where sha256=?", (digest,)),
            "no source folder and no sources row was made for the cancelled download",
            source_dir=str(src_dir),
        )

        # 3. the same link at full speed
        state["bps"] = 50_000_000.0
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "source.transcript.json").write_text(json.dumps(_sidecar()), encoding="utf-8")
        ctx.step(
            "transcript sidecar dropped for the dev fake transcriber (docs/DEV_MODE.md)",
            file=str(src_dir / "source.transcript.json"),
            words=sum(len(s.split()) for s in SENTENCES),
        )
        status, job = ctx.api("POST", "/api/jobs/clip", _clip_body(link))
        ctx.check(
            status == 201 and isinstance(job, dict) and job.get("job_id"),
            "local link: POST /api/jobs/clip accepted (201)",
            status=status,
            answer=short(job),
        )
        done = wait_job(ctx, job["job_id"], timeout=600, every=2)
        ctx.check(
            done.get("status") == "done",
            "the clip job from the link is done",
            status=done.get("status"),
            message=done.get("message"),
            result=short(done.get("result")),
        )
    finally:
        server.shutdown()
        server.server_close()

    ids = list((done.get("result") or {}).get("output_ids") or [])
    ctx.check(bool(ids), "the job result names its outputs", output_ids=ids)
    marks = ",".join("?" * len(ids))
    rows = [
        dict(r)
        for r in ctx.db(
            f"select output_id, kind, path, source_id, channel from outputs where output_id in ({marks})",
            tuple(ids),
        )
    ]
    ctx.check(
        len(rows) == len(ids) and all(r["kind"] == "clip" and r["channel"] == CHANNEL for r in rows),
        "outputs rows: one clip per output id, on the proof channel",
        rows=rows,
    )
    ctx.check(
        all(Path(r["path"]).is_file() and Path(r["path"]).stat().st_size > 0 for r in rows),
        "each clip file exists in the outbox and is not empty",
        files=[(r["path"], _size(r["path"])) for r in rows],
    )
    sources = db_rows(
        ctx, "select source_id, channel, rights_confirmed from sources where sha256=?", (digest,)
    )
    ctx.check(
        sources == [(source_id, CHANNEL, 1)] and all(r["source_id"] == source_id for r in rows),
        "sources row: the downloaded video under its content-hash id, rights confirmed, on the proof channel",
        sources=sources,
    )
    stored = src_dir / "source.mp4"
    ctx.check(
        stored.is_file() and _sha256(stored) == digest,
        "the stored source is byte for byte the video the link served",
        stored=str(stored),
    )
    ctx.check(not dl.exists(), "the download folder is removed after the import", folder=str(dl))
    source_json = ctx.home / "data" / "jobs" / ids[0].split("/")[0] / "source.json"
    origin = json.loads(source_json.read_text("utf-8")).get("origin")
    ctx.check(origin == link, "the job's source.json records the link as its origin", origin=origin)
    # the box also names a real short YouTube URL: it needs the internet and a third party
    ctx.skip(
        "F-B5's real short YouTube URL (yt-dlp downloading from youtube.com): the runner needs internet "
        "access to YouTube and a transcript sidecar cut to the downloaded video's length for the dev fake "
        "transcriber (or the real transcriber on an engine install); the local link above ran the same "
        "yt-dlp download stage"
    )
