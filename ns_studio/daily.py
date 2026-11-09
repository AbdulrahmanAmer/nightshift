"""`studio run --daily`: cadence-planned nightly run with a DB backup at the end (component C7)."""

from __future__ import annotations

import json
import random
import secrets
import traceback
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from ns_common.atomic import read_text_retry, write_text_atomic
from ns_common.errors import NsError
from ns_common.keepawake import keep_awake
from ns_studio import jobs, notifications
from ns_studio.channels import cadence
from ns_studio.channels.crud import get_channel
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
        return max(0, int(json.loads(f.read_text(encoding="utf-8")).get("publish_not_before_days", 0) or 0))
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
        " and status in ('pending', 'approved') and created_at >= ?",
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

    One failing job never stops the others; failures are listed in the report. When a `notifier` is given
    the night ends with a toast (spec J10: videos ready, or the failure code), unless Settings turned
    notifications off. Returns the report dict.
    """
    now = now or datetime.now(UTC)
    rng = rng or random.Random()
    run = run_fn or _default_run
    run_id = f"daily-{now:%Y%m%d}-{secrets.token_hex(2)}"
    env.db.execute(
        "insert into runs(run_id, started_at, mode) values (?,?,?)",
        (run_id, now.isoformat(timespec="seconds"), "daily"),
    )
    report: dict = {"run_id": run_id, "started_at": now.isoformat(timespec="seconds"), "channels": [],
                    "jobs": [], "failures": []}
    gpu_busy = False
    for dj in cadence.plan_daily(env.paths, env.db, now):
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
            job_id = "(not created)"
            try:
                # No stock-footage key: every story would fail at `clips` after an hour of GPU work (gap L3).
                require_footage_key(env.services)
                ctx = jobs.make_job(env, ch, dj.fmt, now=now, rng=rng)
                job_id = ctx.job_id
                ran = run(ctx)
                report["jobs"].append({"job_id": job_id, "channel": ch.slug, "stages": len(ran)})
                continue
            except (NsError, StudioError) as e:
                code, detail = e.code, str(e)
            except Exception as e:  # an uncoded bug or corrupt file ends this job, never the night
                code, detail = UNEXPECTED, f"{UNEXPECTED}: {describe_unexpected(e)}"
                env.log.error("daily_job_crashed", job_id=job_id, traceback=traceback.format_exc())
            env.log.error("daily_job_failed", job_id=job_id, code=code, detail=detail)
            report["failures"].append({"job_id": job_id, "channel": ch.slug, "code": code, "error": detail})
            if code == "NS-E201":
                gpu_busy = True
                report.setdefault("skipped", []).append({"channel": ch.slug, "jobs": dj.count - n})
                break
    done = [j["job_id"] for j in report["jobs"]]
    report["publish_times_set"] = apply_publish_times(env, done, rng, now)
    backup = env.db.backup(env.paths.backup_dir, today=now.date())
    report["backup"] = str(backup)
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
