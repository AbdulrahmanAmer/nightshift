"""C2 supervisor HTTP API (spec 15.3) on 127.0.0.1:8099.

Run: `python -m ns_supervisor.app [--headless] [--port N]`. The logic lives in `core.Supervisor`; this
module only maps it to FastAPI routes and error codes.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI  # pyright: ignore[reportMissingImports]
from fastapi.responses import JSONResponse, StreamingResponse  # pyright: ignore[reportMissingImports]
from pydantic import BaseModel

from ns_common.config import load_runtime
from ns_common.errors import NsError
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_supervisor.core import Supervisor
from ns_supervisor.scheduled import overdue_scheduled_jobs

_STATUS = {"NS-E201": 409, "NS-E202": 502, "NS-E203": 504}


class AcquireBody(BaseModel):
    """`POST /acquire` body."""

    service: str
    owner: str
    model_alias: str | None = None
    wait: bool = True
    timeout_s: float = 7200


class ReleaseBody(BaseModel):
    """`POST /release` body."""

    service: str
    owner: str


class ShutdownBody(BaseModel):
    """`POST /shutdown` body."""

    grace_s: float = 30


class WarmupBody(BaseModel):
    """`POST /warmup` body."""

    service: str


class EventBody(BaseModel):
    """`POST /events` body: lets the studio publish job progress to the dashboard status strip."""

    kind: str = "job"
    data: dict[str, Any] = {}


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


def create_app(sup: Supervisor, *, start_dashboard: bool = False, run_monitor: bool = True) -> FastAPI:
    """Build the FastAPI app around `sup`. `start_dashboard` warms the dashboard service at startup."""

    async def _warm_dashboard() -> None:
        try:
            await sup.warmup("dashboard")
        except NsError as e:  # the dashboard is optional until M3; the engine must still come up
            sup.log.warn("dashboard_unavailable", str(e))

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        sup.write_state()
        tasks: list[asyncio.Task[Any]] = []
        if run_monitor:
            tasks.append(asyncio.create_task(sup.run_monitor()))
        overdue = overdue_scheduled_jobs(sup.paths)
        if overdue:
            sup.log.info("scheduled_jobs_due", f"{overdue} scheduled dashboard job(s) are due", count=overdue)
        if start_dashboard or overdue:  # a headless nightly start still runs jobs that came due
            tasks.append(asyncio.create_task(_warm_dashboard()))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await t
            await sup.stop_all(include_persistent=True)
            sup.remove_state()
            sup.close_job()

    app = FastAPI(title="Nightshift Engine", lifespan=lifespan)

    @app.exception_handler(NsError)
    async def _ns_error(_request: Any, exc: NsError) -> JSONResponse:
        body = {"code": exc.code, "message": exc.definition.message, "detail": exc.detail}
        return JSONResponse(status_code=_STATUS.get(exc.code, 500), content=body)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/status")
    async def status() -> dict[str, Any]:
        return await asyncio.to_thread(sup.status)  # the GPU probe may spawn nvidia-smi

    @app.post("/acquire")
    async def acquire(body: AcquireBody) -> dict[str, Any]:
        return await sup.acquire(
            body.service, body.owner, body.model_alias, wait=body.wait, timeout_s=body.timeout_s
        )

    @app.post("/release")
    async def release(body: ReleaseBody) -> dict[str, Any]:
        return {"ok": await sup.release(body.service, body.owner)}

    @app.post("/stop_all")
    async def stop_all() -> dict[str, Any]:
        await sup.stop_all()
        return {"ok": True}

    @app.post("/shutdown")
    async def shutdown(body: ShutdownBody | None = None) -> dict[str, Any]:
        await sup.shutdown(body.grace_s if body is not None else 30)
        return {"ok": True}

    @app.post("/warmup")
    async def warmup(body: WarmupBody) -> dict[str, Any]:
        return {"state": await sup.warmup(body.service)}

    @app.post("/run_finished")
    async def run_finished() -> dict[str, Any]:
        return sup.note_run_finished()

    @app.post("/events")
    async def post_event(body: EventBody) -> dict[str, Any]:
        sup.publish(body.kind, body.data)
        return {"ok": True}

    @app.get("/events")
    async def events(limit: int = 0) -> StreamingResponse:
        async def gen() -> AsyncIterator[str]:
            q = sup.subscribe()
            try:
                data = {"event": "snapshot", **await asyncio.to_thread(sup.status)}
                snap = {"ts": sup.now().isoformat(), "kind": "service", "data": data}
                yield _sse(snap)
                sent = 1
                while not (limit and sent >= limit) and not sup.exit_requested.is_set():
                    try:
                        ev = await asyncio.wait_for(q.get(), 15)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield _sse(ev)
                    sent += 1
            finally:
                sup.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def main(argv: list[str] | None = None) -> int:
    """Entry point for `python -m ns_supervisor.app`."""
    import uvicorn  # pyright: ignore[reportMissingImports]

    ap = argparse.ArgumentParser(prog="ns_supervisor.app")
    ap.add_argument("--headless", action="store_true", help="no console title, no dashboard service")
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)

    paths = Paths.from_env()
    paths.logs.mkdir(parents=True, exist_ok=True)
    try:
        runtime = load_runtime(paths)
    except NsError:
        runtime = Runtime.default("gpu")  # no runtime.json yet: the engine still comes up
    port = args.port or runtime.supervisor_port
    if not args.headless and sys.platform == "win32":
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW("Nightshift Engine")

    sup = Supervisor(paths, runtime)
    app = create_app(sup, start_dashboard=not args.headless)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))

    async def serve() -> None:
        task = asyncio.create_task(server.serve())
        waiter = asyncio.create_task(sup.exit_requested.wait())
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        server.should_exit = True
        await task

    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
