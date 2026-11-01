"""Format presets as data (spec 17.1): every shipped YAML conforms to the Format model and its JSON schema."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ns_common.models import Format
from ns_common.paths import contracts_dir
from ns_studio.errors import StudioError
from ns_studio.media import formats as F
from ns_studio.media.presets import load_caption_styles

SHIPPED = {
    "short_9x16", "long_16x9", "square_1x1",
    "clip_gaming_facecam", "clip_podcast_stacked", "clip_podcast_single", "clip_talking_head",
    "clip_center_crop", "clip_screen_plus_cam",
}
FILES = sorted(F.FORMATS_DIR.glob("*.yaml"))


def test_the_nine_shipped_presets_exist() -> None:
    assert {p.stem for p in FILES} == SHIPPED


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_every_yaml_validates_against_the_model_and_the_schema(path: Path) -> None:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    fmt = Format.model_validate(raw)
    assert fmt.id == path.stem
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((contracts_dir() / "schemas" / "format.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(raw, schema)
    assert F.format_problems(fmt, set(load_caption_styles())) == []


def test_load_get_and_validate_all() -> None:
    formats = F.load_formats()
    assert set(formats) == SHIPPED
    assert F.get_format("short_9x16").canvas.model_dump() == {"w": 1080, "h": 1920, "fps": 30}
    assert F.get_format("long_16x9").canvas.w == 1920
    assert F.validate_all() == sorted(SHIPPED)
    with pytest.raises(StudioError) as ei:
        F.get_format("nope")
    assert ei.value.code == "NS-E320" and "short_9x16" in ei.value.detail


def test_kinds_and_design_formats() -> None:
    assert {f.kind for i, f in F.load_formats().items() if i.startswith("clip_")} == {"clip"}
    assert F.design_formats() == ["long_16x9", "short_9x16", "square_1x1"]
    assert F.format_ids(kinds=["clip"]) == sorted(i for i in SHIPPED if i.startswith("clip_"))


def test_clip_layouts_match_spec_19_3() -> None:
    gaming = F.get_format("clip_gaming_facecam")
    assert [(r.name, r.source) for r in gaming.layout.regions] == [("cam", "facecam"), ("game", "full")]
    assert gaming.layout.seam_captions is True and gaming.layout.regions[0].h == 640
    stacked = F.get_format("clip_podcast_stacked")
    got = [(r.source, r.y, r.h) for r in stacked.layout.regions]
    assert got == [("face:0", 0, 960), ("face:1", 960, 960)]
    centre = F.get_format("clip_center_crop")
    assert [r.source for r in centre.layout.regions] == ["blur_fill", "full"]
    assert (centre.layout.regions[1].x, centre.layout.regions[1].y) == (152, 220)
    for i in SHIPPED:
        f = F.get_format(i)
        if f.kind in ("clip", "short"):
            assert (f.canvas.w, f.canvas.h) == (1080, 1920)
            assert (f.safe_margins.top, f.safe_margins.bottom) == (220, 320)


def _write(d: Path, name: str, **over: object) -> None:
    base = yaml.safe_load((F.FORMATS_DIR / "short_9x16.yaml").read_text(encoding="utf-8"))
    base.update(over)
    (d / f"{name}.yaml").write_text(yaml.safe_dump(base), encoding="utf-8")


def test_validate_all_reports_every_problem(tmp_path: Path) -> None:
    _write(tmp_path, "good", id="good")
    assert F.validate_all(tmp_path) == ["good"]
    _write(tmp_path, "wrong_id", id="other")
    _write(tmp_path, "off_canvas", id="off_canvas",
           layout={"regions": [{"name": "a", "x": 0, "y": 0, "w": 2000, "h": 100, "source": "full"}]})
    _write(tmp_path, "bad_style", id="bad_style",
           captions={"styles_allowed": ["no_such_style"], "anchor_y_pct": 50, "words_on_screen": 3})
    (tmp_path / "broken.yaml").write_text("id: [unclosed", encoding="utf-8")
    with pytest.raises(StudioError) as ei:
        F.validate_all(tmp_path)
    msg = ei.value.detail
    assert ei.value.code == "NS-E320"
    assert "must equal the file name" in msg and "leaves the" in msg and "unknown caption styles" in msg
    assert "broken.yaml" in msg
    with pytest.raises(StudioError):
        F.load_formats(tmp_path)


def test_validate_all_on_an_empty_folder_fails(tmp_path: Path) -> None:
    with pytest.raises(StudioError):
        F.validate_all(tmp_path)


def test_new_format_is_just_a_new_file(tmp_path: Path) -> None:
    _write(tmp_path, "wide_1x2", id="wide_1x2", canvas={"w": 1080, "h": 2160, "fps": 30},
           layout={"regions": [{"name": "m", "x": 0, "y": 0, "w": 1080, "h": 2160, "source": "full"}]})
    assert F.get_format("wide_1x2", tmp_path).canvas.h == 2160
    assert F.validate_all(tmp_path) == ["wide_1x2"]


# ---------- channel option formats_allowed (task 4) ----------
def test_formats_allowed_defaults_to_all_and_reads_the_channel_profile(tmp_home: Path) -> None:
    from ns_common.config import load_channel, save_channel
    from ns_common.paths import Paths
    from tests.helpers import make_channel

    paths = Paths.from_env()
    assert F.formats_allowed(make_channel()) is None
    ch = make_channel(formats_allowed=["short_9x16", "clip_center_crop"])
    assert F.formats_allowed(ch) == ["short_9x16", "clip_center_crop"]
    assert F.formats_allowed(make_channel(formats_allowed=[])) is None
    save_channel(paths, ch)
    assert F.formats_allowed(load_channel(paths, ch.slug)) == ["short_9x16", "clip_center_crop"]


def test_ensure_allowed_names_the_format_and_the_way_out() -> None:
    F.ensure_allowed("long_16x9", None)
    F.ensure_allowed("long_16x9", ["long_16x9"], channel="after-dark")
    with pytest.raises(StudioError) as ei:
        F.ensure_allowed("square_1x1", ["long_16x9", "short_9x16"], channel="after-dark")
    assert ei.value.code == "NS-E320"
    assert "square_1x1" in ei.value.detail and "after-dark" in ei.value.detail
    assert "long_16x9" in ei.value.detail and "formats_allowed" in ei.value.detail


def test_allowed_ids_filters_by_kind_and_list() -> None:
    assert F.allowed_ids(None, kinds=["short"]) == ["short_9x16"]
    assert F.allowed_ids(["short_9x16", "clip_center_crop"], kinds=["clip"]) == ["clip_center_crop"]
    assert F.allowed_ids([]) == []


def test_series_design_options_follow_the_channel_list() -> None:
    from ns_studio.series.workshop import available_formats, design_options
    from tests.helpers import make_channel

    assert available_formats() == ["long_16x9", "short_9x16", "square_1x1"]
    assert design_options(make_channel())["format"] == available_formats()
    ch = make_channel(formats_allowed=["long_16x9", "clip_center_crop"])
    assert design_options(ch)["format"] == ["long_16x9"]


def test_clip_layout_choice_follows_formats_allowed() -> None:
    from ns_studio.clipper import stages_clip as sc
    from ns_studio.clipper.models import Rect

    cam = Rect(x=944, y=16, w=320, h=180)
    apply = sc.apply_formats_allowed
    assert apply("gaming_facecam", None, cam, 0, explicit=False, channel="c") == ("gaming_facecam", None)
    ok = ["clip_gaming_facecam", "clip_center_crop"]
    assert apply("gaming_facecam", ok, cam, 0, explicit=False, channel="c") == ("gaming_facecam", None)
    # not allowed: the model's pick is replaced by an allowed layout that applies, with an NS-W402 note
    template, note = apply("gaming_facecam", ["clip_center_crop"], cam, 0, explicit=False, channel="c")
    assert template == "center_crop" and note is not None and note.startswith("NS-W402")
    # the section 19.4 auto choice wins when it is allowed
    both = ["clip_talking_head", "clip_center_crop"]
    assert apply("podcast_stacked", both, None, 1, explicit=False, channel="c")[0] == "talking_head"
    # an explicit owner choice is never swapped silently
    with pytest.raises(StudioError) as ei:
        apply("gaming_facecam", ["clip_center_crop"], cam, 0, explicit=True, channel="after-dark")
    assert ei.value.code == "NS-E320" and "clip_gaming_facecam" in ei.value.detail
    # nothing allowed fits this video: plain message that names the way out
    with pytest.raises(StudioError) as ei2:
        apply("center_crop", ["clip_gaming_facecam"], None, 0, explicit=False, channel="after-dark")
    assert ei2.value.code == "NS-E320" and "clip_center_crop" in ei2.value.detail
