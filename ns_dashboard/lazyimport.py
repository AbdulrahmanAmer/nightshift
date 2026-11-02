"""`lazy`: resolve a name from another component when it is first needed (component C9)."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from ns_dashboard.errors import AdapterUnavailable


def lazy(module: str, attr: str) -> Callable[..., Any]:
    """Resolve `module.attr` or raise `AdapterUnavailable` with a message naming it."""
    try:
        mod = importlib.import_module(module)
        return getattr(mod, attr)
    except (ImportError, AttributeError) as e:
        raise AdapterUnavailable(f"{module}.{attr} is not installed yet ({e})") from e
