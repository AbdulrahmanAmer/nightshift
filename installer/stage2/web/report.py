"""What the owner sees from far away: the one-line status and ``install-report.json`` (journey 10).

Pure functions over the SetupState dict and a few files; standard library only. The controller decides when to
call them (at every failure, stop and finish, and when a diagnostics zip is made).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

SCHEMA = 1
REPORT_NAME = "install-report.json"
DONE_STEP = ("done", "skipped", "warning")


def scrub_answers(raw: dict[str, Any]) -> dict[str, Any]:
    """A copy of an answers dict without any API key (every field whose name ends in ``_api_key``)."""
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k.endswith("_api_key"):
            continue
        out[k] = scrub_answers(v) if isinstance(v, dict) else v
    return out


def aware(value: datetime) -> datetime:
    """A timezone-aware datetime: a naive one is taken as local time (the setup window used to write naive
    local stamps while the console wizard wrote UTC ones, and subtracting the two raised TypeError, which
    stopped the window from starting over a console-finished install)."""
    return value if value.tzinfo is not None else value.astimezone()


def span_seconds(start_iso: str | None, end_iso: str | None) -> int | None:
    """Whole seconds between two ISO stamps of any mix of naive/aware; None when either is missing or bad."""
    if not start_iso or not end_iso:
        return None
    try:
        a = aware(datetime.fromisoformat(start_iso))
        b = aware(datetime.fromisoformat(end_iso))
    except ValueError:
        return None
    return max(0, int((b - a).total_seconds()))


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def clock(seconds: float) -> str:
    """``12:03`` under an hour, ``1:02:03`` from an hour on."""
    s = max(0, int(seconds))
    h, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def elapsed_s(state: dict[str, Any], now: datetime | None = None) -> int | None:
    """Seconds since Start, or None when setup has not started."""
    started = _parse(state.get("startedAt"))
    if started is None:
        return None
    return max(0, int((aware(now or datetime.now()) - aware(started)).total_seconds()))


def current_step(state: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
    """(1-based position, step) of the failed step, else the first step that is not finished."""
    steps = state["steps"]
    failure = state.get("failure")
    if failure:
        for i, s in enumerate(steps):
            if s["id"] == failure.get("step"):
                return i + 1, s
    for i, s in enumerate(steps):
        if s["status"] not in DONE_STEP:
            return i + 1, s
    return None


def percent(state: dict[str, Any]) -> int:
    """Download progress while downloading, otherwise finished steps out of all steps."""
    if state.get("phase") == "done" or state.get("run") == "finished":
        return 100
    overall = (state.get("downloads") or {}).get("overall") or {}
    total = overall.get("bytesTotal") or 0
    if state.get("phase") == "downloading" and total > 0:
        return min(99, int(100 * (overall.get("bytesDone") or 0) / total))
    steps = state["steps"]
    done = sum(1 for s in steps if s["status"] in DONE_STEP)
    return int(100 * done / len(steps)) if steps else 0


def status_line(state: dict[str, Any], last_error: dict[str, Any] | None, now: datetime | None = None) -> str:
    """One line the partner can paste to the owner.

    ``Nightshift setup 0.1.7 · step 6/14 Python environments · 41% · 12:03 elapsed · last error NS-E105``
    """
    parts = [f"Nightshift setup {state.get('version', '?')}"]
    run, total = state.get("run"), len(state["steps"])
    at = current_step(state)
    if run == "finished" or at is None and state.get("phase") == "done":
        parts.append(f"finished, {total} of {total} steps")
    elif at is not None:
        n, step = at
        where = f"step {n}/{total} {step['title']}"
        failed = state.get("failure") and run == "waiting"
        if failed:
            where = f"STOPPED at {where}"
        elif run == "stopped":
            where = f"stopped at {where}"
        elif run in ("paused", "pausing"):
            where += " (paused)"
        elif run == "idle":
            where = f"not started, next {where}"
        parts.append(where)
    parts.append(f"{percent(state)}%")
    secs = elapsed_s(state, now)
    if secs is not None:
        parts.append(f"{clock(secs)} elapsed")
    if last_error:
        title = last_error.get("title") or ""
        parts.append(f"last error {last_error.get('code')}" + (f" ({title})" if title else ""))
    return " · ".join(parts)


def step_table(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every step with its status and how long it took (None when it did not run in this session)."""
    out: dict[str, dict[str, Any]] = {}
    for s in state["steps"]:
        a, b = _parse(s.get("startedAt")), _parse(s.get("finishedAt"))
        seconds = round((b - a).total_seconds(), 1) if a and b and b >= a else None
        out[s["id"]] = {"status": s["status"], "seconds": seconds}
        if s.get("detail") and s["status"] in ("skipped", "warning", "failed"):
            out[s["id"]]["detail"] = s["detail"]
    return out


def build_install_report(
    state: dict[str, Any],
    *,
    outcome: str,
    last_error: dict[str, Any] | None,
    attempts: dict[str, int],
    flags: dict[str, Any],
    runtime: dict[str, Any],
    answers: dict[str, Any] | None,
    free_gb: float | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The content of ``install-report.json``. ``outcome`` is ``done``, ``failed`` or ``stopped``."""
    when = now or datetime.now()
    m = state.get("machine") or {}
    failure = state.get("failure")
    failed_step = (failure or {}).get("step") or (last_error or {}).get("step")
    view = state.get("answers") or {}
    return {
        "schema": SCHEMA,
        "version": state.get("version"),
        "outcome": outcome,
        "startedAt": state.get("startedAt"),
        "finishedAt": when.isoformat(timespec="seconds"),
        "durationS": elapsed_s(state, when),
        "failedStep": failed_step if outcome != "done" else None,
        "code": (failure or {}).get("code") if outcome == "failed" else None,
        "attempts": attempts.get(str(failed_step), 0) if failed_step else 0,
        "lastError": last_error,
        "machine": {
            "windows": m.get("windows"),
            "gpu": m.get("gpuName"),
            "vramMb": m.get("vramMb"),
            "driver": m.get("driver"),
            "cuda": m.get("cuda"),
            "ramGb": m.get("ramGb"),
            "profile": m.get("profile"),
            "isAdmin": m.get("isAdmin"),
            "home": (view.get("homePath") or None),
            "freeGb": free_gb,
            "llamaPair": flags.get("llama_pair"),
        },
        "choices": {
            "tier": view.get("llmTier"),
            "llmCustom": view.get("llmCustom"),
            "modelsSource": (view.get("modelsSource") or {}).get("kind"),
            "runTime": view.get("runTime"),
            "installCode": view.get("installCode"),
            "includeVoicedesign": view.get("includeVoicedesign"),
        },
        "engines": {
            "tts": (runtime.get("tts") or {}).get("engine"),
            "ttsDevice": (runtime.get("tts") or {}).get("device"),
            "whisperDevice": (runtime.get("whisper") or {}).get("device"),
            "encoder": (runtime.get("video") or {}).get("encoder"),
            "ttsFallback": flags.get("tts_fallback"),
            "schedule": flags.get("schedule"),
            "scheduleMode": flags.get("schedule_mode"),
            "smoke": flags.get("smoke"),
        },
        "warnings": [n.get("code") for n in state.get("notices") or [] if n.get("code")],
        # designed or cloned voices that did not speak in the setup test (s08): the voice, why, the way out
        "voiceWarnings": [str(w) for w in flags.get("voice_warnings") or []],
        "steps": step_table(state),
        "answers": scrub_answers(answers) if answers else None,
        "statusLine": status_line(state, last_error, when),
    }
