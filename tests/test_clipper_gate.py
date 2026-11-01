"""C8 Clipper: the M3 clipper gate script runs green on the fixture."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from ns_common import ffmpeg

ROOT = Path(__file__).resolve().parents[1]


def load_gate():
    spec = importlib.util.spec_from_file_location("gate_m3_clipper", ROOT / "dev" / "gate_m3_clipper.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_gate_module_imports() -> None:
    mod = load_gate()
    assert callable(mod.run_gate) and callable(mod.main)


@pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not available")
def test_gate_passes(tmp_path: Path) -> None:
    report = load_gate().run_gate(tmp_path)
    failed = [(c.name, c.value) for c in report.checks if not c.passed]
    assert not failed, failed
    names = {c.name for c in report.checks}
    assert {"clips_at_least_2", "clips_1080x1920", "facecam_within_5pct", "captions_present", "qc_passed",
            "sheets_valid"} <= names
