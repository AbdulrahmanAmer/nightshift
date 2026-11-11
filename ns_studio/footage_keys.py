"""The stock-footage key checks that run before any GPU work (component C7, gap L3; card stock-truth).

Every story video needs stock clips, and the clip search is the stage `clips`, after the LLM, the voice and
the captions (30 to 90 minutes). Without a key that stage fails with a Pexels or Pixabay API error. The
nightly planner, `studio run` and the dashboard refuse the job up front instead, with `NS-E502`.

A key that is stored but wrong or revoked was found only at that stage too. `precheck` is the ONE check
before a job that needs stock footage begins (the nightly's `_attempt`, `studio run`, Create and Retry of a
new video through `studio_bridge`): ONE bounded request per site with a stored key (`check_key` of the source:
at most `stock_answer.TIMEOUT_S`, no retry), read by `ns_common.stock_answer`, its answer kept in
`cache/search/stock-keys.json` for `KNOWN_TTL_S` (ok or wrong: a night of several jobs asks once) or
`UNSURE_TTL_S` (busy, failing, out of reach, not understood). The cache is keyed by a hash of the key, so a
key pasted again in Settings is asked about at the next job. It sits beside the night's time decision
(`nightfit.decide` owns what a night begins); it decides nothing about time, only whether footage can be had.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
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


def _read_cache(paths: Paths) -> dict[str, Any]:
    """The kept answers; a missing, torn or foreign file is an empty cache (the next job asks again)."""
    try:
        data = json.loads(read_text_retry(paths.cache_search / CHECK_FILE))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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
        return (f"{first} No video that needs stock footage starts until it is fixed: paste the key again in "
                f"{sa.KEYS_PLACE}. The next video you start, and the next nightly run (while Automatic runs "
                "are on in Settings), check it again.")
    others = " and ".join(a.name for a in answers if a.kind != sa.KEY_WRONG)
    return (f"{first} Videos use {others} footage only until you paste the key again in {sa.KEYS_PLACE} "
            "(or clear it there).")


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


def precheck(
    services: HasSources, paths: Paths, log: NsLogger, *, now: float | None = None
) -> list[sa.Answer]:
    """The check before a job that needs stock footage begins. Raises NS-E502 when no key is stored, and the
    refused key's code (NS-E502 before NS-E501) when EVERY site with a stored key said its key is wrong: no
    job is made and no model loads. A rate limit, a failing site, no connection or an answer not understood
    does not stop the job (the `clips` stage's own retries handle it then). A key refused when it was just
    asked about is told in a note (at most once per `KNOWN_TTL_S` per key); one refused key beside a usable
    site lets the job run on that site."""
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
