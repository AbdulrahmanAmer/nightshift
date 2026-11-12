"""F67: a failed update names the verify check that failed, from the report its `ns verify --quick` wrote."""

import json
import os
import time
from pathlib import Path

import pytest

from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_supervisor import update
from tests.test_ns_update import Env, make_app, make_zip

NEED = "need 14729 + 512 > free 15039"


def report(paths: Paths, checks: list[dict[str, object]]) -> Path:
    paths.logs.mkdir(parents=True, exist_ok=True)
    f = paths.logs / "verify-report.json"
    f.write_text(json.dumps({"passed": False, "checks": checks}), encoding="utf-8")
    return f


def test_the_failed_checks_are_named_with_their_numbers_and_words(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "ns")
    report(paths, [
        {"name": "gpu.fits", "passed": True, "value": 1, "limit": "", "log": "fine"},
        {"name": "llama.vram", "passed": False, "value": 14729, "limit": "<= 15039",
         "log": f"NS-E208: {NEED}"},
        {"name": "tts.mode.fast", "passed": False, "value": None, "limit": "", "log": ""},
    ])
    said = update.verify_failed(paths, time.time() - 5)
    assert said == f"llama.vram (14729, limit <= 15039): NS-E208: {NEED}; tts.mode.fast"
    assert "gpu.fits" not in said and "None" not in said


def test_a_report_from_before_this_run_is_not_read(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "ns")
    f = report(paths, [{"name": "old.check", "passed": False, "log": "from yesterday"}])
    old = time.time() - 3600
    os.utime(f, (old, old))
    assert update.verify_failed(paths, time.time()) == ""


@pytest.mark.parametrize("text", ["", "not json", "[1, 2]", '{"checks": "x"}', '{"checks": [1, null]}'])
def test_a_report_that_cannot_be_read_names_nothing_and_never_raises(tmp_path: Path, text: str) -> None:
    paths = Paths(tmp_path / "ns")
    paths.logs.mkdir(parents=True)
    (paths.logs / "verify-report.json").write_text(text, encoding="utf-8")
    assert update.verify_failed(paths, time.time() - 5) == ""
    assert update.verify_failed(Paths(tmp_path / "none"), time.time() - 5) == ""    # no file at all


def test_one_checks_log_is_cut_not_a_whole_traceback(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "ns")
    report(paths, [{"name": "x", "passed": False, "log": "y" * 5000}])
    assert len(update.verify_failed(paths, time.time() - 5)) < 450


class ReportingEnv(Env):
    """A verify step that writes its report as `ns verify --quick` does, then fails."""

    def __init__(self, paths: Paths, checks: list[dict[str, object]] | None) -> None:
        super().__init__(verify_ok=False)
        self.paths, self.checks = paths, checks

    def verify(self, app: Path) -> bool:
        if self.checks is not None:
            report(self.paths, self.checks)
        return super().verify(app)


def test_a_failed_update_says_which_check_failed_and_rolls_back(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "ns")
    e = ReportingEnv(paths, [{"name": "llama.vram", "passed": False, "log": f"NS-E208: {NEED}"}])
    with pytest.raises(NsError) as ei:
        update.update_app(make_zip(tmp_path), make_app(tmp_path), e.env(), paths=paths)
    assert ei.value.code == "NS-E702"
    assert f"ns verify --quick failed on the new version: llama.vram: NS-E208: {NEED}" in ei.value.detail
    assert "failed at the check of the new version" in ei.value.detail
    assert e.calls == ["stop", "sync", "verify", "sync"]       # still put back


def test_a_failed_update_with_no_report_points_at_the_verify_logs(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "ns")
    stale = report(paths, [{"name": "old.check", "passed": False, "log": "from an earlier run"}])
    old = time.time() - 3600
    os.utime(stale, (old, old))
    e = ReportingEnv(paths, None)
    with pytest.raises(NsError) as ei:
        update.update_app(make_zip(tmp_path), make_app(tmp_path), e.env(), paths=paths)
    assert "old.check" not in ei.value.detail
    assert "it wrote no report: see logs\\verify-*.log" in ei.value.detail
