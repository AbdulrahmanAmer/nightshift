"""Loopback-only checks and path safety (component C9)."""

from __future__ import annotations

from pathlib import Path

# The Host and Origin rule is shared with the engine and the voice service (ns_common.loopback).
from ns_common.loopback import LOOPBACK_NAMES, is_loopback_host, is_loopback_origin

__all__ = ["LOOPBACK_NAMES", "is_loopback_host", "is_loopback_origin", "is_within", "safe_join"]


def safe_join(root: Path, *parts: str) -> Path:
    """Join `parts` under `root` and return the resolved path; ValueError if it escapes `root`."""
    base = root.resolve()
    target = base.joinpath(*parts).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"path escapes {base}: {target}")
    return target


def is_within(root: Path, candidate: Path) -> bool:
    """True when `candidate` resolves to `root` or something inside it."""
    base = root.resolve()
    target = candidate.resolve()
    return target == base or base in target.parents
