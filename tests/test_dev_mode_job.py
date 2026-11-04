# ruff: noqa: E501
"""A dev install's "Create it now" (docs/DEV_MODE.md): the dashboard's real child process runs every stage of the
Shorts pipeline on the in-process fakes because `runtime.json` says `dev_mode`, without the dashboard sending a
`fake` list, and renders a real video with FFmpeg into the outbox."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ns_common import ffmpeg
from ns_common.config import save_runtime
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_dashboard.studio_runner import StudioRunner
from tests.helpers import yaml_only_channel

pytestmark = pytest.mark.skipif(not ffmpeg.available(), reason="FFmpeg required for the fake Shorts pipeline")

SHORTS_MEDIA = ["voice", "captions", "clips", "render", "qc", "outbox"]


def test_a_dev_install_runs_a_job_on_fakes_and_renders_a_real_video(tmp_home: Path):
    paths = Paths.from_env()
    paths.ensure()
    save_runtime(paths, Runtime.default("cpu-dev").model_copy(update={"dev_mode": True}))
    ch = yaml_only_channel(paths)
    seen: list[dict[str, Any]] = []
    done = StudioRunner(paths, poll_s=0.1, grace_s=2).run(
        "run", {"channel": ch.slug, "fmt": "shorts_only", "count": 1}, on_event=seen.append      # no "fake" list
    )
    assert done["output_ids"] and not any(e["ev"] == "error" for e in seen)
    names = [e["stage"] for e in seen if e["ev"] == "stage"]
    assert set(SHORTS_MEDIA) <= set(names), names
    videos = list(paths.outbox.rglob("*.mp4"))
    assert videos and all(v.stat().st_size > 1000 for v in videos)
    assert ffmpeg.duration_s(videos[0]) > 1.0
