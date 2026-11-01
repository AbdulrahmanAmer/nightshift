import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from dev.gate_m3_dashboard import main, run_checks  # noqa: E402


def test_gate_passes_on_fake_services(tmp_home: Path):
    report = run_checks()
    failed = [(c.name, c.value, c.log) for c in report.checks if not c.passed]
    assert not failed, failed
    names = {c.name for c in report.checks}
    assert {"clip_job_done", "clip_in_pending_queue", "status_is_approved", "host_header_rejected"} <= names


def test_gate_cli_writes_a_report(tmp_home: Path, tmp_path: Path):
    out = tmp_path / "report.json"
    assert main(["--fake-services", "--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["slice"] == "m3-c9" and all(c["passed"] for c in data["checks"])
