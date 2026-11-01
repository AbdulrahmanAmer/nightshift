"""Policy text gate for the Series Workshop (component C11; spec 23.4, 18.3, 18.4).

Runs on concepts, the part map and every script before any writing time or production is spent. The
deterministic checks (block list, misleading claims) run first; the LLM check is `policy_check.md` through
`prompts.call_json`, as C5 documented. Codes: NS-E601 unsafe topic, NS-E602 real person/brand/event,
NS-E603 misleading claim, NS-E604 dishonest title or hook, NS-E607 Short does not stand alone.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import cache

from ns_common.errors import NsError
from ns_common.paths import contracts_dir
from ns_studio.context import JobContext
from ns_studio.text.models_local import PolicyCheckResult
from ns_studio.text.prompts import call_json, parse_model


@cache
def policy_list(name: str) -> tuple[str, ...]:
    """Lower-cased entries of `contracts/policy/<name>` (comments and blank lines dropped)."""
    lines = (contracts_dir() / "policy" / name).read_text(encoding="utf-8").splitlines()
    return tuple(ln.strip().lower() for ln in lines if ln.strip() and not ln.lstrip().startswith("#"))


def _hits(text: str, phrases: Iterable[str]) -> list[str]:
    low = text.lower()
    return [p for p in phrases if re.search(rf"(?<!\w){re.escape(p)}(?!\w)", low)]


def real_entity_hits(text: str) -> list[str]:
    """Block-list names (real people, brands, events) that occur in `text`."""
    return _hits(text, policy_list("real_entities_block.txt"))


def claim_hits(text: str) -> list[str]:
    """Misleading-claim phrases ("true story", ...) that occur in `text`."""
    return _hits(text, policy_list("misleading_claims.txt"))


def entities_gate(text: str, *, what: str) -> None:
    """NS-E602 when `text` names anything on the real-entity block list."""
    hits = real_entity_hits(text)
    if hits:
        raise NsError("NS-E602", f"{what}: names a real person, brand or event: {', '.join(hits)}")


def claims_gate(texts: Iterable[str], *, what: str) -> None:
    """NS-E603 when a title, hook or other metadata string claims the fiction is real."""
    hits = [h for t in texts for h in claim_hits(t)]
    if hits:
        raise NsError("NS-E603", f"{what}: misleading claim {', '.join(sorted(set(hits)))}")


def text_gate(
    ctx: JobContext,
    text: str,
    *,
    what: str,
    titles: Iterable[str] = (),
    title: str | None = None,
    hook: str | None = None,
) -> None:
    """Full text gate: block list (E602), claims in `titles` (E603), then the LLM classifier.

    The classifier may answer E602 (category `real_people`), E601 (any other category) or E604 (when a
    `title` or `hook` was given and it promises what the text does not deliver).
    """
    titles = list(titles)
    entities_gate("\n".join([text, *titles]), what=what)
    claims_gate(titles, what=what)
    extra = {k: v for k, v in (("title", title), ("hook", hook)) if v}
    res = parse_model(PolicyCheckResult, call_json(ctx, "policy_check", story=text, **extra), "policy_check")
    if "real_people" in res.unsafe_categories:
        raise NsError("NS-E602", f"{what}: {res.notes}".strip())
    if res.unsafe_categories:
        raise NsError("NS-E601", f"{what}: {', '.join(res.unsafe_categories)}; {res.notes}".strip())
    if extra and (not res.hook_honest or not res.title_honest):
        raise NsError("NS-E604", f"{what}: {res.notes}".strip())


def is_standalone(ctx: JobContext, *, story: str, moment: str) -> bool:
    """True unless the classifier says the selected moment needs outside context (spec 23.4)."""
    res = parse_model(
        PolicyCheckResult,
        call_json(ctx, "policy_check", story=story, span_text=moment),
        "policy_check standalone",
    )
    return res.standalone is not False
