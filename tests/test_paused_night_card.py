"""P47: the Queue card of a half-made nightly job says a cause and a way back that are true TOGETHER.

The card's words come from `ns_studio/ownership.py:_card_text`. The cause follows who stopped the job last (a
pause, or a dead process); the way back follows why it waits now. A paused channel's Retry is refused (409
`channel_paused`), so a card of a job that waits for a pause must never say "press Retry" without "after the
resume", and must say that the first nightly run after the resume continues it, whatever stopped the job the
first time. Nothing rewrites a card when the owner pauses or resumes later, so the words must also hold then.

One test per combination of: why it first stopped (power cut, pause) x why it stopped again (not again, a
pause while the night continued it, a second power cut) x whether the channel was paused when the card was
last written x whether it is paused now. Each enters where the product does: the nightly run
(`daily.run_daily`), the start's recovery (`ownership.recover`) and the dashboard's Retry gate.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest

from ns_common.db import Db
from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_dashboard.errors import ApiError
from ns_dashboard.handlers import Dashboard, build_state
from ns_dashboard.supervisor_link import SupervisorLink
from ns_studio import daily, jobs, ownership, review
from tests import d31_driver as driver
from tests.test_interrupted_nightly import _dead_daily_job, _long_jobs, _queue_rows, _setup

pytest.importorskip("fastapi")

CH = "after-dark"
PAUSED_CODE = "NS-E330"
RESUME_FIRST = "Resume the channel: the next nightly run then continues it"
AFTER_RESUME = "or press Retry after the resume"
PENDING = "The next nightly run continues it, or press Retry (if its channel is paused by then, both wait"

#: (first stop, second stop, paused when the card was last written, paused now)
COMBINATIONS = [
    ("cut", "none", False, False), ("cut", "none", False, True),
    ("cut", "none", True, True), ("cut", "none", True, False),
    ("pause", "none", True, True), ("pause", "none", True, False),
    ("cut", "pause", True, True), ("cut", "pause", True, False),
    ("pause", "pause", True, True), ("pause", "pause", True, False),
    ("cut", "cut", False, False), ("cut", "cut", False, True),
    ("cut", "cut", True, True), ("cut", "cut", True, False),
    ("pause", "cut", False, False), ("pause", "cut", False, True),
    ("pause", "cut", True, True), ("pause", "cut", True, False),
]


def _set_paused(paths: Paths, db: Db, want: bool) -> None:
    from ns_studio.channels import crud

    paused = crud.get_channel(paths, CH).status == "paused"
    if want and not paused:
        review.pause(paths, db, CH, "a strike notice")
    elif paused and not want:
        review.resume(paths, db, CH)


def _night(paths: Paths, db: Db, *, pause_inside: bool) -> dict[str, Any]:
    """One nightly run. With `pause_inside` the owner pauses the channel while its job runs and the studio
    stops it before its next stage (NS-E330, what `orchestrator.run_job` raises); else the job finishes."""

    def run_fn(ctx: Any) -> list[str]:
        if pause_inside:
            review.pause(paths, db, CH, "a strike notice during the night")
            raise NsError(PAUSED_CODE, f"job {ctx.job_id} stopped before its next stage")
        return []

    return daily.run_daily(jobs.open_env(light=True), rng=random.Random(1), run_fn=run_fn)


def _power_cut(paths: Paths, db: Db, job_id: str) -> None:
    """The process that ran the job is gone (its record says running, its pid is dead); the next start of
    Nightshift closes it and writes its card."""
    ownership.write_record(paths, job_id, state="running", pid=2**22)
    ownership.recover(paths, db, note=False)


@pytest.mark.parametrize(("first", "again", "written_paused", "paused_now"), COMBINATIONS)
def test_the_card_of_a_half_made_nightly_job_and_its_retry_are_true_together(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, first: str, again: str, written_paused: bool,
    paused_now: bool,
) -> None:
    paths, db = _setup(tmp_home)
    driver.install(monkeypatch)
    # --- the first stop
    if first == "cut":
        job_id = _dead_daily_job(paths, db)
        _set_paused(paths, db, written_paused if again == "none" else False)
        _power_cut(paths, db, job_id)
    else:
        _night(paths, db, pause_inside=True)
        (job_id,) = _long_jobs(db)
    # --- the second stop
    if again == "pause":
        _set_paused(paths, db, False)  # resumed, so the next night continues it; the pause lands inside that
        report = _night(paths, db, pause_inside=True)
        assert any(p.get("job_id") == job_id for p in report["paused"]) and report["failures"] == []
    elif again == "cut":
        _set_paused(paths, db, written_paused)
        _power_cut(paths, db, job_id)
    _set_paused(paths, db, paused_now)

    (card,) = _queue_rows(db)
    text, error = str(card["message"]), card["result"]["error"]
    assert card["status"] == "failed" and card["result"]["studio_job_ids"] == [job_id]
    # the cause: who stopped it last
    by_pause = first == "pause" and again != "cut"
    assert ("its channel was paused during the night's run" in text) == by_pause, text
    assert ("the PC or Nightshift stopped during the night's run" in text) == (not by_pause), text
    # the way back: why it waited when the card was written, in words that hold whatever the channel is now
    if written_paused:
        assert RESUME_FIRST in text and AFTER_RESUME in text, text
        assert "Press Retry to continue it" not in text and PENDING not in text, text
        assert error["code"] == PAUSED_CODE, error
        if not by_pause:
            assert "Its channel was paused after that." in text, text
    else:
        assert PENDING in text and RESUME_FIRST not in text, text
        assert error["code"] == "NS-E328", error
    # Retry, as the card's button and the Assistant ask it
    link = SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
    dash = Dashboard(build_state(paths, fake_services=True, link=link))  # the channel's own profile
    if paused_now:
        with pytest.raises(ApiError) as refused:
            dash.jobs.check_resume(card["job_id"])
        assert refused.value.status == 409 and refused.value.code == "channel_paused"
        assert "after the resume" in text or "wait for the resume" in text, "the card said so"
    else:
        dash.jobs.check_resume(card["job_id"])  # accepted: the card's "press Retry" is true
    # and the promise: the first nightly run after the resume continues the job
    _set_paused(paths, db, False)
    after = _night(paths, db, pause_inside=False)
    assert any(j["job_id"] == job_id and j.get("resumed") for j in after["jobs"]), after
    (done,) = [r for r in _queue_rows(db) if r["result"].get("studio_job_ids") == [job_id]]
    assert done["status"] == "done" and "continued it and it finished" in done["message"]


def test_a_power_cut_card_then_a_pause_during_the_continuation_then_a_scan_while_paused(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's sequence, night by night: the card never tells the owner to press a Retry that is
    refused, and the scan of a later night (the channel still paused) leaves the same true words."""
    paths, db = _setup(tmp_home)
    driver.install(monkeypatch)
    job_id = _dead_daily_job(paths, db)
    _power_cut(paths, db, job_id)  # night 1 died
    assert PENDING in _queue_rows(db)[0]["message"]
    _night(paths, db, pause_inside=True)  # night 2 continues it; the owner pauses the channel during it
    for _scan in range(2):  # then two more nights find the channel still paused
        (card,) = _queue_rows(db)
        assert RESUME_FIRST in card["message"] and AFTER_RESUME in card["message"], card["message"]
        assert "Its channel was paused after that." in card["message"]
        assert "Press Retry to continue it" not in card["message"]
        assert card["result"]["error"]["code"] == PAUSED_CODE
        assert (ownership.read_record(paths, job_id) or {}).get("state") == "interrupted"
        report = _night(paths, db, pause_inside=False)
        assert report["jobs"] == [], "nothing of a paused channel runs"
