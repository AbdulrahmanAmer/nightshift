"""Engine settings as the Settings page edits them: flat dotted keys with plain values (component C9).

`runtime.json` is nested (`llama.flags.ctx`, `video.encoder`, ...). The page lists one row per plain value, so
the API shows `flags` as `{"llama.flags.ctx": 16384, "video.encoder": "h264_nvenc"}` and takes the same shape
back. Lists and empty values are not shown (and not touched); a value sent as text takes the type of the
setting it replaces, so "8192" stays a number and "false" a boolean.
"""

from __future__ import annotations

from typing import Any

#: Top-level keys the page does not edit as engine flags.
HIDDEN = ("profile", "supervisor_port", "dev_mode")


def flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Plain (str, int, float, bool) leaves of `data` under dotted keys."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if not prefix and key in HIDDEN:
            continue
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten(value, f"{name}."))
        elif isinstance(value, str | int | float | bool):
            out[name] = value
    return out


def _coerce(value: Any, current: Any) -> Any:
    """`value` as the type of `current` when it came as text; anything else is left to validation."""
    if not isinstance(value, str) or current is None:
        return value
    try:
        if isinstance(current, bool):
            low = value.strip().lower()
            if low in ("true", "1", "yes", "on"):
                return True
            if low in ("false", "0", "no", "off"):
                return False
            return value
        if isinstance(current, int):
            return int(value.strip())
        if isinstance(current, float):
            return float(value.strip())
    except ValueError:
        return value
    return value


def apply_flags(current: dict[str, Any], flags: dict[str, Any]) -> dict[str, Any]:
    """`current` with the dotted `flags` (or nested objects) written into a copy."""
    merged = _copy(current)
    for key, value in flags.items():
        if isinstance(value, dict):  # a nested object: merge it key by key
            for sub, v in flatten(value, f"{key}.").items():
                _set(merged, sub, v)
        else:
            _set(merged, key, value)
    return merged


def _copy(data: dict[str, Any]) -> dict[str, Any]:
    return {k: _copy(v) if isinstance(v, dict) else v for k, v in data.items()}


def _set(root: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = root
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = _coerce(value, node.get(parts[-1]))
