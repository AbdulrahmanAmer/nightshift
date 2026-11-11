"""C2 supervisor HTTP API (spec 15.3) on 127.0.0.1:8099.

Run: `python -m ns_supervisor.app [--headless] [--port N]`. The logic lives in `core.Supervisor`; this
module only maps it to FastAPI routes and error codes.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI  # pyright: ignore[reportMissingImports]
from fastapi.responses import JSONResponse, StreamingResponse  # pyright: ignore[reportMissingImports]
from pydantic import BaseModel, Field, StrictInt

from installer.stage2.web import venvrepair
from ns_common import first_start
from ns_common.atomic import sweep_stale_tmp
from ns_common.config import load_install_state, load_runtime
from ns_common.errors import NsError, describe
from ns_common.logging import NsLogger
from ns_common.loopback import NO_BROWSER, LoopbackGuard
from ns_common.models import Runtime
from ns_common.paths import Paths, repo_root
from ns_common.secret_file import relock_existing
from ns_common.singleton import AlreadyRunning, FileLock
from ns_supervisor import launch_reach, notify, tmpsweep, update_check
from ns_supervisor.core import Supervisor, SupervisorConfig
from ns_supervisor.health_cfg import read_health_timeout
from ns_supervisor.scheduled import overdue_scheduled_jobs

_STATUS = {"NS-E201": 409, "NS-E202": 502, "NS-E203": 504}


class AcquireBody(BaseModel):
    """`POST /acquire` body."""

    service: str
    owner: str
    model_alias: str | None = None
    wait: bool = True
    timeout_s: float = 7200
    # The caller's own process (F-H47): the GPU is freed once it is gone. A pid that is not a positive whole
    # number (a string, 0, -5, 1.5, true) is refused with 422; missing means no check.
    pid: Annotated[StrictInt, Field(gt=0, le=0xFFFFFFFF)] | None = None


class ReleaseBody(BaseModel):
    """`POST /release` body."""

    service: str
    owner: str


class OwnerBody(BaseModel):
    """`POST /heartbeat` and `POST /release_owner` body."""

    owner: str
    unless_alive: bool = False  # /release_owner only: refuse while the owner still sends heartbeats


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
        tmpsweep.sweep_state_tmp(sup.paths)  # a crash between "create" and "swap" left a ~ns*.tmp behind
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
            body.service, body.owner, body.model_alias, wait=body.wait, timeout_s=body.timeout_s, pid=body.pid
        )

    @app.post("/release")
    async def release(body: ReleaseBody) -> dict[str, Any]:
        return {"ok": await sup.release(body.service, body.owner)}

    @app.post("/heartbeat")
    async def heartbeat(body: OwnerBody) -> dict[str, Any]:
        return {"ok": sup.heartbeat(body.owner)}

    @app.post("/release_owner")
    async def release_owner(body: OwnerBody) -> dict[str, Any]:
        if body.unless_alive:
            return await sup.release_unless_alive(body.owner)
        return {"ok": await sup.release_owner(body.owner)}

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

    # FN-1: before any route, for every method: a request a browser made, or one addressed to a name other
    # than this PC's, gets 403 NS-E207. A page that keeps trying is told to the owner in a morning note.
    app.add_middleware(
        LoopbackGuard,
        rule=NO_BROWSER,
        log=sup.log,
        # the day's count and the note are one step under the notes lock (`notify.leave_chosen_note`)
        tell=lambda choose, at: notify.leave_chosen_note(sup.paths, "Nightshift", choose, now=at),
    )
    return app


def engine_lock(paths: Paths) -> FileLock:
    """``run/supervisor.lock``: held while an engine of this home lives (the OS drops it on a crash)."""
    return FileLock(paths.home / "run" / "supervisor.lock")


def refuse_unfinished(paths: Paths) -> int:
    """D-39b, the engine's own gate (a start that did not come through `client.ensure_running`, e.g. a
    launcher's `Start-NsSupervisor` or a hand-typed `python -m ns_supervisor.app`): `venvrepair.REFUSED_EXIT`
    when an update left the environments of this program unfinished, said on stderr, in the log and once as
    a morning note; 0 when it may start. Nothing else has been done by then."""
    st = load_install_state(paths)
    why = venvrepair.start_refusal(paths.home, Path(st.app_path) if st.app_path else repo_root())
    if why is None:
        return 0
    sys.stderr.write(f"NS-W210: {why}\n")
    NsLogger("supervisor", paths.logs).warn("start_refused_unfinished_venvs", why, code="NS-W210")
    with contextlib.suppress(OSError):
        notify.leave_note(paths, "Nightshift", why,
                          once=lambda notes: any(n["message"] == why for n in notes))
    return venvrepair.REFUSED_EXIT


def main(argv: list[str] | None = None) -> int:
    """Entry point for `python -m ns_supervisor.app`.

    One engine per home. Its lifespan (state file, children, dashboard) runs before uvicorn binds the port, so
    two starts close together (Open dashboard pressed twice, the Desktop shortcut while the first engine is
    still loading) both got that far; the one that then failed to bind removed ``run/supervisor.json`` of the
    other on its way out. The lock is taken before anything else: a second start says so and exits with 0.
    """
    ap = argparse.ArgumentParser(prog="ns_supervisor.app")
    ap.add_argument("--headless", action="store_true", help="no console title, no dashboard service")
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)

    paths = Paths.from_env()
    paths.logs.mkdir(parents=True, exist_ok=True)
    refused = refuse_unfinished(paths)
    if refused:
        return refused
    lock = engine_lock(paths)
    try:
        lock.acquire()
    except AlreadyRunning as e:
        who = f" (pid {e.owner})" if e.owner else ""
        sys.stderr.write(f"The Nightshift engine is already running{who}.\n")
        return 0
    try:
        # a crash between a write and its swap leaves a ~ns*.tmp in config/ (with every key blob, for
        # secrets.dpapi); the next key write sweeps them too, this catches the ones nothing writes again
        sweep_stale_tmp(paths.config)
        # notes that could not be written when they were left (a writer that hung, a full disk) wait in the
        # spool: the start moves them into the notes the bell shows, each once (never raises)
        notify.drain_spool(paths)
        return _serve(args, paths)
    finally:
        lock.release()


def relock_secret_files(paths: Paths, log: Any) -> int:
    """Lock the key files that exist now; a WARN `secret_file_unlocked` (`NS-E120`) for each that will not.

    A `config/secrets.dpapi` written by 0.1.0 to 0.1.14 (or copied by an old restore into
    `data/backup/pre-restore-*`) can still carry Users / Authenticated Users, and nothing else would lock it
    before the next key save. The engine starts either way. Returns how many files would not lock.

    The log is not where the owner looks, so each failure is also a coded morning note
    (`run/morning_note.json`, shown by the dashboard on the Queue page): `NS-E120: <message> (<file>: <why>)
    <what to do>`, left once while it waits (the next start does not queue it again). A keys file on a drive
    without permissions is no failure: it gets the one-time `NS-W116` note when nobody has given it yet.
    """

    def say(code: str, line: str) -> None:  # the one-time NS-W116
        log.warn("secret_file_unprotected", line, code=code)
        _note_once(paths, log, _with_hint(code, line), code, "")

    problems = relock_existing(paths, notice=say)
    for path, why in problems:
        log.warn("secret_file_unlocked", f"{path}: {why}", path=str(path))
        code = why.split(":", 1)[0] if why.startswith("NS-") else "NS-E120"
        detail = why.split(": ", 1)[1] if why.startswith("NS-") and ": " in why else why
        try:
            message = describe(code).message
        except (KeyError, OSError, ValueError):
            message = "The file that holds your API keys could not be limited to your Windows account."
        _note_once(paths, log, _with_hint(code, f"{code}: {message} ({path}: {detail})"), code, str(path))
    return len(problems)


def _with_hint(code: str, line: str) -> str:
    """`line` and then what to do (the code's hint): the Queue page shows a note's text and nothing else."""
    try:
        return f"{line} {describe(code).hint}"
    except (KeyError, OSError, ValueError):
        return line


def _note_once(paths: Paths, log: Any, message: str, code: str, about: str) -> None:
    """Leave `message` as a morning note unless a note with the same code about the same file still waits
    (the owner of the notes decides that under its lock: two starts at once say it once)."""

    def waits(notes: list[notify.Note]) -> bool:
        return any(n["message"].startswith(f"{code}:") and about in n["message"] for n in notes)

    try:
        notify.leave_note(paths, "Nightshift", message, once=waits)
    except OSError as e:  # neither the notes nor their spool took it (notify logged it); never stop a start
        log.warn("morning_note_failed", str(e), message=message)


def _serve(args: argparse.Namespace, paths: Paths) -> int:
    import uvicorn  # pyright: ignore[reportMissingImports]

    # F-10c: before the engine reads a settings file, an interrupted rollback is finished or undone and the
    # first start after an older updater takes the settings copy (both never raise)
    first_start.ensure(paths.config)
    # card launcher-reach: an install an older updater brought here gets the launch script, shortcuts and a
    # task action that go through it (a thread: it never delays or stops the engine)
    launch_reach.start(paths, first_start.program_dir())
    try:
        runtime = load_runtime(paths)
    except NsError:
        runtime = Runtime.default("gpu")  # no runtime.json yet: the engine still comes up
    port = args.port or runtime.supervisor_port
    if not args.headless and sys.platform == "win32":
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW("Nightshift Engine")

    cfg = SupervisorConfig(health_timeout_s=read_health_timeout(paths))
    sup = Supervisor(paths, runtime, config=cfg, update_check=update_check.make_job(paths))
    relock_secret_files(paths, sup.log)
    app = create_app(sup, start_dashboard=not args.headless)
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", timeout_graceful_shutdown=GRACEFUL_S,
    ))

    async def serve() -> None:
        task = asyncio.create_task(server.serve())
        waiter = asyncio.create_task(sup.exit_requested.wait())
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        exit_watchdog(EXIT_BOUND_S, log=sup.log)        # D-46: from here on the exit is bounded
        server.should_exit = True
        await task

    asyncio.run(serve())
    return 0


#: D-46. The engine's way out had two waits without a bound: uvicorn waits for every request still running
#: (an `/events` stream, a `/acquire` that waits for the GPU) unless `timeout_graceful_shutdown` is set, and
#: at the very end the interpreter joins every `asyncio.to_thread` worker, one stuck in a blocking call
#: included. An engine that had stopped its services and closed its port then stayed alive holding
#: `run/supervisor.lock`, so no new engine could start and an update refused on its venv launcher.
GRACEFUL_S = 5
#: Once exit was requested, the process ends within this, whatever is still waiting (its services were
#: stopped and its state file removed by then; its Job Object ends any child left).
EXIT_BOUND_S = 20.0


def exit_watchdog(bound_s: float, *, log: Any = None, end: Any = None) -> threading.Thread:
    """A daemon thread that ends this process `bound_s` from now unless it has ended by itself. Daemon: it
    never keeps the process alive; when the process is still there, it is what ends it (`os._exit(0)`)."""
    finish = end or os._exit

    def run() -> None:
        time.sleep(bound_s)
        if log is not None:
            with contextlib.suppress(Exception):
                log.warn("exit_forced", f"the engine had not ended {bound_s:g} s after it was asked to stop")
        finish(0)

    thread = threading.Thread(target=run, name="exit-watchdog", daemon=True)
    thread.start()
    return thread


if __name__ == "__main__":
    sys.exit(main())
