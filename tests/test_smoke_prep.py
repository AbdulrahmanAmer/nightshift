"""C1/C2: the smoke test starts from a clean report and waits as long as the install drive needs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from installer.stage2.runner import RunResult
from installer.stage2.simulate import FakeRunner
from installer.stage2.steps import s08_smoke
from installer.stage2.steps.s08_smoke import S08Smoke
from installer.stage2.sysinfo import sample_preflight
from installer.stage2.testing import example_answers, make_test_context
from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_supervisor import health_cfg

ROOT = Path(__file__).resolve().parents[1]
STALE = json.dumps({"checks": [{"name": "llama.start", "passed": True, "value": None, "limit": None}]})


def ctx_with(tmp_path: Path, runner: FakeRunner):
    pf = sample_preflight(tmp_path / "home", ROOT)
    return make_test_context(
        tmp_path, raw_answers=example_answers(), resolve=True, runner=runner, preflight=pf
    )


# ---- the health timeout ------------------------------------------------------------------------------
def test_the_timeout_is_600_seconds_off_an_ssd_and_300_on_one():
    assert health_cfg.timeout_for_drive("SSD") == 300.0
    assert health_cfg.timeout_for_drive("HDD") == 600.0
    assert health_cfg.timeout_for_drive("Unknown") == 600.0


def test_the_supervisor_reads_what_setup_wrote(tmp_path: Path):
    paths = Paths(tmp_path / "home")
    assert health_cfg.read_health_timeout(paths) == 300.0              # no file: the default
    health_cfg.write_health_timeout(paths, 600.0)
    assert health_cfg.read_health_timeout(paths) == 600.0
    health_cfg.health_file(paths).write_text("{not json", encoding="utf-8")
    assert health_cfg.read_health_timeout(paths) == 300.0
    health_cfg.health_file(paths).write_text('{"health_timeout_s": 2}', encoding="utf-8")
    assert health_cfg.read_health_timeout(paths) == 300.0              # nonsense is ignored


def test_the_engine_uses_the_configured_timeout():
    import inspect

    from ns_supervisor import app

    src = inspect.getsource(app.main)
    assert "read_health_timeout(paths)" in src and "config=cfg" in src


@pytest.mark.parametrize(("drive", "seconds"), [("HDD", 600.0), ("SSD", 300.0)])
def test_s08_writes_the_timeout_for_the_install_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drive: str, seconds: float
):
    monkeypatch.setattr(s08_smoke, "drive_type", lambda pf, path: drive)
    ctx = ctx_with(tmp_path, FakeRunner(tmp_path / "desktop"))
    S08Smoke().run(ctx)
    assert health_cfg.read_health_timeout(ctx.paths) == seconds


# ---- the stale report --------------------------------------------------------------------------------
def test_a_stale_report_is_deleted_before_verify_runs(tmp_path: Path):
    seen: list[bool] = []

    def verify_dies(call) -> RunResult:
        seen.append(report_path.exists())
        return RunResult(1, "", "Traceback: boom")             # dies before it writes anything

    runner = FakeRunner(tmp_path / "desktop", results={"ns verify --quick": verify_dies})
    ctx = ctx_with(tmp_path, runner)
    report_path = ctx.paths.logs / "verify-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(STALE, encoding="utf-8")
    with pytest.raises(NsError) as ei:
        S08Smoke().run(ctx)
    assert ei.value.code == "NS-E202" and "no report" in ei.value.detail
    assert seen == [False] and not report_path.exists()


def test_without_the_fix_the_stale_report_would_have_been_believed(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop")
    ctx = ctx_with(tmp_path, runner)
    stale = ctx.paths.logs / "verify-report.json"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(STALE, encoding="utf-8")
    assert s08_smoke.load_report_rows(ctx, RunResult(1, "", "crash")) is not None      # the old behaviour
    s08_smoke.prepare_verify(ctx)
    assert s08_smoke.load_report_rows(ctx, RunResult(1, "", "crash")) is None
