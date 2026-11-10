"""Dashboard worked proof (H-13): `GET /api/status` and the first `GET /api/events` frame, read over HTTP.

Box F-C1 (docs/FINALIZATION.md), its status and event-stream half only: `POST /api/engine/warmup`,
`/shutdown`, `/release_gpu` and `/stop_all` are NOT exercised here (the scope step says so). The effect is
what the dashboard answers: the product version, the engine reachable, the channels it lists matching the
channel files on disk, and an event-stream frame carrying the same status object.
"""

import json

from proofs._channels import loadable_channels

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

    on_disk, unreadable, loadable = loadable_channels(ctx)
    listed = sorted(c.get("slug") for c in status.get("channels", []))
    ctx.step("channel files on disk", channels=on_disk, unreadable_profiles=unreadable)
    ctx.check(
        listed == loadable,
        "the status lists exactly the channels whose profile loads",
        listed=listed,
        loadable=loadable,
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
    # The same object, not just its version: the fields that do not change between two looks at an idle
    # install (the version, the schedule flag, and each channel's slug, state and format) must be equal.
    code, again = ctx.api("GET", "/api/status")
    ctx.check(code == 200 and isinstance(again, dict), "GET /api/status answers again", code=code)
    fields = {
        "version": lambda o: o.get("version"),
        "schedule_registered": lambda o: o.get("schedule_registered"),
        "channels": lambda o: sorted(
            (c.get("slug"), c.get("status"), c.get("format")) for c in (o.get("channels") or [])
        ),
    }
    differing = {k: [get(payload), get(again)] for k, get in fields.items() if get(payload) != get(again)}
    ctx.check(
        not differing,
        "the frame's status equals GET /api/status (version, schedule flag, channels)",
        differing=differing,
    )
