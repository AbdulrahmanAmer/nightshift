"""F-B7: the review loop on one Short.

Reject with a note, "uploaded" refused while rejected, re-review (approve), mark uploaded. The Queue card
(GET /api/outputs/{id}, what the Queue page reads), the `outputs` and `uploads` rows and the files on disk
must agree
at every step. Ported from runs/qa/fb7b.py (evidence log 2026-11-09, 0.1.14).

The files are found from the output's own `outputs.path` row (what the Queue reads), never by looking for the
job id in a path: the product names a story's folder and files after its title
(`ns_studio/media/outbox.py:outbox_target`). The video's sheets sit beside it with the same stem; they move
with it (reject) and come back with it (approve).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ns_common.paths import Paths
from ns_studio.media.outbox import rejected_path
from proofs._lib import db_rows, pauses_proof_channels, proof_channel, run_short, short

PROOF = {
    "id": "f-b7-review-reject",
    "boxes": ["F-B7"],
    "install": "dev",
    "window": False,
    "needs": ["engine"],
    "timeout_s": 900,
    "fakes": [],
}

CHANNEL = "proof-review"
NOTE = "proof F-B7: hook too slow"
URL = "https://youtu.be/proof-fb7"


def _output_of(ctx: Any, job: dict[str, Any]) -> str:
    """The output the job made: the newest pending output of the proof channel."""
    status, outs = ctx.api("GET", "/api/outputs?status=all&limit=100")
    ctx.check(status == 200 and isinstance(outs, list), "outputs listed", status=status)
    mine = [o for o in outs if o.get("channel") == CHANNEL and o.get("status") == "pending"]
    ctx.check(
        bool(mine),
        "the job left a pending output on the proof channel",
        job_id=job.get("job_id"),
        pending=[o.get("output_id") for o in mine],
    )
    return str(mine[0]["output_id"])


def _row_path(ctx: Any, oid: str) -> Path:
    """`outputs.path` of the output: the file the Queue shows."""
    return Path(db_rows(ctx, "select path from outputs where output_id=?", (oid,))[0][0])


def _group(video: Path) -> list[str]:
    """The names of the video and the files beside it that carry its stem (sheets, thumbnails): the files that
    move together."""
    return sorted(p.name for p in video.parent.glob(f"{video.stem}.*") if p.is_file())


def _place(video: Path, names: list[str]) -> list[str]:
    """Those of `names` that are files in the folder of `video`."""
    return [n for n in names if (video.parent / n).is_file()]


@pauses_proof_channels
def run(ctx: Any) -> None:
    proof_channel(ctx, CHANNEL)
    job = run_short(ctx, CHANNEL)
    ctx.check(
        job.get("status") == "done",
        "the Short job is done",
        job_id=job.get("job_id"),
        status=job.get("status"),
        message=job.get("message"),
    )
    oid = _output_of(ctx, job)
    outbox, rejected = ctx.home / "outbox", ctx.home / "rejected"
    path0 = _row_path(ctx, oid)
    group = _group(path0)
    ctx.check(
        path0.is_file() and path0.is_relative_to(outbox) and path0.name in group,
        "before review: the output's file is in outbox/",
        path=str(path0),
        files=group,
    )
    ctx.check(len(group) >= 2, "before review: the video has its upload sheet beside it", files=group)
    # where the product puts a rejected file: `rejected/` mirrors the Outbox tree
    away = rejected_path(Paths(ctx.home), path0)

    status, answer = ctx.api("POST", f"/api/outputs/{oid}/reject", {"reason": NOTE})
    ctx.check(
        status == 200, "reject with a note answers 200", output=oid, status=status, answer=short(answer)
    )
    rows = db_rows(ctx, "select status, review_note from outputs where output_id=?", (oid,))
    ctx.check(rows == [("rejected", NOTE)], "outputs row: rejected with the note", rows=rows)
    status, card = ctx.api("GET", f"/api/outputs/{oid}")
    ctx.check(
        status == 200 and card.get("status") == "rejected" and card.get("review_note") == NOTE,
        "Queue card: rejected with the note",
        card_status=card.get("status"),
        note=card.get("review_note"),
    )
    path1 = _row_path(ctx, oid)
    in_rejected = _place(away, group)
    left = _place(path0, group)
    ctx.check(
        in_rejected == group
        and not left
        and path1 == away
        and path1.is_file()
        and path1.is_relative_to(rejected)
        and not path0.exists()
        and card.get("path") == str(path1),
        "the files MOVED to rejected/: none left in outbox/, outputs.path and the card point under rejected/",
        rejected=in_rejected,
        left_in_outbox=left,
        expected=str(away),
        path=str(path1),
        card_path=card.get("path"),
    )

    status, answer = ctx.api("POST", f"/api/outputs/{oid}/uploaded", {"url": URL})
    ctx.check(
        status == 409, "mark uploaded while rejected is refused with 409", status=status, answer=short(answer)
    )
    ctx.check(
        db_rows(ctx, "select count(*) from uploads where output_id=?", (oid,)) == [(0,)],
        "no uploads row after the refusal",
    )

    status, answer = ctx.api("POST", f"/api/outputs/{oid}/approve")
    ctx.check(status == 200, "re-review: approve answers 200", status=status, answer=short(answer))
    rows = db_rows(ctx, "select status from outputs where output_id=?", (oid,))
    ctx.check(rows == [("approved",)], "outputs row: approved", rows=rows)
    status, card = ctx.api("GET", f"/api/outputs/{oid}")
    ctx.check(
        status == 200 and card.get("status") == "approved",
        "Queue card: approved",
        card_status=card.get("status"),
    )
    path2 = _row_path(ctx, oid)
    back = _place(path0, group)
    still_away = _place(away, group)
    ctx.check(
        back == group
        and not still_away
        and path2 == path0
        and path2.is_file()
        and path2.is_relative_to(outbox)
        and not path1.exists(),
        "the files are back in outbox/ and gone from rejected/, outputs.path follows (D-6)",
        outbox=back,
        left_in_rejected=still_away,
        path=str(path2),
    )

    status, answer = ctx.api("POST", f"/api/outputs/{oid}/uploaded", {"url": URL})
    ctx.check(status == 200, "mark uploaded after approve answers 200", status=status, answer=short(answer))
    rows = db_rows(ctx, "select status from outputs where output_id=?", (oid,))
    ups = db_rows(ctx, "select video_url from uploads where output_id=?", (oid,))
    status, card = ctx.api("GET", f"/api/outputs/{oid}")
    ctx.check(
        rows == [("uploaded",)]
        and ups == [(URL,)]
        and card.get("status") == "uploaded"
        and card.get("url") == URL,
        "card, outputs row and uploads row agree: uploaded with the URL",
        rows=rows,
        uploads=ups,
        card_status=card.get("status"),
        card_url=card.get("url"),
    )
