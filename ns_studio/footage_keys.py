"""The stock-footage key check that runs before any GPU work (component C7, gap L3).

Every story video needs stock clips, and the clip search is the stage `clips`, after the LLM, the voice and
the captions (30 to 90 minutes). Without a key that stage fails with a Pexels or Pixabay API error. The
nightly planner, `studio run` and the dashboard refuse the job up front instead, with `NS-E502`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from ns_common.errors import NsError

NO_FOOTAGE_KEY = "No stock footage key. Add a Pixabay key (free) in Settings > Stock footage keys"


class HasFootageKey(Protocol):
    """Anything that knows whether a stock-footage key is stored (`ns_studio.context.Services`)."""

    footage_key: bool


def has_key(keys: Mapping[str, str]) -> bool:
    """True when at least one of the Pexels and Pixabay keys is stored."""
    return any(bool(v) for v in keys.values())


def require_footage_key(services: HasFootageKey) -> None:
    """Raise `NS-E502` when no stock-footage key is stored (a fake clip source needs none)."""
    if not services.footage_key:
        raise NsError("NS-E502", NO_FOOTAGE_KEY)
