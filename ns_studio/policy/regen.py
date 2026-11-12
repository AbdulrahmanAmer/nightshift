"""Regenerate-once policy for gate failures (spec section 18.8, component C7).

A failure moves the item to `rejected\\` with the reason, regenerates once from the earliest
affected stage, and logs the code. A second failure of the same cause ends the job.

Besides the policy codes NS-E601 to NS-E614 the same rule covers the other failures of J3 step 2: NS-E303,
NS-E304 (the ideas stage), NS-E305, NS-E402, NS-E403 and NS-E404 (QC). A story that misses the critic's
bar has its own rule (spec 16.2): the idea is discarded and the next one drawn, three ideas at most.

One retry per cause (card story-robust S4): `policy_attempts.json` keeps three counters under `causes`,
each at most `MAX_REGENERATIONS`: `idea` (NS-E601 raised by the ideas stage), `text` (NS-E303, E304, E305
and the text gate's codes) and `media` (NS-E402, E403, E404 and the final gate's codes). `regenerations`
stays the total number of regenerations (`JobContext.attempt`, the hotter prompts, the seeds). A file from
before the counters (no `causes`) reads as that one count for all three, so a job in flight never gets more
retries than it had. An NS-E601 marked `final` (the owner's own idea refused in a job he started by hand)
ends the job at once and spends nothing.

The owner's idea is never redrawn by a refusal of its story (S3b, `same_idea_start`): when the job's idea is
banked, a story-level failure that would restart at `ideas` restarts at the stage after it, so the one text
retry writes the story again from the same idea; when that retry is spent the job ends with the reason. A
story of his idea that misses the critic's bar (NS-E304 of a critic stage) is not redrawn either: the same
text retry, then NS-E304 with its detail.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from ns_common.errors import NsError
from ns_studio import ownership
from ns_studio.context import JobContext
from ns_studio.orchestrator import plan_format, plan_job, run_job
from ns_studio.policy.base import POLICY_CODES
from ns_studio.stages import StageState
from ns_studio.text.idea_redraw import CRITIC_STAGES, MAX_IDEAS, record_failed_idea

ATTEMPTS_FILE = "policy_attempts.json"
MAX_REGENERATIONS = 1

# Candidate restart stages per code, earliest sensible first; the first one present in the plan wins. Each
# tuple names the stages of every plan: shorts_only (`draft`, `hooks`, `seo`), master_first (`chapters_draft`,
# `shorts_select`, `seo_long`, `seo_shorts`), master_from_script (`seo_long`, `seo_shorts`) and the owner
# script plans (Short: `seo`, `visual_plan`; long: `shorts_select`, `seo_long`, `seo_shorts`). A name that is
# in no plan sends the job back to the first stage of its plan: an hour of LLM work for one disliked tag.
# For an owner's own script that stage is only the import of the same text, so the job fails at once with
# the original code.
REGEN_FROM: dict[str, tuple[str, ...]] = {
    "NS-E601": ("ideas",),
    "NS-E602": ("draft", "chapters_draft", "ideas"),
    "NS-E603": ("seo", "seo_long", "ideas"),
    "NS-E604": ("hooks", "shorts_select", "seo", "seo_long", "ideas"),
    "NS-E605": ("ideas",),
    "NS-E606": ("seo", "seo_long", "ideas"),
    "NS-E607": ("shorts_select", "shorts_plan", "ideas"),
    "NS-E611": ("clips", "visual_plan", "ideas"),
    "NS-E612": ("expand", "chapters_draft", "draft", "plan", "long_plan", "ideas"),
    "NS-E613": ("clips", "visual_plan", "ideas"),
    "NS-E614": ("voice", "render", "ideas"),
    # J3 step 2 failures that are not policy gates. No candidate in the plan (the owner's own script, a series
    # part) means the job fails at once: redoing the import or the voice of the same text would fail again.
    "NS-E303": ("draft", "chapters_draft"),
    "NS-E304": ("ideas",),
    "NS-E305": ("chapters_draft", "long_plan"),
    "NS-E402": ("clips", "visual_plan", "visual_plan_long"),
    "NS-E403": ("voice", "captions"),
    "NS-E404": ("voice", "render", "render_master"),
}
J3_CODES: frozenset[str] = frozenset({"NS-E303", "NS-E304", "NS-E305", "NS-E402", "NS-E403", "NS-E404"})
# NS-E404 names the failed QC checks in its detail ("s2: duration, black_frames"). The earliest stage that can
# change a check is redone: timing and loudness come from the voice, footage from the clips, the container and
# the frame size from the render (each tuple lists alternatives; the one in the plan is used).
QC_RESTART: dict[str, tuple[str, ...]] = {
    "duration": ("voice",),
    "loudness": ("voice",),
    "caption_drift_ms": ("voice",),
    "hook_first_1500ms": ("voice",),
    "chapters": ("voice",),
    "midroll": ("voice",),
    "black_frames": ("clips",),
    "aspect": ("render", "render_master"),
    "file_opens": ("render", "render_master"),
    # `media/derive.py` cuts a Short from the master and raises "sN: 71 s exceeds the 60 s Shorts limit": a
    # new voice take cannot shorten the span the selection chose, only a new selection can. A plan whose
    # Shorts come with the script (a series part) has no such stage: nothing there can change them.
    "Shorts limit": ("shorts_select",),
}
# Metadata failures are fixed by rewriting the metadata alone: only these stages are redone (everything after
# them is gated on its input hashes and follows when the SEO files change), never the chapters.
REGEN_ONLY: dict[str, tuple[str, ...]] = {
    "NS-E603": ("seo", "seo_long", "seo_shorts"),
    "NS-E606": ("seo", "seo_long", "seo_shorts"),
}
# Checks that share a code with another check but are fixed at a different stage: tried before REGEN_FROM.
# `short_hook` (NS-E611) is a hook that was not voiced or captioned at the start of its Short: the footage
# is fine, so the voice is redone (another TTS seed) instead of the clips.
REGEN_FROM_CHECK: dict[str, tuple[str, ...]] = {
    "short_hook": ("voice", "ideas"),
}
_SMALL_FILES = (
    "idea.json",
    "story.json",
    "parts.json",
    "seo.json",
    "seo_long.json",
    "seo_shorts.json",
    "shorts_plan.json",
    "policy_text.json",
    "policy_final.json",
    "policy_notes.json",
)
_MEDIA_DIRS = ("long", "shorts")
_EVIDENCE_DIRS = ("qc",)  # small JSON that says why; copied, because the stage writes it again


def earliest_stage(code: str, names: list[str], detail: str = "", check: str | None = None) -> str:
    """The stage to restart from for `code`, falling back to the first stage of the plan.

    A failing `check` that shares its code with another one comes first (`REGEN_FROM_CHECK`). For NS-E404
    the failed checks named in `detail` decide (`QC_RESTART`); a detail that names none restarts at the
    voice, the first stage of `REGEN_FROM`.
    """
    if code == "NS-E404":
        found = [
            min(names.index(a) for a in alternatives if a in names)
            for check_name, alternatives in QC_RESTART.items()
            if check_name in detail and any(a in names for a in alternatives)
        ]
        if found:
            return names[min(found)]
    for cand in (*REGEN_FROM_CHECK.get(check or "", ()), *REGEN_FROM.get(code, ())):
        if cand in names:
            return cand
    return names[0]


def has_restart_stage(code: str, names: list[str], detail: str = "") -> bool:
    """True when the plan has a stage that can redo the work behind `code` (always for the policy codes).

    For NS-E404 whose `detail` names a failed check, a stage that can change that check must be in the plan.
    """
    if code == "NS-E404":
        named = [alts for check, alts in QC_RESTART.items() if check in detail]
        if named:
            return any(a in names for alts in named for a in alts)
    return code not in J3_CODES or any(c in names for c in REGEN_FROM[code])


#: Files a stage keeps as its own memo and returns early on; the retry would reproduce them unchanged.
#: Deleted here for every stage a regeneration restarts. A memo that lives in a folder of its own is keyed on
#: `JobContext.generation` instead (`drafts/ch<n>.json` of `chapters_draft`, the voice signature): a
#: regeneration changes the generation, a plain resume does not.
STAGE_MEMOS: dict[str, tuple[str, ...]] = {
    "hooks": ("hooks_all.json", "closings.json"),  # HooksStage skips on the first; a redo drops both
    "voice": ("audio/manifest.json",),  # synthesize() reuses every WAV with an unchanged signature
}


def invalidate_from(ctx: JobContext, names: list[str], stage: str) -> None:
    """Forget `stage` and every later stage, and their memo files, so the next `run_job` really redoes them.

    The retry also differs from the first pass: prompts run hotter (`text.prompts.attempt_temperature`),
    TTS gets another seed and clip draws another seed with the rejected clips blocked.
    """
    state = StageState.load(ctx.job_dir)
    for n in names[names.index(stage) :]:
        state.entries.pop(n, None)
        for memo in STAGE_MEMOS.get(n, ()):
            ctx.file(memo).unlink(missing_ok=True)
    state.save(ctx.job_dir)


def invalidate_only(ctx: JobContext, names: list[str], only: tuple[str, ...]) -> list[str]:
    """Forget just the stages of `names` that are in `only` (and their memos); returns those stages."""
    state = StageState.load(ctx.job_dir)
    hit = [n for n in names if n in only]
    for n in hit:
        state.entries.pop(n, None)
        for memo in STAGE_MEMOS.get(n, ()):
            ctx.file(memo).unlink(missing_ok=True)
    state.save(ctx.job_dir)
    return hit


def _attempts(ctx: JobContext) -> dict:
    f = ctx.file(ATTEMPTS_FILE)
    fresh: dict = {"regenerations": 0, "history": []}
    if not f.exists():
        return fresh
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return (
            {**fresh, **data}
            if isinstance(data, dict) and isinstance(data.get("history", []), list)
            else fresh
        )
    except (OSError, ValueError):  # a half-written file must not turn the failure being handled into a crash
        return fresh


#: The three causes that each have their own retry (S4).
CAUSES: tuple[str, ...] = ("idea", "text", "media")
#: Failures of the rendered media and of the final gate: the `media` cause. NS-E601 of the final gate's
#: re-check restarts at `ideas` (a new story) and counts as `text`, like the text gate's.
MEDIA_CODES: frozenset[str] = frozenset(
    {"NS-E402", "NS-E403", "NS-E404", "NS-E611", "NS-E612", "NS-E613", "NS-E614"}
)


def _ideas_unfinished(ctx: JobContext, names: list[str]) -> bool:
    """True when the plan has `ideas` and it has not finished: the failure came from the ideas stage."""
    if "ideas" not in names:
        return False
    return StageState.load(ctx.job_dir).entries.get("ideas", {}).get("status") != "done"


def cause_of(ctx: JobContext, err: NsError, names: list[str]) -> str:
    """Which retry `err` spends: `idea` (the ideas stage refused an idea), `media` or `text` (S4)."""
    if err.code == "NS-E601" and (err.data.get("stage") == "ideas" or _ideas_unfinished(ctx, names)):
        return "idea"
    return "media" if err.code in MEDIA_CODES else "text"


def idea_is_banked(ctx: JobContext) -> bool:
    """True when this job's idea is one the owner approved (card story-robust S3b).

    The fact on disk: the `ideas` row this job claimed from the bank (`draw_banked_idea` sets `status='used'`
    and this job's id in one update; an idea of the model's own is inserted with `status='new'`, `used=1`)
    whose premise is the premise of the job's `idea.json`. Both survive a kill and a resume, and a job claimed
    by an older version carries the same row. An idea that was redrawn after a critic failure no longer
    matches the claimed row's premise.
    """
    try:
        idea = ctx.read_json("idea.json")
    except (OSError, ValueError):
        return False
    premise = idea.get("premise") if isinstance(idea, dict) else None
    if not isinstance(premise, str) or not premise:
        return False
    row = ctx.db.one(
        "select idea_id from ideas where job_id=? and status='used' and premise=? limit 1",
        (ctx.job_id, premise),
    )
    return row is not None


def same_idea_start(ctx: JobContext, cause: str, start: str, names: list[str]) -> str:
    """Where a story's refusal restarts (S3b): never at `ideas` when the job's idea is the owner's.

    A failure whose restart stage is `ideas` but that is not the idea stage's own refusal (the text gate's
    or the final gate's NS-E601, NS-E605, an `ideas` fallback) would draw another idea in place of his. For a
    banked idea the story is written again from the SAME idea: the first stage after `ideas`. A job whose idea
    is the model's own keeps `start`.
    """
    if start != "ideas" or cause == "idea" or _ideas_unfinished(ctx, names):
        return start
    i = names.index("ideas")
    if i + 1 >= len(names) or not idea_is_banked(ctx):
        return start
    ctx.log.info("same_idea_restart", job_id=ctx.job_id, restart_at=names[i + 1])
    return names[i + 1]


def _count(value: object) -> int:
    """A counter read from the file; one that is not a number reads as spent (never more retries)."""
    try:
        return max(0, int(value))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return MAX_REGENERATIONS


def cause_counts(att: dict) -> dict[str, int]:
    """The regenerations spent per cause; a file from before the counters gives its one count to all three."""
    raw = att.get("causes")
    if not isinstance(raw, dict):
        total = _count(att.get("regenerations", 0))
        return dict.fromkeys(CAUSES, total)
    return {c: _count(raw.get(c, 0)) for c in CAUSES}


def reject_artifacts(ctx: JobContext, err: NsError, attempt: int) -> Path:
    """Move rendered media and copy the text artifacts to `rejected\\<slug>\\<date>\\<job>\\attempt<N>`."""
    now = datetime.now(UTC)
    dest = ctx.paths.rejected_dir(ctx.channel.slug, f"{now:%Y-%m-%d}", ctx.job_id) / f"attempt{attempt}"
    dest.mkdir(parents=True, exist_ok=True)
    reason = {
        "code": err.code,
        "message": err.definition.message,
        "detail": err.detail,
        "ts": now.isoformat(),
    }
    (dest / "reason.json").write_text(json.dumps(reason, indent=2) + "\n", encoding="utf-8")
    for name in _SMALL_FILES:
        if ctx.file(name).exists():
            shutil.copy2(ctx.file(name), dest / name)
    for d in _MEDIA_DIRS:
        src = ctx.file(d)
        if src.is_dir():
            shutil.move(str(src), str(dest / d))
    for d in _EVIDENCE_DIRS:
        if ctx.file(d).is_dir():
            shutil.copytree(ctx.file(d), dest / d, dirs_exist_ok=True)
    return dest


def _redraw_idea(ctx: JobContext, err: NsError, att: dict, names: list[str]) -> None:
    """A story missed the critic's bar: reject it and restart at `ideas` for the next idea (spec 16.2).

    Raises NS-E304 once `MAX_IDEAS` ideas have failed. The count is the length of `ideas_failed.json`,
    which is why a resumed job does not get three new ideas. A second cap counts the redraws themselves (the
    history entries of `policy_attempts.json` that carry an `idea` key): a premise that could not be recorded
    (no readable `idea.json`, or a premise that failed before) would otherwise leave the first count at
    zero and the loop without an end.
    """
    tried = record_failed_idea(ctx)
    dest = reject_artifacts(ctx, err, len(att["history"]) + 1)
    att["history"].append({"code": err.code, "detail": err.detail, "rejected_to": str(dest), "idea": tried})
    ctx.write_json(ATTEMPTS_FILE, att)
    redraws = sum(1 for h in att["history"] if isinstance(h, dict) and "idea" in h)
    tried = max(tried, redraws)
    if tried >= MAX_IDEAS:
        ctx.log.error("policy_fail_final", code=err.code, job_id=ctx.job_id, detail=err.detail)
        raise NsError("NS-E304", f"{tried} ideas failed the critic; last: {err.detail}") from err
    ctx.log.warn("idea_redraw", code=err.code, tried=tried, job_id=ctx.job_id, detail=err.detail)
    invalidate_from(ctx, names, "ideas")


def _critic_failed(ctx: JobContext, names: list[str]) -> bool:
    """True when the first stage of the plan that has not finished is a critic stage (the one that raised)."""
    state = StageState.load(ctx.job_dir)
    for n in names:
        if state.entries.get(n, {}).get("status") != "done":
            return n in CRITIC_STAGES and "ideas" in names
    return False


def run_job_guarded(ctx: JobContext, *, resume: bool = True) -> list[str]:
    """`run_job` with regenerate-once handling of NS-E601 to NS-E614 policy failures and the J3 failures.

    The voice setup queued (`config/pending_voice.json`) is created first, so every path that runs a job
    (`studio run`, the dashboard's Run now, a series part) has it before the voice stage asks for it.

    The job is owned for as long as it runs (`ownership.hold_job`: an OS lock that dies with the process), so
    a kill leaves a job the next start can tell is dead, and a second process cannot run the same job at once.
    """
    with ownership.hold_job(ctx.paths, ctx.job_id, channel=ctx.channel.slug, fmt=str(ctx.fmt)):
        return _run_job_guarded(ctx, resume=resume)


def _run_job_guarded(ctx: JobContext, *, resume: bool) -> list[str]:
    from ns_studio.channels.crud import design_name_problems, pool_name_problems, sync_channel_row
    from ns_studio.errors import StudioError
    from ns_studio.voice_setup import apply_pending_voices

    # A series part is created by the dashboard without `make_job`: give its channel the display name of its
    # profile (a YAML-only channel was left with its slug) and refuse unknown preset names up front (NS-E320).
    sync_channel_row(ctx.db, ctx.channel)
    if ctx.series:
        problems = [*pool_name_problems(ctx.channel.pools), *design_name_problems(ctx.series)]
        if problems:
            raise StudioError("NS-E320", "; ".join(problems))
    apply_pending_voices(ctx)
    first = True
    while True:
        try:
            return run_job(ctx, resume=resume if first else True)
        except NsError as e:
            if e.code not in POLICY_CODES and e.code not in J3_CODES:
                raise
            att = _attempts(ctx)
            names = [s.name for s in plan_job(plan_format(ctx.fmt, ctx.job_dir), ctx.policy)]
            if e.data.get("final"):
                # The owner's own idea was refused in a job he started by hand (IdeasStage, S3): it is back
                # in his bank and nothing may be written in its place. No retry is spent.
                att["history"].append(
                    {"code": e.code, "detail": e.detail, "rejected_to": None, "cause": "idea"}
                )
                ctx.write_json(ATTEMPTS_FILE, att)
                ctx.log.error("policy_fail_final", code=e.code, job_id=ctx.job_id, detail=e.detail)
                raise
            # A story that missed the critic's bar draws the next idea (spec 16.2), unless the idea is the
            # owner's: then it takes the one text retry from the SAME idea below (`same_idea_start`, S3b).
            if e.code == "NS-E304" and _critic_failed(ctx, names) and not idea_is_banked(ctx):
                _redraw_idea(ctx, e, att, names)
                first = False
                continue
            cause = cause_of(ctx, e, names)
            counts = cause_counts(att)
            n = counts[cause]
            check = str(e.data.get("check") or "") or None
            start = same_idea_start(ctx, cause, earliest_stage(e.code, names, e.detail, check), names)
            if (ctx.owner_script and start == names[0]) or not has_restart_stage(e.code, names, e.detail):
                # Only the import of the owner's own text would be redone: it would fail the same way.
                att["history"].append(
                    {"code": e.code, "detail": e.detail, "rejected_to": None, "cause": cause}
                )
                ctx.write_json(ATTEMPTS_FILE, att)
                ctx.log.error("policy_fail_final", code=e.code, job_id=ctx.job_id, detail=e.detail)
                raise
            dest = reject_artifacts(ctx, e, len(att["history"]) + 1)
            att["history"].append(
                {"code": e.code, "detail": e.detail, "rejected_to": str(dest), "cause": cause}
            )
            att["causes"] = counts  # an old one-counter file is written in the new form, with nothing added
            if n >= MAX_REGENERATIONS:
                ctx.write_json(ATTEMPTS_FILE, att)
                ctx.log.error(
                    "policy_fail_final", code=e.code, cause=cause, job_id=ctx.job_id, detail=e.detail
                )
                raise
            counts[cause] = n + 1
            att["regenerations"] = _count(att.get("regenerations", 0)) + 1
            ctx.write_json(ATTEMPTS_FILE, att)
            ctx.log.warn("policy_regenerate", code=e.code, cause=cause, restart_at=start, job_id=ctx.job_id)
            only = REGEN_ONLY.get(e.code)
            if not (only and invalidate_only(ctx, names, only)):
                invalidate_from(ctx, names, start)
            first = False
