# ruff: noqa: E501
"""Package P10a: formats load safely, jobs record the format they use, caption sizes follow the frame size.

Pure tests: no FFmpeg. The renders that take these pieces to a file are in `tests/test_p10_shorts_format.py`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from ns_common.models import Format
from ns_common.paths import Paths, contracts_dir
from ns_studio.errors import StudioError
from ns_studio.media import formats as F
from ns_studio.media import job_formats as JF
from ns_studio.media import qc as Q
from ns_studio.media.caption_plan import AssOptions, safe_zones_for
from ns_studio.media.captions import build_ass, caption_band, group_words
from ns_studio.media.presets import caption_style, load_caption_styles
from ns_studio.media.transcribe import Word
from ns_studio.orchestrator import FORMAT_STAGES, run_job
from ns_studio.stages import REGISTRY, register
from tests.helpers import make_channel, make_ctx


class Rec:
    """A logger that keeps what it was told (the code under test calls `info` and `warn`)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict[str, Any]]] = []

    def info(self, event: str, msg: str = "", **data: Any) -> None:
        self.events.append(("info", event, msg, data))

    def warn(self, event: str, msg: str = "", **data: Any) -> None:
        self.events.append(("warn", event, msg, data))

    def names(self, level: str | None = None) -> list[str]:
        return [e for lv, e, _m, _d in self.events if level in (None, lv)]


def shipped(format_id: str) -> dict[str, Any]:
    return yaml.safe_load((F.FORMATS_DIR / f"{format_id}.yaml").read_text(encoding="utf-8"))


def put(paths: Paths, fid: str, base: str = "short_9x16", **changes: Any) -> dict[str, Any]:
    """Write an owner's format file `fid`, a copy of the shipped `base` with `changes` merged in (dicts merge)."""
    doc = copy.deepcopy(shipped(base))
    doc["id"] = fid
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(doc.get(key), dict):
            doc[key] = {**doc[key], **value}
        else:
            doc[key] = value
    paths.formats_dir.mkdir(parents=True, exist_ok=True)
    (paths.formats_dir / f"{fid}.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return doc


SMALL = {
    "canvas": {"w": 720, "h": 1280},
    "layout": {
        "seam_captions": False,
        "regions": [{"name": "main", "x": 0, "y": 0, "w": 720, "h": 1280, "source": "full"}],
    },
}


# ---------- safe loading ----------
def test_one_broken_user_format_does_not_stop_the_others(tmp_home: Path) -> None:
    paths = Paths.from_env()
    put(paths, "short_copy", **SMALL)
    (paths.formats_dir / "broken.yaml").write_text("id: broken\nname: no canvas\n", encoding="utf-8")
    (paths.formats_dir / "wild.yaml").write_text("id: [unclosed", encoding="utf-8")
    put(paths, "wrong_name", base="short_9x16")
    (paths.formats_dir / "wrong_name.yaml").write_text(
        (paths.formats_dir / "wrong_name.yaml")
        .read_text(encoding="utf-8")
        .replace("id: wrong_name", "id: other"),
        encoding="utf-8",
    )
    formats = F.load_formats()
    assert "short_copy" in formats and "short_9x16" in formats and "clip_center_crop" in formats
    assert "broken" not in formats and "wild" not in formats and "wrong_name" not in formats
    problems = F.load_problems()
    assert set(problems) == {"broken", "wild", "wrong_name"}
    assert "canvas is required" in problems["broken"] and "must equal the file name" in problems["wrong_name"]
    with pytest.raises(StudioError) as ei:
        F.get_format("broken")
    assert ei.value.code == "NS-E320" and "canvas is required" in ei.value.detail
    assert F.get_format("short_9x16").canvas.w == 1080
    with pytest.raises(StudioError) as ei2:
        F.get_format("never_made")
    assert "unknown format" in ei2.value.detail


def test_a_clip_job_still_finds_its_six_layouts_next_to_a_broken_file(tmp_home: Path) -> None:
    from ns_studio.clipper import layout as lay

    paths = Paths.from_env()
    paths.formats_dir.mkdir(parents=True)
    (paths.formats_dir / "broken.yaml").write_text("id: broken\n", encoding="utf-8")
    lay.load_templates.cache_clear()
    try:
        assert set(lay.load_templates()) == {
            "gaming_facecam",
            "podcast_stacked",
            "podcast_single",
            "talking_head",
            "center_crop",
            "screen_plus_cam",
        }
    finally:
        lay.load_templates.cache_clear()


def test_an_edit_saved_after_the_first_load_is_seen_without_a_restart(tmp_home: Path) -> None:
    paths = Paths.from_env()
    assert "short_copy" not in F.load_formats()
    put(paths, "short_copy")
    assert "short_copy" in F.load_formats()
    assert F.get_format("short_copy").canvas.w == 1080
    put(paths, "short_copy", **SMALL)  # saved again, no restart, no forget_loaded_formats()
    assert F.get_format("short_copy").canvas.w == 720
    (paths.formats_dir / "short_copy.yaml").unlink()
    assert "short_copy" not in F.load_formats()


def test_forget_loaded_formats_still_works(tmp_home: Path) -> None:
    F.load_formats()
    F.forget_loaded_formats()
    assert "short_9x16" in F.load_formats()


def test_the_series_workshop_lists_a_duplicate_made_after_its_first_listing(tmp_home: Path) -> None:
    from ns_studio.series.workshop import available_formats

    assert "short_copy" not in available_formats()
    put(Paths.from_env(), "short_copy")
    assert "short_copy" in available_formats()


# ---------- written ----------
def test_written_tells_a_value_from_a_default() -> None:
    plain = Format.model_validate(shipped("short_9x16"))
    assert not F.written(plain, "captions.anchor_y_pct")
    assert not F.written(plain, "captions.words_on_screen")
    assert not F.written(plain, "canvas.fps")
    assert (
        F.written(plain, "canvas.w")
        and F.written(plain, "safe_margins.top")
        and F.written(plain, "shots.min_s")
    )
    assert not F.written(plain, "encoder.preset")
    raw = shipped("short_9x16")
    raw["captions"]["anchor_y_pct"] = 55
    raw["canvas"]["fps"] = 24
    raw["encoder"] = {"preset": "p5"}
    mine = Format.model_validate(raw)
    assert F.written(mine, "captions.anchor_y_pct") and F.written(mine, "canvas.fps")
    assert F.written(mine, "encoder.preset")
    assert F.fps_of(mine, 30) == 24 and F.fps_of(plain, 25) == 25
    assert F.encoder_preset_of(mine, "p4") == "p5" and F.encoder_preset_of(plain, "p7") == "p7"
    # a model_dump round trip marks everything as written: jobs therefore keep the file's own mapping
    round_trip = Format.model_validate(plain.model_dump())
    assert F.written(round_trip, "captions.anchor_y_pct")


def test_the_shipped_presets_write_nothing_a_render_would_change() -> None:
    for fid in ("short_9x16", "long_16x9", "square_1x1"):
        fmt = F.get_format(fid)
        for field in ("captions.anchor_y_pct", "captions.words_on_screen", "canvas.fps", "encoder.preset"):
            assert not F.written(fmt, field), (fid, field)


def test_shipped_presets_validate_after_the_trims() -> None:
    assert F.validate_all() == sorted(p.stem for p in F.FORMATS_DIR.glob("*.yaml"))
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((contracts_dir() / "schemas" / "format.schema.json").read_text(encoding="utf-8"))
    for p in F.FORMATS_DIR.glob("*.yaml"):
        jsonschema.validate(yaml.safe_load(p.read_text(encoding="utf-8")), schema)


# ---------- kinds and problems ----------
def test_clip_layout_kind_of_every_shipped_preset_equals_clip_templates_yaml() -> None:
    from ns_studio.clipper import layout as lay

    compat = yaml.safe_load(lay.templates_path().read_text(encoding="utf-8"))
    assert len(compat) == 6
    for name, body in compat.items():
        fmt = F.get_format(body["format"])
        assert F.clip_layout_kind(fmt) == body["layout"], name
        assert F.clip_layout_problems(fmt) == [], name


def test_clip_layout_kind_is_none_for_an_unknown_mix() -> None:
    raw = shipped("clip_gaming_facecam")
    raw["layout"]["regions"][1]["source"] = "screen"
    assert F.clip_layout_kind(Format.model_validate(raw)) == "screen_cam"
    raw["layout"]["regions"][1]["source"] = "facecam"
    raw["layout"]["regions"][1]["name"] = "cam2"
    assert F.clip_layout_kind(Format.model_validate(raw)) is None


def test_story_role_refuses_facecam_and_two_full_regions() -> None:
    assert F.story_role_problems(F.get_format("short_9x16")) == []
    assert F.story_role_problems(F.get_format("square_1x1")) == []
    gaming = F.get_format("clip_gaming_facecam")
    assert any("facecam" in p for p in F.story_role_problems(gaming))
    raw = shipped("short_9x16")
    raw["layout"]["regions"] = [
        {"name": "a", "x": 0, "y": 0, "w": 1080, "h": 960, "source": "full"},
        {"name": "b", "x": 0, "y": 960, "w": 1080, "h": 960, "source": "full"},
    ]
    assert any("exactly one region" in p for p in F.story_role_problems(Format.model_validate(raw)))
    blur = shipped("short_9x16")
    blur["layout"]["regions"] = [
        {"name": "bg", "x": 0, "y": 0, "w": 1080, "h": 1920, "source": "blur_fill"},
        {"name": "fg", "x": 0, "y": 300, "w": 1080, "h": 1200, "source": "full"},
    ]
    assert F.story_role_problems(Format.model_validate(blur)) == []


def test_odd_canvas_is_a_problem() -> None:
    raw = shipped("short_9x16")
    raw["canvas"] = {"w": 721, "h": 1280}
    raw["layout"]["regions"][0].update(w=721, h=1280)
    problems = F.format_problems(Format.model_validate(raw), set(load_caption_styles()))
    assert any("must be even" in p for p in problems)


def test_definition_problems_name_the_field_with_the_pydantic_model() -> None:
    raw = shipped("short_9x16")
    raw["canvas"]["w"] = 0
    out = F.definition_problems(raw)
    assert out and out[0].startswith("canvas.w:")
    raw = shipped("short_9x16")
    raw["captions"]["anchor_y_pct"] = 140
    assert F.definition_problems(raw)[0].startswith("captions.anchor_y_pct:")
    assert F.definition_problems({"id": "x"}) and "is required" in " ".join(
        F.definition_problems({"id": "x"})
    )
    assert F.definition_problems(shipped("short_9x16")) == []
    assert F.definition_problems("nope") == ["a format must be a mapping of settings"]


def test_shot_range_clamps_and_falls_back_when_disjoint() -> None:
    fmt = F.get_format("short_9x16")  # shots 1.5 - 3
    for tr in ((1.8, 3.0), (1.5, 2.4), (2.0, 3.0)):
        assert F.shot_range(tr, fmt) == tr  # every shipped treatment is untouched
    assert F.shot_range((1.0, 5.0), fmt) == (1.5, 3.0)
    assert F.shot_range((4.0, 6.0), fmt) == (1.5, 3.0)  # disjoint: the format's own range
    assert F.shot_range((1.0, 2.0), None) == (1.0, 2.0)
    raw = shipped("short_9x16")
    del raw["shots"]
    assert F.shot_range((1.0, 5.0), Format.model_validate(raw)) == (1.0, 5.0)


def test_every_shipped_treatment_fits_inside_the_shipped_shot_ranges() -> None:
    from ns_studio.media.presets import load_visual_treatments

    short, long = F.get_format("short_9x16"), F.get_format("long_16x9")
    for name, t in load_visual_treatments().items():
        assert F.shot_range(t.cut_range_s, short) == t.cut_range_s, name
        assert F.shot_range(t.long_cut_range_s, long) == t.long_cut_range_s, name


def test_caption_style_not_allowed_by_the_format_is_swapped() -> None:
    raw = shipped("short_9x16")
    raw["captions"]["styles_allowed"] = ["clean_white"]
    fmt = Format.model_validate(raw)
    assert F.style_for_format(fmt, "punch_yellow") == ("clean_white", True)
    assert F.style_for_format(fmt, "clean_white") == ("clean_white", False)
    assert F.style_for_format(F.get_format("short_9x16"), "boxed_dark") == ("boxed_dark", False)


def test_a_format_file_replaced_while_it_is_read_is_read_again(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review nit: a dashboard save landing at job start must not make the owner's file a problem."""
    paths = Paths.from_env()
    put(paths, "short_copy", **SMALL)
    target = paths.formats_dir / "short_copy.yaml"
    real = Path.read_text
    refused: list[int] = []

    def busy_once(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == target and not refused:
            refused.append(1)
            raise PermissionError("the file is being replaced")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", busy_once)
    F.forget_loaded_formats()
    assert "short_copy" in F.load_formats(paths=paths) and F.load_problems(paths=paths) == {}
    assert refused == [1]


# ---------- which format a role uses ----------
def channel_roles(tmp_home: Path, allowed: list[str], design: str | None = None, role: str = "short") -> Any:
    log = Rec()
    res = JF.resolve_role(role, allowed, design, paths=Paths.from_env(), log=log)  # type: ignore[arg-type]
    return res, log


def test_resolve_role_order_design_then_first_allowed_of_the_kind_then_default(tmp_home: Path) -> None:
    paths = Paths.from_env()
    put(paths, "short_copy", **SMALL)
    put(paths, "short_copy2", **SMALL)
    put(paths, "long_copy", base="long_16x9")
    # nothing says anything: the default
    res, _ = channel_roles(tmp_home, [])
    assert (res.id, res.why, res.source) == ("short_9x16", "default (formats_allowed is empty)", "shipped")
    # the first allowed format of the kind, in list order
    res, _ = channel_roles(tmp_home, ["short_copy2", "short_copy", "short_9x16"])
    assert (res.id, res.why, res.source) == ("short_copy2", "first allowed of that kind", "yours")
    # other kinds in the list do not matter for this role
    res, _ = channel_roles(tmp_home, ["long_16x9", "clip_center_crop", "short_copy"])
    assert res.id == "short_copy"
    res, _ = channel_roles(tmp_home, ["short_copy", "long_copy"], role="long")
    assert res.id == "long_copy"
    # an explicit series design format of the right kind wins over the list
    res, _ = channel_roles(tmp_home, ["short_9x16"], design="short_copy")
    assert (res.id, res.why) == ("short_copy", "series design")
    # a long design format fills the long role, and the short role falls through to the list
    res, _ = channel_roles(tmp_home, ["short_copy"], design="long_copy")
    assert res.id == "short_copy"
    res, _ = channel_roles(tmp_home, ["short_copy"], design="long_copy", role="long")
    assert (res.id, res.why) == ("long_copy", "series design")


def test_square_fills_either_role_but_never_beats_a_format_of_the_kind(tmp_home: Path) -> None:
    res, _ = channel_roles(tmp_home, ["square_1x1"])
    assert (res.id, res.why) == ("square_1x1", "first allowed square format")
    res, _ = channel_roles(tmp_home, ["square_1x1"], role="long")
    assert res.id == "square_1x1"
    res, _ = channel_roles(tmp_home, ["square_1x1", "short_9x16"])
    assert res.id == "short_9x16"
    res, _ = channel_roles(tmp_home, ["square_1x1", "long_16x9"], role="long")
    assert res.id == "long_16x9"


def test_unknown_and_broken_ids_are_skipped_with_a_log_line(tmp_home: Path) -> None:
    paths = Paths.from_env()
    put(paths, "short_copy", **SMALL)
    (paths.formats_dir / "short_broken.yaml").write_text("id: short_broken\n", encoding="utf-8")
    off = shipped("short_9x16")
    off["layout"]["regions"][0]["w"] = 5000
    put(paths, "short_off", layout=off["layout"])
    res, log = channel_roles(tmp_home, ["gone", "short_broken", "short_off", "short_copy"])
    assert res.id == "short_copy"
    skipped = [(m, d["format_id"]) for lv, e, m, d in log.events if e == "format_skipped"]
    assert [fid for _m, fid in skipped] == ["gone", "short_broken", "short_off"]
    assert "canvas is required" in skipped[1][0] and "leaves the" in skipped[2][0]


def test_nothing_usable_in_a_non_empty_list_falls_back_with_a_log_line(tmp_home: Path) -> None:
    res, log = channel_roles(tmp_home, ["clip_center_crop", "gone"])
    assert res.id == "short_9x16" and "default" in res.why
    assert "format_role_not_allowed" in log.names("warn")


def test_an_explicit_design_format_that_is_missing_or_unusable_falls_back_with_a_note(
    tmp_home: Path,
) -> None:
    """Review blocker 1: a series part that renders on main must not die on its design format."""
    paths = Paths.from_env()
    put(paths, "short_copy", **SMALL)
    (paths.formats_dir / "short_broken.yaml").write_text("id: short_broken\n", encoding="utf-8")
    off = shipped("short_9x16")
    off["layout"]["regions"][0]["w"] = 5000
    put(paths, "short_off", layout=off["layout"])
    put(paths, "short_shots", shots={"min_s": 5, "max_s": 2})
    # missing: the next candidate (first allowed of the kind) is used and the entry says why
    res, log = channel_roles(tmp_home, ["short_copy"], design="gone")
    assert res.id == "short_copy" and res.source == "yours"
    assert "gone" in res.why and "series design" in res.why and "first allowed of that kind" in res.why
    assert res.note is not None and "gone" in res.note and "unknown format" in res.note
    assert "format_design_skipped" in log.names("warn")
    # no allowed list: the shipped default
    res, log = channel_roles(tmp_home, [], design="gone")
    assert res.id == "short_9x16" and "gone" in res.why and "default" in res.why
    # a file that does not load, a region off the canvas, shots.min_s > shots.max_s
    for design, needle in (
        ("short_broken", "canvas is required"),
        ("short_off", "leaves the"),
        ("short_shots", "shots.min_s"),
    ):
        res, log = channel_roles(tmp_home, [], design=design)
        assert res.id == "short_9x16", design
        assert res.note is not None and design in res.note and needle in res.note, design
        assert design in res.why and "format_design_skipped" in log.names("warn"), design
        entry = res.entry()
        assert entry["note"] == res.note and entry["id"] == "short_9x16"


def test_a_series_design_with_a_valid_long_format_leaves_the_short_role_alone(tmp_home: Path) -> None:
    paths = Paths.from_env()
    put(paths, "long_copy", base="long_16x9")
    res, log = channel_roles(tmp_home, [], design="long_copy")
    assert res.id == "short_9x16" and res.why == "default (formats_allowed is empty)" and res.note is None
    assert "format_design_skipped" not in log.names("warn")


def test_a_series_design_naming_a_broken_long_duplicate_does_not_stop_the_short(tmp_home: Path) -> None:
    paths = Paths.from_env()
    (paths.formats_dir).mkdir(parents=True)
    (paths.formats_dir / "long_broken.yaml").write_text("id: long_broken\n", encoding="utf-8")
    res, _ = channel_roles(tmp_home, [], design="long_broken")
    assert res.id == "short_9x16" and res.note is not None and "long_broken" in res.note


def test_a_broken_owner_copy_of_a_clip_layout_is_named_not_silently_ignored(tmp_home: Path) -> None:
    """`get_format` hands back the shipped clip preset; the job log must say the owner's file was not used."""
    paths = Paths.from_env()
    paths.formats_dir.mkdir(parents=True)
    (paths.formats_dir / "clip_center_crop.yaml").write_text("id: clip_center_crop\n", encoding="utf-8")
    (paths.formats_dir / "short_mine.yaml").write_text(
        "id: short_mine\n", encoding="utf-8"
    )  # not a shipped id
    assert (
        F.get_format("clip_center_crop", paths=paths).id == "clip_center_crop"
    )  # the shipped one, as before
    log = Rec()
    ids = ["clip_center_crop", "clip_talking_head", "short_mine"]
    assert JF.warn_broken_overrides(ids, paths=paths, log=log) == ["clip_center_crop"]
    (event,) = [e for e in log.events if e[1] == "format_override_broken"]
    assert event[0] == "warn" and event[3]["format_id"] == "clip_center_crop"
    assert "does not load" in event[2] and "canvas is required" in event[2]
    assert JF.warn_broken_overrides(["clip_talking_head"], paths=paths, log=log) == []


def test_a_broken_owner_copy_of_the_default_falls_back_to_the_shipped_one_with_a_warning(
    tmp_home: Path,
) -> None:
    paths = Paths.from_env()
    paths.formats_dir.mkdir(parents=True)
    (paths.formats_dir / "short_9x16.yaml").write_text("id: short_9x16\nname: broken\n", encoding="utf-8")
    res, log = channel_roles(tmp_home, [])
    assert res.id == "short_9x16" and res.source == "shipped" and res.note and "does not load" in res.note
    assert "format_override_broken" in log.names("warn")


# ---------- the job's record ----------
def test_prepare_job_formats_writes_the_raw_definition_once_and_reuses_it(tmp_home: Path) -> None:
    paths = Paths.from_env()
    put(paths, "short_copy", **SMALL)
    ctx = make_ctx(tmp_home, channel=make_channel(formats_allowed=["short_copy"]))
    doc = JF.prepare_job_formats(ctx)
    assert doc is not None
    entry = doc["short"]
    assert (
        entry["id"] == "short_copy"
        and entry["source"] == "yours"
        and entry["why"] == "first allowed of that kind"
    )
    stored = json.loads(ctx.file("formats.json").read_text(encoding="utf-8"))
    assert stored["short"]["definition"]["canvas"] == {"w": 720, "h": 1280}
    # the file's own mapping is kept: what it did not write is still not written
    fmt = JF.job_format(ctx, "short")
    assert (
        fmt.canvas.w == 720
        and not F.written(fmt, "captions.anchor_y_pct")
        and not F.written(fmt, "canvas.fps")
    )
    # an edit saved while the job runs does not change the job
    put(paths, "short_copy", canvas={"w": 540, "h": 960})
    assert JF.job_format(ctx, "short").canvas.w == 720
    JF.prepare_job_formats(ctx)
    assert json.loads(ctx.file("formats.json").read_text(encoding="utf-8")) == stored
    # the log says which format and why
    log_text = "\n".join(p.read_text(encoding="utf-8") for p in ctx.paths.logs.glob("*.jsonl"))
    assert "job_format" in log_text and "short_copy" in log_text and "first allowed of that kind" in log_text


def test_a_job_with_a_hand_broken_record_is_refused_naming_the_field(tmp_home: Path) -> None:
    ctx = make_ctx(tmp_home)
    JF.prepare_job_formats(ctx)
    doc = json.loads(ctx.file("formats.json").read_text(encoding="utf-8"))
    doc["short"]["definition"]["canvas"]["w"] = -4
    ctx.file("formats.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(StudioError) as ei:
        JF.prepare_job_formats(ctx)
    assert ei.value.code == "NS-E320" and "canvas.w" in ei.value.detail


def test_a_damaged_record_does_not_advise_deleting_it_for_a_job_with_finished_stages(tmp_home: Path) -> None:
    ctx = make_ctx(tmp_home)
    ctx.file("formats.json").write_text("[]", encoding="utf-8")
    with pytest.raises(StudioError) as ei:
        JF.prepare_job_formats(ctx)
    assert "not a mapping" in ei.value.detail and "start it again from the beginning" in ei.value.detail
    assert "no finished stage" in ei.value.detail
    ctx.file("formats.json").write_text(json.dumps({"version": 1}), encoding="utf-8")
    with pytest.raises(StudioError) as ei2:
        JF.prepare_job_formats(ctx)
    assert "has no short format" in ei2.value.detail and "start it again" in ei2.value.detail


def test_a_job_from_before_the_update_keeps_the_shipped_definitions(tmp_home: Path) -> None:
    paths = Paths.from_env()
    put(paths, "short_9x16", **SMALL)  # the owner's override, made after the job started
    ctx = make_ctx(tmp_home)
    ctx.write_json("stage_state.json", {"voice": {"status": "done", "finished_at": "x", "inputs_hash": "y"}})
    doc = JF.prepare_job_formats(ctx)
    assert doc is not None and doc["short"]["legacy"] is True and doc["short"]["source"] == "shipped"
    assert JF.job_format(ctx, "short").canvas.w == 1080
    assert doc["short"]["definition"] == shipped("short_9x16")


def test_clip_and_series_compilation_jobs_record_no_story_formats(tmp_home: Path) -> None:
    for kind in ("clip", "series_compilation"):
        ctx = make_ctx(tmp_home, fmt=kind)
        assert JF.prepare_job_formats(ctx) is None and not ctx.file("formats.json").exists()


def test_master_first_jobs_record_the_short_format_and_say_so(tmp_home: Path) -> None:
    ctx = make_ctx(tmp_home, fmt="master_first")
    doc = JF.prepare_job_formats(ctx)
    assert doc is not None and set(doc) == {"version", "short"}


def probe_job(monkeypatch: pytest.MonkeyPatch, name: str) -> list[str]:
    ran: list[str] = []

    class Probe:
        tenant = None
        inputs: tuple[str, ...] = ()

        def __init__(self) -> None:
            self.name = name
            self.outputs = (f"{name}.json",)

        def run(self, ctx: Any) -> None:
            ran.append(self.name)
            ctx.write_json(f"{name}.json", {})

    monkeypatch.setitem(REGISTRY, name, register(Probe()))  # type: ignore[arg-type]
    monkeypatch.setitem(FORMAT_STAGES, "shorts_only", [name])
    return ran


@pytest.mark.parametrize(
    ("design", "needle"),
    [("short_missing", "unknown format"), ("short_shots", "shots.min_s"), ("long_copy", None)],
)
def test_a_series_part_whose_design_format_is_missing_or_broken_still_runs_and_says_so(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, design: str, needle: str | None
) -> None:
    ran = probe_job(monkeypatch, "p10_probe")
    paths = Paths.from_env()
    put(paths, "short_shots", shots={"min_s": 5, "max_s": 2})
    put(paths, "long_copy", base="long_16x9")
    ctx = make_ctx(tmp_home)
    ctx.write_json("series_context.json", {"series_title": "S", "design": {"format": design}})
    assert run_job(ctx, dry_run=True) == ["p10_probe"] and not ctx.file("formats.json").exists()
    assert run_job(ctx) == ["p10_probe"] and ran == ["p10_probe"]
    entry = json.loads(ctx.file("formats.json").read_text(encoding="utf-8"))["short"]
    assert entry["id"] == "short_9x16"
    if needle is None:  # a valid long format is not for the Short: nothing to report
        assert "note" not in entry and design not in entry["why"]
    else:
        assert design in entry["note"] and needle in entry["note"] and design in entry["why"]


def test_run_job_records_the_formats_before_the_first_stage(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []

    class Probe:
        name = "p10_probe2"
        tenant = None
        inputs: tuple[str, ...] = ()
        outputs = ("p10_probe2.json",)

        def run(self, ctx: Any) -> None:
            seen.append(ctx.file("formats.json").exists())
            ctx.write_json("p10_probe2.json", {})

    monkeypatch.setitem(REGISTRY, "p10_probe2", register(Probe()))  # type: ignore[arg-type]
    monkeypatch.setitem(FORMAT_STAGES, "shorts_only", ["p10_probe2"])
    put(Paths.from_env(), "short_copy")
    ctx = make_ctx(tmp_home, channel=make_channel(formats_allowed=["short_copy"]))
    run_job(ctx)
    assert seen == [True]
    assert json.loads(ctx.file("formats.json").read_text(encoding="utf-8"))["short"]["id"] == "short_copy"


# ---------- captions follow the frame ----------
def fixed_words(n: int = 8) -> list[Word]:
    return [Word(f"w{i}", i * 0.3, i * 0.3 + 0.25, 0) for i in range(n)]


def style_fields(ass: str) -> list[str]:
    return next(ln for ln in ass.splitlines() if ln.startswith("Style: Default,")).split(",")


def test_ass_options_for_the_shipped_short_equal_todays_options() -> None:
    today = AssOptions(variant="vertical", safe_zones=safe_zones_for("vertical"))
    assert AssOptions.for_format(F.get_format("short_9x16")) == today
    extra = AssOptions.for_format(F.get_format("short_9x16"), emphasis={0: "w1"}, mask_profanity=True)
    assert extra == AssOptions(
        variant="vertical", safe_zones=(220, 320), emphasis={0: "w1"}, mask_profanity=True
    )
    long = AssOptions.for_format(F.get_format("long_16x9"))
    assert (long.variant, long.safe_zones, long.canvas) == ("landscape", (54, 54), None)
    square = AssOptions.for_format(F.get_format("square_1x1"))
    assert square.variant == "vertical" and square.canvas == (1080, 1080) and square.anchor_y_pct is None


def test_ass_options_take_the_written_values_and_the_seam() -> None:
    raw = shipped("short_9x16")
    raw["captions"].update(anchor_y_pct=40, words_on_screen=2)
    raw["safe_margins"] = {"top": 100, "bottom": 500}
    opts = AssOptions.for_format(Format.model_validate(raw))
    assert (opts.anchor_y_pct, opts.max_words, opts.safe_zones) == (40.0, 2, (100, 500))
    seam = AssOptions.for_format(F.get_format("clip_gaming_facecam"))
    assert seam.anchor_y_pct is not None and seam.anchor_y_pct == pytest.approx(100 * 640 / 1920)


def test_a_format_that_writes_no_safe_margins_keeps_the_platform_zones_of_its_variant() -> None:
    """Review nit: an owner file without `safe_margins` got no safe zones (today: 220/320, 54/54)."""
    raw = shipped("short_9x16")
    del raw["safe_margins"]
    assert AssOptions.for_format(Format.model_validate(raw)).safe_zones == (220, 320)
    wide = shipped("long_16x9")
    del wide["safe_margins"]
    assert AssOptions.for_format(Format.model_validate(wide)).safe_zones == (54, 54)
    raw["safe_margins"] = {"top": 0, "bottom": 0}  # written as none: none
    assert AssOptions.for_format(Format.model_validate(raw)).safe_zones is None


def test_caption_geometry_scales_with_the_canvas() -> None:
    empty = Path(__file__).parent  # a folder without fonts is not needed: the fallback font is only a name
    base = build_ass(fixed_words(), "clean_white", fonts_dir=empty, anchor_y_pct=50)
    small = build_ass(fixed_words(), "clean_white", fonts_dir=empty, anchor_y_pct=50, canvas=(720, 1280))
    assert "PlayResX: 1080" in base and "PlayResY: 1920" in base
    assert "PlayResX: 720" in small and "PlayResY: 1280" in small
    geo = caption_style("clean_white").vertical
    size = round(geo.font_size * 720 / 1080)
    fields = style_fields(small)
    assert int(fields[2]) == size
    assert int(fields[-2]) == max(20, round(1280 * 0.5 - 0.6 * size))  # MarginV: the anchor formula on 1280
    assert int(style_fields(base)[2]) == geo.font_size
    # the reference canvas given explicitly is the same file
    assert (
        build_ass(fixed_words(), "clean_white", fonts_dir=empty, anchor_y_pct=50, canvas=(1080, 1920)) == base
    )
    band = caption_band("clean_white", "vertical", anchor_y_pct=50, canvas=(720, 1280))
    assert band.x + band.w <= 720 and band.y + band.h <= 1280


def test_caption_geometry_of_a_square_frame_stays_inside_it() -> None:
    ass = build_ass(fixed_words(), "punch_yellow", canvas=(1080, 1080), safe_zones=(100, 200))
    assert "PlayResY: 1080" in ass
    band = caption_band("punch_yellow", "vertical", safe_zones=(100, 200), canvas=(1080, 1080))
    assert 100 <= band.y and band.y + band.h <= 1080 - 200


def shown_words(ass: str) -> list[int]:
    import re

    out = []
    for ln in ass.splitlines():
        if ln.startswith("Dialogue:"):
            text = ln.split(",,", 1)[1]
            out.append(len(re.sub(r"\{[^}]*\}", "", text).split()))
    return out


def test_words_on_screen_caps_the_groups_only_when_written() -> None:
    plain = AssOptions.for_format(F.get_format("short_9x16"))
    assert plain.max_words is None
    base = plain.build(fixed_words(), "clean_white")
    assert max(shown_words(base)) >= 3
    raw = shipped("short_9x16")
    raw["captions"]["words_on_screen"] = 2
    capped = AssOptions.for_format(Format.model_validate(raw))
    assert capped.max_words == 2
    assert max(shown_words(capped.build(fixed_words(), "clean_white"))) <= 2
    st = caption_style("clean_white")
    assert max(len(g) for g in group_words(fixed_words(), st)) >= 3


def test_old_ass_opts_json_without_the_new_keys_loads() -> None:
    old = {
        "variant": "vertical",
        "safe_zones": [220, 320],
        "avoid_regions": [],
        "emphasis": {"0": "w"},
        "speakers": {},
        "speaker_breaks": [],
        "mask_profanity": False,
        "anchor_y_pct": None,
    }
    opts = AssOptions.from_json(old)
    assert opts.canvas is None and opts.max_words is None and opts.safe_zones == (220, 320)
    new = AssOptions(variant="vertical", canvas=(720, 1280), max_words=2)
    assert AssOptions.from_json(new.to_json()) == new


# ---------- QC ----------
def test_qc_aspect_uses_the_report_canvas_and_defaults_for_an_old_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Q.ff, "video_size", lambda _v: (720, 1280))
    video = Path("x.mp4")
    assert Q.aspect_check(video, "short", (720, 1280)).passed
    old = Q.aspect_check(video, "short")  # an old report has no canvas: today's 1080x1920
    assert not old.passed and old.limit == "1080x1920"
    assert Q.aspect_check(video, "long").limit == "1920x1080"
    assert Q._canvas_of({"canvas": [720, 1280]}) == (720, 1280)
    assert Q._canvas_of({}) is None and Q._canvas_of({"canvas": "x"}) is None
