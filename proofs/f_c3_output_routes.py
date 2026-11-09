"""F-C3: the output and media routes behind the Queue card's buttons and images.

On a Short the proof's own job made: GET /api/outputs/{id} (the card), POST /api/outputs/{id}/uploaded (a bad
link is refused 400 bad_url and changes nothing; a youtu.be link is stored and shown in the detail, and the
`outputs` and `uploads` rows agree), GET /api/media/{id}/thumb.jpg (a JPEG), GET /api/media/{id} with a Range
header (206, 100 bytes, the Content-Range of the file's real size), GET /api/thumbs/{n}/{id} (404 file_missing
on a Short, 404 not_found for n=4). On a long video: GET /api/thumbs/{1,2,3}/{id} answer exactly the three
thumb_N.jpg files that lie beside the video. The pictures and the video are read as bytes, the answers the
page's img and video elements read.

The last part, POST /api/outputs/{id}/open-folder, ends as skipped: on a real install it opens a Windows
Explorer window that the proof has no honest way to close (no global input, no kill by name). Ported from
runs/qa/croutes/croutes.py box F-C3 and runs/qa/fc3b.py (evidence log 2026-11-09, 0.1.14).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from proofs._lib import db_rows, proof_channel, run_short, short, wait_job

PROOF = {
    "id": "f-c3-output-routes",
    "boxes": ["F-C3"],
    "install": "dev",
    "window": False,
    "needs": ["engine"],
    "timeout_s": 3600,
    "fakes": [],
}

CHANNEL = "proof-outputs"
LINK = "https://youtu.be/proof-fc3"
JPEG = b"\xff\xd8\xff"


def _raw(
    ctx: Any, method: str, path: str, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    """One dashboard request read as bytes: (status, lower-cased headers, body). No proxy."""
    url = f"http://127.0.0.1:{ctx.ports['dashboard']}{path}"
    req = urllib.request.Request(url, method=method, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=120) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def _code(body: bytes) -> Any:
    """The `error.code` of a coded refusal body."""
    try:
        return json.loads(body.decode("utf-8")).get("error", {}).get("code")
    except (ValueError, AttributeError):
        return None


def _enc(output_id: str) -> str:
    return urllib.parse.quote(output_id, safe="/")


def _new_short(ctx: Any) -> str:
    job = run_short(ctx, CHANNEL)
    ctx.check(
        job.get("status") == "done",
        "the Short job is done",
        job_id=job.get("job_id"),
        status=job.get("status"),
        message=job.get("message"),
    )
    status, outs = ctx.api("GET", f"/api/outputs?status=pending&kind=short&channel={CHANNEL}&limit=50")
    ctx.check(
        status == 200 and isinstance(outs, list) and bool(outs),
        "the job left a pending Short on the proof channel",
        status=status,
        pending=[o.get("output_id") for o in outs] if isinstance(outs, list) else outs,
    )
    return str(outs[0]["output_id"])


def _short_part(ctx: Any, oid: str) -> None:
    enc = _enc(oid)
    rows = db_rows(ctx, "select kind, status, path from outputs where output_id=?", (oid,))
    ctx.check(len(rows) == 1 and rows[0][0] == "short", "outputs row: a Short", rows=rows)
    video = Path(rows[0][2])
    status, card = ctx.api("GET", f"/api/outputs/{enc}")
    ctx.check(
        status == 200
        and card.get("output_id") == oid
        and card.get("kind") == "short"
        and card.get("status") == "pending"
        and card.get("file_exists") is True
        and card.get("folder") == str(video.parent)
        and card.get("upload") is None,
        "GET /api/outputs/{id}: the card of a pending Short whose file and folder exist",
        status=status,
        card_status=card.get("status"),
        file_exists=card.get("file_exists"),
        folder=card.get("folder"),
    )

    status, answer = ctx.api("POST", f"/api/outputs/{enc}/uploaded", {"url": "not a link"})
    err = answer.get("error", {}) if isinstance(answer, dict) else {}
    ctx.check(
        status == 400 and err.get("code") == "bad_url",
        "a link that is not https://... is refused 400 bad_url",
        status=status,
        answer=short(answer),
    )
    ctx.check(
        db_rows(ctx, "select status from outputs where output_id=?", (oid,)) == [("pending",)]
        and db_rows(ctx, "select count(*) from uploads where output_id=?", (oid,)) == [(0,)],
        "the refusal changed nothing: still pending, no uploads row",
    )

    status, answer = ctx.api("POST", f"/api/outputs/{enc}/uploaded", {"url": LINK})
    ctx.check(
        status == 200 and answer.get("status") == "uploaded" and answer.get("url") == LINK,
        "a youtu.be link is accepted: the answer says uploaded with the link",
        status=status,
        answer=short(answer),
    )
    status, card = ctx.api("GET", f"/api/outputs/{enc}")
    row = db_rows(ctx, "select status from outputs where output_id=?", (oid,))
    ups = db_rows(ctx, "select video_url from uploads where output_id=?", (oid,))
    ctx.check(
        status == 200
        and card.get("status") == "uploaded"
        and (card.get("upload") or {}).get("video_url") == LINK
        and row == [("uploaded",)]
        and ups == [(LINK,)],
        "the detail shows the link; the outputs row and the uploads row agree",
        card_status=card.get("status"),
        upload=card.get("upload"),
        rows=row,
        uploads=ups,
    )

    status, headers, body = _raw(ctx, "GET", f"/api/media/{enc}/thumb.jpg")
    ctx.check(
        status == 200 and headers.get("content-type", "").startswith("image/jpeg") and body[:3] == JPEG,
        "GET /api/media/{id}/thumb.jpg is an image/jpeg with a JPEG header",
        status=status,
        content_type=headers.get("content-type"),
        bytes=len(body),
    )

    size = video.stat().st_size
    status, headers, body = _raw(ctx, "GET", f"/api/media/{enc}", {"Range": "bytes=0-99"})
    ctx.check(
        status == 206
        and len(body) == 100
        and headers.get("content-range") == f"bytes 0-99/{size}"
        and headers.get("content-type", "").startswith("video/")
        and body[4:8] == b"ftyp",
        "GET /api/media/{id} with Range bytes=0-99 answers 206, 100 bytes of the MP4 and its true size",
        status=status,
        bytes=len(body),
        content_range=headers.get("content-range"),
        file_bytes=size,
        content_type=headers.get("content-type"),
    )

    for n in (1, 2, 3):
        status, _, body = _raw(ctx, "GET", f"/api/thumbs/{n}/{enc}")
        ctx.check(
            status == 404 and _code(body) == "file_missing",
            f"GET /api/thumbs/{n}/{{id}} on a Short: 404 file_missing (only long videos have thumb_N.jpg)",
            status=status,
            code=_code(body),
        )
    status, _, body = _raw(ctx, "GET", f"/api/thumbs/4/{enc}")
    ctx.check(
        status == 404 and _code(body) == "not_found",
        "GET /api/thumbs/4/{id}: 404 not_found (thumbnails are numbered 1 to 3)",
        status=status,
        code=_code(body),
    )


def _thumbs_of(path: str) -> list[Path]:
    return [Path(path).parent / f"thumb_{n}.jpg" for n in (1, 2, 3)]


def _find_long(ctx: Any) -> tuple[str, str] | None:
    """The newest long video that is not rejected and has its three thumb files beside it."""
    rows = db_rows(
        ctx,
        "select output_id, path from outputs where kind='long' and status != 'rejected'"
        " order by created_at desc limit 50",
    )
    for oid, path in rows:
        if Path(path).is_file() and all(t.is_file() for t in _thumbs_of(path)):
            return str(oid), str(path)
    return None


def _long_part(ctx: Any) -> None:
    found = _find_long(ctx)
    if found:
        ctx.step("using a long video the install already holds", output=found[0])
    else:
        ctx.step("no long video with thumbnails on the install: the proof makes one (master_first)")
        status, job = ctx.api("POST", "/api/jobs/run", {"channel": CHANNEL, "format": "master_first"})
        ctx.check(
            status in (200, 201, 202) and isinstance(job, dict) and bool(job.get("job_id")),
            "master_first job accepted by POST /api/jobs/run",
            status=status,
            answer=short(job),
        )
        done = wait_job(ctx, str(job["job_id"]), timeout=2700, every=10)
        ctx.check(done.get("status") == "done", "the master job is done", message=done.get("message"))
        found = _find_long(ctx)
        ctx.check(found is not None, "the job left a long video with thumb_1..3.jpg beside it")
    assert found is not None
    oid, path = found
    enc = _enc(oid)
    status, card = ctx.api("GET", f"/api/outputs/{enc}")
    ctx.check(
        status == 200 and card.get("kind") == "long" and (card.get("duration_s") or 0) > 0,
        "GET /api/outputs/{id}: the card of a long video",
        status=status,
        kind=card.get("kind"),
        duration_s=card.get("duration_s"),
    )
    for n, disk in zip((1, 2, 3), _thumbs_of(path), strict=True):
        status, headers, body = _raw(ctx, "GET", f"/api/thumbs/{n}/{enc}")
        ctx.check(
            status == 200
            and headers.get("content-type", "").startswith("image/jpeg")
            and body[:3] == JPEG
            and body == disk.read_bytes(),
            f"GET /api/thumbs/{n}/{{id}} answers the JPEG that thumb_{n}.jpg holds on disk",
            status=status,
            bytes=len(body),
            file=disk.name,
        )
    status, _, body = _raw(ctx, "GET", f"/api/thumbs/4/{enc}")
    ctx.check(
        status == 404 and _code(body) == "not_found",
        "GET /api/thumbs/4/{id} on a long video: 404 not_found",
        status=status,
    )


def run(ctx: Any) -> None:
    proof_channel(ctx, CHANNEL)
    oid = _new_short(ctx)
    _short_part(ctx, oid)
    _long_part(ctx)
    ctx.skip(
        "POST /api/outputs/{id}/open-folder opens a Windows Explorer window on the output's folder: the "
        "proof cannot close it without global input or a kill by name. Everything else of F-C3 was checked "
        "above. The runner needs a way to see and close that one window (UI Automation bound to the window "
        "the dashboard just opened) or a dashboard started with the fake folder opener"
    )
