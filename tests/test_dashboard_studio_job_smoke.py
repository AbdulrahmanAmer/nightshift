# ruff: noqa: E501
"""A real `python -m ns_dashboard.studio_job` process, with fake services (dashboard re-review, incomplete row 1).

Every other runner test swaps the interpreter for a script or runs the handler in this process, so a break in
the `-m` entry, `_paths()`, the argument file, the real `studio_bridge` call signature or the protocol lines
would still pass CI. This starts the genuine child exactly as `StudioRunner` does (the CI interpreter stands in
for the studio venv) and runs the shorts pipeline on the fake LLM, voice, captions source and embed service.
"""

from __future__ import annotations

import json
import os
import subprocess  # noqa: TID251  (the test starts the real child process)
import sys
from pathlib import Path
from typing import Any

import pytest

from ns_common import ffmpeg
from ns_common.paths import Paths, repo_root
from ns_dashboard.studio_job import PREFIX
from ns_dashboard.studio_runner import StudioRunner, parse_event
from ns_studio.context import FAKEABLE
from tests.helpers import yaml_only_channel

pytestmark = pytest.mark.skipif(not ffmpeg.available(), reason="FFmpeg required for the fake Shorts pipeline")

SHORTS_MEDIA = ["voice", "captions", "clips", "render", "qc", "outbox"]


def child(kind: str, args: dict[str, Any], tmp_path: Path) -> tuple[int, list[dict[str, Any]], str]:
    """Start the real child; returns (exit code, protocol events, output that is not protocol)."""
    f = tmp_path / f"{kind}.json"
    f.write_text(json.dumps(args), encoding="utf-8")
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    proc = subprocess.run(
        [sys.executable, "-m", "ns_dashboard.studio_job", kind, "--args", str(f)],
        cwd=str(repo_root()), env=env, capture_output=True, text=True, encoding="utf-8", timeout=600, check=False,
    )
    events = [e for e in (parse_event(line) for line in proc.stdout.splitlines()) if e is not None]
    other = "\n".join(line for line in proc.stdout.splitlines() if not line.startswith(PREFIX))
    return proc.returncode, events, other + proc.stderr


def test_the_runner_starts_the_real_child_and_reports_its_events(tmp_home: Path):
    """The path the dashboard takes: `StudioRunner` starts the child, parses its lines and returns the payload."""
    paths = Paths.from_env()
    paths.ensure()
    ch = yaml_only_channel(paths)
    seen: list[dict[str, Any]] = []
    done = StudioRunner(paths, poll_s=0.1, grace_s=2).run(
        "run", {"channel": ch.slug, "fmt": "shorts_only", "count": 1, "fake": sorted(FAKEABLE)}, on_event=seen.append
    )
    assert done["output_ids"] and seen[0]["ev"] == "job"
    stages = [e for e in seen if e["ev"] == "stage"]
    names = [e["stage"] for e in stages]
    assert set(SHORTS_MEDIA) <= set(names), names
    assert [e["index"] for e in stages] == sorted(e["index"] for e in stages)  # progress only moves forward
    assert {e["total"] for e in stages} == {len(names)}
    assert {(e["job_index"], e["job_total"]) for e in stages} == {(0, 1)}
    assert not any(e["ev"] == "error" for e in seen)


def test_the_child_exits_1_with_a_coded_error_for_an_unknown_channel(tmp_home: Path, tmp_path: Path):
    code, events, _ = child("run", {"channel": "nope", "fmt": "shorts_only", "count": 1, "fake": sorted(FAKEABLE)}, tmp_path)
    assert code == 1
    assert events[-1]["ev"] == "error" and (events[-1]["code"] or events[-1]["message"])
    assert not any(e["ev"] == "result" for e in events)


def test_the_child_rejects_an_unknown_kind(tmp_home: Path, tmp_path: Path):
    code, events, noise = child("banana", {}, tmp_path)
    assert code == 2 and events == [] and "invalid choice" in noise
