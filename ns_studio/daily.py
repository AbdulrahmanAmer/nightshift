"""`studio run --daily`: cadence-planned nightly run with a DB backup at the end (component C7)."""

from __future__ import annotations

import json
import os
import random
import sqlite3
import traceback
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, ExitStack
from datetime import UTC, date, datetime, timedelta, tzinfo
from functools import partial
from pathlib import Path
from typing import Any

from ns_common import dbrestore
from ns_common.atomic import read_text_retry, write_text_atomic
from ns_common.db import BackupFailed, claim_job_id
from ns_common.dbrestore import BROKE_MID_RUN, WAY_OUT_WINDOW, is_damage_error
from ns_common.dbrestore import DAMAGED_CODE as DB_DAMAGED
from ns_common.errors import NsError, describe
from ns_common.keepawake import keep_awake
from ns_common.models import Channel
from ns_studio import jobs, nightcause, nightclock, nightfit, notifications, ownership
from ns_studio.acquirer import StudioAcquirer
from ns_studio.channels import cadence
from ns_studio.channels.crud import broken_channels, get_channel
from ns_studio.context import JobContext
from ns_studio.diskroom import require_story_room
from ns_studio.errors import OUT_OF_TIME, PAUSED, UNEXPECTED, StudioError, describe_unexpected
from ns_studio.footage_keys import precheck
from ns_studio.media.outbox import retime_sheet
from ns_studio.media.sheet_read import read_sheet_file
from ns_studio.policy.regen import run_job_guarded
from ns_studio.stages import StageState

RunFn = Callable[[JobContext], list[str]]


def _default_run(ctx: JobContext) -> list[str]:
    return run_job_guarded(ctx)


def _series_delay(env: jobs.Env, job_id: str) -> int:
    """Days a series part waits after the nightly slot (`publish_not_before_days` of its design)."""
    f = env.paths.job_dir(job_id) / "series_context.json"
    try:
        return max(0, int(json.loads(read_text_retry(f)).get("publish_not_before_days", 0) or 0))
    except (OSError, ValueError, AttributeError):
        return 0


SCHEDULE_LOOKBACK_DAYS = 14


def scheduled_shorts(
    env: jobs.Env, slug: str, today: date, skip_jobs: set[str], now: datetime
) -> dict[date, int]:
    """Shorts of `slug` that earlier nights already gave a publish day on or after `today`, by day.

    Read from the `publish_after` of the sheet beside each pending or approved Short in the Outbox (uploaded
    and rejected ones no longer occupy a slot). `skip_jobs` are tonight's jobs, which are being slotted now.
    """
    since = (now - timedelta(days=SCHEDULE_LOOKBACK_DAYS)).isoformat()
    rows = env.db.query(
        "select job_id, path from outputs where channel=? and kind='short'"
        " and status in ('pending', 'approved') and created_at >= ? and profile is null",
        (slug, since),
    )
    taken: dict[date, int] = {}
    for r in rows:
        if r["job_id"] in skip_jobs:
            continue
        sheet = read_sheet_file(Path(r["path"]).with_suffix(".json"), "json")
        try:
            day = datetime.fromisoformat(str(sheet.value.publish_after)).date() if sheet.usable else None
        except ValueError:
            day = None
        if day is None:
            continue
        if day >= today:
            taken[day] = taken.get(day, 0) + 1
    return taken


def apply_publish_times(
    env: jobs.Env, job_ids: list[str], rng: random.Random, now: datetime, tz: tzinfo | None = None
) -> int:
    """Rewrite `publish_after` in the outbox sheets of `job_ids` with jittered slots (spec 18.6).

    Slots are local wall-clock times, so the calendar starts at the *local* date of `now` (`tz`, default the
    PC's own zone); the UTC date is up to a day off at night. Shorts that earlier nights scheduled count
    against `shorts_per_day_max` (`scheduled_shorts`).

    Each finished job lists its files in `<job dir>/outbox/manifest.json` (the Outbox itself is organised by
    story, with no job id in the path). Both the `.json` and the `.txt` sheet of every item are rewritten.
    Returns how many items were rewritten.
    """
    rewritten = 0
    by_channel: dict[str, list[tuple[cadence.ReleaseItem, dict[str, Any]]]] = {}
    for job_id in job_ids:
        manifest_file = env.paths.job_dir(job_id) / "outbox" / "manifest.json"
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for item, entry in manifest.items():
            sheet = read_sheet_file(Path(entry["json"]), "json") if "json" in entry else None
            if sheet is None or not sheet.usable:  # the start sweep and the Queue mend a sheet not whole
                continue
            data = sheet.value.model_dump(mode="json")
            if data.get("profile"):  # a test output (tiny_story) is never given a publish slot
                refusal = describe("NS-E329").message
                env.log.warn("publish_slot_refused", refusal, code="NS-E329", job_id=job_id, item=item)
                continue
            kind = "long" if data.get("kind") == "long" else "short"
            release = cadence.ReleaseItem(f"{job_id}:{item}", job_id, kind)
            delay = _series_delay(env, job_id)
            slot = {**entry, "delay": delay}
            by_channel.setdefault(str(data.get("channel", "")), []).append((release, slot))
    for slug, entries in by_channel.items():
        try:
            ch = get_channel(env.paths, slug)
        except StudioError:
            continue
        today = now.astimezone(tz).date()
        times = cadence.assign_publish_times(
            [e[0] for e in entries], today, ch.publish_window, ch.shorts_per_day_max, rng,
            taken=scheduled_shorts(env, slug, today, set(job_ids), now),
        )
        for release, entry in entries:
            when = times[release.output_id] + timedelta(days=int(entry.get("delay", 0)))
            if retime_sheet(entry, when.isoformat(timespec="minutes")) is not None:
                rewritten += 1
    return rewritten


def no_room_entry(channel: str, fmt: str, jobs_left: int, e: NsError) -> dict[str, Any]:
    """The report line for jobs the nightly did not start for lack of disk space (``NS-E104``, D-33)."""
    return {
        "channel": channel, "format": fmt, "jobs": jobs_left, "message": e.detail,
        "short": e.data.get("short", e.detail), "need_bytes": e.data.get("need_bytes"),
        "free_bytes": e.data.get("free_bytes"),
    }


def _no_room(env: jobs.Env, report: dict, ch: Channel, fmt: str, jobs_left: int) -> bool:
    """D-33: True when the drive cannot hold one more job of ``fmt``; the report then lists the jobs left.

    Nothing is created, so nothing is half made. Any other trouble reading the answer is not a reason to stop
    the night (``diskneed`` already treats an unreadable volume as room enough).
    """
    try:
        require_story_room(env.paths, env.log, ch, fmt, fake=env.fake)
    except NsError as e:
        if e.code != "NS-E104":
            env.log.warn("room_check_failed", str(e))
            return False
        report.setdefault("no_room", []).append(no_room_entry(ch.slug, fmt, jobs_left, e))
        return True
    return False


def _no_room_to_resume(env: jobs.Env, report: dict, job_id: str, slug: str) -> bool:
    """D-33: True when the drive cannot hold what an interrupted job still has to write.

    The job stays interrupted (the next night tries again, the morning note says so) and the report lists it
    under ``no_room``. A job that cannot even be reopened is left to ``_attempt`` to report as before.
    """
    fmt = ""
    job_dir = env.paths.job_dir(job_id)
    try:
        # read job.json here: `jobs.load_job` is what `_attempt` calls, once, to reopen the job
        info = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        fmt = str(info["fmt"])
        channel = get_channel(env.paths, str(info["channel"]))
        require_story_room(env.paths, env.log, channel, fmt, fake=env.fake, resume_dir=job_dir)
    except NsError as e:
        if e.code != "NS-E104":
            return False
        report.setdefault("no_room", []).append(no_room_entry(slug, f"{fmt} (resumed)", 1, e))
        return True
    except Exception:  # noqa: BLE001 - `_attempt` meets the same trouble and reports it
        return False
    return False


def _claim_run_id(env: jobs.Env, run_id: str) -> None:
    """Refuse a run id that already names a night (`claim_job_id` then draws another one).

    A night's id names its row in `runs` and its owner file. A second night on the same day (after a refusal,
    a retry, a test) drew from the same 65536 ids, and an id drawn twice made the new night fail on its
    `runs` row. This looks, it does not take: a run's lock is keyed by its id, so two nights that start in
    the same moment and draw the same id are not kept apart here. The `runs` row then refuses the second one
    with an IntegrityError: loud, and no job has been made yet, where the old id clash was the same error on
    every unlucky second night of a day."""
    # `read_record` answers None for an owners folder it cannot read; `hold_run` then refuses the night
    # (NS-E327), so nothing is caught here
    if env.db.one("select 1 from runs where run_id=?", (run_id,)) or ownership.read_record(env.paths, run_id):
        raise sqlite3.IntegrityError(f"run id {run_id} is taken")


def run_daily(
    env: jobs.Env,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
    run_fn: RunFn | None = None,
    notifier: notifications.Notifier | None = None,
    awake: Callable[[], AbstractContextManager[None]] = keep_awake,
    clock: nightclock.Clock | None = None,
    environ: Mapping[str, str] | None = None,
    process: Any = None,
) -> dict:
    """The nightly run (see `_run_daily`), with the PC held awake for as long as it lasts.

    The 03:00 task wakes the PC, but with nobody at the keyboard Windows puts it back to sleep after a couple
    of minutes, GPU load or not. `awake()` (`ES_CONTINUOUS | ES_SYSTEM_REQUIRED`, per thread) is held on this
    thread from the first plan to the report and released in `finally`, also when the night breaks.

    `clock` is the night's time (`nightclock.SystemClock` by default; every timing decision reads it),
    `environ` where the launch script's `NS_NIGHT_STARTED` is read (this process's environment by default)
    and `process` the process whose parents name the task (`psutil.Process()` by default).
    """
    with awake():
        return _run_daily(
            env, now=now, rng=rng, run_fn=run_fn, notifier=notifier, clock=clock, environ=environ,
            process=process,
        )


def _open_night(
    env: jobs.Env, run_id: str, clock: nightclock.Clock, environ: Mapping[str, str], process: Any
) -> nightclock.Night:
    """The night's clock (S0, S1). One that cannot be worked out runs with no deadline, and says so: a
    night is never refused for time on a guess."""
    try:
        return nightclock.open_night(env.paths, run_id, clock, environ, process=process, log=env.log)
    except Exception as e:  # noqa: BLE001 - never stops the night
        env.log.warn("night_clock_failed", str(e))
        start = nightclock.TaskStart(clock.now(), nightclock.START_PROCESS, (f"no deadline: {e}",))
        return nightclock.Night(env.paths, run_id, clock, start, None, env.log)


def _close_night(env: jobs.Env, night: nightclock.Night, report: dict) -> dict[str, Any]:
    """S9: the `night_end` line and the report's `timing`, also for a night that broke."""
    try:
        return night.close(
            jobs=len(report.get("jobs") or []), out_of_time=len(report.get("out_of_time") or []),
            failed=report.get("failed"),
        )
    except Exception as e:  # noqa: BLE001 - the report is written whatever the ledger does
        env.log.warn("night_close_failed", str(e))
        return {}


def _run_daily(
    env: jobs.Env,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
    run_fn: RunFn | None = None,
    notifier: notifications.Notifier | None = None,
    clock: nightclock.Clock | None = None,
    environ: Mapping[str, str] | None = None,
    process: Any = None,
) -> dict:
    """Plan with the governor, run every due job, back up the database and write the nightly report.

    First it closes what a dead process left open (a night killed by a power cut or a crash: its run row, its
    half-made job, a morning note) and continues that job from the stage it reached instead of starting from
    ideas. One failing job never stops the others; failures are listed in the report. When a `notifier` is
    given the night ends with a toast (spec J10: videos ready, or the failure code, and a word about an
    interrupted night), unless Settings turned notifications off. Returns the report dict.

    A night the task started never begins a job or a stage that would end after its deadline
    (`nightclock`); what it left is in the report's `out_of_time`, its time in `timing`, and the owner reads
    both in a morning note (NS-W211). Every second of it is in the ledger (`ledger.py`).
    """
    clock = clock or nightclock.SystemClock()
    now = now or clock.now()
    rng = rng or random.Random()
    run = run_fn or _default_run
    run_id = claim_job_id("daily", partial(_claim_run_id, env), now)
    report: dict = {"run_id": run_id, "started_at": now.isoformat(timespec="seconds"), "channels": [],
                    "jobs": [], "failures": [], "interrupted_jobs": [], "not_due": {}}
    unreadable = broken_channels(env.paths)  # the plan below skips them; the report and the CLI name them
    if unreadable:
        report["unreadable_profiles"] = [list(pair) for pair in unreadable]
    recovered = _close_dead_work(env, now, report)
    # this row has a live owner until it is closed; a night that cannot be owned does not start, and says so
    owned = ExitStack()
    try:
        owned.enter_context(ownership.hold_run(env.paths, run_id, origin="daily"))
    except (ownership.JobBusy, ownership.LockUnavailable) as e:
        _refuse_night(env, run_id, now, report, e, recovered)
        raise
    with owned:
        env.db.execute(
            "insert into runs(run_id, started_at, mode) values (?,?,?)",
            (run_id, now.isoformat(timespec="seconds"), "daily"),
        )
        night = _open_night(env, run_id, clock, os.environ if environ is None else environ, process)
        acquirer = env.services.acquirer
        if isinstance(acquirer, StudioAcquirer):  # vram-busy: a run nobody watches waits for the memory
            acquirer.vram_wait = True
        try:
            _tonight(env, run, rng, now, run_id, report, recovered, night)
            done = [j["job_id"] for j in report["jobs"]]
            report["publish_times_set"] = apply_publish_times(env, done, rng, now)
            try:
                report["backup"] = str(env.db.backup(env.paths.backup_dir, today=now.date()))
                dbrestore.backup_made(env.paths)
            except BackupFailed as e:  # 4b.3: no copy tonight; the older copies all stay. Counted and SAID
                # (NS-W214 with the age of the newest copy); a copy whose check answered damage makes the
                # next start check the live file in full.
                report["backup"], report["backup_failed"] = None, str(e)
                report["backup_note"] = dbrestore.backup_failed(
                    env.paths, str(e), env.log, notifications.db_note(env.paths), damaged=e.damaged)
        except BaseException as e:  # the night itself broke: close its row, so it never looks unfinished
            damaged = _damage_of(e)
            report["failed"] = DB_DAMAGED if damaged is not None else getattr(e, "code", UNEXPECTED)
            report["timing"] = _close_night(env, night, report)
            if damaged is not None:
                _stop_for_damage(env, report, now, damaged)
            try:
                env.db.execute(
                    "update runs set finished_at=?, summary_json=? where run_id=?",
                    (clock.now().isoformat(timespec="seconds"), json.dumps(report), run_id),
                )
            except Exception as closing:  # noqa: BLE001 - a damaged database cannot take the row: said above
                if damaged is None:
                    raise
                env.log.warn("daily_row_not_closed", str(closing), run_id=run_id)
            if damaged is not None and damaged is not e:
                raise damaged from e
            raise
        finally:
            if isinstance(acquirer, StudioAcquirer):
                acquirer.vram_wait = False
        report["timing"] = _close_night(env, night, report)
        finished = clock.now()
        report["finished_at"] = finished.isoformat(timespec="seconds")
        env.db.execute(
            "update runs set finished_at=?, summary_json=? where run_id=?",
            (report["finished_at"], json.dumps(report), run_id),
        )
    env.paths.logs.mkdir(parents=True, exist_ok=True)
    nightly = env.paths.logs / f"nightly-{now:%Y%m%d}.json"
    write_text_atomic(nightly, json.dumps(report, indent=2) + "\n")
    env.log.info("daily_done", run_id=run_id, jobs=len(done), failures=len(report["failures"]))
    masters, shorts = notifications.output_counts(env.db, done)
    _leave_time_note(env, report, clock, now)
    _leave_sleep_note(env, report, clock, now)
    _leave_vram_note(env, report, clock, now)
    notifications.send(env.paths, env.log, notifier, notifications.nightly_message(report, masters, shorts))
    finish = getattr(env.services.acquirer, "run_finished", None)
    if finish is not None:  # gap 25: the engine this run needed leaves once nobody else uses it
        finish()
    return report


def _damage_of(e: BaseException) -> NsError | None:
    """db-integrity 4b.2: the NS-E209 that ends the night when `e` is the database itself breaking (SQLite's
    "malformed", "not a database"), else None. One job's error that is not about the database stays that
    job's failure."""
    if isinstance(e, NsError) and e.code == DB_DAMAGED:
        return e
    if not is_damage_error(e):
        return None
    return NsError(DB_DAMAGED, f"{type(e).__name__}: {e}", hint=WAY_OUT_WINDOW, said=BROKE_MID_RUN)


def _stop_for_damage(env: jobs.Env, report: dict, now: datetime, damaged: NsError) -> None:
    """The night stops on a damaged database (4b.2): the report says so (the run's row may not take it, so the
    night's own file `logs/nightly-<date>.json` does), and the owner's morning note says what to do. The other
    jobs are not tried: every one of them would fail the same way."""
    raw = damaged.detail or str(damaged)
    job_id = str(damaged.data.get("job_id") or "(the night)")
    report["failures"].append({"job_id": job_id, "channel": "The night", "code": DB_DAMAGED,
                               "error": BROKE_MID_RUN, "detail": raw})
    env.log.error("daily_db_damaged", raw, code=DB_DAMAGED)
    # The quick check of the next start may pass (an index entry without its row): this mark makes that start
    # run the full check and decide on it, so the note's promise below holds or the owner reads why not.
    dbrestore.want_full_check(env.paths, f"the night stopped: {raw}", env.log)
    try:
        env.paths.logs.mkdir(parents=True, exist_ok=True)
        write_text_atomic(env.paths.logs / f"nightly-{now:%Y%m%d}.json", json.dumps(report, indent=2) + "\n")
    except OSError as e:
        env.log.warn("nightly_report_not_written", str(e))
    try:
        notifications.db_note(env.paths)(f"{DB_DAMAGED}: {BROKE_MID_RUN}")
    except Exception as e:  # noqa: BLE001 - the toast and the log still say it
        env.log.warn("morning_note_failed", str(e))


def _refusal_words(error: StudioError) -> tuple[str, str]:
    """`(morning note, short reason)` for a night that could not be owned, in plain words naming the cause."""
    if isinstance(error, ownership.JobBusy):
        if error.owner:
            reason = f"another Nightshift run is already running with the same name (process {error.owner})"
        else:  # the lock file names no process: do not claim one
            reason = (
                "another process is holding the run's lock file (a second Nightshift run, or a program "
                "scanning the file)"
            )
        note = (
            f"The night's run did not start: {reason}. If that is another run it carries on; this one made "
            "nothing and nothing was lost. If no other run should be going, close any old Nightshift window "
            "and run it again."
        )
        return note, reason
    cause = getattr(error, "cause", None)
    why = (getattr(cause, "strerror", None) or str(cause)) if cause is not None else str(error)
    reason = f"the file that marks a run as alive cannot be made ({getattr(error, 'path', None)}: {why})"
    note = (
        f"The night's run did not start: {reason}. Nothing was made and nothing was lost. Check that the "
        "folder is not read-only, full or blocked by antivirus; the next nightly run tries again "
        "(while Automatic runs are on in Settings), or start a run yourself."
    )
    return note, reason


def _refuse_night(
    env: jobs.Env,
    run_id: str,
    now: datetime,
    report: dict,
    error: StudioError,
    recovered: ownership.Recovery | None,
) -> None:
    """The night cannot be owned (another live process holds its lock, or no lock file can be made after the
    tries): it does not start, and it is never silent about it.

    Refusing is deliberate: a night run with no owner would be closed as dead by the next start and continued
    next to itself. What is not allowed is losing it unseen: this writes the closed `runs` row the way a night
    that broke leaves it (`failed` and the reason, which `/api/status` shows), logs it, and leaves the morning
    note; the CLI then sends its failure toast and exits 1. Each of the three is its own try: a disk that
    cannot be written (the very cause, maybe) must not hide the others or replace the error the caller sees.
    """
    note, reason = _refusal_words(error)
    report["failed"], report["reason"] = error.code, reason
    # The Queue's "Last night" card shows a stop only from `failures` (a red dot and "<channel>: <code>.
    # <error>"). The morning note can be cleared ("Mark all read") or never left (notifications off), so one
    # entry here keeps the cause on screen. `channel` is shown as written: "The night: NS-E327. <reason>".
    report["failures"].append(
        {"job_id": NIGHT_NOT_STARTED, "channel": "The night", "code": error.code, "error": reason}
    )
    env.log.error("daily_not_started", run_id=run_id, code=error.code, detail=str(error), reason=reason)
    try:
        env.db.execute(
            "insert into runs(run_id, started_at, finished_at, mode, summary_json) values (?,?,?,?,?)",
            (
                run_id,
                now.isoformat(timespec="seconds"),
                datetime.now(UTC).isoformat(timespec="seconds"),
                "daily",
                json.dumps(report),
            ),
        )
    except Exception as e:  # noqa: BLE001 - a database that cannot be written must not hide the real cause
        env.log.warn("daily_not_started_row_failed", str(e), run_id=run_id)
    if recovered is not None and recovered.any:
        # the jobs `_close_dead_work` closed for tonight's resume are told by `_tonight` once it has tried;
        # this night never gets there, so they are told here as waiting for the next one
        ownership.leave_recovery_note(env.paths, env.log, recovered)
    ownership.leave_note(env.paths, env.log, note)


def _close_dead_work(env: jobs.Env, now: datetime, report: dict) -> ownership.Recovery | None:
    """Close the runs and jobs whose process is gone (before this night plans).

    A job it cannot continue itself is listed in the report as interrupted (the toast says Retry) and told in
    a morning note at once. The note for a job it will continue is left by `_tonight` once it has tried, so
    that it says what became of the job; this returns that part (None when there is none). If the night is
    killed again before that, the next start closes the job again and tells it then. Never stops the night: a
    failure here is logged and the night plans as if nothing was found.
    """
    try:
        out = ownership.recover(env.paths, env.db, now=now, log=env.log, note=False)
    except Exception as e:  # noqa: BLE001 - recovery is a courtesy; the night must still run
        env.log.warn("recover_failed", str(e))
        return None
    for j in out.jobs:
        if not j.resumable and not j.dashboard_owned:
            report["interrupted_jobs"].append(
                ownership.interrupted_entry(j.job_id, j.channel, j.stage, "open", has_card=j.has_card)
            )
    later = ownership.Recovery(jobs=[j for j in out.jobs if j.resumable])
    now_part = ownership.Recovery(
        runs=[] if later.jobs else out.runs, jobs=[j for j in out.jobs if not j.resumable]
    )
    if now_part.any:
        ownership.leave_recovery_note(env.paths, env.log, now_part)
    return later if later.any else None


def _attempt(
    env: jobs.Env,
    run: RunFn,
    report: dict,
    make: Callable[[], JobContext],
    slug: str,
    *,
    run_id: str,
    fmt: str,
    resume_dir: Path | None = None,
    resumed: bool = False,
    night: nightclock.Night | None = None,
) -> str | None:
    """Make (or reopen) one job and run it, owned for as long as it runs. Returns the error code on failure
    (`BUSY` when another process is already running that job: not a failure, nothing is reported).

    A failure (coded or not) ends this job and is listed in the report, never the night. A job the night
    stopped before a stage for lack of time (`OUT_OF_TIME`) is not a failure either: `_left_for_time`.
    `fmt` is the job's format; `resume_dir` the folder of a job being continued (the check before it asks
    whether that job still fetches stock footage).
    """
    job_id = "(not created)" if resume_dir is None else resume_dir.name
    raw = ""  # an NsError's own detail (NS-E208: the engine's words naming the programs)
    try:
        # A job that will still fetch stock footage, with no key or every stored key refused by its site,
        # would fail at `clips` after an hour of GPU work (gap L3, card stock-truth): `footage_keys.precheck`
        # asks the one question (`needs_stock`), then each site once, and keeps the answer.
        precheck(env.services, env.paths, env.log, fmt=fmt, job_dir=resume_dir)
        ctx = make()
        job_id = ctx.job_id
        if night is not None:  # the night's clock, deadline and run id for every stage and wait of the job
            ctx.clock, ctx.deadline, ctx.run_id = night.clock, night, night.run_id
        ownership.note_run_job(env.paths, run_id, job_id, slug)
        with ownership.hold_job(env.paths, job_id, run_id=run_id, channel=slug, fmt=str(ctx.fmt)):
            ran = run(ctx)
        entry: dict[str, Any] = {"job_id": job_id, "channel": slug, "stages": len(ran)}
        if resumed:
            entry["resumed"] = True
        report["jobs"].append(entry)
        return None
    except ownership.JobBusy as e:
        # A Retry from the Queue is running this very job: not a failure of the night, and the job's card, the
        # report and the toast stay as they are. The Retry finishes it (or fails it) on its own.
        env.log.info("daily_job_busy", job_id=job_id, detail=str(e))
        return BUSY
    except (NsError, StudioError) as e:
        damaged = _damage_of(e)
        if damaged is not None:  # 4b.2: the database broke under this job: the night ends, not this job
            damaged.data.setdefault("job_id", job_id)
            if damaged is e:
                raise
            raise damaged from e
        code, detail, raw = e.code, str(e), str(getattr(e, "detail", "") or "")
        if code == PAUSED and job_id != "(not created)":  # stopped between two stages by a pause (P47)
            _paused_mid_job(env, report, job_id, slug, resumed=resumed)
            return PAUSED
        decision = _decision_of(e)
        if decision is not None and night is not None and job_id != "(not created)":
            _left_for_time(env, report, night, job_id, slug, decision, resumed=resumed)
            return OUT_OF_TIME
    except Exception as e:  # an uncoded bug or corrupt file ends this job, never the night
        damaged = _damage_of(e)
        if damaged is not None:  # 4b.2: ... unless the file is the database: then every job would fail
            damaged.data.setdefault("job_id", job_id)
            if damaged is e:
                raise
            raise damaged from e
        code, detail = UNEXPECTED, f"{UNEXPECTED}: {describe_unexpected(e)}"
        env.log.error("daily_job_crashed", job_id=job_id, traceback=traceback.format_exc())
    if night is not None and job_id != "(not created)":
        gap = night.slept_through(job_id)
        # 4b.4: it stopped because the PC slept: interrupted and continued, not failed (unless it cannot be
        # closed as interrupted: then it is the failure it was, with Retry)
        if gap is not None and _slept_mid_job(env, report, night, job_id, slug, gap, code, resumed=resumed):
            return SLEPT
    env.log.error("daily_job_failed", job_id=job_id, code=code, detail=detail)
    failure = {"job_id": job_id, "channel": slug, "code": code, "error": detail}
    if code == VRAM:  # the engine's own words (programs, numbers, the wait), for the note and the card data
        failure["said"] = raw
    report["failures"].append(failure)
    return code


def _paused_mid_job(env: jobs.Env, report: dict, job_id: str, slug: str, *, resumed: bool) -> None:
    """The channel was paused while its job ran: the studio stopped it before its next stage (NS-E330), its
    finished stages kept. It is not a failure: the job is closed as interrupted, the way a killed one is (a
    Queue card with Retry, which is refused while the channel is paused), so the first night after the resume
    continues it from that stage (`_resume_interrupted`, within `RESUME_WITHIN_DAYS`); the report lists it
    under `paused` with its `job_id`. A job the night was already continuing keeps its card, rewritten."""
    if resumed:
        ownership.write_record(env.paths, job_id, state="interrupted")
        # whatever stopped it the first time, its card now says that it waits for the resume and that the
        # first nightly run after it continues the job (`ownership._card_text` goes by this reason)
        ownership.settle_needs_retry(env.db, job_id, ownership.PAUSED_REASON)
    else:
        rec = {**(ownership.read_record(env.paths, job_id) or {}), "id": job_id}
        try:  # the same closing as for a killed job: the card first, then the record (`ownership._close_job`)
            ownership._close_job(env.paths, env.db, rec, datetime.now(UTC), ownership.PAUSED_OWNER)
        except Exception as e:  # noqa: BLE001 - the record says failed; Retry and `studio run --resume` remain
            env.log.warn("paused_job_not_closed", str(e), job_id=job_id)
    _hold_for_pause(env, report, None, slug, 1, job_id=job_id)


def slept_words(gap: nightclock.Sleep, channel: str, stage: str | None) -> str:
    """4b.4's sentence (no full stop): when the PC slept and woke (each to within one beat), what it does
    not hold off, and what became of the video."""
    where = f" at {stage}" if stage else ""
    return (
        f"The PC went to sleep at about {nightcause.hhmm(gap.frm)} during the night's run and woke at about "
        f"{nightcause.hhmm(gap.to)} ({ownership.SLEEP_WAYS}). {channel}'s video stopped{where} as it woke, "
        f"and the next nightly run continues it{ownership.IF_NIGHTLY_ON}, or press Retry in the Queue"
    )


def _slept_mid_job(
    env: jobs.Env,
    report: dict,
    night: nightclock.Night,
    job_id: str,
    slug: str,
    gap: nightclock.Sleep,
    code: str,
    *,
    resumed: bool,
) -> bool:
    """4b.4: the job's stage ended in an error because the PC slept (`Night.slept_through`). Not a failure:
    the job is closed as interrupted at the stage it reached (owner `SLEPT_OWNER`, its finished stages kept),
    so the next nightly run continues it like a job left for time; its card says so; the report lists it
    under `slept` with the sentence the morning note shows (`_leave_sleep_note`). False (nothing said) when
    it cannot be closed as interrupted: the caller then reports the failure it was."""
    rec = ownership.read_record(env.paths, job_id) or {}
    fmt = str(rec.get("fmt") or "")
    stage = ownership.reached_stage(env.paths, job_id, fmt)
    now = night.clock.now()
    env.log.warn("daily_job_slept", job_id=job_id, channel=slug, stage=stage, error=code,
                 slept_from=gap.frm.isoformat(timespec="seconds"),
                 slept_to=gap.to.isoformat(timespec="seconds"))
    if resumed:  # its card stands already: it now says why it waits
        ownership.write_record(env.paths, job_id, state="interrupted", owner=ownership.SLEPT_OWNER,
                               interrupted_at_stage=stage, reason=ownership.SLEPT_REASON,
                               closed_at=now.isoformat(timespec="seconds"), cause=ownership.SLEPT_REASON,
                               cause_said=None)
        try:
            ownership.settle_slept(env.db, job_id, stage)
        except Exception as e:  # noqa: BLE001 - the record is right; the card keeps its older words
            env.log.warn("slept_job_card_failed", str(e), job_id=job_id)
    else:
        try:  # the same closing as for a killed, paused or left job: the card first, then the record
            closed = ownership._close_job(env.paths, env.db, {**rec, "id": job_id}, now,
                                          ownership.SLEPT_OWNER)
        except Exception as e:  # noqa: BLE001 - not closed: it stays the failure it was (Retry remains)
            env.log.warn("slept_job_not_closed", str(e), job_id=job_id)
            return False
        if closed is None:  # finished, or nothing of it was written: nothing to continue
            return False
        ownership.write_record(env.paths, job_id, reason=ownership.SLEPT_REASON, cause=ownership.SLEPT_REASON)
    report.setdefault("slept", []).append({
        "job_id": job_id, "channel": slug, "stage": stage, "error": code, "resumed": resumed,
        "from": gap.frm.isoformat(timespec="seconds"), "to": gap.to.isoformat(timespec="seconds"),
        "slept_s": round(gap.slept_s), "said": slept_words(gap, slug, stage),
    })
    return True


def _leave_sleep_note(env: jobs.Env, report: dict, clock: nightclock.Clock, started: datetime) -> None:
    """4b.4: the morning note of the jobs a sleep stopped tonight, once per night, through the notes' one
    owner (`ns_supervisor.notify`); a job the night's recovery note already tells (`in_recovery_note`) is
    left out."""
    told = [e for e in report.get("slept") or [] if not e.get("in_recovery_note")]
    if not told or not notifications.notifications_enabled(env.paths):
        return
    message = " ".join(f"{e['said']}." for e in told)
    since = started.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")

    def said(notes: list[Any]) -> bool:
        return any(n["message"] == message and str(n["at"]) >= since for n in notes)

    try:
        from ns_supervisor import notify

        notify.leave_note(env.paths, notifications.TITLE, message, once=said,
                          now=clock.now().astimezone().replace(tzinfo=None))
    except Exception as e:  # noqa: BLE001 - a note never stops a night; the job's card says it
        env.log.warn("morning_note_failed", str(e))


def _decision_of(e: NsError | StudioError) -> nightfit.Decision | None:
    """The night's time decision that stopped the job (S6 before a stage, S7 before a wait for the graphics
    card: `nightclock.OutOfTime`), or None for any other error."""
    return e.decision if isinstance(e, nightclock.OutOfTime) else None


def _seconds(value: Any) -> int | None:
    return None if value is None else round(float(value))


def _left_entry(
    report: dict,
    night: nightclock.Night,
    d: nightfit.Decision,
    *,
    channel: str,
    plan: str,
    fmt: str,
    jobs: int = 1,
    job_id: str | None = None,
    resumed: bool = False,
) -> dict[str, Any]:
    """The report's `out_of_time` entry of a decision that was not BEGIN: its answer, reason, numbers and the
    ONE sentence of that reason (`nightfit.say`), which the morning note, the toast's count, the Last night
    card and the job's card all show. A new job a stage of which cannot fit a night is told in the morning
    note once per piece of evidence (`told`); the card shows it every night."""
    entry: dict[str, Any] = {
        "channel": channel, "fmt": fmt, "jobs": jobs, "stage": d.stage, "need_s": _seconds(d.need_s),
        "left_s": _seconds(d.left_s), "basis": d.basis, "answer": d.answer, "reason": d.reason,
        "said": nightclock.marked(night.card, nightfit.say(d, channel=channel, plan=plan, jobs=jobs)),
    }
    if job_id is not None:
        entry["job_id"] = job_id
    if resumed:
        entry["resumed"] = True
    if d.reason == nightfit.CANNOT_FIT_NEW:
        key = {"plan": plan, "stage": d.stage, "passes": d.passes, "over": d.over,
               "longest": _seconds(d.longest)}
        told = [x for x in night.lines if x.get("kind") == "too_long_told"
                and all(x.get(k) == v for k, v in key.items())]
        entry["told"] = not told
        if not told:
            night.write("too_long_told", **key)
    report.setdefault("out_of_time", []).append(entry)
    return entry


def _keep(env: jobs.Env, night: nightclock.Night, job_id: str, d: nightfit.Decision) -> None:
    """What the job's record keeps of tonight's decision: the nights it was left (`cuts`), the fresh misses,
    whether this night finished a stage of it (its age restarts only then: `progressed_at`; a stage that
    finishes after `kept_at`, on a night killed later, counts too: `Night.job_facts`) and whether it asks
    for the next night's first place. A decision that gives it up leaves its sentence as the reason the
    nightly no longer continues it (`ownership.resume_problem`) and on its card."""
    fields: dict[str, Any] = {"cuts": d.cuts, "misses": d.misses, "last_progressed": d.refresh_age,
                              "first_next": d.first_next,
                              "kept_at": night.clock.now().isoformat(timespec="seconds")}
    if d.refresh_age:
        fields["progressed_at"] = night.clock.now().isoformat(timespec="seconds")
    ownership.write_record(env.paths, job_id, **fields)


def _give_up(env: jobs.Env, job_id: str, said: str) -> None:
    """CANNOT_FIT for a made job: the nightly no longer continues it; its card says why and offers Retry
    (a run the owner starts has no time limit, and its pass is the newest measurement of that stage)."""
    ownership.write_record(env.paths, job_id, cannot_fit=said, resume_problem=said)
    try:
        ownership.settle_needs_retry(env.db, job_id, said)
    except Exception as e:  # noqa: BLE001 - the record holds it; the card keeps its older words
        env.log.warn("given_up_card_failed", str(e), job_id=job_id)


def _left_for_time(
    env: jobs.Env,
    report: dict,
    night: nightclock.Night,
    job_id: str,
    slug: str,
    d: nightfit.Decision,
    *,
    resumed: bool,
) -> dict[str, Any]:
    """S6/S7: the night's decision before a stage (or the wait for the card ahead of it) was STOP or
    CANNOT_FIT. Not a failure: the job is closed as interrupted (reason `out_of_time`, its finished stages
    kept) at the stage it reached; STOP: the next nightly run continues it; CANNOT_FIT: it is given up
    with its card. The report lists it under `out_of_time` with the decision's sentence."""
    rec = ownership.read_record(env.paths, job_id) or {}
    fmt = str(rec.get("fmt") or "")
    plan = nightclock.plan_of(fmt, env.paths.job_dir(job_id))
    entry = _left_entry(report, night, d, channel=slug, plan=plan, fmt=fmt, job_id=job_id, resumed=resumed)
    env.log.info("daily_job_left_for_time", job_id=job_id, channel=slug, stage=d.stage, reason=d.reason)
    limit = night.limit_s
    now = night.clock.now()
    if resumed:  # its card stands already: it now says why it waits
        ownership.write_record(
            env.paths, job_id, state="interrupted", owner=ownership.OUT_OF_TIME_OWNER,
            interrupted_at_stage=ownership.reached_stage(env.paths, job_id, fmt),
            reason=ownership.OUT_OF_TIME_REASON, limit_s=limit, closed_at=now.isoformat(timespec="seconds"),
        )
        if d.answer == nightfit.STOP:
            try:
                ownership.settle_left_for_time(env.db, job_id, d.stage, limit)
            except Exception as e:  # noqa: BLE001 - the record is right; the card keeps its older words
                env.log.warn("left_job_card_failed", str(e), job_id=job_id)
    else:
        try:  # the same closing as for a killed or paused job: the card first, then the record
            ownership._close_job(env.paths, env.db, {**rec, "id": job_id, "limit_s": limit}, now,
                                 ownership.OUT_OF_TIME_OWNER)
        except Exception as e:  # noqa: BLE001 - the record says failed; Retry and `studio run --resume` remain
            env.log.warn("left_job_not_closed", str(e), job_id=job_id)
            return entry
        ownership.write_record(env.paths, job_id, reason=ownership.OUT_OF_TIME_REASON, limit_s=limit)
    _keep(env, night, job_id, d)
    if d.answer == nightfit.CANNOT_FIT:
        _give_up(env, job_id, entry["said"])
    return entry


def _ask_new(night: nightclock.Night, fmt: str) -> nightfit.Decision:
    """S5: the decision for one new job of `fmt` (its whole plan, gates included: an estimate errs long)."""
    from ns_studio.orchestrator import plan_names

    return night.ask(nightfit.NEW, fmt, plan_names(fmt, True))


def _ask_resume(
    env: jobs.Env, night: nightclock.Night, rec: Mapping[str, Any]
) -> tuple[nightfit.Decision, str]:
    """S3: the decision for a half-made job, from the stages it has not finished, and its plan. A job whose
    stages cannot be read is asked with none (BEGIN): `_attempt` then meets the same trouble and reports
    it."""
    from ns_studio.orchestrator import stage_names

    job_id = str(rec["id"])
    job_dir = env.paths.job_dir(job_id)
    try:
        fmt = str(rec.get("fmt") or ownership.job_info(env.paths, job_id).get("fmt") or "")
        done = {n for n, e in StageState.load(job_dir).entries.items() if e.get("status") == "done"}
        rest = [n for n in stage_names(fmt, job_dir) if n not in done]
        plan = nightclock.plan_of(fmt, job_dir)
    except Exception as e:  # noqa: BLE001
        env.log.warn("resume_estimate_failed", str(e), job_id=job_id)
        return night.ask(nightfit.RESUME, "", [], job_id), ""
    return night.ask(nightfit.RESUME, plan, rest, job_id), plan


def _resume_order(env: jobs.Env, pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The half-made jobs in the order the night takes them up (`nightfit.resume_key`): those that asked for a
    night's first place, then the one no night moved on for longest."""
    def key(rec: dict[str, Any]) -> tuple[bool, str]:
        try:
            anchor = ownership.age_anchor(env.paths, rec)
        except Exception:  # noqa: BLE001 - unknown age: last
            anchor = None
        return nightfit.resume_key(bool(rec.get("first_next")), anchor.isoformat() if anchor else "~")

    return sorted(pending, key=key)


def _left_last_night(env: jobs.Env, run_id: str) -> set[str]:
    """S4: the channels the previous nightly run left work of for time (they go first tonight)."""
    try:
        row = env.db.one(
            "select summary_json from runs where mode='daily' and run_id<>? and finished_at is not null"
            " order by started_at desc limit 1",
            (run_id,),
        )
        summary = json.loads(row["summary_json"] or "{}") if row is not None else {}
        return {str(e.get("channel")) for e in summary.get("out_of_time") or [] if isinstance(e, dict)}
    except Exception as e:  # noqa: BLE001 - the order is a courtesy
        env.log.warn("last_night_unreadable", str(e))
        return set()


def _leave_time_note(env: jobs.Env, report: dict, clock: nightclock.Clock, started: datetime) -> None:
    """S10: the morning note of a night that left work for time, once per night (the same words already
    waiting from this night are not added again), through the notes' one owner (`ns_supervisor.notify`).
    Settings > Notifications off leaves none, as for every morning note of the studio; the Queue's Last
    night card still says it."""
    message = notifications.time_note(report)
    if not message or not notifications.notifications_enabled(env.paths):
        return
    since = started.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")

    def said(notes: list[Any]) -> bool:
        return any(n["message"] == message and str(n["at"]) >= since for n in notes)

    try:
        from ns_supervisor import notify

        notify.leave_note(env.paths, notifications.TITLE, message, once=said,
                          now=clock.now().astimezone().replace(tzinfo=None))
    except Exception as e:  # noqa: BLE001 - a note never stops a night; the report and the card say it
        env.log.warn("morning_note_failed", str(e))


def _leave_vram_note(env: jobs.Env, report: dict, clock: nightclock.Clock, started: datetime) -> None:
    """Card vram-busy, V8: the one morning note of a night whose model could not load because other programs
    held the graphics card's memory (`notifications.vram_note`, the engine's words), once per night, through
    the notes' one owner. Settings > Notifications off leaves none; the Last night card still says it."""
    message = notifications.vram_note(report)
    if not message or not notifications.notifications_enabled(env.paths):
        return
    since = started.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")

    def said(notes: list[Any]) -> bool:
        return any(n["message"] == message and str(n["at"]) >= since for n in notes)

    try:
        from ns_supervisor import notify

        notify.leave_note(env.paths, notifications.TITLE, message, once=said,
                          now=clock.now().astimezone().replace(tzinfo=None))
    except Exception as e:  # noqa: BLE001 - a note never stops a night; the report and the card say it
        env.log.warn("morning_note_failed", str(e))


#: The engine's answers that mean the graphics card is not free for the night now: held by another task
#: (NS-E201), or its memory held by other programs (NS-E208, card vram-busy). The ONE place a night turns
#: them into "this job and the rest of tonight wait for the next nightly run" is `_card_busy` and the two
#: loops that call it; neither code is a time decision (`nightfit` gave the wait its time before it began).
VRAM = ownership.VRAM_CODE
CARD_BUSY = ("NS-E201", VRAM)
#: The `reason` of a `skipped` entry when the card's memory was held (an NS-E201 night's has none).
VRAM_SKIPPED = "vram"


def _card_busy(
    env: jobs.Env, report: dict, night: nightclock.Night, job_id: str, slug: str, stage: str | None,
    *, resumed: bool, has_card: bool,
) -> None:
    """A job met a busy card (`CARD_BUSY`): it waits for the next nightly run, it has not failed. `_attempt`'s
    failure entry is taken out (no red line beside "waiting" on the card, no failure in the toast), the job is
    left interrupted at its stage, and its `interrupted_jobs` entry says `waiting` (with the engine's words
    naming the programs, for NS-E208). A NEW job is closed the way a killed one is (its Queue card first,
    then the record, owner `VRAM_OWNER` for NS-E208, `CARD_BUSY_OWNER` for NS-E201: one behaviour for one
    "card not free" state), so the next night continues it. The first NS-E208 of the night is kept as
    `report["vram"]` for the morning note and the toast."""
    mine = [f for f in report["failures"] if f["job_id"] == job_id and f["code"] in CARD_BUSY]
    vram = next((f for f in mine if f["code"] == VRAM), None)
    if not mine:
        return
    report["failures"] = [f for f in report["failures"] if f not in mine]
    said = str(vram.get("said") or "") if vram is not None else None
    if resumed:
        ownership.write_record(env.paths, job_id, state="interrupted")
    else:
        rec = ownership.read_record(env.paths, job_id) or {}
        try:
            owner = ownership.VRAM_OWNER if vram is not None else ownership.CARD_BUSY_OWNER
            closed = ownership._close_job(env.paths, env.db, {**rec, "id": job_id}, night.clock.now(), owner)
        except Exception as e:  # noqa: BLE001 - the record says failed; Retry and `studio run --resume` remain
            env.log.warn("vram_job_not_closed", str(e), job_id=job_id)
            report["failures"].extend(mine)
            return
        stage = closed.stage if closed is not None else stage
        has_card = closed is not None and closed.card_id is not None
    report["interrupted_jobs"].append(
        ownership.interrupted_entry(job_id, slug, stage, "waiting", has_card=has_card, said=said or None)
    )
    if vram is not None and "vram" not in report:
        at = night.clock.now().astimezone().strftime("%H:%M")
        report["vram"] = {"job_id": job_id, "channel": slug, "at": at, "said": said or ""}


def _skip_rest(report: dict, busy: str, slug: str, jobs_left: int) -> None:
    """The jobs the night does not start after a busy card, under `skipped` (`reason: vram` for NS-E208)."""
    if jobs_left <= 0:
        return
    entry: dict[str, Any] = {"channel": slug, "jobs": jobs_left}
    if busy == VRAM:
        entry["reason"] = VRAM_SKIPPED
    report.setdefault("skipped", []).append(entry)


Outcomes = dict[str, tuple[str, str | None]]
#: What `_attempt` returns for a job another process is running (a Retry from the Queue): not an error code.
BUSY = "busy"
#: What `_attempt` returns for a job whose stage stopped because the PC slept (4b.4): not an error code.
SLEPT = "slept"
#: The `job_id` of the one `failures` entry that a night which could not be owned leaves (`_refuse_night`).
NIGHT_NOT_STARTED = "(night did not start)"


def _resume_interrupted(
    env: jobs.Env,
    run: RunFn,
    now: datetime,
    run_id: str,
    report: dict,
    outcomes: Outcomes,
    night: nightclock.Night,
    left_channels: dict[str, str],
) -> tuple[str | None, set[str]]:
    """Continue the jobs an earlier night left half made: the same job from its stage, not a new idea.

    Returns `(busy, failed_channels)`: the code (`CARD_BUSY`) when the card stayed busy (the rest of the
    night waits, as for any job), else None, and the channels whose continued job failed again tonight
    (they get no second job from ideas
    tonight: a failure at a late stage would only cost another full run). `outcomes` receives what became of
    each job, for the morning note. The night's time decision (`nightfit`, S3) is asked for each job first:
    not BEGIN, it is not continued tonight (or given up with its card), and its channel goes into
    `left_channels` with the decision's sentence (no new job of it starts tonight either).
    """
    failed_channels: set[str] = set()
    try:
        pending = _resume_order(env, ownership.pending_resumes(env.paths, env.db, now))
    except Exception as e:  # noqa: BLE001 - see _close_dead_work
        env.log.warn("resume_scan_failed", str(e))
        return None, failed_channels
    for rec in pending:
        job_id, slug = str(rec["id"]), str(rec.get("channel") or "")
        stage = rec.get("interrupted_at_stage")
        if slug and _paused_now(env, slug):  # paused since the scan, or during an earlier job: leave it
            _hold_for_pause(env, report, None, slug, 1, job_id=job_id)
            outcomes[job_id] = ("paused", None)
            continue
        d, plan = _ask_resume(env, night, rec)
        if d.answer != nightfit.BEGIN:  # S3: not continued tonight, or given up (the one decision says which)
            entry = _left_entry(report, night, d, channel=slug, plan=plan, fmt=str(rec.get("fmt") or ""),
                                job_id=job_id, resumed=True)
            _keep(env, night, job_id, d)
            if d.answer == nightfit.CANNOT_FIT:
                _give_up(env, job_id, entry["said"])
            env.log.info("daily_resume_left_for_time", job_id=job_id, channel=slug, reason=d.reason)
            fate = "gave_up" if d.answer == nightfit.CANNOT_FIT else "out_of_time"
            outcomes[job_id] = (fate, entry["said"])
            left_channels[slug] = entry["said"]
            continue
        env.log.info("daily_job_resume", job_id=job_id, channel=slug, stage=stage)
        if _no_room_to_resume(env, report, job_id, slug):
            ownership.write_record(env.paths, job_id, state="interrupted")
            continue
        reopen = partial(jobs.load_job, env, job_id)
        code = _attempt(env, run, report, reopen, slug, run_id=run_id, fmt=str(rec.get("fmt") or ""),
                        resume_dir=env.paths.job_dir(job_id), resumed=True, night=night)
        has_card = ownership.has_card(rec)
        if code == BUSY:  # a Retry runs it: leave the job, its card and the report alone
            outcomes[job_id] = ("busy", None)
            continue
        if code == PAUSED:  # paused while it was being continued: interrupted again, it waits for the resume
            outcomes[job_id] = ("paused", None)
            continue
        if code == OUT_OF_TIME:  # S6/S7 while it was being continued: left again, or given up
            left = report["out_of_time"][-1]
            gave_up = left.get("answer") == nightfit.CANNOT_FIT
            outcomes[job_id] = ("gave_up" if gave_up else "out_of_time", str(left["said"]))
            left_channels[slug] = str(left["said"])
            continue
        if code == SLEPT:  # 4b.4 while it was being continued: interrupted again, the next night continues it
            slept = report["slept"][-1]
            outcomes[job_id] = ("slept", str(slept["said"]))
            left_channels[slug] = str(slept["said"])
            continue
        if code in CARD_BUSY:  # the card is busy (NS-E201, NS-E208): it stays interrupted, the next night
            _card_busy(env, report, night, job_id, slug, stage, resumed=True, has_card=has_card)
            outcomes[job_id] = ("waiting", None)
            return code, failed_channels
        fate = "resumed" if code is None else "failed_again"
        # A job the last night left for time was not interrupted by anything: when it finishes now, the
        # morning says so through `jobs` (`resumed`), not as an interrupted night that was resumed.
        if not (fate == "resumed" and rec.get("owner") == ownership.OUT_OF_TIME_OWNER):
            report["interrupted_jobs"].append(
                ownership.interrupted_entry(job_id, slug, stage, fate, has_card=has_card)
            )
        if code is None:
            rows = env.db.query("select output_id from outputs where job_id=?", (job_id,))
            ownership.settle_resumed(env.db, job_id, [r["output_id"] for r in rows])
            outcomes[job_id] = ("resumed", None)
        else:
            last = report["failures"][-1]
            ownership.settle_failed(env.db, job_id, str(last["code"]), str(last["error"]))
            outcomes[job_id] = ("failed", str(last["code"]))
            failed_channels.add(slug)
    return None, failed_channels


def _tonight(
    env: jobs.Env,
    run: RunFn,
    rng: random.Random,
    now: datetime,
    run_id: str,
    report: dict,
    recovered: ownership.Recovery | None = None,
    night: nightclock.Night | None = None,
) -> None:
    """Continue what was interrupted, tell the owner how that went, then make what is due.

    Inside the night's deadline (`night`, S3 to S6): a job that would end after it is not started, a stage
    that would is not begun, and the channels the previous night left work of go first."""
    if night is None:  # a caller with no clock of its own: the night has no deadline
        start = nightclock.TaskStart(now, nightclock.START_PROCESS)
        night = nightclock.Night(env.paths, run_id, nightclock.SystemClock(), start, None, env.log)
    outcomes: Outcomes = {}
    left_channels: dict[str, str] = {}
    try:
        gpu_busy, failed_channels = _resume_interrupted(
            env, run, now, run_id, report, outcomes, night, left_channels
        )
    finally:
        if recovered is not None and recovered.any:
            ownership.leave_recovery_note(env.paths, env.log, recovered, outcomes)
            in_note = {j.job_id for j in recovered.jobs}
            for e in report.get("slept") or []:  # told there (its outcome): not again in the sleep note
                e["in_recovery_note"] = e["job_id"] in in_note
    why: dict[str, str] = {}
    due = cadence.plan_daily(env.paths, env.db, now, why=why)
    for dj in due:
        if dj.channel.slug in failed_channels:
            why[dj.channel.slug] = (
                "its interrupted job was continued tonight and stopped again; Retry it in the Queue"
            )
        elif dj.channel.slug in left_channels:  # the night's time decision about its half-made video
            why[dj.channel.slug] = f"it has a half-made video: {left_channels[dj.channel.slug]}"
    due = [dj for dj in due if dj.channel.slug not in failed_channels | set(left_channels)]
    first = _left_last_night(env, run_id)  # S4: what the last night left goes first tonight
    due.sort(key=lambda dj: dj.channel.slug not in first)
    report["not_due"] = why
    for dj in due:
        if _paused_now(env, dj.channel.slug):  # paused after the plan was made: nothing of it starts
            _hold_for_pause(env, report, why, dj.channel.slug, dj.count)  # also when the GPU stayed busy
            continue
        if gpu_busy:  # J7: the card stayed busy for the whole wait; the other jobs wait for the next night
            _skip_rest(report, gpu_busy, dj.channel.slug, dj.count)
            continue
        ch = dj.channel
        bar = cadence.apply_quality_drop(env.paths, env.db, ch, dj.plan, now)
        if bar is not None:
            ch = ch.model_copy(update={"critic_bar": bar})
        report["channels"].append(
            {"channel": ch.slug, "plan": dj.plan.describe(), "jobs": dj.count, "critic_bar": ch.critic_bar,
             "reasons": dj.plan.reasons}
        )
        for n in range(dj.count):
            if n and _paused_now(env, ch.slug):  # paused during the night: its remaining jobs are not started
                _hold_for_pause(env, report, why, ch.slug, dj.count - n)
                break
            # Less room than one job writes (D-33): nothing is created, the rest of this channel's jobs
            # would not fit either, and the morning note says why (not a failure one stage in).
            if _no_room(env, report, ch, dj.fmt, dj.count - n):
                break
            # S5: the night's time decision for one more job of this plan: not BEGIN, nothing is created,
            # the channel stays due (the next night plans it again, first) and the entry says why
            d = _ask_new(night, dj.fmt)
            if d.answer != nightfit.BEGIN:
                _left_entry(report, night, d, channel=ch.slug, plan=dj.fmt, fmt=dj.fmt, jobs=dj.count - n)
                break
            long_job: dict[str, Any] | None = None
            if d.reason in (nightfit.FIRST_WORK, nightfit.FIRST_WORK_LONG):  # made over several nights
                long_job = {
                    "channel": ch.slug, "fmt": dj.fmt, "need_s": _seconds(d.need_s), "nights": d.nights,
                    "reason": d.reason,
                    "said": nightclock.marked(night.card, nightfit.say(d, channel=ch.slug, plan=dj.fmt)),
                }  # fmt: skip
            make = partial(jobs.make_job, env, ch, dj.fmt, now=now, rng=rng, origin="daily", run_id=run_id)
            code = _attempt(env, run, report, make, ch.slug, run_id=run_id, fmt=dj.fmt, night=night)
            # "Begun as the night's first work and made over about N nights: each nightly run after this one
            # continues it" is said only of a job this night really left half made for time. Said before the
            # attempt, it stood in the note beside the failure of a job that was never made (a stock key
            # refused before any job: block E final review), or of one that failed, or that finished tonight.
            if long_job is not None and code == OUT_OF_TIME:
                report.setdefault("long_jobs", []).append(long_job)
            if code == OUT_OF_TIME:  # S6/S7: left at a stage boundary; the rest of this channel waits too
                rest = _ask_new(night, dj.fmt) if dj.count - n - 1 else None
                if rest is not None and rest.answer != nightfit.BEGIN:
                    _left_entry(report, night, rest, channel=ch.slug, plan=dj.fmt, fmt=dj.fmt,
                                jobs=dj.count - n - 1)
                break
            if code == SLEPT:  # 4b.4: its half-made job is continued next night; no second job of it tonight
                break
            if code == PAUSED:  # paused while this job ran: it waits for the resume, the others do not start
                if dj.count - n - 1:
                    _hold_for_pause(env, report, why, ch.slug, dj.count - n - 1)
                break
            if code in CARD_BUSY:
                gpu_busy = code
                failed = report["failures"][-1] if report["failures"] else {}
                if failed.get("code") == code and failed.get("job_id") != "(not created)":
                    # the job waits for the next night (NS-E201 and NS-E208 alike): not counted as skipped
                    _card_busy(env, report, night, str(failed["job_id"]), ch.slug, None, resumed=False,
                               has_card=False)  # fmt: skip
                    _skip_rest(report, code, ch.slug, dj.count - n - 1)
                else:
                    _skip_rest(report, code, ch.slug, dj.count - n)
                break


PAUSED_TONIGHT = (
    "its channel was paused during the night; resume it and the next night continues "
    f"{ownership.WHILE_NIGHTLY_ON_CLAUSE} (a half-made job that started more than "
    f"{ownership.RESUME_WITHIN_DAYS} days before that night needs Retry in the Queue)"
)


def _paused_now(env: jobs.Env, slug: str) -> bool:
    """True when the channel's profile says `paused` right now (read again: a pause can come at any time,
    from the Dashboard, the Assistant or `studio pause` in another window). Unreadable (invalid, gone, or held
    open by another program on Windows: OSError) means not paused: a WARN and the night goes on."""
    try:
        return get_channel(env.paths, slug).status == "paused"
    except (StudioError, OSError) as e:
        env.log.warn("channel_profile_unreadable", str(e), channel=slug, check="pause")
        return False


def _hold_for_pause(
    env: jobs.Env,
    report: dict,
    why: dict[str, str] | None,
    slug: str,
    jobs_left: int,
    *,
    job_id: str | None = None,
) -> None:
    """A channel paused mid-night: its remaining jobs are listed under `paused` with the reason, not started.

    `skipped` keeps its old meaning (an earlier job stopped, so the others wait), which the Queue page words
    as such; a pause has its own key. A half-made job (`job_id`) is left exactly as it was: still interrupted,
    so the next night continues it once the channel is resumed, while it is under `RESUME_WITHIN_DAYS` old."""
    entry: dict[str, Any] = {"channel": slug, "jobs": jobs_left, "reason": PAUSED_TONIGHT}
    if job_id:
        entry["job_id"] = job_id
    report.setdefault("paused", []).append(entry)
    if why is not None:
        why[slug] = PAUSED_TONIGHT
    env.log.info("daily_channel_paused", channel=slug, jobs=jobs_left, job_id=job_id)


def nothing_due_message(report: dict) -> str:
    """The console sentence when the night planned nothing, with the true reason for each channel.

    A channel with a job still being made says so (job, stage, process); one that has its video says that;
    with no active channel the sentence says so instead of claiming every channel is done.
    """
    why: dict[str, str] = report.get("not_due") or {}
    unreadable: list[list[str]] = report.get("unreadable_profiles") or []
    parts = [f"{slug}: {reason}" for slug, reason in why.items()]
    if unreadable:  # a channel that was skipped for its profile is not "not active"
        parts.append("not run, profile cannot be read: " + ", ".join(str(p[0]) for p in unreadable))
    if not parts:
        return "nothing due tonight: no channel is active"
    return "nothing due tonight: " + "; ".join(parts)
