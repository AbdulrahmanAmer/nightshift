"""The Clipper's layout list comes from the `clip` format presets (and the owner's edits of them)."""

from pathlib import Path

import yaml

from ns_common.paths import Paths
from ns_dashboard.templates import load_templates
from ns_studio.media.formats import FORMATS_DIR, load_formats

SHIPPED = [
    "gaming_facecam", "podcast_stacked", "podcast_single", "talking_head", "center_crop", "screen_plus_cam",
]


def _owner_format(paths: Paths, fid: str, **changes: object) -> None:
    """Write `<home>\\config\\formats\\<fid>.yaml` as Library > Formats does (the shipped file plus edits)."""
    base = FORMATS_DIR / f"{fid}.yaml"
    body = yaml.safe_load(base.read_text(encoding="utf-8")) if base.exists() else {}
    body.update(changes)
    paths.formats_dir.mkdir(parents=True, exist_ok=True)
    (paths.formats_dir / f"{fid}.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")


def _own_layout() -> dict[str, object]:
    return {
        "id": "clip_split_screen",
        "name": "Clip: split screen",
        "kind": "clip",
        "canvas": {"w": 1080, "h": 1920, "fps": 30},
        "duration": {"min_s": 15, "max_s": 90},
        "captions": {"styles_allowed": ["clean_white"], "anchor_y_pct": 40, "words_on_screen": 3},
        "layout": {
            "seam_captions": False,
            "regions": [
                {"name": "left", "x": 0, "y": 0, "w": 540, "h": 1920, "source": "full"},
                {"name": "right", "x": 540, "y": 0, "w": 540, "h": 1920, "source": "screen"},
            ],
        },
    }


def test_lists_auto_then_every_shipped_clip_layout(tmp_home: Path):
    got = load_templates(Paths.from_env())
    assert [t["id"] for t in got] == ["auto", *SHIPPED]
    clip_formats = {i for i, f in load_formats().items() if f.kind == "clip"}
    assert {f"clip_{i}" for i in SHIPPED} == clip_formats  # nothing is listed that is not a preset


def test_regions_and_caption_anchor_come_from_the_preset(tmp_home: Path):
    by_id = {t["id"]: t for t in load_templates(Paths.from_env())}
    stacked = by_id["podcast_stacked"]
    assert [r["name"] for r in stacked["regions"]] == ["top", "bottom"]
    assert stacked["regions"][1] == {"name": "bottom", "x": 0.0, "y": 0.5, "w": 1.0, "h": 0.5}
    assert stacked["caption_y_pct"] == 75.0
    gaming = by_id["gaming_facecam"]
    assert gaming["regions"][0]["h"] == 0.3333 and gaming["caption_y_pct"] == 33.3  # captions sit on the seam
    assert by_id["auto"]["regions"] == []


def test_every_entry_has_the_fields_the_ui_reads(tmp_home: Path):
    for t in load_templates(Paths.from_env()):
        assert t["label"] and t["description"]
        assert isinstance(t["when"], str) and isinstance(t["title_card"], bool)
        assert 0 < t["caption_y_pct"] <= 100
        for r in t["regions"]:
            assert r["x"] + r["w"] <= 1.001 and r["y"] + r["h"] <= 1.001


def test_owner_edit_of_a_shipped_layout_shows_up(tmp_home: Path):
    paths = Paths.from_env()
    before = {t["id"]: t for t in load_templates(paths)}["center_crop"]
    _owner_format(paths, "clip_center_crop", name="Clip: soft centre")
    by_id = {t["id"]: t for t in load_templates(paths)}  # the dashboard stays up, so no restart is needed
    assert before["label"] != by_id["center_crop"]["label"] == "Soft centre"
    assert [t["id"] for t in load_templates(paths)] == ["auto", *SHIPPED]


def test_owner_added_layout_is_listed_after_the_shipped_ones(tmp_home: Path):
    paths = Paths.from_env()
    paths.formats_dir.mkdir(parents=True)
    (paths.formats_dir / "clip_split_screen.yaml").write_text(yaml.safe_dump(_own_layout()), encoding="utf-8")
    got = load_templates(paths)
    assert [t["id"] for t in got] == ["auto", *SHIPPED, "split_screen"]
    mine = got[-1]
    assert mine["label"] == "Split screen" and mine["caption_y_pct"] == 40.0
    assert [r["name"] for r in mine["regions"]] == ["left", "right"]
    assert mine["regions"][1]["x"] == 0.5 and mine["description"]


def test_non_clip_formats_are_not_layouts(tmp_home: Path):
    ids = {t["id"] for t in load_templates(Paths.from_env())}
    assert not ids & {"short_9x16", "long_16x9", "square_1x1"}


def test_a_broken_owner_file_does_not_take_the_list_down(tmp_home: Path):
    paths = Paths.from_env()
    paths.formats_dir.mkdir(parents=True)
    (paths.formats_dir / "clip_broken.yaml").write_text("id: [unclosed", encoding="utf-8")
    assert [t["id"] for t in load_templates(paths)] == ["auto", *SHIPPED]


def test_api_returns_the_shipped_layouts_and_the_owners_edited_one(tmp_home: Path):
    from tests.test_dashboard_api import make_env  # noqa: PLC0415

    paths = Paths.from_env()
    paths.formats_dir.mkdir(parents=True)
    (paths.formats_dir / "clip_split_screen.yaml").write_text(yaml.safe_dump(_own_layout()), encoding="utf-8")
    client, _ = make_env(tmp_home)
    got = client.get("/api/templates").json()
    assert [t["id"] for t in got] == ["auto", *SHIPPED, "split_screen"]
    assert {"id", "label", "description", "when", "regions", "caption_y_pct", "title_card"} <= set(got[0])


def _client_with_own_layout(tmp_home: Path):
    from ns_dashboard.fakes import seed_channel  # noqa: PLC0415
    from tests.test_dashboard_api import make_env  # noqa: PLC0415

    paths = Paths.from_env()
    paths.formats_dir.mkdir(parents=True)
    (paths.formats_dir / "clip_split_screen.yaml").write_text(yaml.safe_dump(_own_layout()), encoding="utf-8")
    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths)
    return client, state


def test_a_new_clip_job_with_an_owner_added_layout_is_refused_before_it_is_queued(tmp_home: Path):
    from tests.test_dashboard_api import clip_file  # noqa: PLC0415

    client, state = _client_with_own_layout(tmp_home)
    body = {
        "path": str(clip_file(tmp_home)), "channel": "after-dark", "template": "split_screen",
        "rights_confirmed": True,
    }
    r = client.post("/api/jobs/clip", json=body)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "invalid_options" and "Split screen" in err["message"]
    assert "shipped layouts" in err["message"]
    assert state.store.list_jobs(kind="clip") == []  # nothing was queued, so no GPU or LLM time is spent


def test_a_clip_job_still_runs_with_auto_and_with_an_owner_edited_shipped_layout(tmp_home: Path):
    from tests.test_dashboard_api import clip_file  # noqa: PLC0415

    client, state = _client_with_own_layout(tmp_home)
    _owner_format(Paths.from_env(), "clip_talking_head", name="Clip: my talking head")
    video = clip_file(tmp_home)
    for template in ("auto", "talking_head"):
        body = {"path": str(video), "channel": "after-dark", "template": template, "rights_confirmed": True}
        r = client.post("/api/jobs/clip", json=body)
        assert r.status_code == 201, r.text
    assert len(state.store.list_jobs(kind="clip")) == 2


def test_a_clip_rerun_with_an_owner_added_layout_is_refused(tmp_home: Path):
    client, state = _client_with_own_layout(tmp_home)
    clip = state.store.create("clip", "after-dark", {})
    state.store.finish(clip.job_id, "done", "Done", {"output_ids": []})
    r = client.post("/api/jobs/clip/rerun", json={"job_id": clip.job_id, "template": "split_screen"})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "invalid_options"
    assert "shipped layouts" in r.json()["error"]["message"]
