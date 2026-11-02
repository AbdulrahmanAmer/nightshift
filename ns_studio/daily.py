"""`studio run --daily`: cadence-planned nightly run with a DB backup at the end (component C7)."""

from __future__ import annotations

import json
import random
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ns_common.errors import NsError
from ns_studio import jobs
from ns_studio.channels import cadence
from ns_studio.channels.crud import get_channel
from ns_studio.context import JobContext
from ns_studio.errors import StudioError
from ns_studio.policy.regen import run_job_guarded

RunFn = Callable[[JobContext], list[str]]


def _default_run(ctx: JobContext) -> list[str]:
    return run_job_guarded(ctx)


def apply_publish_times(env: jobs.Env, job_ids: list[str], rng: random.Random, now: datetime) -> int:
    """Rewrite `publish_after` in the outbox sheets of `job_ids` with jittered slots (spec 18.6).

    Works on whatever sheets exist (`*.json` under `outbox\\<slug>\\<date>\\<job>` that already carry a
    `publish_after` key) and returns how many were rewritten.
    """
    rewritten = 0
    by_channel: dict[str, list[tuple[cadence.ReleaseItem, Path]]] = {}
    for job_id in job_ids:
        for sheet in env.paths.outbox.glob(f"*/*/{job_id}/**/*.json"):
            try:
                data = json.loads(sheet.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or "publish_after" not in data:
                continue
            kind = "long" if data.get("kind") == "long" else "short"
            item = cadence.ReleaseItem(f"{job_id}:{sheet}", job_id, kind)
            by_channel.setdefault(str(data.get("channel", "")), []).append((item, sheet))
    for slug, entries in by_channel.items():
        try:
            ch = get_channel(env.paths, slug)
        except StudioError:
            continue
        times = cadence.assign_publish_times(
            [e[0] for e in entries], now.date(), ch.publish_window, ch.shorts_per_day_max, rng
        )
        for item, sheet in entries:
            data = json.loads(sheet.read_text(encoding="utf-8"))
            data["publish_after"] = times[item.output_id].isoformat(timespec="minutes")
            sheet.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            rewritten += 1
    return rewritten


def run_daily(
    env: jobs.Env,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
    run_fn: RunFn | None = None,
) -> dict:
    """Plan with the governor, run every due job, back up the database and write the nightly report.

    One failing job never stops the others; failures are listed in the report. Returns the report dict.
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
            ctx = jobs.make_job(env, ch, dj.fmt, now=now, rng=rng)
            try:
                ran = run(ctx)
                report["jobs"].append({"job_id": ctx.job_id, "channel": ch.slug, "stages": len(ran)})
            except (NsError, StudioError) as e:
                env.log.error("daily_job_failed", job_id=ctx.job_id, code=e.code, detail=str(e))
                failure = {"job_id": ctx.job_id, "channel": ch.slug, "code": e.code, "error": str(e)}
                report["failures"].append(failure)
                if e.code == "NS-E201":
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
    nightly.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    env.log.info("daily_done", run_id=run_id, jobs=len(done), failures=len(report["failures"]))
    return report
