"""The child process of a dashboard job: runs studio work in the studio venv (component C9).

The dashboard service lives in the supervisor venv, which has no faster-whisper, GPU DLL wheels, spaCy or
OpenCV. `ns_dashboard.studio_runner.StudioRunner` therefore starts
`<home>/venvs/studio/Scripts/python.exe -m ns_dashboard.studio_job <kind> --args <f>` and reads what this
module prints: one JSON object per line, each behind the prefix `NSJOB ` so a stray `print` from a library
cannot be mistaken for an event. The events are

- `job`: a studio job was created (`job_id`),
- `stage`: a stage starts (`stage`, `index`, `total`, `job_index`, `job_total`),
- `download`: link download progress (`percent`),
- `result`: the work is done (the payload, for example `output_ids`),
- `error`: the work failed (`code` of an `NS-E...` condition or None, `detail`, `message`).
- `cancelled`: the dashboard asked the job to stop (see below) and it did, after releasing what it held.

Stopping: the runner creates the file named by the environment variable `NS_JOB_CANCEL_FILE` when the owner
presses Cancel. `report` looks for it before each stage and raises `CancelRequested`, so the orchestrator
unwinds through its `finally` blocks and the GPU lease goes back to the supervisor. A stage that does not end
within the runner's grace period is killed; the dashboard then releases the lease itself.

The functions behind the kinds are the ones in `ns_dashboard.studio_bridge`, so a job runs exactly the code
that `studio run` and `studio clip` run, with the same regenerate-once policy. Imports of anything but the
standard library are lazy: the module must import in any interpreter.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

PREFIX = "NSJOB "
KINDS = ("run", "clip", "rerun", "series", "download", "transcribe", "apply_voice")
CANCEL_ENV = "NS_JOB_CANCEL_FILE"


class CancelRequested(Exception):
    """The dashboard asked this job to stop (raised between stages)."""


def cancel_requested() -> bool:
    """True when the flag file that the runner created for this process exists."""
    name = os.environ.get(CANCEL_ENV)
    return bool(name) and os.path.exists(name)


def emit(ev: str, **data: Any) -> None:
    """Print one protocol line to stdout (flushed at once so the dashboard sees progress live)."""
    sys.stdout.write(PREFIX + json.dumps({"ev": ev, **data}, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


class SeenJobs(list[str]):
    """The list `studio_bridge` fills with studio job ids; every new id is announced as a `job` event."""

    def append(self, job_id: str) -> None:
        """Remember `job_id` and tell the dashboard."""
        super().append(job_id)
        emit("job", job_id=job_id)


def report(stage: str, index: int, total: int, job_index: int, job_total: int) -> None:
    """`studio_bridge.Report`: announce a stage."""
    if cancel_requested():
        raise CancelRequested()
    emit("stage", stage=stage, index=index, total=total, job_index=job_index, job_total=job_total)


def _paths() -> Any:
    from ns_common.paths import Paths

    return Paths.from_env()


def _acquirer(args: dict[str, Any]) -> Any:
    """The supervisor as GPU owner, wrapped in `StudioAcquirer` so a held service is renewed on a thread
    (the supervisor reclaims a lease that stays silent) and released with the same code as `studio run`."""
    url = args.get("supervisor_url")
    if not url:
        return None
    from ns_common.config import load_runtime
    from ns_common.models import Runtime
    from ns_dashboard.supervisor_link import make_acquirer
    from ns_studio.acquirer import StudioAcquirer

    paths = _paths()
    try:
        runtime = load_runtime(paths)
    except Exception:  # no or unreadable runtime.json: the defaults
        runtime = Runtime.default("gpu")
    client: Any = make_acquirer(str(url))  # a SupervisorClient (has `heartbeat`) or the HTTP fallback
    return StudioAcquirer(runtime, paths, client=client, ensure=lambda _client: None)


def _common(args: dict[str, Any]) -> dict[str, Any]:
    return {"acquirer": _acquirer(args), "fake": tuple(args.get("fake") or ()), "paths": _paths()}


def do_run(args: dict[str, Any]) -> dict[str, Any]:
    """`studio run --channel <channel> --format <fmt> --count <count>` (with the regenerate-once policy)."""
    from ns_dashboard import studio_bridge

    seen = SeenJobs()
    ids = studio_bridge.run_story_jobs(
        str(args["channel"]), str(args["fmt"]), args.get("count"), report, seen=seen, **_common(args)
    )
    return {"output_ids": ids, "job_ids": list(seen)}


def do_clip(args: dict[str, Any]) -> dict[str, Any]:
    """`studio clip <source> --channel ...`; `args["params"]` are the options the dashboard stores."""
    from ns_dashboard import studio_bridge

    seen = SeenJobs()
    ids = studio_bridge.run_clip(
        Path(str(args["source_path"])), dict(args["params"]), report, seen=seen, **_common(args)
    )
    return {"output_ids": ids, "job_ids": list(seen)}


def do_rerun(args: dict[str, Any]) -> dict[str, Any]:
    """The later stages of an earlier clip job again with other options (`studio clip --reselect`)."""
    from ns_dashboard import studio_bridge

    ids = studio_bridge.rerun_clip(
        str(args["studio_job_id"]), dict(args["changes"]), report, **_common(args)
    )
    return {"output_ids": ids, "job_ids": [str(args["studio_job_id"])]}


def do_series(args: dict[str, Any]) -> dict[str, Any]:
    """Produce one approved series part (the `master_from_script` plan)."""
    from ns_dashboard import studio_bridge

    ids = studio_bridge.run_series_part(dict(args["params"]), report, **_common(args))
    return {"output_ids": ids, "job_ids": [str(args["params"]["job_id"])]}


def watch_cancel_file(
    cancel_file: Any, token: Any, stop: threading.Event, poll_s: float = 0.2
) -> threading.Thread | None:
    """Cancel `token` (kills yt-dlp, removes its folder) once `cancel_file` exists; ends on `stop`."""
    if not cancel_file:
        return None
    flag = Path(str(cancel_file))

    def watch() -> None:
        while not stop.is_set():
            if flag.exists():
                token.cancel()
                return
            stop.wait(poll_s)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    return thread


def do_download(args: dict[str, Any]) -> dict[str, Any]:
    """Download a link with the pinned yt-dlp; the `cancel_file` flag (made on Cancel) cancels the token."""
    from ns_dashboard import studio_bridge
    from ns_studio.clipper.source import CancelToken

    def progress(percent: float) -> None:
        emit("download", percent=percent)

    token = CancelToken()
    stop = threading.Event()
    watch_cancel_file(args.get("cancel_file"), token, stop)
    try:
        path = studio_bridge.download_source(
            _paths(), str(args["url"]), str(args["source_id"]), progress, cancel_token=token
        )
    finally:
        stop.set()
    return {"path": str(path)}


def do_transcribe(args: dict[str, Any]) -> dict[str, Any]:
    """The words spoken in a WAV (faster-whisper), as one text; the dashboard holds the `whisper` tenant."""
    from ns_common.config import load_runtime
    from ns_studio.media.transcribe import WhisperTranscriber

    paths = _paths()
    try:
        cfg = load_runtime(paths).whisper
    except Exception:  # no or unreadable runtime.json: the default whisper settings
        from ns_common.models import Runtime

        cfg = Runtime.default("gpu").whisper
    segs = WhisperTranscriber(cfg, paths).segments(Path(str(args["wav"])), "")
    return {"text": " ".join(seg.text for seg in segs).strip()}


def do_apply_voice(args: dict[str, Any]) -> dict[str, Any]:
    """Create the voice that setup queued (`config/pending_voice.json`); `name` is None if none was queued."""
    from ns_dashboard import studio_bridge

    return {"name": studio_bridge.apply_pending_voice(**_common(args))}


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "run": do_run,
    "clip": do_clip,
    "rerun": do_rerun,
    "series": do_series,
    "download": do_download,
    "transcribe": do_transcribe,
    "apply_voice": do_apply_voice,
}


def error_event(e: BaseException) -> dict[str, Any]:
    """Fields of the `error` event: the `NS-` code and detail of a coded condition, else the exception."""
    code = getattr(e, "code", None)
    coded = isinstance(code, str) and code.startswith("NS-")
    return {
        "code": code if coded else None,
        "detail": str(getattr(e, "detail", "")) if coded else "",
        "message": f"{type(e).__name__}: {e}",
    }


def main(argv: list[str] | None = None) -> int:
    """`python -m ns_dashboard.studio_job <kind> --args <file>`: 0 after a `result`, 1 after an `error`."""
    ap = argparse.ArgumentParser(prog="python -m ns_dashboard.studio_job", description=__doc__)
    ap.add_argument("kind", choices=KINDS)
    ap.add_argument("--args", required=True, type=Path, help="JSON file with the arguments of the kind")
    ns = ap.parse_args(argv)
    try:
        args = json.loads(ns.args.read_text(encoding="utf-8"))
        result = HANDLERS[ns.kind](args)
    except CancelRequested:
        emit("cancelled")
        return 0
    except Exception as e:  # report every failure to the dashboard; it decides what the owner sees
        emit("error", **error_event(e))
        return 1
    emit("result", **result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
