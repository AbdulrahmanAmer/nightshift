"""Channel profile validation with plain-English errors and distinctness warnings (component C9).

Mirrors what C7's channel CRUD will check; replace with its function when it lands.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from ns_common.models import Channel

POOL_NAMES = (
    "hooks", "structures", "pov_tones", "pacings", "caption_styles", "visual_treatments", "title_formulas"
)
MIN_POOL = 3
WINDOW_RE = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})$")


def _friendly(err: dict[str, Any]) -> dict[str, str]:
    field = ".".join(str(p) for p in err.get("loc", ()))
    kind = err.get("type", "")
    ctx = err.get("ctx") or {}
    msg = str(err.get("msg", "Invalid value"))
    if kind == "missing":
        msg = "This field is required."
    elif kind == "string_pattern_mismatch":
        msg = (
            "Use lowercase letters, numbers and single dashes (for example after-dark)."
            if field == "slug"
            else "The format is not right."
        )
    elif kind in ("greater_than_equal", "greater_than"):
        msg = f"Must be at least {ctx.get('ge', ctx.get('gt', ''))}."
    elif kind in ("less_than_equal", "less_than"):
        msg = f"Must be at most {ctx.get('le', ctx.get('lt', ''))}."
    elif kind == "literal_error":
        msg = f"Pick one of: {ctx.get('expected', 'the listed options')}."
    elif kind == "extra_forbidden":
        msg = "This setting is not known."
    elif msg.startswith("Value error, "):
        msg = msg[len("Value error, "):]
    return {"field": field, "message": msg}


def _jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = {x.casefold() for x in a}, {x.casefold() for x in b}
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def _warnings(ch: Channel, others: list[Channel]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    pools = ch.pools.model_dump()
    for name in POOL_NAMES:
        options: list[str] = pools[name]
        if len(options) < MIN_POOL:
            out.append(
                {
                    "field": f"pools.{name}",
                    "message": f"Only {len(options)} option(s); add at least {MIN_POOL} for variety.",
                }
            )
        if len({o.casefold() for o in options}) != len(options):
            out.append({"field": f"pools.{name}", "message": "Some options are listed twice."})
    m = WINDOW_RE.match(ch.publish_window)
    if m:
        a, b = int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4])
        if int(m[1]) > 23 or int(m[3]) > 23 or int(m[2]) > 59 or int(m[4]) > 59:
            out.append({"field": "publish_window", "message": "Use times like 18:00-21:00."})
        elif b <= a:
            out.append({"field": "publish_window", "message": "The window should end after it starts."})
    if ch.critic_bar >= 9.5:
        out.append({"field": "critic_bar", "message": "A bar this high will reject most stories."})
    if ch.made_for_kids:
        msg = "Made-for-kids videos lose comments and personalised ads."
        out.append({"field": "made_for_kids", "message": msg})
    for other in others:
        if other.slug == ch.slug:
            continue
        sims = [_jaccard(pools[n], other.pools.model_dump()[n]) for n in POOL_NAMES]
        name = other.display_name
        if sum(sims) / len(sims) >= 0.75:
            msg = f"These pools are almost the same as '{name}'; look-alike channels may look duplicated."
            out.append({"field": "pools", "message": msg})
        if ch.niche.strip().casefold() == other.niche.strip().casefold() and ch.language == other.language:
            out.append({"field": "niche", "message": f"Same niche and language as '{name}'."})
        if ch.seed_keywords and _jaccard(ch.seed_keywords, other.seed_keywords) >= 0.6:
            out.append({"field": "seed_keywords", "message": f"Most keywords are shared with '{name}'."})
    return out


def validate_profile(raw: dict[str, Any], others: list[Channel] | None = None) -> dict[str, Any]:
    """`{ok, errors, warnings}` for a draft profile; `others` are the channels already saved."""
    try:
        ch = Channel.model_validate(raw)
    except ValidationError as e:
        return {"ok": False, "errors": [_friendly(dict(x)) for x in e.errors()], "warnings": []}
    warnings = _warnings(ch, others or [])
    errors = [w for w in warnings if w["field"] == "publish_window" and w["message"].startswith("Use times")]
    if errors:
        return {"ok": False, "errors": errors, "warnings": [w for w in warnings if w not in errors]}
    return {"ok": True, "errors": [], "warnings": warnings}
