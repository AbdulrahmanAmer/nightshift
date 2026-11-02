import json
from pathlib import Path

import jsonschema
import pytest

from ns_common import ffmpeg as ff
from ns_common.paths import contracts_dir
from ns_studio.media import master as M
from ns_studio.media import qc as Q
from ns_studio.media import thumbnails as T
from ns_studio.media.stages_media import MASTER_MEDIA_STAGES
from ns_studio.orchestrator import FORMAT_STAGES, run_job
from tests.helpers import make_combo, make_ctx

pytestmark = pytest.mark.skipif(not ff.available(), reason="ffmpeg required")

MASTER_TITLE = "The Long Night Story"


def chapter_text(prefix: str) -> str:
    sentences = []
    for s in range(3):
        words = " ".join(f"{prefix}{chr(97 + s)}{chr(97 + i)}" for i in range(9))
        sentences.append(f"{words} end.")
    return " ".join(sentences)


def beat(chapter: int, n: int, start: int, end: int, vertical: bool) -> dict:
    return {"part": None, "chapter": chapter, "beat": n, "sentence_start": start, "sentence_end": end,
            "keywords": [f"night road {chapter}{n}", f"car lights {chapter}{n}"], "mood": "eerie",
            "need_vertical": vertical}


def fake_make_thumbnails(frames, text, out_dir, layouts=None):
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(3):
        p = out_dir / f"thumb_{i + 1}.jpg"
        p.write_bytes(frames[i % len(frames)].read_bytes())
        paths.append(p)
    return paths


def test_master_first_media_pipeline(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    names = [s.name for s in MASTER_MEDIA_STAGES]
    assert names == ["voice", "duration_check", "captions", "shorts_timing", "clips", "render_master",
                     "thumbnails", "shorts_derive", "qc", "outbox"]
    monkeypatch.setitem(FORMAT_STAGES, "master_first", names)
    monkeypatch.setattr(M, "FLOOR_S", 20.0)
    monkeypatch.setattr(Q, "LONG_RANGE_S", (20.0, 200.0))
    monkeypatch.setattr(T, "make_thumbnails", fake_make_thumbnails)

    ctx = make_ctx(tmp_home, fmt="master_first", combo=make_combo(visual_treatment="cinematic_teal"))
    ctx.write_json("story.json", {"working_title": MASTER_TITLE, "chapters": [
        {"n": i, "title": f"Chapter title {i}", "text": chapter_text(chr(120 - i))} for i in (1, 2, 3)
    ]})
    ctx.write_json("shorts_plan.json", [
        {"k": 1, "chapter": 1, "sentence_start": 0, "sentence_end": 1, "hooks": ["Listen to this now."],
         "chosen_hook": "Listen to this opening line now.",
         "closing_line": "The full story is on the channel.",
         "combo_id": "c-test", "why_standalone": "x"},
        {"k": 2, "chapter": 2, "sentence_start": 1, "sentence_end": 2, "hooks": ["Nobody believed me."],
         "chosen_hook": "Nobody believed me at first.",
         "closing_line": "Watch the whole thing on the channel.",
         "combo_id": "c-test", "why_standalone": "x"},
    ])
    short_combos = {
        "1": make_combo(combo_id="c-s1", caption_style="clean_white", visual_treatment="handheld_grit",
                   length_bucket=None),
        "2": make_combo(combo_id="c-s2", caption_style="boxed_dark", visual_treatment="clean_bright",
                   length_bucket=None),
    }
    ctx.write_json("short_combos.json", {k: c.model_dump(mode="json") for k, c in short_combos.items()})
    ctx.write_json("visual_plan.json", [
        beat(1, 0, 0, 1, True), beat(1, 1, 2, 2, False),
        beat(2, 0, 0, 0, False), beat(2, 1, 1, 2, True),
        beat(3, 0, 0, 2, False),
    ])
    ctx.write_json("seo_long.json", {
        "title": MASTER_TITLE,
        "description": "A drive home goes wrong.\nNothing is as it seems.\n\n[[CHAPTERS]]\n\n"
        "Original fiction.",
        "tags": [f"tag{i}" for i in range(10)], "hashtags": ["#horror", "#story", "#shorts"], "keywords": [],
        "chapter_titles": [{"n": i, "title": f"Chapter title {i}"} for i in (1, 2, 3)],
    })
    ctx.write_json("seo_shorts.json", [
        {"title": f"Short {k}: The Long Night", "description": "A hook.\nOriginal fiction.",
         "tags": [f"tag{i}" for i in range(10)], "hashtags": ["#Shorts", "#horror", "#story"], "keywords": []}
        for k in (1, 2)
    ])

    assert run_job(ctx) == names

    # chapters, timeline, mid-rolls
    doc = ctx.read_json("long/chapters.json")
    assert [c["timestamp"] for c in doc["chapters"]][0] == "00:00" and len(doc["chapters"]) == 3
    master = ctx.file("long/master.mp4")
    assert ff.video_size(master) == (1920, 1080) and ff.has_faststart(master)
    assert abs(ff.duration_s(master) - doc["total_s"]) < 0.4
    assert doc["midroll_suggestions_s"] == []  # the whole video is shorter than 4 minutes
    assert ctx.read_json("duration_check.json")["expansions"] == []
    qc = ctx.read_json("qc/report.json")
    assert set(qc) == {"long", "s1", "s2"} and all(v["passed"] for v in qc.values()), qc

    # Outbox
    man = ctx.read_json("outbox/manifest.json")
    full = Path(man["long"]["mp4"]).parent
    story = ctx.paths.outbox / "after-dark" / "stories" / "The Long Night Story"
    assert full == story / "full" and Path(man["long"]["mp4"]).name == f"{MASTER_TITLE}.mp4"
    for name in (f"{MASTER_TITLE}.mp4", f"{MASTER_TITLE}.json", f"{MASTER_TITLE}.txt",
                 "thumb_1.jpg", "thumb_2.jpg", "thumb_3.jpg"):
        assert (full / name).exists(), name
    schema = json.loads((contracts_dir() / "schemas" / "metadata.schema.json").read_text(encoding="utf-8"))
    long_meta = json.loads(Path(man["long"]["json"]).read_text(encoding="utf-8"))
    jsonschema.validate(long_meta, schema)
    assert long_meta["kind"] == "long" and "00:00 Chapter title 1" in long_meta["description"]
    assert "[[CHAPTERS]]" not in long_meta["description"] and "#shorts" not in long_meta["hashtags"]
    assert long_meta["thumbnails"] == ["thumb_1.jpg", "thumb_2.jpg", "thumb_3.jpg"]
    for k in (1, 2):
        meta = json.loads(Path(man[f"s{k}"]["json"]).read_text(encoding="utf-8"))
        jsonschema.validate(meta, schema)
        assert meta["kind"] == "short" and meta["k"] == k and meta["parent_job_id"] == ctx.job_id
        assert meta["related_video_instruction"] == f"Set Related video -> {MASTER_TITLE}"
        assert f"From the full story: {MASTER_TITLE}" in meta["description"]
        sheet = Path(man[f"s{k}"]["txt"]).read_text(encoding="utf-8")
        assert f"Related video: set Related video -> {MASTER_TITLE}" in sheet
        video = Path(man[f"s{k}"]["mp4"])
        assert video.parent == story / "shorts" and video.name == f"{MASTER_TITLE} - Short {k:02d}.mp4"
        assert ff.video_size(video) == (1080, 1920)
        tl = ctx.read_json(f"captions/s{k}.json")
        assert abs(ff.duration_s(video) - tl["audio_s"]) < 0.3
        assert tl["words"][0]["sentence_idx"] == -1 and tl["words"][-1]["sentence_idx"] == -2
    # Review row 5: the Short's own combo is what is rendered and what the outputs row records
    assert ctx.read_json("captions/s1.json")["style"] == "clean_white"
    assert ctx.read_json("captions/s2.json")["style"] == "boxed_dark"
    rows = ctx.db.query("select output_id, combo_id from outputs where job_id=?", (ctx.job_id,))
    assert {r["output_id"].split("/")[1]: r["combo_id"] for r in rows} == {
        "long": "c-test", "s1": "c-s1", "s2": "c-s2",
    }
    kinds = sorted(r["kind"] for r in ctx.db.query("select kind from outputs where job_id=?", (ctx.job_id,)))
    assert kinds == ["long", "short", "short"]
    used = ctx.db.one(
        "select count(*) c from clip_usage where job_id=? and item='long'", (ctx.job_id,)
    )
    assert used["c"] >= 3
