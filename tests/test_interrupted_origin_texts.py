# ruff: noqa: E501
"""Every text about an interrupted job says what is true for who started it (block B install proof, defect 3).

The morning note for a job started with `studio run` read "The night's run was interrupted", its Queue card
said the PC stopped "during the night's run", and `studio status` said "press Retry in the Queue" of a job no
card stood for yet. One test per origin, each through the product's own entries (`jobs.make_job`, the owner
record, `ownership.recover`, the dashboard's job store), reading every text that names the origin:

| origin | Queue card | morning note | `studio status` |
|---|---|---|---|
| the nightly run | ours: "during the night's run" | "The night's run was interrupted" | Retry, or the next night |
| `studio run` | ours: "while this job ran" | "A job you started yourself" | the command, then Retry |
| Run now | the dashboard's own | none (the card says it) | Retry |
| a dashboard request whose row does not name the job yet | ours: "while this job ran" | "A job you started" | Retry |
| a series part | the dashboard's own | "A job you started yourself", the command | the command |
| a clip | the dashboard's own | none: a clip has no owner record | none |

`ns doctor` has no row about an interrupted job, so there is nothing there to word.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ns_common.db import Db
from ns_common.paths import Paths
from ns_studio import jobs, ownership
from ns_studio.channels import crud
from ns_studio.stages import StageState
from tests.helpers import make_channel

CH = "after-dark"
NIGHT_WORDS = ("night's run", "nightly", "The night")


def _setup() -> tuple[Paths, Db]:
    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    created = (datetime.now(UTC) - timedelta(weeks=11)).isoformat(timespec="seconds")
    crud.add_channel(paths, db, make_channel(format="master_first", created_at=created))
    return paths, db


def _dead_job(paths: Paths, origin: str, **kw: Any) -> str:
    """A job made the way the product makes one (`make_job` announces it), one stage done, its process gone."""
    ctx = jobs.make_job(jobs.open_env(light=True), crud.get_channel(paths, CH), "master_first", origin=origin, **kw)
    state = StageState()
    state.mark_done("ideas", "h")
    state.save(ctx.job_dir)
    ownership.write_record(paths, ctx.job_id, state="running", pid=2**22)
    return ctx.job_id


def _cards(db: Db) -> list[dict[str, Any]]:
    out = []
    for r in db.query("select * from dashboard_jobs order by created_at, rowid"):
        payload = json.loads(r["params_json"])
        out.append({**dict(r), "params": payload["params"], "result": payload["result"] or {}})
    return out


def _notes(paths: Paths) -> list[str]:
    from ns_supervisor.notify import read_notes

    return [str(n["message"]) for n in read_notes(paths)]


def _status(paths: Paths, db: Db, job_id: str) -> str:
    [line] = [ln for ln in ownership.status_lines(paths, db, CH) if f"job {job_id} " in ln]
    return line


def _dashboard_row(db: Db, kind: str, job_id: str | None) -> str:
    """A row of the dashboard's own job store that is `running`, naming the studio job when it has one."""
    card_id = f"{kind}-20261110-0001"
    result = {"studio_job_ids": [job_id]} if job_id else None
    payload = json.dumps({"params": {"channel": CH}, "result": result, "dismissed_at": None})
    stamp = datetime.now(UTC).isoformat(timespec="milliseconds")
    db.execute(
        "insert into dashboard_jobs(job_id, kind, channel, status, progress, message, created_at, started_at,"
        " finished_at, params_json) values (?, ?, ?, 'running', 0.3, 'long_plan (2/21)', ?, ?, null, ?)",
        (card_id, kind, CH, stamp, stamp, payload),
    )
    return card_id


def _dashboard_recovers(paths: Paths) -> None:
    """What the dashboard does at engine start (`JobWorker.start`): its own rows first, then the studio's
    dead work, through the state the real dashboard builds."""
    from ns_dashboard.handlers import build_state
    from ns_dashboard.supervisor_link import SupervisorLink

    state = build_state(paths, fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2))
    state.store.recover_interrupted()
    assert state.worker.recover_studio is not None
    state.worker.recover_studio()


def test_a_job_of_the_nightly_run_is_worded_as_the_nights_run(tmp_home: Path) -> None:
    paths, db = _setup()
    db.execute("insert into runs(run_id, started_at, mode) values ('daily-x', ?, 'daily')",
               (datetime.now(UTC).isoformat(timespec="seconds"),))
    job = _dead_job(paths, "daily", run_id="daily-x")
    before = _status(paths, db, job)
    assert "its process is gone" in before and f"studio run --resume {job}" in before
    assert "the next nightly run continues it too (while Automatic runs are on in Settings)" in before
    ownership.recover(paths, db)
    [card] = _cards(db)
    assert card["message"].startswith("Interrupted at long_plan: the PC or Nightshift stopped during the night's run.")
    assert "The next nightly run continues it, or press Retry" in card["message"]
    assert card["result"]["error"]["hint"] == ownership.INTERRUPTED_HINT
    assert card["result"]["interrupted"]["origin"] == "daily"
    [note] = _notes(paths)
    assert note.startswith("The night's run was interrupted (power cut, crash or restart). after-dark stopped")
    assert "started yourself" not in note
    after = _status(paths, db, job)
    assert "press Retry in the Queue or the next nightly run continues it" in after
    assert "its process is gone" not in after


def test_a_job_started_with_studio_run_is_never_called_the_nights_run(tmp_home: Path) -> None:
    """The proof's job: `studio run`, killed. No Queue row exists until something closes it."""
    paths, db = _setup()
    job = _dead_job(paths, "manual")
    # before anything closed it: no card stands for it, so the way that works now is the command
    assert _cards(db) == []
    before = _status(paths, db, job)
    assert before.index(f"continue it now with studio run --resume {job}") < before.index("Retry")
    assert "press Retry in the Queue after the next start of Nightshift" in before
    assert not any(w in before for w in NIGHT_WORDS), before
    ownership.recover(paths, db)
    [card] = _cards(db)
    assert card["message"] == (
        "Interrupted at long_plan: the PC or Nightshift stopped while this job ran. Press Retry to continue it."
    )
    hint = card["result"]["error"]["hint"]
    assert card["result"]["error"]["code"] == "NS-E328" and hint == ownership.INTERRUPTED_HINT_BY_HAND
    assert card["result"]["interrupted"]["origin"] == "manual"
    [note] = _notes(paths)
    assert note == (
        "A job you started yourself was interrupted (power cut, crash or restart). after-dark stopped at "
        "long_plan. Press Retry in the Queue to continue it."
    )
    after = _status(paths, db, job)
    assert after.endswith("; press Retry in the Queue")
    for text in (card["message"], hint, note, after):
        assert not any(w in text for w in NIGHT_WORDS), text
    # continued by hand (`studio run --resume` settles the card): the card does not credit the nightly run
    ownership.settle_resumed(db, job, ["o1"], by="hand")
    [card] = _cards(db)
    assert card["status"] == "done" and card["message"].endswith(
        "while this job ran. It was continued with studio run --resume and it finished."
    )


def test_a_run_now_job_keeps_the_dashboards_own_card_and_gets_no_second_text(tmp_home: Path) -> None:
    paths, db = _setup()
    job = _dead_job(paths, "manual")
    row = _dashboard_row(db, "run", job)
    _dashboard_recovers(paths)
    [card] = _cards(db)                                     # one card: the dashboard's own
    assert card["job_id"] == row and card["status"] == "failed"
    assert card["message"] == "Interrupted by a restart"
    hint = card["result"]["error"]["hint"]
    assert hint == ownership.INTERRUPTED_HINT_BY_HAND      # the two modules say the same sentence
    assert _notes(paths) == []                              # the card says it: no note about a night
    status = _status(paths, db, job)
    assert status.endswith("; press Retry in the Queue")
    for text in (card["message"], hint, status):
        assert not any(w in text for w in NIGHT_WORDS), text


def test_a_dashboard_request_killed_before_its_row_named_the_job_is_worded_as_started_by_hand(
    tmp_home: Path,
) -> None:
    """Run now or a scheduled request, killed between `make_job` and the row learning the studio job id: the
    studio's recovery offers the job itself, and must not call it the night's."""
    paths, db = _setup()
    job = _dead_job(paths, "manual")
    _dashboard_row(db, "run", None)
    _dashboard_recovers(paths)
    [ours] = [c for c in _cards(db) if c["params"].get("source") == ownership.OFFER_SOURCE]
    assert ours["result"]["studio_job_ids"] == [job]
    assert "while this job ran. Press Retry to continue it." in ours["message"]
    [note] = _notes(paths)
    assert note.startswith("A job you started yourself was interrupted")
    for text in (ours["message"], ours["result"]["error"]["hint"], note, _status(paths, db, job)):
        assert not any(w in text for w in NIGHT_WORDS), text


def test_a_series_part_names_its_command_and_no_night(tmp_home: Path) -> None:
    """A series part is known only through `hold` (nothing announced it): no Retry card of ours stands for
    it, so no text says "press Retry" and none calls it the night's run."""
    paths, db = _setup()
    job = _dead_job(paths, "manual")
    (paths.job_dir(job) / "script_source.json").write_text("{}", encoding="utf-8")
    ownership.write_record(paths, job, announced_at=None)
    before = _status(paths, db, job)
    assert before.endswith(f"; continue it with studio run --resume {job}")
    ownership.recover(paths, db)
    assert _cards(db) == []
    [note] = _notes(paths)
    assert note == (
        "A job you started yourself was interrupted (power cut, crash or restart). after-dark stopped at "
        f"script_import. To continue it, run studio run --resume {job}."
    )
    after = _status(paths, db, job)
    assert after.endswith(f"; continue it with studio run --resume {job}")
    for text in (note, before, after):
        assert "Retry" not in text and not any(w in text for w in NIGHT_WORDS), text


def test_a_clip_has_the_dashboards_card_only(tmp_home: Path) -> None:
    """A clip job takes no owner record (the Clipper runs through `run_job`, not `run_job_guarded`), so the
    studio's recovery, the morning note and `studio status` never speak of it; its card is the dashboard's."""
    paths, db = _setup()
    row = _dashboard_row(db, "clip", None)
    _dashboard_recovers(paths)
    [card] = _cards(db)
    assert card["job_id"] == row and card["message"] == "Interrupted by a restart"
    assert not any(w in card["result"]["error"]["hint"] for w in NIGHT_WORDS)
    assert _notes(paths) == [] and ownership.status_lines(paths, db, CH) == []
    bridge = (Path(__file__).resolve().parents[1] / "ns_dashboard" / "studio_bridge.py").read_text("utf-8")
    assert bridge.count("run_job(watched(ctx, clip_stage_names(), 0, 1, report))") == 2


def test_a_night_and_a_job_started_by_hand_are_told_apart_in_one_note(tmp_home: Path) -> None:
    paths, db = _setup()
    night = ownership.Interrupted("j-night", CH, "master_first", "voice", "daily", "daily-x", "gone", True)
    night.card_id = "c1"
    hand = ownership.Interrupted("j-hand", "other", "shorts_only", "policy_text", "manual", None, "gone", False)
    two = ownership.Interrupted("j-two", "third", "shorts_only", None, "manual", None, "gone", False)
    note = ownership.recovery_message(ownership.Recovery(jobs=[night, hand]))
    assert note is not None
    first, second = note.split(" A job you started yourself was interrupted (power cut, crash or restart). ")
    assert first.startswith("The night's run was interrupted (power cut, crash or restart). after-dark stopped at")
    assert "It continues at the next nightly run" in first
    assert second == "other stopped at policy_text. To continue it, run studio run --resume j-hand."
    # a run of the night that had made nothing, and two jobs started by hand
    runs = [{"run_id": "daily-x", "owner": "gone", "interrupted_jobs": []}]
    note = ownership.recovery_message(ownership.Recovery(runs=runs, jobs=[hand, two]))
    assert note is not None and note.startswith(
        "The night's run was interrupted (power cut, crash or restart). It had not made a video yet, so nothing "
        "is lost. Jobs you started yourself were interrupted (power cut, crash or restart). other stopped at"
    )
    assert "third stopped. To continue it" in note
    assert ownership.recovery_message(ownership.Recovery()) is None


@pytest.mark.parametrize(("origin", "run_id", "expected"), [
    ("daily", "daily-x", "daily"), ("manual", None, "manual"),
    (None, "daily-x", "daily"),            # a card written before the key existed: a run id means the nightly
    (None, None, "manual"), ("", None, "manual"),
])
def test_the_origin_of_a_card_written_before_the_key_existed(
    origin: str | None, run_id: str | None, expected: str
) -> None:
    assert ownership._origin_of({"origin": origin, "run_id": run_id}) == expected


def test_a_resume_by_hand_of_a_nightly_job_does_not_credit_the_nightly_run() -> None:
    by_night = ownership._card_text("voice", "resumed", None, "gone")
    by_hand = ownership._card_text("voice", "resumed", None, "gone", "daily", "hand")
    assert by_night.endswith("during the night's run. The nightly run continued it and it finished.")
    assert by_hand.endswith("during the night's run. It was continued with studio run --resume and it finished.")
    # a job started by hand is never promised a nightly run, whatever state the card is given
    for resume, reason in (("pending", None), ("needs_retry", ownership.PAUSED_REASON), ("needs_retry", None)):
        text = ownership._card_text("voice", resume, reason, "gone", "manual")
        assert not any(w in text for w in NIGHT_WORDS) and "Press Retry to continue it" in text, text


def test_every_cause_sentence_keeps_the_nightly_condition(tmp_home: Path) -> None:
    """Card night-fits-b test 22: each sentence `nightcause` can give a dead night, and the sleep's, in the
    morning note and on the Queue card of a job the next night continues, keeps "while Automatic runs are on"."""
    import importlib
    from types import SimpleNamespace

    try:
        nightcause = importlib.import_module("ns_studio.nightcause")
    except ImportError:
        nightcause = None
    assert nightcause is not None, "no cause of a dead night (ns_studio.nightcause)"
    paths = Paths.from_env()
    last = datetime(2026, 11, 12, 3, 0, tzinfo=UTC)
    task = last - timedelta(hours=6)
    lines = [{"kind": "night_start", "run_id": "r", "at": task.isoformat(), "task_start": task.isoformat(),
              "limit": 21600},
             {"kind": "cut_expected", "run_id": "r", "at": (last - timedelta(seconds=100)).isoformat()}]
    beat = {"run_id": "r", "at": last.isoformat()}

    def event(i: int, at: datetime, **kw: Any) -> Any:
        base = {"provider": "", "process": None, "reason": None, "action": None, "stopped_at": None}
        return SimpleNamespace(id=i, at=at, **{**base, **kw})

    class Log:
        def __init__(self, *events: Any) -> None:
            self.events = events

        def read(self, since: datetime, until: datetime) -> list[Any]:
            return [e for e in self.events if since <= e.at <= until]

    soon, boot = last + timedelta(seconds=20), last + timedelta(minutes=5)
    causes = [
        nightcause.cause(paths, "r", Log(), last - timedelta(days=1), lines=lines, beat=beat),
        nightcause.cause(paths, "r", Log(event(1074, soon, process="TrustedInstaller.exe")), boot, lines=lines,
                         beat=beat),
        nightcause.cause(paths, "r", Log(event(6008, boot, stopped_at=soon)), boot, lines=lines, beat=beat),
        nightcause.cause(paths, "r", Log(event(1074, soon, process="explorer.exe", action="restart")), boot,
                         lines=lines, beat=beat),
        nightcause.cause(paths, "r", Log(), boot, lines=lines[:1], beat=beat),
    ]
    assert [c.kind for c in causes] == ["time_limit", "update_restart", "power_loss", "shutdown", "unknown"]
    for c in causes:
        job = ownership.Interrupted("j1", CH, "master_first", "voice", "daily", "r", "gone", True, cause=c)
        job.card_id = "card"
        note = ownership.recovery_message(ownership.Recovery(jobs=[job])) or ""
        assert note.startswith(f"{c.said}, while {CH}'s video was at voice. ") and "power cut" not in note, note
        assert "while Automatic runs are on in Settings" in note, note
        card = ownership._card_text("voice", "pending", None, "gone", cause_said=c.said)
        assert ownership.WHILE_NIGHTLY_ON in card and "the PC or Nightshift stopped" not in card, card
    slept = ownership._card_text("voice", "pending", None, ownership.SLEPT_OWNER)
    assert "went to sleep" in slept and ownership.WHILE_NIGHTLY_ON in slept
    job = ownership.Interrupted("j1", CH, "master_first", "voice", "daily", "r", "gone", True)
    job.card_id = "card"
    from ns_studio import daily

    gap = SimpleNamespace(frm=last, to=last + timedelta(hours=2), slept_s=7200.0)
    words = daily.slept_words(gap, CH, "voice")  # type: ignore[arg-type]
    note = ownership.recovery_message(ownership.Recovery(jobs=[job]), outcomes={"j1": ("slept", words)}) or ""
    assert "while Automatic runs are on in Settings" in note and "went to sleep" in note
