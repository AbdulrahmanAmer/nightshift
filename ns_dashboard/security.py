"""Loopback-only checks and path safety (component C9)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})


def _host_only(value: str) -> str:
    """Strip an optional port and IPv6 brackets from a Host header value, lower-cased."""
    host = value.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end != -1 else host
    if host.count(":") == 1:
        return host.split(":", 1)[0]
    return host


def is_loopback_host(host_header: str | None) -> bool:
    """True when a `Host` header names this machine (127.0.0.1, localhost, ::1), with or without port."""
    if not host_header:
        return False
    return _host_only(host_header) in LOOPBACK_NAMES


def is_loopback_origin(origin: str | None) -> bool:
    """True when `Origin` is absent (non-browser caller) or a loopback origin; `null` is refused."""
    if origin is None or origin.strip() == "":
        return True
    if origin.strip() == "null":
        return False
    return is_loopback_host(urlsplit(origin).netloc)


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
