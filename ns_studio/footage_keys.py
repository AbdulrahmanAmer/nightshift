"""The stock-footage key checks that run before any GPU work (component C7, gap L3; card stock-truth).

Every story video needs stock clips, and the clip search is the stage `clips`, after the LLM, the voice and
the captions (30 to 90 minutes). Without a key that stage fails with a Pexels or Pixabay API error. The
nightly planner, `studio run` and the dashboard refuse the job up front instead, with `NS-E502`.

A key that is stored but wrong or revoked was found only at that stage too. `precheck` is the ONE check
before a job begins (the nightly's `_attempt`, `studio run` and its `--resume`, Create and Retry through
`studio_bridge`), for a job that will still fetch stock footage (`needs_stock`, the one question): ONE
bounded request per site with a stored key (`check_key` of the source:
at most `stock_answer.TIMEOUT_S`, no retry), read by `ns_common.stock_answer`, its answer kept in
`cache/search/stock-keys.json` for `KNOWN_TTL_S` (ok or wrong: a night of several jobs asks once) or
`UNSURE_TTL_S` (busy, failing, out of reach, not understood). The cache is keyed by a hash of the key, so a
key pasted again in Settings is asked about at the next job. It sits beside the night's time decision
(`nightfit.decide` owns what a night begins); it decides nothing about time, only whether footage can be had.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from ns_common import stock_answer as sa
from ns_common.atomic import read_text_retry, write_text_atomic
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.paths import Paths

NO_FOOTAGE_KEY = "No stock footage key. Add a Pixabay key (free) in Settings > Stock footage keys"
CHECK_FILE = "stock-keys.json"
#: How long an answer that says something about the key (ok, wrong) is believed: longer than a night.
KNOWN_TTL_S = 12 * 3600
#: How long a "could not be checked now" answer stands before the next job asks again.
UNSURE_TTL_S = 30 * 60
#: Said after the sentence of a wrong key that stopped a job (the way out follows it).
NOT_STARTED = "The job did not start; no model was loaded."


class HasFootageKey(Protocol):
    """Anything that knows whether a stock-footage key is stored (`ns_studio.context.Services`)."""

    footage_key: bool


class HasSources(HasFootageKey, Protocol):
    """`ns_studio.context.Services`: the key flag and the clip sources."""

    clip_sources: Any


def has_key(keys: Mapping[str, str]) -> bool:
    """True when at least one of the Pexels and Pixabay keys is stored."""
    return any(bool(v) for v in keys.values())


def require_footage_key(services: HasFootageKey) -> None:
    """Raise `NS-E502` when no stock-footage key is stored (a fake clip source needs none)."""
    if not services.footage_key:
        raise NsError("NS-E502", NO_FOOTAGE_KEY)


def _read_cache_file(f: Path) -> dict[str, Any]:
    """The kept answers; a missing, torn or foreign file is an empty cache (the next job asks again)."""
    try:
        data = json.loads(read_text_retry(f))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_cache(paths: Paths) -> dict[str, Any]:
    return _read_cache_file(paths.cache_search / CHECK_FILE)


def _fresh(entry: Any, key_id: str, now: float) -> sa.Answer | None:
    """The kept answer for this key while it is young enough, else None."""
    if not isinstance(entry, dict) or entry.get("key_id") != key_id or entry.get("kind") not in sa.KINDS:
        return None
    try:
        age = now - float(entry["at"])
    except (KeyError, TypeError, ValueError):
        return None
    ttl = KNOWN_TTL_S if entry["kind"] in sa.CHECKED else UNSURE_TTL_S
    if not 0 <= age < ttl:  # a clock set back counts as old
        return None
    return sa.Answer(str(entry.get("provider") or ""), str(entry["kind"]), str(entry.get("why") or ""))


def check_stock_keys(
    services: HasSources, paths: Paths, log: NsLogger, *, now: float | None = None
) -> tuple[list[sa.Answer], list[sa.Answer]]:
    """`(answers, asked)`: one answer per real source with a key (the fake source has none), from the cache
    while fresh, else from ONE request each; `asked` are those that came from a request just now."""
    from ns_studio.media.clip_sources import keyed

    sources = keyed(list(services.clip_sources))
    if not sources:
        return [], []
    now = time.time() if now is None else now
    cache = _read_cache(paths)
    answers: list[sa.Answer] = []
    asked: list[sa.Answer] = []
    for src in sources:
        kept = _fresh(cache.get(src.name), src.key_id, now)
        if kept is not None:
            answers.append(sa.Answer(src.name, kept.kind, kept.why))
            continue
        a = src.check_key()
        log.info("stock_key_checked", source=src.name, kind=a.kind, code=a.code, why=a.why)
        cache[src.name] = {
            "provider": src.name, "key_id": src.key_id, "kind": a.kind, "why": a.why, "at": now,
        }
        answers.append(a)
        asked.append(a)
    if asked:
        try:
            write_text_atomic(paths.cache_search / CHECK_FILE, json.dumps(cache, indent=2) + "\n")
        except OSError as e:  # not kept: the next job asks again, which costs one request
            log.warn("stock_key_cache_not_written", str(e))
    return answers, asked


def wrong_key_note(wrong: list[sa.Answer], answers: list[sa.Answer]) -> str:
    """The note for the owner when a site refused a stored key: what stops, and the way out."""
    names = " and ".join(a.name for a in wrong)
    one = len(wrong) == 1
    first = (f"{names} did not accept the key stored on this PC: it is wrong or was revoked." if one
             else f"{names} did not accept the keys stored on this PC: they are wrong or were revoked.")
    if len(wrong) == len(answers):
        from ns_studio.ownership import IF_NIGHTLY_ON  # " (while Automatic runs are on in Settings)"

        return (f"{first} No video that needs stock footage starts until it is fixed: paste the key again in "
                f"{sa.KEYS_PLACE}. The next video you start, and the next nightly run{IF_NIGHTLY_ON}, check "
                "it again.")
    way = f"paste the key again in {sa.KEYS_PLACE} (or clear it there)"
    usable = " and ".join(a.name for a in answers if a.ok)
    if usable:
        return f"{first} Videos use {usable} footage only until you {way}."
    unsure = " and ".join(a.name for a in answers if not a.checked)
    return (f"{first} {unsure} could not be checked now; videos use its footage if it answers. To use both "
            f"sites again, {way}.")


def _leave_note(paths: Paths, log: NsLogger, message: str) -> None:
    """The note through the notes' one owner (`ns_supervisor.notify`), unless Settings turned notes off; not
    added again while the same words are waiting."""
    from ns_studio import notifications

    if not notifications.notifications_enabled(paths):
        return
    try:
        from ns_supervisor import notify

        notify.leave_note(paths, notifications.TITLE, message,
                          once=lambda notes: any(n["message"] == message for n in notes))
    except Exception as e:  # noqa: BLE001 - a note never stops the check; the error and the log say it
        log.warn("morning_note_failed", str(e))


#: The stages that search or download stock footage (each builds a `Searcher` over the clip sources):
#: `clips` (a Short's parts, and the master's through `run_clips_master`) and `shorts_derive` (fresh Shorts).
STOCK_STAGES = ("clips", "shorts_derive")


def needs_stock(services: HasSources, fmt: str, job_dir: Path | None = None) -> bool:
    """The ONE question before a job (the nightly, `studio run`, Create and Retry ask it alike): will this job
    still fetch stock footage? Answered from the job's own plan and stage record (`stage_state.json`):

    | the job | answer |
    |---|---|
    | the clip sources are placeholders (fake services, dev install, `fake_footage` engine test) | no |
    | its plan has no stock stage (`STOCK_STAGES`) | no |
    | a new job (`job_dir` None) whose plan has one | yes |
    | a resumed job with a stock stage not recorded `done` | yes |
    | a resumed job whose stock stages are all recorded `done` | no |
    | a resumed job whose stage record cannot be read, or whose format nothing names | yes (the safe side) |

    The Clipper (own footage) never asks: it builds no story job. A stock stage whose inputs changed since it
    was recorded done reruns; its own search then tells a refused key with the same classifier."""
    from ns_studio.media.clip_sources import keyed
    from ns_studio.orchestrator import plan_format, plan_names
    from ns_studio.stages import StageState

    sources = list(services.clip_sources)
    if sources and not keyed(sources) and services.footage_key:  # placeholders only: no stock is fetched
        return False
    if job_dir is not None and not fmt:  # a record that names no format: the job's own `job.json` does
        fmt = str(_read_cache_file(job_dir / "job.json").get("fmt") or "")
        if not fmt:
            return True  # nothing tells: the safe side (the check only asks)
    key = plan_format(fmt, job_dir) if job_dir is not None else fmt
    stock = [n for n in plan_names(key, True) if n in STOCK_STAGES]
    if not stock:
        return False
    if job_dir is None:
        return True
    try:
        entries = StageState.load(job_dir).entries
    except (OSError, ValueError):
        return True
    return any(not isinstance(entries.get(n), dict) or entries[n].get("status") != "done" for n in stock)


def stock_key_id(provider: str, key: str) -> str:
    """A short hash that tells two keys apart (the check's cache); the key cannot be read back from it."""
    return hashlib.sha256(f"stock-key|{provider}|{key}".encode()).hexdigest()[:16]


def remember_refused(cache_dir: Path, provider: str, key_id: str, why: str, now: float | None = None) -> bool:
    """A search found the key refused (the `clips` stage, after an earlier "ok" was kept): correct the kept
    answer, so the next job stops at its start instead of failing at `clips` again. Only an entry for this
    very key in an existing check file is changed (a test source's folder gets no file); True when changed."""
    f = cache_dir / CHECK_FILE
    if not f.is_file():
        return False
    cache = _read_cache_file(f)
    entry = cache.get(provider)
    if not isinstance(entry, dict) or entry.get("key_id") != key_id or entry.get("kind") == sa.KEY_WRONG:
        return False
    cache[provider] = {**entry, "kind": sa.KEY_WRONG, "why": why, "at": time.time() if now is None else now}
    try:
        write_text_atomic(f, json.dumps(cache, indent=2) + "\n")
    except OSError:
        return False
    return True


def refused_sentence(paths: Paths, keys: Mapping[str, str], *, now: float | None = None) -> str | None:
    """What the kept answers already say about the stored `keys` (no request): the sentence of a refused key,
    for an answer given before the job runs (the dashboard's Create), else None."""
    cache = _read_cache(paths)
    now = time.time() if now is None else now
    wrong = [a for p, k in keys.items() if k and (a := _fresh(cache.get(p), stock_key_id(p, k), now))
             and a.kind == sa.KEY_WRONG]
    if not wrong:
        return None
    return " ".join(f"{sa.NAMES.get(a.provider, a.provider)} did not accept the key stored on this PC "
                    f"({a.why}): it is wrong or was revoked." for a in wrong) + (
        f" Paste it again in {sa.KEYS_PLACE}.")


def precheck(
    services: HasSources, paths: Paths, log: NsLogger, *, fmt: str, job_dir: Path | None = None,
    now: float | None = None,
) -> list[sa.Answer]:
    """The check before a job begins, for a job that will still fetch stock footage (`needs_stock`; any
    other job passes untouched, a missing key too). Raises NS-E502 when no key is stored, and the refused
    key's code (NS-E502 before NS-E501) when EVERY site with a stored key said its key is wrong: no job is
    made and no model loads. A rate limit, a failing site, no connection or an answer not understood does
    not stop the job (the `clips` stage's own retries handle it then). A key refused when it was just
    asked about is told in a note (at most once per `KNOWN_TTL_S` per key); one refused key beside a usable
    site lets the job run on that site."""
    if not needs_stock(services, fmt, job_dir):
        log.info("stock_check_not_needed", fmt=fmt, resumed=job_dir is not None)
        return []
    require_footage_key(services)
    answers, asked = check_stock_keys(services, paths, log, now=now)
    wrong = [a for a in answers if a.kind == sa.KEY_WRONG]
    if any(a.kind == sa.KEY_WRONG for a in asked):
        _leave_note(paths, log, wrong_key_note(wrong, answers))
    for a in answers:
        if not a.checked:
            log.warn("stock_key_unchecked", source=a.provider, kind=a.kind, code=a.code, why=a.why)
    if answers and len(wrong) == len(answers):
        first = next((a for a in wrong if a.provider == "pixabay"), wrong[0])
        log.error("stock_key_refused", source=first.provider, code=first.code)
        raise first.error(NOT_STARTED)
    return answers
