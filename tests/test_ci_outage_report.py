"""H-1: the CI step that makes outage skips visible (dev/ci_outage_report.py)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from dev import ci_outage_report as cor

XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="4" skipped="2" failures="1">
<testcase classname="tests.test_resolve_manifest" name="test_a_online">
  <skipped type="pytest.skip" message="upstream outage: github.com HTTP 504">x</skipped></testcase>
<testcase classname="tests.test_resolve_manifest" name="test_b">
  <skipped type="pytest.skip" message="no ffmpeg on this machine">x</skipped></testcase>
<testcase classname="tests.test_downloader_network" name="test_c">
  <failure message="upstream outage: objects.example.com HTTP 504 after 4 tries">tb</failure></testcase>
<testcase classname="tests.test_x" name="test_d"><failure message="assert 504 == 200">tb</failure></testcase>
</testsuite></testsuites>
"""


@pytest.fixture(autouse=True)
def summary_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """GitHub sets GITHUB_STEP_SUMMARY for every CI step; these tests must never write into the real one."""
    p = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(p))
    return p


@pytest.fixture
def xml_file(tmp_path: Path) -> Path:
    p = tmp_path / "report.xml"
    p.write_text(XML, encoding="utf-8")
    return p


def test_only_outage_skips_and_failures_are_counted(xml_file: Path):
    skipped, failed = cor.scan([xml_file, xml_file.with_name("missing.xml")])
    assert [n for n, _ in skipped] == ["tests.test_resolve_manifest::test_a_online"]
    assert [n for n, _ in failed] == ["tests.test_downloader_network::test_c"]


def test_the_log_gets_a_warning_for_the_skip_and_an_error_for_the_failure(xml_file: Path):
    out = io.StringIO()
    assert cor.report([xml_file], required=False, out=out) == 0
    text = out.getvalue()
    assert "::warning::1 online check(s) skipped for an upstream outage: test_a_online" in text
    assert "::error::1 real-network test(s) failed for an upstream outage" in text


def test_required_runs_exit_1_when_a_check_was_skipped(xml_file: Path):
    out = io.StringIO()
    assert cor.report([xml_file], required=True, out=out) == 1
    assert "NS_REQUIRE_ONLINE is set" in out.getvalue()


def test_nothing_skipped_is_said_plainly_and_exits_0(tmp_path: Path):
    p = tmp_path / "ok.xml"
    p.write_text(
        '<testsuites><testsuite><testcase classname="a" name="b"/></testsuite></testsuites>', encoding="utf-8"
    )
    out = io.StringIO()
    assert cor.report([p], required=True, out=out) == 0
    assert "no online check was skipped" in out.getvalue()


def test_main_reads_ns_require_online_from_the_environment(
    xml_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.delenv("NS_REQUIRE_ONLINE", raising=False)
    assert cor.main([str(xml_file)]) == 0
    monkeypatch.setenv("NS_REQUIRE_ONLINE", "1")
    assert cor.main([str(xml_file)]) == 1
    capsys.readouterr()


def test_the_job_summary_gets_an_entry_for_an_outage_and_none_otherwise(
    xml_file: Path, summary_file: Path, tmp_path: Path
):
    ok = tmp_path / "ok.xml"
    ok.write_text(
        '<testsuites><testsuite><testcase classname="a" name="b"/></testsuite></testsuites>', encoding="utf-8"
    )
    assert cor.report([ok], required=True, out=io.StringIO()) == 0
    assert not summary_file.exists()
    cor.report([xml_file], required=False, out=io.StringIO())
    text = summary_file.read_text(encoding="utf-8")
    assert text.count("### Upstream outage") == 1
    assert "test_a_online" in text
