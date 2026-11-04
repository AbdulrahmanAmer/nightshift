"""Dev install switches (docs/DEV_MODE.md): one place that says whether this install runs on fake engines.

`INSTALL.cmd --dev` installs the real program with stub model files, no GPU packages and fake engines. The
installer writes `dev_mode: true` into `config/runtime.json`; everything that must behave differently reads it
here. Standard library only, so the installer's bare Python can import it. A missing or unreadable
`runtime.json` is a real install (False).
"""

from __future__ import annotations

import json
from pathlib import Path

from ns_common.paths import Paths

#: The services `ns_studio.context.FAKEABLE` can replace with in-process fakes (a test keeps them equal).
DEV_FAKES: tuple[str, ...] = ("llm", "tts", "whisper", "embed", "clips")

#: What a dev install tells the owner, in the setup window's footer and on the Done screen.
BANNER = "DEV MODE: fake engines, no models"


def runtime_is_dev(runtime_file: Path) -> bool:
    """True when the `runtime.json` at `runtime_file` says `"dev_mode": true`."""
    try:
        data = json.loads(runtime_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("dev_mode") is True


def is_dev(paths: Paths) -> bool:
    """True when this install (`NIGHTSHIFT_HOME`) is a dev install."""
    return runtime_is_dev(paths.runtime_file)


def dev_fakes(paths: Paths) -> frozenset[str]:
    """The services to fake: all of `DEV_FAKES` in a dev install, none otherwise."""
    return frozenset(DEV_FAKES) if is_dev(paths) else frozenset()
