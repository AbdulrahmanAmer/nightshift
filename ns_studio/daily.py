"""`studio run --daily`: cadence-planned nightly run with a DB backup at the end (component C7)."""

from __future__ import annotations

import json
import random
import secrets
import traceback
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from datetime import UTC, date, datetime, timedelta, tzinfo
from functools import partial
from pathlib import Path
from typing import Any

from ns_common.atomic import read_text_retry, write_text_atomic
from ns_common.errors import NsError, describe
from ns_common.keepawake import keep_awake
from ns_studio import jobs, notifications, ownership
from ns_studio.channels import cadence
from ns_studio.channels.crud import broken_channels, get_channel
from ns_studio.context import JobContext
from ns_studio.errors import UNEXPECTED, StudioError, describe_unexpected
from ns_studio.footage_keys import require_footage_key
from ns_studio.media.outbox import retime_sheet
from ns_studio.policy.regen import run_job_guarded

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
        try:
            data = json.loads(read_text_retry(Path(r["path"]).with_suffix(".json")))
            day = datetime.fromisoformat(str(data["publish_after"])).date()
        except (OSError, ValueError, KeyError, TypeError):
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
            try:
                data = json.loads(Path(entry["json"]).read_text(encoding="utf-8"))
            except (OSError, KeyError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or "publish_after" not in data:
                continue
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


def run_daily(
    env: jobs.Env,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
    run_fn: RunFn | None = None,
    notifier: notifications.Notifier | None = None,
    awake: Callable[[], AbstractContextManager[None]] = keep_awake,
) -> dict:
    """The nightly run (see `_run_daily`), with the PC held awake for as long as it lasts.

    The 03:00 task wakes the PC, but with nobody at the keyboard Windows puts it back to sleep after a couple
    of minutes, GPU load or not. `awake()` (`ES_CONTINUOUS | ES_SYSTEM_REQUIRED`, per thread) is held on this
    thread from the first plan to the report and released in `finally`, also when the night breaks.
    """
    with awake():
        return _run_daily(env, now=now, rng=rng, run_fn=run_fn, notifier=notifier)


def _run_daily(
    env: jobs.Env,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
    run_fn: RunFn | None = None,
    notifier: notifications.Notifier | None = None,
) -> dict:
    """Plan with the governor, run every due job, back up the database and write the nightly report.

    First it closes what a dead process left open (a night killed by a power cut or a crash: its run row, its
    half-made job, a morning note) and continues that job from the stage it reached instead of starting from
    ideas. One failing job never stops the others; failures are listed in the report. When a `notifier` is
    given the night ends with a toast (spec J10: videos ready, or the failure code, and a word about an
    interrupted night), unless Settings turned notifications off. Returns the report dict.
    """
    now = now or datetime.now(UTC)
    rng = rng or random.Random()
    run = run_fn or _default_run
    run_id = f"daily-{now:%Y%m%d}-{secrets.token_hex(2)}"
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
        try:
            _tonight(env, run, rng, now, run_id, report, recovered)
            done = [j["job_id"] for j in report["jobs"]]
            report["publish_times_set"] = apply_publish_times(env, done, rng, now)
            backup = env.db.backup(env.paths.backup_dir, today=now.date())
            report["backup"] = str(backup)
        except BaseException as e:  # the night itself broke: close its row, so it never looks unfinished
            report["failed"] = getattr(e, "code", UNEXPECTED)
            env.db.execute(
                "update runs set finished_at=?, summary_json=? where run_id=?",
                (datetime.now(UTC).isoformat(timespec="seconds"), json.dumps(report), run_id),
            )
            raise
        finished = datetime.now(UTC)
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
    notifications.send(env.paths, env.log, notifier, notifications.nightly_message(report, masters, shorts))
    finish = getattr(env.services.acquirer, "run_finished", None)
    if finish is not None:  # gap 25: the engine this run needed leaves once nobody else uses it
        finish()
    return report


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
        "folder is not read-only, full or blocked by antivirus; the next nightly run tries again."
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
    resumed: bool = False,
) -> str | None:
    """Make (or reopen) one job and run it, owned for as long as it runs. Returns the error code on failure
    (`BUSY` when another process is already running that job: not a failure, nothing is reported).

    A failure (coded or not) ends this job and is listed in the report, never the night.
    """
    job_id = "(not created)"
    try:
        # No stock-footage key: every story would fail at `clips` after an hour of GPU work (gap L3).
        require_footage_key(env.services)
        ctx = make()
        job_id = ctx.job_id
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
        code, detail = e.code, str(e)
    except Exception as e:  # an uncoded bug or corrupt file ends this job, never the night
        code, detail = UNEXPECTED, f"{UNEXPECTED}: {describe_unexpected(e)}"
        env.log.error("daily_job_crashed", job_id=job_id, traceback=traceback.format_exc())
    env.log.error("daily_job_failed", job_id=job_id, code=code, detail=detail)
    report["failures"].append({"job_id": job_id, "channel": slug, "code": code, "error": detail})
    return code


Outcomes = dict[str, tuple[str, str | None]]
#: What `_attempt` returns for a job another process is running (a Retry from the Queue): not an error code.
BUSY = "busy"
#: The `job_id` of the one `failures` entry that a night which could not be owned leaves (`_refuse_night`).
NIGHT_NOT_STARTED = "(night did not start)"


def _resume_interrupted(
    env: jobs.Env, run: RunFn, now: datetime, run_id: str, report: dict, outcomes: Outcomes
) -> tuple[bool, set[str]]:
    """Continue the jobs an earlier night left half made: the same job from its stage, not a new idea.

    Returns `(gpu_busy, failed_channels)`: True when the GPU stayed busy (the rest of the night waits, as for
    any job), and the channels whose continued job failed again tonight (they get no second job from ideas
    tonight: a failure at a late stage would only cost another full run). `outcomes` receives what became of
    each job, for the morning note.
    """
    failed_channels: set[str] = set()
    try:
        pending = ownership.pending_resumes(env.paths, env.db, now)
    except Exception as e:  # noqa: BLE001 - see _close_dead_work
        env.log.warn("resume_scan_failed", str(e))
        return False, failed_channels
    for rec in pending:
        job_id, slug = str(rec["id"]), str(rec.get("channel") or "")
        stage = rec.get("interrupted_at_stage")
        env.log.info("daily_job_resume", job_id=job_id, channel=slug, stage=stage)
        reopen = partial(jobs.load_job, env, job_id)
        code = _attempt(env, run, report, reopen, slug, run_id=run_id, resumed=True)
        has_card = ownership.has_card(rec)
        if code == BUSY:  # a Retry runs it: leave the job, its card and the report alone
            outcomes[job_id] = ("busy", None)
            continue
        if code == "NS-E201":  # the GPU is busy: it stays interrupted, the next night tries again
            ownership.write_record(env.paths, job_id, state="interrupted")
            # It is waiting, not failed and not skipped: `_attempt` listed the NS-E201 as a failure, which the
            # card and the toast must not repeat. Its interrupted entry says "waiting" instead.
            report["failures"] = [
                f for f in report["failures"] if not (f["job_id"] == job_id and f["code"] == "NS-E201")
            ]
            report["interrupted_jobs"].append(
                ownership.interrupted_entry(job_id, slug, stage, "waiting", has_card=has_card)
            )
            outcomes[job_id] = ("waiting", None)
            return True, failed_channels
        fate = "resumed" if code is None else "failed_again"
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
    return False, failed_channels


def _tonight(
    env: jobs.Env,
    run: RunFn,
    rng: random.Random,
    now: datetime,
    run_id: str,
    report: dict,
    recovered: ownership.Recovery | None = None,
) -> None:
    """Continue what was interrupted, tell the owner how that went, then make what is due."""
    outcomes: Outcomes = {}
    try:
        gpu_busy, failed_channels = _resume_interrupted(env, run, now, run_id, report, outcomes)
    finally:
        if recovered is not None and recovered.any:
            ownership.leave_recovery_note(env.paths, env.log, recovered, outcomes)
    why: dict[str, str] = {}
    due = cadence.plan_daily(env.paths, env.db, now, why=why)
    for dj in due:
        if dj.channel.slug in failed_channels:
            why[dj.channel.slug] = (
                "its interrupted job was continued tonight and stopped again; Retry it in the Queue"
            )
    due = [dj for dj in due if dj.channel.slug not in failed_channels]
    report["not_due"] = why
    for dj in due:
        if gpu_busy:  # J7: the GPU stayed busy for the whole wait; the other jobs wait for the next night
            report.setdefault("skipped", []).append({"channel": dj.channel.slug, "jobs": dj.count})
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
            make = partial(jobs.make_job, env, ch, dj.fmt, now=now, rng=rng, origin="daily", run_id=run_id)
            if _attempt(env, run, report, make, ch.slug, run_id=run_id) == "NS-E201":
                gpu_busy = True
                report.setdefault("skipped", []).append({"channel": ch.slug, "jobs": dj.count - n})
                break


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
