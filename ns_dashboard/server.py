"""FastAPI app for the Nightshift Dashboard on 127.0.0.1:8100 (component C9, spec 20).

This module only routes: every operation lives in the framework-free service layer
(`ns_dashboard.handlers.Dashboard`). Run with `python -m ns_dashboard.server [--port 8100] [--open]`.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_dashboard import DEFAULT_PORT, media
from ns_dashboard.adapters import Adapters
from ns_dashboard.errors import AdapterUnavailable, ApiError
from ns_dashboard.handlers import Dashboard, build_state, default_static_dir
from ns_dashboard.security import is_loopback_host, is_loopback_origin
from ns_dashboard.state import AppState
from ns_dashboard.supervisor_link import EventRelay, SupervisorLink

TRUE_WORDS = {"true", "1", "on", "yes"}
CSP = (
    "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
    "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


class SafeJSONResponse(JSONResponse):
    """JSON response that stringifies anything json cannot encode (paths, datetimes)."""

    def render(self, content: Any) -> bytes:
        """Serialise `content` as UTF-8 JSON."""
        return json.dumps(content, ensure_ascii=False, default=str).encode("utf-8")


class LoopbackMiddleware:
    """Pure ASGI middleware: refuse non-loopback `Host`/`Origin`, add security headers."""

    def __init__(self, app: Any, log: Any = None) -> None:
        self.app = app
        self.log = log

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """Handle one ASGI connection."""
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        host = headers.get(b"host", b"").decode("latin-1")
        origin = headers.get(b"origin")
        origin_s = origin.decode("latin-1") if origin is not None else None
        if not is_loopback_host(host) or not is_loopback_origin(origin_s):
            if self.log is not None:
                self.log.warn("request_refused", "non-loopback Host or Origin", host=host, origin=origin_s)
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            err = {"code": "forbidden_host", "message": "Nightshift only answers on 127.0.0.1.", "hint": None}
            body = json.dumps({"error": err}).encode()
            length = str(len(body)).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", length)],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        path = scope.get("path", "")

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                extra = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer")]
                if not path.startswith("/api/"):
                    extra.append((b"content-security-policy", CSP.encode()))
                elif not path.startswith("/api/media/") and path != "/api/events":
                    extra.append((b"cache-control", b"no-store"))
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def body_json(request: Request) -> dict[str, Any]:
    """Parse the request body as a JSON object (empty body -> `{}`)."""
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ApiError(400, "bad_json", "The request body is not valid JSON.") from e
    if not isinstance(data, dict):
        raise ApiError(400, "bad_json", "The request body must be a JSON object.")
    return data


async def read_form(request: Request) -> Any:
    """Parse a multipart form (needs python-multipart)."""
    try:
        return await request.form()
    except (AssertionError, ImportError) as e:
        raise ApiError(501, "multipart_missing", "File uploads need the python-multipart package.") from e


def query_int(request: Request, name: str, default: int) -> int:
    """Integer query parameter with a default; 422 when it is not a number."""
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ApiError(422, "invalid_options", f"'{name}' must be a whole number.") from e


def ok(data: Any, status: int = 200) -> SafeJSONResponse:
    """JSON response."""
    return SafeJSONResponse(data, status_code=status)


def create_app(
    paths: Paths | None = None,
    *,
    adapters: Adapters | None = None,
    link: SupervisorLink | None = None,
    fake_services: bool = False,
    start_worker: bool = True,
    state: AppState | None = None,
    static_dir: Path | None = None,
    heartbeat_s: float = 2.0,
) -> FastAPI:
    """Build the app. `start_worker=False` leaves the job thread and the supervisor relay stopped
    (tests drive `state.worker.run_next()` directly)."""
    st = state or build_state(paths, adapters=adapters, link=link, fake_services=fake_services)
    dash = Dashboard(st, heartbeat_s=heartbeat_s)
    relay = EventRelay(st.link, st.hub, st.log)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if start_worker:
            st.worker.start()
            relay.start()
        try:
            yield
        finally:
            relay.stop()
            st.worker.stop()

    app = FastAPI(
        title="Nightshift Dashboard", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.dashboard = dash
    app.state.nightshift = st

    # ------------------------------------------------------------ errors
    @app.exception_handler(ApiError)
    async def _api_error(_req: Request, exc: ApiError) -> SafeJSONResponse:
        return ok(exc.to_body(), exc.status)

    @app.exception_handler(NsError)
    async def _ns_error(_req: Request, exc: NsError) -> SafeJSONResponse:
        body = {"error": {"code": exc.code, "message": str(exc), "hint": exc.definition.hint}}
        return ok(body, 500)

    @app.exception_handler(AdapterUnavailable)
    async def _unavailable(_req: Request, exc: AdapterUnavailable) -> SafeJSONResponse:
        return ok({"error": {"code": "unavailable", "message": str(exc), "hint": None}}, 501)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_req: Request, exc: StarletteHTTPException) -> SafeJSONResponse:
        code = "not_found" if exc.status_code == 404 else f"http_{exc.status_code}"
        return ok({"error": {"code": code, "message": str(exc.detail), "hint": None}}, exc.status_code)

    # ------------------------------------------------------------ status / engine / events
    @app.get("/api/status")
    async def status() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.engine.status))

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        raw = request.query_params.get("limit")
        limit = query_int(request, "limit", 0) if raw else None
        return StreamingResponse(
            dash.engine.events(limit or None),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/engine/warmup")
    async def engine_warmup(request: Request) -> SafeJSONResponse:
        body = await body_json(request)
        return ok(await run_in_threadpool(dash.engine.warmup, body.get("service")))

    @app.post("/api/engine/shutdown")
    async def engine_shutdown(request: Request) -> SafeJSONResponse:
        body = await body_json(request)
        return ok(await run_in_threadpool(dash.engine.shutdown, body.get("grace_s")))

    @app.post("/api/engine/stop_all")
    async def engine_stop_all() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.engine.stop_all))

    # ------------------------------------------------------------ channels
    @app.get("/api/channels")
    async def channels_list() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.channels.cards))

    @app.post("/api/channels")
    async def channels_create(request: Request) -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.channels.create, await body_json(request)), 201)

    @app.get("/api/channels/{slug}")
    async def channels_get(slug: str) -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.channels.get, slug))

    @app.put("/api/channels/{slug}")
    async def channels_update(slug: str, request: Request) -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.channels.update, slug, await body_json(request)))

    @app.post("/api/channels/{slug}/pause")
    async def channels_pause(slug: str, request: Request) -> SafeJSONResponse:
        body = await body_json(request)
        return ok(await run_in_threadpool(dash.channels.pause, slug, body.get("note")))

    @app.post("/api/channels/{slug}/resume")
    async def channels_resume(slug: str, request: Request) -> SafeJSONResponse:
        body = await body_json(request)
        return ok(await run_in_threadpool(dash.channels.resume, slug, body.get("note")))

    @app.post("/api/channels/{slug}/validate")
    async def channels_validate(slug: str, request: Request) -> SafeJSONResponse:
        draft = await body_json(request)
        return ok(await run_in_threadpool(dash.channels.validate, slug, draft or None))

    # ------------------------------------------------------------ outputs
    @app.get("/api/outputs")
    async def outputs_list(request: Request) -> SafeJSONResponse:
        q = request.query_params
        data = await run_in_threadpool(
            dash.outputs.list_outputs,
            q.get("status"),
            q.get("kind"),
            q.get("channel"),
            query_int(request, "limit", 100),
            query_int(request, "offset", 0),
        )
        return ok(data)

    @app.post("/api/outputs/{output_id:path}/approve")
    async def outputs_approve(output_id: str) -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.outputs.approve, output_id))

    @app.post("/api/outputs/{output_id:path}/reject")
    async def outputs_reject(output_id: str, request: Request) -> SafeJSONResponse:
        body = await body_json(request)
        return ok(await run_in_threadpool(dash.outputs.reject, output_id, body.get("reason")))

    @app.post("/api/outputs/{output_id:path}/uploaded")
    async def outputs_uploaded(output_id: str, request: Request) -> SafeJSONResponse:
        body = await body_json(request)
        return ok(await run_in_threadpool(dash.outputs.mark_uploaded, output_id, body.get("url")))

    @app.post("/api/outputs/{output_id:path}/open-folder")
    async def outputs_open_folder(output_id: str) -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.outputs.open_folder, output_id))

    @app.get("/api/outputs/{output_id:path}")
    async def outputs_detail(output_id: str) -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.outputs.detail, output_id))

    # ------------------------------------------------------------ media
    @app.get("/api/media/{output_id:path}/thumb.jpg")
    async def media_thumb(output_id: str) -> FileResponse:
        path = await run_in_threadpool(dash.outputs.thumb_path, output_id)
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    @app.api_route("/api/media/{output_id:path}", methods=["GET", "HEAD"])
    async def media_stream(output_id: str, request: Request) -> StreamingResponse:
        path = await run_in_threadpool(dash.outputs.media_path, output_id)
        res = media.open_range(path, request.headers.get("range"))
        headers = {k: v for k, v in res.headers.items() if k.lower() != "content-type"}
        body = res.body if request.method == "GET" else iter(())
        return StreamingResponse(
            body, status_code=res.status, headers=headers, media_type=res.headers["Content-Type"]
        )

    # ------------------------------------------------------------ uploads
    @app.post("/api/uploads")
    async def uploads_start(request: Request) -> SafeJSONResponse:
        body = await body_json(request)
        size = body.get("size")
        info = await run_in_threadpool(
            dash.jobs.uploads.start, str(body.get("filename") or ""), size if isinstance(size, int) else 0
        )
        data = info.to_dict()
        data["chunk_size"] = 8 * 1024 * 1024
        return ok(data, 201)

    @app.get("/api/uploads/{upload_id}")
    async def uploads_status(upload_id: str) -> SafeJSONResponse:
        return ok((await run_in_threadpool(dash.jobs.uploads.status, upload_id)).to_dict())

    @app.put("/api/uploads/{upload_id}")
    async def uploads_append(upload_id: str, request: Request) -> SafeJSONResponse:
        offset = query_int(request, "offset", -1)
        data = await request.body()
        info = await run_in_threadpool(dash.jobs.uploads.append, upload_id, offset, data)
        return ok(info.to_dict())

    @app.post("/api/uploads/{upload_id}/complete")
    async def uploads_complete(upload_id: str) -> SafeJSONResponse:
        return ok((await run_in_threadpool(dash.jobs.uploads.complete, upload_id)).to_dict())

    # ------------------------------------------------------------ jobs
    @app.post("/api/jobs/run")
    async def jobs_run(request: Request) -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.jobs.create_run, await body_json(request)), 201)

    @app.post("/api/jobs/verify")
    async def jobs_verify(request: Request) -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.jobs.create_verify, await body_json(request)), 201)

    @app.post("/api/jobs/clip")
    async def jobs_clip(request: Request) -> SafeJSONResponse:
        uploaded = None
        if request.headers.get("content-type", "").startswith("multipart/form-data"):
            form = await read_form(request)
            body: dict[str, Any] = {k: v for k, v in form.items() if isinstance(v, str)}
            body["rights_confirmed"] = str(body.get("rights_confirmed", "")).strip().lower() in TRUE_WORDS
            upload = form.get("file")
            if upload is not None and getattr(upload, "filename", None):
                dash.jobs.require_rights(body)
                uploaded = await run_in_threadpool(dash.jobs.import_stream, upload.filename, upload.file)
            await form.close()
        else:
            body = await body_json(request)
        return ok(await run_in_threadpool(dash.jobs.create_clip, body, uploaded), 201)

    @app.get("/api/jobs")
    async def jobs_list(request: Request) -> SafeJSONResponse:
        q = request.query_params
        data = await run_in_threadpool(
            dash.jobs.list_jobs, q.get("status"), q.get("kind"), query_int(request, "limit", 50)
        )
        return ok(data)

    @app.get("/api/jobs/{job_id}")
    async def jobs_get(job_id: str) -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.jobs.get, job_id))

    @app.post("/api/jobs/{job_id}/cancel")
    async def jobs_cancel(job_id: str) -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.jobs.cancel, job_id))

    # ------------------------------------------------------------ runs / logs / learn
    @app.get("/api/runs")
    async def runs(request: Request) -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.engine.runs, query_int(request, "limit", 30)))

    @app.get("/api/logs/tail")
    async def logs_tail(request: Request) -> SafeJSONResponse:
        q = request.query_params
        data = await run_in_threadpool(
            dash.settings.tail,
            q.get("component"),
            q.get("level"),
            query_int(request, "n", 200),
            q.get("after"),
        )
        return ok(data)

    @app.get("/api/logs/components")
    async def logs_components() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.settings.log_components))

    @app.post("/api/learn/import")
    async def learn_import(request: Request) -> SafeJSONResponse:
        form = await read_form(request)
        upload = form.get("file") or form.get("csv")
        if upload is None or not getattr(upload, "filename", None):
            raise ApiError(400, "no_file", "Choose the CSV file exported from YouTube Studio.")
        result = await run_in_threadpool(dash.jobs.import_learn, upload.filename, upload.file)
        await form.close()
        return ok(result)

    @app.get("/api/learn/summary")
    async def learn_summary() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.settings.learn_summary))

    # ------------------------------------------------------------ settings
    @app.get("/api/settings")
    async def settings_get() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.settings.get_settings))

    @app.put("/api/settings")
    async def settings_put(request: Request) -> SafeJSONResponse:
        body = await body_json(request)
        unlock = request.query_params.get("unlock", "").lower() in TRUE_WORDS
        return ok(await run_in_threadpool(dash.settings.put_settings, body, unlock))

    @app.post("/api/settings/keys")
    async def settings_keys(request: Request) -> SafeJSONResponse:
        body = await body_json(request)
        return ok(await run_in_threadpool(dash.settings.set_key, body.get("name"), body.get("value")))

    @app.post("/api/diagnostics")
    async def diagnostics() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.settings.diagnostics))

    @app.get("/api/diagnostics/latest")
    async def diagnostics_latest() -> FileResponse:
        path = await run_in_threadpool(dash.settings.latest_bundle)
        return FileResponse(path, media_type="application/zip", filename=path.name)

    # ------------------------------------------------------------ templates / voices
    @app.get("/api/templates")
    async def templates() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.settings.templates))

    @app.get("/api/templates/{template_id}/preview.png")
    async def template_preview(template_id: str) -> FileResponse:
        path = await run_in_threadpool(dash.settings.template_preview, template_id)
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/voices")
    async def voices() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.settings.voices))

    @app.post("/api/voices/preview")
    async def voices_preview(request: Request) -> Response:
        data = await run_in_threadpool(dash.settings.voice_preview, await body_json(request))
        return Response(content=data, media_type="audio/wav")

    # ------------------------------------------------------------ static UI (served last)
    root = static_dir or default_static_dir()
    app.mount("/", StaticFiles(directory=str(root), html=True, check_dir=False), name="static")

    app.add_middleware(LoopbackMiddleware, log=st.log)
    return app


def main(argv: list[str] | None = None) -> int:
    """`python -m ns_dashboard.server [--port 8100] [--open] [--fake-services]`."""
    ap = argparse.ArgumentParser(prog="python -m ns_dashboard.server", description="Nightshift Dashboard")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    ap.add_argument("--fake-services", action="store_true", help="use fake adapters (no GPU, no models)")
    args = ap.parse_args(argv)
    import uvicorn

    app = create_app(fake_services=args.fake_services)
    url = f"http://127.0.0.1:{args.port}"
    sys.stdout.write(f"Nightshift Dashboard: {url}\n")
    if args.open:
        threading.Timer(1.0, webbrowser.open, args=[url]).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
