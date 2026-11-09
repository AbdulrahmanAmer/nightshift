"""Dashboard worked proof (H-13): `GET /api/status` and the first `GET /api/events` frame, read over HTTP.

Box F-C1 (docs/FINALIZATION.md), its status and event-stream half only: `POST /api/engine/warmup`,
`/shutdown`, `/release_gpu` and `/stop_all` are NOT exercised here (the scope step says so). The effect is
what the dashboard answers: the product version, the engine reachable, the channels it lists matching the
channel files on disk, and an event-stream frame carrying the same status object.
"""

import json

PROOF = {
    "id": "f-c1-status-events",
    "boxes": ["F-C1"],
    "install": "any",
    "window": False,
    "needs": ["engine"],
    "timeout_s": 120,
    "fakes": [],
}


def run(ctx):
    ctx.step(
        "scope",
        covers="GET /api/status, first frame of GET /api/events",
        not_covered="POST /api/engine/warmup, shutdown, release_gpu, stop_all",
    )
    code, status = ctx.api("GET", "/api/status")
    ctx.check(
        code == 200 and isinstance(status, dict), "GET /api/status answers 200 with an object", code=code
    )
    ctx.check(
        status.get("version") == ctx.version,
        "the status names the installed version",
        api=status.get("version"),
        install=ctx.version,
    )
    engine = status.get("engine") or {}
    ctx.check(engine.get("reachable") is True, "the engine is reachable", engine=engine)

    channels_dir = ctx.home / "config" / "channels"
    on_disk = {p.stem for p in channels_dir.glob("*.yaml")} if channels_dir.is_dir() else set()
    listed = {c.get("slug") for c in status.get("channels", [])}
    ctx.check(
        listed <= on_disk,
        "every channel the status lists has its file",
        listed=sorted(listed),
        on_disk=sorted(on_disk),
    )

    code, stream = ctx.api("GET", "/api/events?limit=1", timeout=30)
    ctx.check(code == 200 and isinstance(stream, str), "GET /api/events answers a text stream", code=code)
    data = next(
        (line[len("data:") :].strip() for line in stream.splitlines() if line.startswith("data:")), ""
    )
    frame = json.loads(data or "{}")
    ctx.check(frame.get("kind") == "status", "the first frame is a status frame", kind=frame.get("kind"))
    payload = frame.get("payload") or {}
    ctx.check(
        payload.get("version") == ctx.version and "engine" in payload,
        "the frame carries the same status object",
        frame_version=payload.get("version"),
    )
