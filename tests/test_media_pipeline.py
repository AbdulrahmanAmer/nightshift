import json
from pathlib import Path

import jsonschema
import pytest

from ns_common import ffmpeg as ff
from ns_common.paths import contracts_dir
from ns_studio.media import stages_media  # noqa: F401
from ns_studio.media.outbox import job_date
from ns_studio.orchestrator import FORMAT_STAGES, run_job
from ns_studio.text import stages_shorts  # noqa: F401
from tests.helpers import make_combo, make_ctx

pytestmark = pytest.mark.skipif(not ff.available(), reason="ffmpeg required")

SENTENCES = [
    "The doorbell camera pings every night at the same hour.",
    "I stopped checking the clips a week ago because of that.",
    "Tonight my phone buzzed while I stood on the porch outside.",
    "The footage showed me standing right where I was standing.",
    "Behind me the front door was open and waiting for me.",
]
TEXT = " ".join(SENTENCES)


def test_media_stages_produce_a_valid_outbox_short(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(FORMAT_STAGES, "media_only", FORMAT_STAGES["shorts_only"][-6:])
    ctx = make_ctx(tmp_home, fmt="media_only", combo=make_combo(length_bucket=23))
    words = len(TEXT.split())
    ctx.write_json("parts.json", [{
        "n": 1, "text": TEXT, "hook": "My doorbell camera records someone every night.",
        "word_count": words, "est_seconds": 19.0, "cliffhanger": False,
    }])
    ctx.write_json("visual_plan.json", [
        {"part": 1, "beat": 0, "sentence_start": 0, "sentence_end": 2, "keywords": ["doorbell", "dark porch"],
         "mood": "eerie", "need_vertical": True},
        {"part": 1, "beat": 1, "sentence_start": 3, "sentence_end": 4,
         "keywords": ["open door", "night street"],
         "mood": "horror", "need_vertical": True},
    ])
    ctx.write_json("seo.json", [{
        "title": "Scary Story: The Doorbell Camera",
        "description": "A camera records someone. Original fiction.",
        "tags": [f"tag{i}" for i in range(10)], "keywords": [],
        "hashtags": ["#Shorts", "#horror", "#scarystory"],
    }])

    ran = run_job(ctx)
    assert ran == ["voice", "captions", "clips", "render", "qc", "outbox"]

    rep = ctx.read_json("captions/report.json")["s1"]
    assert rep["first_caption_s"] == 0.0 and rep["drift_ms"] <= 120
    qc = ctx.read_json("qc/s1.json")
    assert qc["passed"], qc
    assert 19.5 <= ff.duration_s(Path(ctx.read_json("render/report.json")["s1"]["path"])) <= 26.5

    date = job_date(ctx.job_id).strftime("%Y-%m-%d")
    d = ctx.paths.outbox / "after-dark" / date / ctx.job_id / "shorts"
    assert ff.video_size(d / "s1.mp4") == (1080, 1920) and ff.has_faststart(d / "s1.mp4")
    meta = json.loads((d / "s1.json").read_text(encoding="utf-8"))
    schema = json.loads((contracts_dir() / "schemas" / "metadata.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(meta, schema)
    assert len(meta["clips"]) >= 6 and meta["kind"] == "short" and meta["k"] == 1
    assert ctx.db.one("select count(*) c from outputs")["c"] == 1
    assert ctx.db.one("select count(*) c from clip_usage")["c"] == len(meta["clips"])

    # second run is a no-op for the stages and leaves the Outbox intact
    run_job(ctx)
    assert (d / "s1.mp4").exists()
    assert ctx.db.one("select count(*) c from outputs")["c"] == 1
