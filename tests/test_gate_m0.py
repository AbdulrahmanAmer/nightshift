import json
from pathlib import Path

from dev.gate_m0 import run_checks


def test_run_checks_structure(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    report = run_checks(skip_slow=True)   # skips pytest/ruff/pyright/manifest subprocesses
    names = [c.name for c in report.checks]
    assert {"fake_llm_schema", "fake_tts_wav", "fake_embed_vector", "ffmpeg_clip"} <= set(names)
    assert report.slice == "m0-c3"
    json.dumps(report.model_dump(mode="json"))
