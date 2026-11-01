"""Per-channel options that the Channel model does not list yet (spec 17.1 and 17.2).

The options are `formats_allowed` and `captions.mask_profanity`. The Channel model forbids unknown keys, so
`config.load_channel` rejects a profile that carries them until CR-C6-6 lands in `ns_common.models`. These
readers look at the model attribute first (so they keep working once the field exists) and fall back to the
channel YAML read as a plain mapping.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import yaml

from ns_common.models import Channel
from ns_common.paths import Paths


def _dig(data: Any, parts: Sequence[str]) -> Any:
    cur = data
    for p in parts:
        if isinstance(cur, Mapping):
            if p not in cur:
                return None
            cur = cur[p]
        else:
            cur = getattr(cur, p, None)
        if cur is None:
            return None
    return cur


def channel_setting(paths: Paths, channel: Channel, dotted: str, default: Any = None) -> Any:
    """A channel option such as `captions.mask_profanity`: model attribute, else channel YAML, else `default`.

    Unreadable or malformed files give `default`.
    """
    parts = dotted.split(".")
    found = _dig(channel, parts)
    if found is not None:
        return found
    try:
        f = paths.channel_file(channel.slug)
        if f.exists():
            raw = yaml.safe_load(f.read_text(encoding="utf-8"))
            found = _dig(raw, parts) if isinstance(raw, Mapping) else None
    except (OSError, yaml.YAMLError):
        found = None
    return default if found is None else found
