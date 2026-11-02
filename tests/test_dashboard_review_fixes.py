# ruff: noqa: E501
"""Regression tests for the clean-clone review of the dashboard (one section per review row)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ns_dashboard.errors import ApiError
from ns_dashboard.fakes import seed_channel, seed_output
from ns_dashboard.handlers import Dashboard
from tests.test_dashboard_handlers import code_of, make

# ------------------------------------------------------------------ row 20: clip length bounds match ClipParams


@pytest.mark.parametrize(
    ("min_s", "max_s"),
    [(5, 6), (5, 10), (20, 60), (20, 179), (20, 180), (60, 60), (60, 30), (5, 179), (4, 20), (120, 179), (121, 179), (178, 179), (179, 179)],
)
def test_clip_length_bounds_are_the_clipparams_bounds(tmp_home: Path, min_s: int, max_s: int):
    """The API accepted `max_s` 180 and `min_s == max_s`; the job then failed in `ClipParams.validate`."""
    from ns_studio.clipper.models import ClipParams

    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths)
    video = tmp_home / "in.mp4"
    video.write_bytes(b"video")
    try:
        ClipParams(source="x", channel="after-dark", min_s=min_s, max_s=max_s, rights_confirmed=True).validate()
        studio_ok = True
    except ValueError:
        studio_ok = False
    body = {"path": str(video), "channel": "after-dark", "rights_confirmed": True, "min_s": min_s, "max_s": max_s}
    try:
        dash.jobs.create_clip(body)
        api_ok = True
    except ApiError as e:
        assert e.code == "invalid_options"
        api_ok = False
    assert api_ok == studio_ok, f"min_s={min_s} max_s={max_s}: API {api_ok}, ClipParams {studio_ok}"
