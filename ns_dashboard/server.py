"""FastAPI app for the Nightshift Dashboard on 127.0.0.1:8100 (component C9, spec 20).

This module only routes: every operation lives in the framework-free service layer
(`ns_dashboard.handlers.Dashboard`). Run with `python -m ns_dashboard.server [--port 8100] [--open]`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import webbrowser
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import NotModifiedResponse, StaticFiles
from starlette.types import Scope

from ns_common.errors import NsError
from ns_common.loopback import LOOPBACK_PAGES, LoopbackGuard, media_type
from ns_common.paths import Paths
from ns_dashboard import DEFAULT_PORT, media
from ns_dashboard.adapters import Adapters
from ns_dashboard.assistant.llm import LlmProvider
from ns_dashboard.errors import AdapterUnavailable, ApiError
from ns_dashboard.events import sse_frame
from ns_dashboard.handlers import Dashboard, build_state, default_static_dir
from ns_dashboard.state import AppState
from ns_dashboard.supervisor_link import EventRelay, SupervisorLink
from ns_dashboard.uploads import tmp_dir
from ns_studio.errors import hint_of

TRUE_WORDS = {"true", "1", "on", "yes"}
CSP = (
    "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
    "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


#: A built file whose name carries its content hash (`assets/geist-latin-wght-normal-BgDaEnEv.woff2`, the
#: Vite `[name]-[hash]` pattern): the name changes whenever the bytes do, so it can be cached for good.
#: The hash part must hold a digit or a capital (a lower-case word such as `assets/foo-original.png` is not a
#: hash) and the extension must be one the build emits there, so a hand-added file is never cached for a year.
HASHED_ASSET = re.compile(r"^/assets/[^/]+-([A-Za-z0-9_-]{8})\.(?:woff2?|ttf|js|css|png|jpe?g|webp|svg|gif)$")
HASH_CHARS = re.compile(r"[0-9A-Z]")

#: The two fixed-name files `index.html` loads. They change with every update under the same URL, so the page
#: names them with a version token (`./app.js?v=<token>`): a window that cached the old `app.js` (stored by a
#: version that sent no Cache-Control) asks for a URL it has never seen as soon as the page itself is new.
BUNDLE_FILES = ("app.js", "app.css")


def bundle_token(root: Path) -> str:
    """A short token that changes whenever `app.js` or `app.css` changes (modification time and size)."""
    parts: list[str] = []
    for name in BUNDLE_FILES:
        try:
            st = (root / name).stat()
        except OSError:
            parts.append(f"{name}:-")
        else:
            parts.append(f"{name}:{st.st_mtime_ns}:{st.st_size}")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]


def versioned_page(html: str, token: str) -> str:
    """`index.html` with `./app.js` and `./app.css` pointing at the versioned URLs."""
    for name in BUNDLE_FILES:
        html = html.replace(f'"./{name}"', f'"./{name}?v={token}"')
    return html


class DashboardFiles(StaticFiles):
    """`StaticFiles` that serves `index.html` with versioned bundle URLs (D-29).

    The page never answers 304 to a validator it did not issue: an `If-None-Match` or `If-Modified-Since` kept
    from an older version describes the old page (without the `?v=` token), so the page is sent again.
    """

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        root = Path(self.directory or ".").resolve()
        if status_code != 200 or Path(full_path).resolve() != root / "index.html":
            return super().file_response(full_path, stat_result, scope, status_code)
        body = versioned_page(Path(full_path).read_text(encoding="utf-8"), bundle_token(root))
        raw = body.encode("utf-8")
        etag = '"' + hashlib.sha1(raw).hexdigest()[:20] + '"'
        sent = [v.strip() for v in Headers(scope=scope).get("if-none-match", "").split(",")]
        if etag in sent:
            return NotModifiedResponse(Headers({"etag": etag}))
        head = scope.get("method") == "HEAD"
        return Response(
            b"" if head else raw,
            media_type="text/html",
            headers={"etag": etag, "content-length": str(len(raw))},
        )


def static_cache_control(path: str, status: int = 200) -> str:
    """The Cache-Control rule of one non-API path (D-29).

    `index.html` and the fixed-name bundle (`app.js`, `app.css`) change with every update under the same
    URL, so the browser must ask first: `no-cache` keeps the ETag round trip (an unchanged file is answered
    304, no body). Only a content-hashed file under `/assets/` is immutable, and an error answer never is.
    """
    hashed = HASHED_ASSET.match(path)
    if status < 400 and hashed and HASH_CHARS.search(hashed.group(1)):
        return "public, max-age=31536000, immutable"
    return "no-cache"


class SafeJSONResponse(JSONResponse):
    """JSON response that stringifies anything json cannot encode (paths, datetimes)."""

    def render(self, content: Any) -> bytes:
        """Serialise `content` as UTF-8 JSON."""
        return json.dumps(content, ensure_ascii=False, default=str).encode("utf-8")


def refusal_notes(paths: Paths) -> tuple[Callable[[str, datetime], None], Callable[[], list[dict[str, Any]]]]:
    """How the loopback check tells the owner about a page that keeps trying: the morning notes of this
    home (`run/morning_note.json`, which the bell and the Queue page read), and the notes that wait now."""
    from ns_supervisor import notify

    def tell(message: str, at: datetime) -> None:
        notify.leave_note(paths, "Nightshift", message, now=at)

    def waiting() -> list[dict[str, Any]]:
        return list(notify.read_notes(paths))

    return tell, waiting


class LoopbackMiddleware(LoopbackGuard):
    """Pure ASGI middleware: the shared loopback check (`ns_common.loopback`, the rule for a server whose
    own page is a browser), then the security and cache headers on every answer that passed.

    A refused request gets 403 `{"error": {"code": "NS-E207", ...}}`; a page that keeps trying is told to the
    owner in a morning note (`paths` given), as the engine does.
    """

    def __init__(self, app: Any, log: Any = None, paths: Paths | None = None) -> None:
        tell, waiting = refusal_notes(paths) if paths is not None else (None, None)
        super().__init__(app, rule=LOOPBACK_PAGES, log=log, tell=tell, waiting=waiting)

    async def answer(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """A request that passed the check: the app, with the headers added to its answer."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                extra = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer")]
                if not path.startswith("/api/"):
                    extra.append((b"content-security-policy", CSP.encode()))
                    rule = static_cache_control(path, int(message.get("status", 200)))
                    extra.append((b"cache-control", rule.encode()))
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
    kind = media_type(request.headers.get("content-type", ""))
    if kind != "application/json" and not kind.endswith("+json"):
        # a form's body (text/plain can carry JSON) is never read as JSON: the loopback check refuses a form
        # from anywhere but the dashboard's own page, and this keeps the parser from being a way round it
        raise ApiError(415, "not_json", "Send the request body as JSON (Content-Type: application/json).")
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
    llm_provider: LlmProvider | None = None,
) -> FastAPI:
    """Build the app. `start_worker=False` leaves the job thread and the supervisor relay stopped
    (tests drive `state.worker.run_next()` directly)."""
    st = state or build_state(paths, adapters=adapters, link=link, fake_services=fake_services)
    dash = Dashboard(st, heartbeat_s=heartbeat_s, llm_provider=llm_provider)
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
            dash.close()

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
        body = {"error": {"code": exc.code, "message": str(exc), "hint": hint_of(exc)}}
        return ok(body, 500)

    @app.exception_handler(AdapterUnavailable)
    async def _unavailable(_req: Request, exc: AdapterUnavailable) -> SafeJSONResponse:
        return ok({"error": {"code": "unavailable", "message": str(exc), "hint": None}}, 501)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_req: Request, exc: StarletteHTTPException) -> SafeJSONResponse:
        code = "not_found" if exc.status_code == 404 else f"http_{exc.status_code}"
        return ok({"error": {"code": code, "message": str(exc.detail), "hint": None}}, exc.status_code)

    # ------------------------------------------------------------ status / engine / events
    @app.get("/api/health")
    async def health() -> SafeJSONResponse:
        """The supervisor's readiness probe (`ServiceDef.health_path`); without it the dashboard stayed
        "starting" and the engine showed "Loading dashboard..." forever (first dev-mode launch)."""
        return SafeJSONResponse({"ok": True, "service": "dashboard"})

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

    @app.post("/api/engine/release_gpu")
    async def engine_release_gpu(request: Request) -> SafeJSONResponse:
        body = await body_json(request)
        return ok(await run_in_threadpool(dash.engine.release_gpu, body.get("owner")))

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

    @app.get("/api/channels/{slug}/formats")
    async def channels_formats(slug: str) -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.channels.formats_in_use, slug))

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
    @app.get("/api/thumbs/{n}/{output_id:path}")
    async def media_thumb_choice(n: int, output_id: str) -> FileResponse:
        path = await run_in_threadpool(dash.outputs.thumb_choice_path, output_id, n)
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

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
        form_types = ("multipart/form-data", "application/x-www-form-urlencoded")
        if request.headers.get("content-type", "").startswith(form_types):
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

    @app.post("/api/jobs/clip/rerun")
    async def jobs_clip_rerun(request: Request) -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.jobs.create_clip_rerun, await body_json(request)), 201)

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

    @app.post("/api/jobs/{job_id}/resume")
    async def jobs_resume(job_id: str) -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.jobs.create_resume, job_id), 201)

    @app.post("/api/jobs/{job_id}/cancel")
    async def jobs_cancel(job_id: str) -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.jobs.cancel, job_id))

    @app.post("/api/jobs/{job_id}/dismiss")
    async def jobs_dismiss(job_id: str) -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.jobs.dismiss, job_id))

    # ------------------------------------------------------------ runs / logs / learn
    @app.get("/api/notes")
    async def notes() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.engine.notes))

    @app.post("/api/notes/clear")
    async def notes_clear() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.engine.clear_notes))

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

    @app.post("/api/settings/export")
    async def settings_export() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.settings.export_settings))

    @app.get("/api/settings/export/latest")
    async def settings_export_latest() -> FileResponse:
        path = await run_in_threadpool(dash.settings.latest_backup)
        return FileResponse(path, media_type="application/zip", filename=path.name)

    @app.post("/api/settings/import")
    async def settings_import(request: Request) -> SafeJSONResponse:
        form = await read_form(request)
        upload = form.get("file")
        if upload is None or not getattr(upload, "filename", None):
            raise ApiError(400, "no_file", "Choose the nightshift-backup zip that Export settings made.")
        result = await run_in_threadpool(dash.settings.import_settings, upload.filename, upload.file)
        await form.close()
        return ok(result)

    @app.get("/api/settings/uninstall")
    async def settings_uninstall() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.settings.uninstall_info))

    # ------------------------------------------------------------ templates / voices
    @app.get("/api/templates")
    async def templates() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.settings.templates))

    @app.get("/api/voices")
    async def voices() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.settings.voices))

    @app.post("/api/voices/preview")
    async def voices_preview(request: Request) -> Response:
        data = await run_in_threadpool(dash.settings.voice_preview, await body_json(request))
        return Response(content=data, media_type="audio/wav")

    @app.get("/api/voices/pending")
    async def voices_pending() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.voices.pending))

    @app.post("/api/voices/apply-pending")
    async def voices_apply_pending() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.voices.apply_pending))

    @app.post("/api/voices/design")
    async def voices_design(request: Request) -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.voices.design, await body_json(request)))

    @app.post("/api/voices/clone")
    async def voices_clone(request: Request) -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.voices.clone, await body_json(request)))

    # ------------------------------------------------------------ library
    @app.get("/api/library/ideas")
    async def library_ideas(request: Request) -> SafeJSONResponse:
        q = request.query_params
        data = await run_in_threadpool(
            dash.library.list_ideas,
            q.get("channel"),
            q.get("status"),
            q.get("q"),
            query_int(request, "limit", 200),
        )
        return ok(data)

    @app.post("/api/library/ideas")
    async def library_ideas_add(request: Request) -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.library.add_idea, await body_json(request)), 201)

    @app.put("/api/library/ideas/{idea_id}")
    async def library_ideas_status(idea_id: str, request: Request) -> SafeJSONResponse:
        if not idea_id.isdigit():
            raise ApiError(404, "not_found", "That idea was not found.")
        body = await body_json(request)
        return ok(await run_in_threadpool(dash.library.set_idea_status, int(idea_id), body.get("status")))

    @app.get("/api/library/stories")
    async def library_stories(request: Request) -> SafeJSONResponse:
        q = request.query_params
        data = await run_in_threadpool(
            dash.library.stories, q.get("q"), q.get("channel"), query_int(request, "limit", 50)
        )
        return ok(data)

    @app.get("/api/library/footage")
    async def library_footage(request: Request) -> SafeJSONResponse:
        data = await run_in_threadpool(
            dash.library.footage, request.query_params.get("q"), query_int(request, "limit", 60)
        )
        return ok(data)

    @app.post("/api/library/footage/fetch")
    async def library_footage_fetch(request: Request) -> SafeJSONResponse:
        b = await body_json(request)
        data = await run_in_threadpool(
            dash.library.fetch_footage, b.get("q"), b.get("orientation"), b.get("count"), b.get("source")
        )
        return ok(data)

    @app.delete("/api/library/footage/{source}/{clip_id}")
    async def library_footage_delete(source: str, clip_id: str) -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.library.delete_footage, source, clip_id))

    @app.get("/api/library/formats")
    async def library_formats() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.library.list_formats))

    @app.post("/api/library/formats")
    async def library_formats_create(request: Request) -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.library.create_format, await body_json(request)), 201)

    @app.put("/api/library/formats/{format_id}")
    async def library_formats_update(format_id: str, request: Request) -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.library.update_format, format_id, await body_json(request)))

    @app.get("/api/library/series")
    async def library_series() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.library.series))

    @app.post("/api/library/series/{slug}/produce")
    async def library_series_produce(slug: str) -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.library.produce_next_part, slug), 201)

    # ------------------------------------------------------------ update
    @app.get("/api/update/check")
    async def update_check() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.library.update_check))

    @app.get("/api/update/state")
    async def update_state() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.library.update_state))

    @app.post("/api/models/get")
    async def models_get(request: Request) -> SafeJSONResponse:
        body = await body_json(request)
        return ok(await run_in_threadpool(dash.library.models_get, body.get("name")), 202)

    @app.get("/api/models/state")
    async def models_state() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.library.models_state))

    @app.post("/api/update/file")
    async def update_file(request: Request) -> SafeJSONResponse:
        form = await read_form(request)
        upload = form.get("file")
        side = form.get("checksum")
        if upload is None or not getattr(upload, "filename", None):
            raise ApiError(400, "no_file", "Choose the Nightshift-Update zip and its .sha256 file.")
        side_fh = side.file if side is not None and getattr(side, "filename", None) else None
        apply = dash.library.update_apply_file
        result = await run_in_threadpool(apply, upload.filename, upload.file, side_fh)
        await form.close()
        return ok(result)

    @app.post("/api/update/apply")
    async def update_apply() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.library.update_apply))

    # ------------------------------------------------------------ assistant
    @app.post("/api/assistant/chat")
    async def assistant_chat(request: Request) -> StreamingResponse:
        body = await body_json(request)
        context = body.get("context")
        events = dash.assistant.chat(
            str(body.get("message") or ""),
            body.get("conversation_id") or None,
            context if isinstance(context, dict) else None,
        )

        def frames() -> Iterator[str]:
            for e in events:
                yield sse_frame(e["kind"], e["payload"])

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/assistant/confirm")
    async def assistant_confirm(request: Request) -> SafeJSONResponse:
        body = await body_json(request)
        return ok(await run_in_threadpool(dash.assistant.confirm, str(body.get("action_id") or "")))

    @app.post("/api/assistant/dismiss")
    async def assistant_dismiss(request: Request) -> SafeJSONResponse:
        body = await body_json(request)
        return ok(await run_in_threadpool(dash.assistant.dismiss, str(body.get("action_id") or "")))

    @app.get("/api/assistant/history")
    async def assistant_history(request: Request) -> SafeJSONResponse:
        cid = request.query_params.get("conversation_id") or ""
        return ok(await run_in_threadpool(dash.assistant.history, cid))

    @app.get("/api/assistant/conversations")
    async def assistant_conversations() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.assistant_store.conversations))

    @app.get("/api/assistant/tools")
    async def assistant_tools() -> SafeJSONResponse:
        return ok([
            {"name": t.spec.name, "description": t.spec.description, "side_effect": t.spec.side_effect,
             "topic": t.spec.topic}
            for t in dash.registry.all()
        ])

    @app.post("/api/assistant/queue/run")
    async def assistant_queue_run() -> SafeJSONResponse:
        return ok(await run_in_threadpool(dash.assistant.run_queued))

    # ------------------------------------------------------------ static UI (served last)
    root = static_dir or default_static_dir()
    # StaticFiles raises at request time for a missing folder, so mount it only when it exists
    if root.is_dir():
        app.mount("/", DashboardFiles(directory=str(root), html=True), name="static")

    app.add_middleware(LoopbackMiddleware, log=st.log, paths=st.paths)
    return app


def configure_tmp(paths: Paths) -> Path:
    """Point Starlette's upload spooling and Python's temporary files at the `tmp` folder of the home.

    A multipart upload is spooled to a temporary file before the route sees it; by default that is `%TEMP%`
    on C:, so a 30 GB recording needed that much room there and on the Nightshift drive. Leftovers from a
    crashed run are removed.
    """
    folder = tmp_dir(paths)
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.iterdir():
        if old.is_file():
            old.unlink(missing_ok=True)
    tempfile.tempdir = str(folder)
    return folder


def main(argv: list[str] | None = None) -> int:
    """`python -m ns_dashboard.server [--port 8100] [--open] [--fake-services]`."""
    ap = argparse.ArgumentParser(prog="python -m ns_dashboard.server", description="Nightshift Dashboard")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    ap.add_argument("--fake-services", action="store_true", help="use fake adapters (no GPU, no models)")
    args = ap.parse_args(argv)
    import uvicorn

    configure_tmp(Paths.from_env())
    app = create_app(fake_services=args.fake_services)
    url = f"http://127.0.0.1:{args.port}"
    sys.stdout.write(f"Nightshift Dashboard: {url}\n")
    if args.open:
        threading.Timer(1.0, webbrowser.open, args=[url]).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
