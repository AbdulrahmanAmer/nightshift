from pathlib import Path

import pytest
import yaml

from ns_common.paths import Paths
from ns_dashboard import templates
from ns_dashboard.templates import BUILTIN, load_templates, normalise_template, render_preview


def test_builtin_has_the_six_spec_templates_plus_auto():
    ids = [t["id"] for t in BUILTIN]
    assert ids == [
        "auto", "gaming_facecam", "podcast_stacked", "podcast_single", "talking_head", "center_crop",
        "screen_plus_cam",
    ]
    for t in BUILTIN:
        for r in t["regions"]:
            assert 0 <= r["x"] and 0 <= r["y"]
            assert r["x"] + r["w"] <= 1.001 and r["y"] + r["h"] <= 1.001
        assert 0 < t["caption_y_pct"] <= 100


def test_load_templates_adds_preview_urls(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(templates, "yaml_path", lambda: None)
    got = load_templates(Paths.from_env())
    assert got[0]["id"] == "auto"
    assert got[1]["preview_url"] == "/api/templates/gaming_facecam/preview.png"


def test_normalise_accepts_pixel_and_list_forms():
    t = normalise_template(
        "demo_layout",
        {
            "regions": {"cam": {"x": 0, "y": 0, "w": 1080, "h": 640}, "game": [0, 640, 1080, 1280]},
            "caption": {"y_pct": 0.5},
            "title_card": {"enabled": True},
        },
    )
    assert t["label"] == "Demo Layout"
    cam, game = t["regions"]
    assert cam == {"name": "cam", "x": 0.0, "y": 0.0, "w": 1.0, "h": 0.3333}
    assert game["name"] == "game" and game["y"] == 0.3333 and game["h"] == 0.6667
    assert t["caption_y_pct"] == 50.0 and t["title_card"] is True


def test_normalise_list_regions_and_bad_input():
    t = normalise_template("x", {"regions": [{"name": "a", "x": 0, "y": 0, "w": 0.5, "h": 0.5}, "junk"]})
    assert [r["name"] for r in t["regions"]] == ["a"]
    assert normalise_template("y", {"regions": 5, "caption_y_pct": "n/a"})["caption_y_pct"] == 70.0


def test_yaml_overlays_builtin(tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    f = tmp_path / "clip_templates.yaml"
    f.write_text(
        "templates:\n"
        "  center_crop:\n"
        "    label: Soft centre\n"
        "  brand_new:\n"
        "    label: Brand new\n"
        "    regions:\n"
        "      main: {x: 0, y: 0, w: 1, h: 1}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(templates, "yaml_path", lambda: f)
    by_id = {t["id"]: t for t in load_templates(Paths.from_env())}
    assert by_id["center_crop"]["label"] == "Soft centre"
    assert by_id["center_crop"]["regions"], "regions fall back to the built-in layout"
    assert by_id["brand_new"]["regions"][0]["name"] == "main"


def test_broken_yaml_falls_back_to_builtin(tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    f = tmp_path / "bad.yaml"
    f.write_text("templates: [unclosed", encoding="utf-8")
    monkeypatch.setattr(templates, "yaml_path", lambda: f)
    assert len(load_templates(Paths.from_env())) == len(BUILTIN)


def test_preview_png_drawn_and_cached(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("PIL")
    monkeypatch.setattr(templates, "yaml_path", lambda: None)
    paths = Paths.from_env()
    png = render_preview(paths, "gaming_facecam")
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    from PIL import Image

    with Image.open(png) as im:
        assert im.size == (216, 384)
    mtime = png.stat().st_mtime_ns
    assert render_preview(paths, "gaming_facecam") == png and png.stat().st_mtime_ns == mtime
    with pytest.raises(KeyError):
        render_preview(paths, "nope")


def test_real_clip_templates_yaml_shape(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    from ns_common.models import ClipTemplate

    f = tmp_home / "clip_templates.yaml"
    f.write_text(
        "gaming_facecam:\n"
        "  layout: gaming\n"
        "  description: Facecam on top.\n"
        "  needs: facecam\n"
        "  when: A facecam was found.\n"
        "  regions:\n"
        "    - {id: cam, x: 0, y: 0, w: 1080, h: auto, max_h: 640}\n"
        "    - {id: game, x: 0, y: seam, w: 1080, h: rest}\n"
        "  caption_anchor_y_pct: seam\n"
        "podcast_stacked:\n"
        "  layout: stacked\n"
        "  description: Two speakers.\n"
        "  regions:\n"
        "    - {id: top, x: 0, y: 0, w: 1080, h: 960}\n"
        "    - {id: bottom, x: 0, y: 960, w: 1080, h: 960}\n"
        "  caption_anchor_y_pct: 75\n",
        encoding="utf-8",
    )
    # the file is valid for the shared model, so C8 and the dashboard read the same thing
    for name, raw in yaml.safe_load(f.read_text(encoding="utf-8")).items():
        ClipTemplate.model_validate({"name": name, **raw})
    monkeypatch.setattr(templates, "yaml_path", lambda: f)
    by_id = {t["id"]: t for t in load_templates(Paths.from_env())}
    gaming = by_id["gaming_facecam"]
    assert gaming["label"] == "Gameplay with face cam"  # built-in friendly label survives the overlay
    cam, game = gaming["regions"]
    assert (cam["name"], cam["y"], cam["h"]) == ("cam", 0.0, 0.3333)
    assert (game["name"], game["y"], game["h"]) == ("game", 0.3333, 0.6667)
    assert gaming["caption_y_pct"] == 33.3
    assert by_id["podcast_stacked"]["caption_y_pct"] == 75.0
    assert [r["name"] for r in by_id["podcast_stacked"]["regions"]] == ["top", "bottom"]
