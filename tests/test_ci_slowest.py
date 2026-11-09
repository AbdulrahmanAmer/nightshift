"""H-3: the twenty slowest tests go into the job summary of the final `test` job (dev/ci_slowest.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from dev import ci_slowest as cs


def write_report(path: Path, cases: list[tuple[str, str, str]]) -> None:
    rows = "".join(f'<testcase classname="{c}" name="{n}" time="{t}"/>' for c, n, t in cases)
    path.write_text(
        f'<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">{rows}</testsuite>'
        "</testsuites>",
        encoding="utf-8",
    )


@pytest.fixture
def reports(tmp_path: Path) -> list[Path]:
    a, b = tmp_path / "report-shard-1.xml", tmp_path / "report-shard-2.xml"
    write_report(a, [("tests.test_a", "test_fast", "0.01"), ("tests.test_a.TestK", "test_slow", "12.5")])
    write_report(b, [("tests.test_b", "test_mid[x|y]", "3.25"), ("tests.test_b", "test_slowest", "40.0")])
    return [a, b]


@pytest.fixture(autouse=True)
def summary_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """GitHub sets GITHUB_STEP_SUMMARY for every CI step; these tests must never write into the real one."""
    p = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(p))
    return p


def test_the_slowest_tests_come_first_across_all_reports(reports: list[Path]):
    rows = cs.slowest(reports, top=3)
    assert [(r.seconds, r.test) for r in rows] == [
        (40.0, "tests.test_b::test_slowest"),
        (12.5, "tests.test_a.TestK::test_slow"),
        (3.25, "tests.test_b::test_mid[x|y]"),
    ]
    assert [r.source for r in rows] == ["shard 2", "shard 1", "shard 2"]


def test_the_default_is_twenty(tmp_path: Path):
    p = tmp_path / "report-shard-1.xml"
    write_report(p, [("tests.test_a", f"test_{i}", str(i)) for i in range(50)])
    assert len(cs.slowest([p])) == 20
    assert cs.slowest([p])[0].seconds == 49.0


def test_a_missing_or_broken_report_is_skipped_not_fatal(tmp_path: Path, reports: list[Path]):
    broken = tmp_path / "report-shard-3.xml"
    broken.write_text("<testsuites><testsuite>", encoding="utf-8")
    rows = cs.slowest([*reports, broken, tmp_path / "nothing.xml"], top=2)
    assert len(rows) == 2


def test_the_markdown_has_a_row_per_test_and_escapes_pipes(reports: list[Path]):
    text = cs.render(cs.slowest(reports, top=3), reports)
    assert text.startswith("### 3 slowest tests")
    assert "| 1 | 40.0 | `tests.test_b::test_slowest` | shard 2 |" in text
    assert "test_mid[x\\|y]" in text  # a pipe inside a table cell would split the row
    assert "4 tests in 2 report(s)" in text


def test_main_prints_and_appends_to_the_job_summary(
    reports: list[Path], summary_file: Path, capsys: pytest.CaptureFixture[str]
):
    assert cs.main(["--top", "2", "--summary", *map(str, reports)]) == 0
    out = capsys.readouterr().out
    assert "tests.test_b::test_slowest" in out
    body = summary_file.read_text(encoding="utf-8")
    assert body.startswith("### 2 slowest tests") and "test_slow" in body


def test_main_without_summary_flag_leaves_the_summary_file_alone(reports: list[Path], summary_file: Path):
    assert cs.main([*map(str, reports)]) == 0
    assert not summary_file.exists()


def test_main_without_any_readable_report_still_succeeds(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    assert cs.main([str(tmp_path / "gone.xml")]) == 0  # informational: it must never turn a run red
    assert "no test timings" in capsys.readouterr().out
