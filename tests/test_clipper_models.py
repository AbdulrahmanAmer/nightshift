"""C8 Clipper: model tests (spec 15.6 transcript, clip_plan, clip_layout, params)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from ns_common.errors import NsError
from ns_common.models import ClipLayout, ClipPlanItem, Crop, Rect, Transcript
from ns_studio.clipper.models import ClipItem, ClipMetadata, ClipParams
from tests.fixtures.clipper.make_sample import build_transcript


def test_rect_properties() -> None:
    r = Rect(x=10, y=20, w=100, h=50)
    assert (r.right, r.bottom, r.area) == (110, 70, 5000)
    assert r.center == (60.0, 45.0)
    assert r.inside(Rect(x=0, y=0, w=200, h=200))
    assert not r.inside(Rect(x=0, y=0, w=100, h=100))
    assert r.inside(Rect(x=0, y=0, w=100, h=100), tol=20)


def test_rect_rejects_empty_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Rect(x=0, y=0, w=0, h=10)
    with pytest.raises(ValidationError):
        Rect.model_validate({"x": 0, "y": 0, "w": 1, "h": 1, "extra": 3})


def test_transcript_roundtrip() -> None:
    t = Transcript.model_validate(build_transcript())
    again = Transcript.model_validate_json(t.model_dump_json())
    assert again == t
    assert len(t.segments) == 6


def test_transcript_rejects_reversed_word() -> None:
    raw = build_transcript()
    raw["words"][0]["end"] = raw["words"][0]["start"] - 0.5
    with pytest.raises(ValidationError):
        Transcript.model_validate(raw)


def test_clip_item_bounds() -> None:
    ok = ClipPlanItem(k=1, start_s=1.0, end_s=31.0, title="t", why="w", score=0.5)
    assert ok.duration_s == 30.0
    with pytest.raises(ValidationError):
        ClipPlanItem(k=1, start_s=10.0, end_s=10.0, title="t", why="w", score=0.5)
    with pytest.raises(ValidationError):
        ClipPlanItem(k=1, start_s=1.0, end_s=5.0, title="t", why="w", score=1.5)


def test_clip_item_is_the_shared_model_and_carries_emphasis() -> None:
    assert ClipItem is ClipPlanItem
    base = {"k": 1, "start_s": 1.0, "end_s": 5.0, "title": "t", "score": 0.5}
    assert ClipItem.model_validate(base | {"emphasis": ["word"]}).emphasis == ["word"]
    assert ClipItem.model_validate(base).emphasis is None


def test_clip_layout_roundtrip_with_second_panel() -> None:
    lay = ClipLayout(
        template="gaming_facecam",
        facecam=Rect(x=944, y=16, w=320, h=180),
        crops=[
            Crop(
                t_start=0,
                t_end=30,
                region=Rect(x=0, y=0, w=600, h=720),
                region_b=Rect(x=944, y=16, w=320, h=180),
            )
        ],
    )
    assert ClipLayout.model_validate_json(lay.model_dump_json()) == lay


def test_params_save_load(tmp_path) -> None:
    p = ClipParams(source="x.mp4", channel="after-dark", template="talking_head", max_clips=3)
    p.save(tmp_path)
    assert ClipParams.load(tmp_path) == p


def test_params_validation(tmp_path) -> None:
    with pytest.raises(ValueError):
        ClipParams(source="x", channel="c", template="nope").save(tmp_path)
    with pytest.raises(ValueError):
        ClipParams(source="x", channel="c", min_s=50, max_s=40).save(tmp_path)
    with pytest.raises(ValueError):
        ClipParams(source="x", channel="c", max_clips=0).save(tmp_path)


def test_params_missing_file_is_e326(tmp_path) -> None:
    with pytest.raises(NsError) as ei:
        ClipParams.load(tmp_path)
    assert ei.value.code == "NS-E326"  # not NS-E405 ("cannot be used for clips: too long, no audio")


def test_clip_metadata_kind_is_clip() -> None:
    m = ClipMetadata(
        channel="after-dark", job_id="after-dark-20261101-ab12", k=1, title="t", description="d", tags=["a"],
        hashtags=["#Shorts"], publish_after="2026-11-02T18:00", source_video="sample.mp4",
        template="center_crop",
    )
    assert m.kind == "clip"
