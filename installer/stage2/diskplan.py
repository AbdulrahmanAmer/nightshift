"""The space an install needs, for the console wizard's steps (H-6). One place the steps ask.

The answers are not resolved yet when the home folder is picked, so this reads the raw answers (the same keys
the steps read later) and the machine facts of stage 1. ``ns_common.diskneed`` does the sum.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from installer.stage2.sysinfo import llama_pair_for
from ns_common import diskneed

if TYPE_CHECKING:
    from installer.stage2.context import StepContext


def ctx_need(
    ctx: StepContext, home: Path, *, tier: str | None = None, custom: dict[str, Any] | None = None
) -> diskneed.Need:
    """What this install would use on the volume of ``home``, less what is already there.

    ``tier`` and ``custom`` (the story model the owner named) default to the raw answers. A dev install is the
    fixed ``DEV_INSTALL_BYTES``; the engine test with ``fake_voice`` leaves out the voice models and engine.
    """
    raw = ctx.raw_answers
    if custom is None and not ctx.dev_mode:
        found = raw.get("llm_custom")
        custom = found if isinstance(found, dict) else None
    pf = ctx.preflight or {}
    profile = ctx.profile
    pair = (
        None
        if profile == "cpu-dev"
        else (str(pf.get("llama_pair") or "") or llama_pair_for(str(pf.get("cuda") or "")) or "cuda13")
    )
    source = raw.get("models_source") or None
    return diskneed.install_need(
        ctx.manifest.model_dump(mode="json"),
        home,
        profile=profile,
        tier=tier or str(raw.get("llm_tier") or "small"),
        custom=None if ctx.dev_mode else custom,
        include_code=raw.get("install_code") is not False,
        llama_pair=pair,
        voice=not (raw.get("fake_voice") is True and not ctx.dev_mode),
        voicedesign=raw.get("include_voicedesign") is True,
        models_source=None if ctx.dev_mode else source,
        dev=ctx.dev_mode,
    )


def free_bytes_of(ctx: StepContext, home: Path) -> int | None:
    """Free bytes at ``home`` from the context's reader (tests inject one); None when it cannot be read."""
    try:
        return int(ctx.free_gb_fn(home) * diskneed.GIB)
    except (OSError, ValueError) as e:
        ctx.log.warn("disk_free_unknown", f"cannot read the free space for {home}: {type(e).__name__}: {e}")
        return None
