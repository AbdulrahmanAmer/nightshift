"""Re-review row 16: a series part gets its channel row and its preset names checked before it renders."""

from pathlib import Path

import pytest

from ns_studio.errors import StudioError
from ns_studio.policy import regen
from tests.helpers import make_channel, make_ctx


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(regen, "run_job", lambda ctx, resume=True: seen.append("run") or ["voice"])
    return seen


def test_the_channel_row_gets_its_display_name_before_a_series_part_runs(tmp_home: Path, runs: list[str]):
    """`record_combo`'s bare insert left display_name = slug on a YAML-only channel."""
    ctx = make_ctx(tmp_home, fmt="master_first", channel=make_channel(display_name="After Dark Tales"),
                   channel_row=False)  # fmt: skip
    ctx.db.execute(
        "insert into channels(slug, display_name, created_at) values (?,?,?)",
        (ctx.channel.slug, ctx.channel.slug, "2026-11-01T00:00:00"),
    )
    ctx.write_json("series_context.json", {"part_n": 1, "design": {}})
    regen.run_job_guarded(ctx)
    row = ctx.db.one("select display_name from channels where slug=?", (ctx.channel.slug,))
    assert row["display_name"] == "After Dark Tales" and runs == ["run"]


def test_an_unknown_design_style_is_ns_e320_before_any_work(tmp_home: Path, runs: list[str]):
    """It used to surface mid-render as NS-E327."""
    ctx = make_ctx(tmp_home, fmt="master_first")
    ctx.write_json("series_context.json", {
        "part_n": 1, "design": {"caption_style": "neon_glow", "visual_treatment": "dark_slowzoom"},
    })
    with pytest.raises(StudioError) as ei:
        regen.run_job_guarded(ctx)
    assert ei.value.code == "NS-E320" and "neon_glow" in ei.value.detail and runs == []


def test_a_known_design_and_an_ordinary_job_pass(tmp_home: Path, runs: list[str]):
    ctx = make_ctx(tmp_home, fmt="master_first")
    ctx.write_json("series_context.json", {
        "part_n": 1, "design": {"caption_style": "boxed_dark", "visual_treatment": "cinematic_teal"},
    })
    regen.run_job_guarded(ctx)
    plain = make_ctx(tmp_home)
    regen.run_job_guarded(plain)
    assert runs == ["run", "run"]
