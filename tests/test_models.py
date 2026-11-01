import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ns_common import models as m
from ns_common.paths import contracts_dir


def test_channel_defaults_and_validation():
    ch = m.Channel(slug="after-dark", display_name="After Dark", niche="horror", language="en")
    assert ch.status == "active"
    assert ch.format == "master_first"
    assert ch.long_minutes == (10, 14)
    assert ch.pools.hooks  # defaults non-empty
    with pytest.raises(ValidationError):
        m.Channel(slug="Bad Slug!", display_name="x", niche="y", language="en")


def test_job_id_pattern():
    md = m.Metadata(
        channel="after-dark", job_id="after-dark-20261101-ab12", kind="short", k=1,
        title="t", description="d", tags=["a"], hashtags=["#Shorts"], publish_after="2026-11-02T18:00:00",
        ai_disclosure="no", made_for_kids=False, age_restrict_suggested=False, clips=[], combo_id="c1",
        prompt_versions={"hook": 1},
    )
    assert md.kind == "short"
    with pytest.raises(ValidationError):
        md.model_copy(update={"job_id": "nope"}).model_validate(md.model_dump() | {"job_id": "nope"})


def test_runtime_default_profiles():
    rt = m.Runtime.default("cpu-dev")
    assert rt.video.encoder == "libx264"
    assert rt.llama.port == 8080 and rt.embed.port == 8081 and rt.tts.port == 8091
    gpu = m.Runtime.default("gpu")
    assert gpu.video.encoder == "h264_nvenc"
    assert gpu.llama.flags.ctx == 16384


def test_every_schema_file_matches_models():
    out = contracts_dir() / "schemas"
    for name, model in m.SCHEMA_MODELS.items():
        f = out / f"{name}.schema.json"
        assert f.exists(), f"missing {f}; run python -m ns_common.models"
        assert json.loads(f.read_text(encoding="utf-8")) == model.model_json_schema(), name


def test_export_writes_all(tmp_path: Path):
    files = m.export_schemas(tmp_path)
    assert {f.stem.replace(".schema", "") for f in files} == set(m.SCHEMA_MODELS)
