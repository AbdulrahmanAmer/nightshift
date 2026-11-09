"""Status lines in contracts/CHANGE_REQUESTS.md that name a commit, a PR or a reader must be true."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / "contracts" / "CHANGE_REQUESTS.md").read_text(encoding="utf-8")


def entry(title_start: str, occurrence: int = 0) -> str:
    """The text of one `## CR-...` entry, from its title line to the next entry."""
    starts = [m.start() for m in re.finditer(r"^## " + re.escape(title_start), TEXT, re.M)]
    assert len(starts) > occurrence, f"no entry {title_start!r} #{occurrence}"
    begin = starts[occurrence]
    nxt = TEXT.find("\n## ", begin + 1)
    return TEXT[begin : nxt if nxt != -1 else len(TEXT)]


def test_direct_commits_are_not_credited_to_a_pull_request() -> None:
    # 59e9a45 and 03a99dc were pushed straight to main on 2026-11-01; `gh api commits/<sha>/pulls` is empty.
    assert not re.search(r"59e9a45 \(PR", TEXT)
    assert not re.search(r"03a99dc, PR", TEXT)
    assert re.search(r"59e9a45 \(a direct commit on main", TEXT)
    assert re.search(r"03a99dc, a direct commit on main", TEXT)
    # The NS-E408 change that did come through a pull request is 95580e6 (PR 15).
    assert "95580e6 (PR 15)" in entry("CR-C6-3")


def test_dashboard_job_rejection_names_the_other_readers_and_writers() -> None:
    status = next(line for line in entry("CR-C9-2").splitlines() if line.startswith("- **Status:**"))
    assert "nothing outside `ns_dashboard` reads it" not in status
    assert "ns_supervisor/scheduled.py" in status
    assert "ns_studio/series/workshop.py" in status
    assert "make_db_enqueue" in status
    # The claims are true of the code the entry names.
    assert "schedule_at" in (ROOT / "ns_supervisor" / "scheduled.py").read_text(encoding="utf-8")
    workshop = (ROOT / "ns_studio" / "series" / "workshop.py").read_text(encoding="utf-8")
    assert "def make_db_enqueue" in workshop
    assert "insert or ignore into dashboard_jobs" in workshop


def test_dashboard_service_entry_lists_every_difference_from_the_request() -> None:
    from ns_dashboard import DEFAULT_PORT
    from ns_supervisor.services import DASHBOARD_PORT

    status = next(line for line in entry("CR-C9-3").splitlines() if line.startswith("- **Status:**"))
    assert "`/api/health`" in status
    assert "`persistent`" in status
    assert "no `--port 8100`" in status
    assert DEFAULT_PORT == DASHBOARD_PORT == 8100


def test_engineering_doc_does_not_promise_a_dashboard_job_schema() -> None:
    text = (ROOT / "docs" / "ENGINEERING.md").read_text(encoding="utf-8")
    assert "`clip_template`, `dashboard_job` if not already in v1" not in text
