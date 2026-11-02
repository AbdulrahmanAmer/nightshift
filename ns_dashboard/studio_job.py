"""The child process of a dashboard job: runs studio work in the studio venv (component C9).

The dashboard service lives in the supervisor venv, which has no faster-whisper, GPU DLL wheels, spaCy or
OpenCV. `ns_dashboard.studio_runner.StudioRunner` therefore starts
`<venvs\studio\Scripts\python.exe> -m ns_dashboard.studio_job <kind> --args <file.json>` and reads what this
module prints: one JSON object per line, each behind the prefix `NSJOB ` so a stray `print` from a library
cannot be mistaken for an event. The events are

- `job`: a studio job was created (`job_id`),
- `stage`: a stage starts (`stage`, `index`, `total`, `job_index`, `job_total`),
- `download`: link download progress (`percent`),
- `result`: the work is done (the payload, for example `output_ids`),
- `error`: the work failed (`code` of an `NS-E...` condition or None, `detail`, `message`).

The functions behind the kinds are the ones in `ns_dashboard.studio_bridge`, so a job runs exactly the code
that `studio run` and `studio clip` run, with the same regenerate-once policy. Imports of anything but the
standard library are lazy: the module must import in any interpreter.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

PREFIX = "NSJOB "
KINDS = ("run", "clip", "rerun", "series", "download", "transcribe")


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
    emit("stage", stage=stage, index=index, total=total, job_index=job_index, job_total=job_total)


def _paths() -> Any:
    from ns_common.paths import Paths

    return Paths.from_env()


def _acquirer(args: dict[str, Any]) -> Any:
    url = args.get("supervisor_url")
    if not url:
        return None
    from ns_dashboard.supervisor_link import make_acquirer

    return make_acquirer(str(url))


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


def do_download(args: dict[str, Any]) -> dict[str, Any]:
    """Download a link with the pinned yt-dlp; the whole process tree dies with this process on cancel."""
    from ns_dashboard import studio_bridge

    def progress(percent: float) -> None:
        emit("download", percent=percent)

    path = studio_bridge.download_source(_paths(), str(args["url"]), str(args["source_id"]), progress)
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


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "run": do_run,
    "clip": do_clip,
    "rerun": do_rerun,
    "series": do_series,
    "download": do_download,
    "transcribe": do_transcribe,
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
    except Exception as e:  # report every failure to the dashboard; it decides what the owner sees
        emit("error", **error_event(e))
        return 1
    emit("result", **result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
