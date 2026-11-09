# ruff: noqa: E501
"""Package P10b: series designs and parts draw from their format, and a bad format is refused where it is chosen.

A series design names one format. A short format shapes the part's Shorts, a long or square format its full
video. The Workshop refuses a format that is missing, does not load or cannot be drawn when the design is
saved and again when the part is queued (NS-E320, naming the problem); the studio keeps its fallback for the
night a format breaks after that, so a part that renders today does not stop.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ns_common import ffmpeg as ff
from ns_common.paths import Paths
from ns_studio.media import formats as F
from ns_studio.media import job_formats as JF
from ns_studio.series import workshop as W
from ns_studio.series.store import StoryDir, WorkshopError
from tests.helpers import make_ctx
from tests.test_p10_formats_core import Rec, put, shipped
from tests.test_p10_master_format import (
    HD,
    master_job,
    master_pipeline,
    play_res,
    render_chapter,
    style_fields,
)
from tests.test_series_workshop import Recorder, built, make_workshop

needs_ffmpeg = pytest.mark.skipif(not ff.available(), reason="ffmpeg required")


def off_canvas() -> dict[str, Any]:
    layout = shipped("long_16x9")["layout"]
    layout["regions"][0]["w"] = 5000
    return layout


# ---------- refused where it is chosen: the design ----------
@pytest.mark.parametrize(
    ("make", "needle"),
    [
        ("missing", "unknown format"),
        ("region", "leaves the"),
        ("shots", "shots.min_s"),
        ("broken", "canvas is required"),
        ("clip", "draws clips"),
        ("odd", "must be even"),
    ],
)
def test_design_refuses_a_broken_or_missing_format_with_ns_e320(
    tmp_home: Path, make: str, needle: str
) -> None:
    ws = make_workshop(tmp_home)
    slug = built(ws, write=())
    paths = Paths.from_env()
    fid = {"missing": "long_gone", "clip": "clip_center_crop"}.get(make, f"long_{make}")
    if make == "region":
        put(paths, fid, base="long_16x9", layout=off_canvas())
    elif make == "shots":
        put(paths, fid, base="long_16x9", shots={"min_s": 9, "max_s": 5})
    elif make == "odd":
        put(paths, fid, base="long_16x9", canvas={"w": 1921, "h": 1080})
    elif make == "broken":
        paths.formats_dir.mkdir(parents=True, exist_ok=True)
        (paths.formats_dir / f"{fid}.yaml").write_text(f"id: {fid}\n", encoding="utf-8")
    with pytest.raises(WorkshopError) as err:
        ws.design(slug, format=fid)
    assert "NS-E320" in str(err.value) and fid in str(err.value) and needle in str(err.value), str(err.value)
    assert not StoryDir.open(ws.paths, slug).design.exists()
    # the Assistant's check before its confirm card refuses exactly the same
    with pytest.raises(WorkshopError, match="NS-E320"):
        ws.check("workshop_design", {"slug": slug, "format": fid})


def test_design_accepts_a_good_duplicate_and_the_shipped_formats(tmp_home: Path) -> None:
    ws = make_workshop(tmp_home)
    slug = built(ws, write=())
    put(Paths.from_env(), "long_hd", base="long_16x9", **HD)
    for fid in ("long_hd", "long_16x9", "short_9x16", "square_1x1"):
        assert ws.design(slug, format=fid).format == fid


def test_design_offers_a_duplicate_made_after_the_first_listing(tmp_home: Path) -> None:
    ws = make_workshop(tmp_home)
    slug = built(ws, write=())
    assert "long_hd" not in ws.design_choices(slug)["format"]
    put(Paths.from_env(), "long_hd", base="long_16x9", **HD)
    assert "long_hd" in ws.design_choices(slug)["format"]
    assert ws.design(slug, format="long_hd").format == "long_hd"


def test_the_design_lists_only_formats_a_part_can_use(tmp_home: Path) -> None:
    paths = Paths.from_env()
    put(paths, "long_off", base="long_16x9", layout=off_canvas())
    put(paths, "long_ok", base="long_16x9")
    assert "long_off" in F.design_formats()  # the model accepts it
    found = W.available_formats()
    assert "long_ok" in found and "long_off" not in found
    assert {"short_9x16", "long_16x9", "square_1x1"} <= set(found)


# ---------- refused where it is chosen: the part is queued ----------
@pytest.mark.parametrize("how", ["deleted", "broken"])
def test_produce_refuses_when_the_designed_format_was_deleted_or_broken_since(
    tmp_home: Path, how: str
) -> None:
    rec = Recorder()
    ws = make_workshop(tmp_home, enqueue=rec)
    slug = built(ws, write=(1, 2, 3))
    ws.approve(slug)
    paths = Paths.from_env()
    put(paths, "long_hd", base="long_16x9", **HD)
    ws.design(slug, format="long_hd", publish_cadence_days=7)
    assert ws.check("workshop_produce", {"slug": slug}) is None  # the card may be shown
    if how == "deleted":
        (paths.formats_dir / "long_hd.yaml").unlink()
    else:
        put(paths, "long_hd", base="long_16x9", shots={"min_s": 9, "max_s": 5})
    with pytest.raises(WorkshopError) as err:
        ws.produce(slug, "all")
    assert "NS-E320" in str(err.value) and "long_hd" in str(err.value)
    with pytest.raises(WorkshopError, match="NS-E320"):
        ws.check("workshop_produce", {"slug": slug})
    # nothing was queued and no part changed state
    assert rec.requests == [] and not list(ws.paths.jobs_dir.glob("*"))
    st = StoryDir.open(ws.paths, slug).read_status()
    assert [st.parts[n].status for n in (1, 2, 3)] == ["approved"] * 3


def test_produce_still_works_for_a_good_designed_duplicate(tmp_home: Path) -> None:
    rec = Recorder()
    ws = make_workshop(tmp_home, enqueue=rec)
    slug = built(ws, write=(1,))
    ws.approve(slug, 1)
    put(Paths.from_env(), "long_hd", base="long_16x9", **HD)
    ws.design(slug, format="long_hd")
    assert [r["part"] for r in ws.produce(slug, 1)] == [1]
    assert rec.requests[0].design["format"] == "long_hd"


# ---------- what the part's formats are ----------
def part_context(tmp_home: Path, design_format: str, allowed: list[str] | None = None) -> Any:
    """A job context for the first part `Workshop.produce` queued, holding the part's own series_context.json."""
    rec = Recorder()
    ws = make_workshop(tmp_home, enqueue=rec)
    slug = built(ws, write=(1,))
    ws.approve(slug, 1)
    ws.design(slug, format=design_format)
    ws.produce(slug, 1)
    produced = json.loads(
        (ws.paths.job_dir(rec.requests[0].job_id) / "series_context.json").read_text("utf-8")
    )
    from tests.helpers import make_channel

    ctx = make_ctx(tmp_home, fmt="master_first", channel=make_channel(formats_allowed=allowed or []))
    ctx.write_json("series_context.json", produced)
    return ctx


def test_an_old_design_naming_short_9x16_still_loads_and_produces_a_16x9_master(tmp_home: Path) -> None:
    ctx = part_context(tmp_home, "short_9x16")
    assert ctx.series["design"]["format"] == "short_9x16"
    doc = JF.prepare_job_formats(ctx)
    assert doc is not None
    assert (doc["long"]["id"], doc["long"]["why"]) == ("long_16x9", "default (formats_allowed is empty)")
    assert (doc["short"]["id"], doc["short"]["why"]) == ("short_9x16", "series design")
    assert JF.job_format(ctx, "long").canvas.w == 1920 and "note" not in doc["long"]


def test_a_long_design_format_shapes_the_full_video_and_leaves_the_shorts_alone(tmp_home: Path) -> None:
    put(Paths.from_env(), "long_hd", base="long_16x9", **HD)
    ctx = part_context(tmp_home, "long_hd")
    doc = JF.prepare_job_formats(ctx)
    assert doc is not None
    assert (doc["long"]["id"], doc["long"]["why"]) == ("long_hd", "series design")
    assert doc["short"]["id"] == "short_9x16" and "note" not in doc["short"]


def test_a_square_design_format_shapes_the_full_video(tmp_home: Path) -> None:
    ctx = part_context(tmp_home, "square_1x1")
    doc = JF.prepare_job_formats(ctx)
    assert doc is not None and (doc["long"]["id"], doc["long"]["why"]) == ("square_1x1", "series design")
    assert doc["short"]["id"] == "short_9x16"


def test_a_short_design_format_shapes_only_the_shorts(tmp_home: Path) -> None:
    put(
        Paths.from_env(),
        "short_small",
        base="short_9x16",
        canvas={"w": 720, "h": 1280},
        layout={
            "seam_captions": False,
            "regions": [{"name": "main", "x": 0, "y": 0, "w": 720, "h": 1280, "source": "full"}],
        },
    )
    ctx = part_context(tmp_home, "short_small")
    doc = JF.prepare_job_formats(ctx)
    assert doc is not None and doc["short"]["id"] == "short_small" and doc["long"]["id"] == "long_16x9"


def test_a_series_part_renders_its_chapters_in_the_designed_duplicate(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The series-part path at the effect: the design's duplicate reaches the text handed to FFmpeg."""
    put(Paths.from_env(), "long_hd", base="long_16x9", **HD)
    series = {"series_title": "S", "part_n": 1, "design": {"format": "long_hd"}}
    ctx = master_job(tmp_home, [], series=series)
    rec, card_ass = render_chapter(ctx, monkeypatch, tmp_path)
    ((args, graph),) = rec.calls
    assert "color=c=0x2A3350:s=1280x720:r=24" in args and "scale=1280:720" in graph
    assert play_res(card_ass) == (1280, 720)
    assert ctx.read_json("formats.json")["long"]["why"] == "series design"


# ---------- a design format that stops working: the fallback prefers the right kind ----------
def resolve(tmp_home: Path, role: str, allowed: list[str], design: str | None) -> tuple[Any, Rec]:
    log = Rec()
    return JF.resolve_role(role, allowed, design, paths=Paths.from_env(), log=log), log  # type: ignore[arg-type]


def test_a_skipped_short_design_prefers_a_short_format_over_a_square(tmp_home: Path) -> None:
    paths = Paths.from_env()
    put(paths, "short_bad", shots={"min_s": 5, "max_s": 2})
    put(paths, "short_ok")
    # the next allowed short format
    res, _ = resolve(tmp_home, "short", ["short_bad", "square_1x1", "short_ok"], "short_bad")
    assert res.id == "short_ok" and "short_bad" in (res.note or "")
    # none allowed: the shipped short, not the square the list also holds
    res, _ = resolve(tmp_home, "short", ["short_bad", "square_1x1"], "short_bad")
    assert res.id == "short_9x16" and "short_bad" in (res.note or "")
    # the same when the design file is gone and its kind cannot be told
    res, _ = resolve(tmp_home, "short", ["square_1x1"], "short_gone")
    assert res.id == "short_9x16"


def test_the_declared_kind_reads_only_files_in_the_formats_folders(tmp_home: Path) -> None:
    """Review nit: a design format id is a file name; an id with a path in it reads nothing outside."""
    paths = Paths.from_env()
    paths.formats_dir.parent.mkdir(parents=True, exist_ok=True)
    (paths.formats_dir.parent / "outside.yaml").write_text("kind: long\n", encoding="utf-8")
    put(paths, "short_inside")
    assert F.declared_kind("short_inside", paths=paths) == "short"
    for sneaky in ("../outside", "..\\outside", "sub/../../outside", "C:outside", ""):
        assert F.declared_kind(sneaky, paths=paths) is None, sneaky


def test_a_square_is_still_chosen_for_shorts_when_the_owner_allows_only_a_square(tmp_home: Path) -> None:
    res, _ = resolve(tmp_home, "short", ["square_1x1"], None)
    assert res.id == "square_1x1" and res.why == "first allowed square format"
    put(Paths.from_env(), "long_hd", base="long_16x9", **HD)
    res, _ = resolve(tmp_home, "short", ["square_1x1"], "long_hd")  # a valid long design is not for the Short
    assert res.id == "square_1x1"


def test_a_skipped_long_design_prefers_a_long_format_over_a_square(tmp_home: Path) -> None:
    paths = Paths.from_env()
    put(paths, "long_bad", base="long_16x9", shots={"min_s": 9, "max_s": 5})
    put(paths, "long_ok", base="long_16x9")
    res, _ = resolve(tmp_home, "long", ["long_bad", "square_1x1", "long_ok"], "long_bad")
    assert res.id == "long_ok"
    res, _ = resolve(tmp_home, "long", ["long_bad", "square_1x1"], "long_bad")
    assert res.id == "long_16x9"
    # a square the owner chose in the design may fall back to another allowed square
    put(paths, "square_bad", base="square_1x1", shots={"min_s": 9, "max_s": 5})
    res, _ = resolve(tmp_home, "long", ["square_bad", "square_1x1"], "square_bad")
    assert res.id == "square_1x1"


# ---------- the whole part on fakes (FFmpeg: CI) ----------
@needs_ffmpeg
def test_a_series_part_renders_in_the_designed_duplicate(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Design a duplicate of the square preset (caption anchor 40 %); the part's master and ASS take it."""
    from tests.test_p10_shorts_format import anchored_margin

    paths = Paths.from_env()
    put(paths, "square_one", base="square_1x1", captions={"anchor_y_pct": 40})
    series = {
        "series_slug": "the-doorbell-camera",
        "series_title": "The Doorbell Camera",
        "part_n": 1,
        "parts_total": 3,
        "title": "The Doorbell Camera - Part 01",
        "short_titles": [],
        "end_card": "That was the final part.",
        "publish_not_before_days": 0,
        "pacing": "standard",
        "hook": "cold_open",
        "design": {"format": "square_one"},
    }
    ctx = master_pipeline(tmp_home, monkeypatch, [], series=series)
    info = ctx.read_json("render/long.json")
    assert ff.video_size(Path(info["path"])) == (1080, 1080) and info["format"] == "square_one"
    ass = ctx.file("captions/long_c1.ass").read_text(encoding="utf-8")
    assert play_res(ass) == (1080, 1080)
    assert int(style_fields(ass)[-2]) == anchored_margin("clean_white", 1080, 1080, 40)
    assert ctx.read_json("qc/long.json")["checks"]["aspect"]["limit"] == "1080x1080"
    assert ff.video_size(Path(ctx.read_json("render/report.json")["s1"]["path"])) == (
        1080,
        1920,
    )  # Shorts: 9:16
