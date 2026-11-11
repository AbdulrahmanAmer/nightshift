"""Dev install switches (docs/DEV_MODE.md): one place that says whether this install runs on fake engines.

`INSTALL.cmd --dev` installs the real program with stub model files, no GPU packages and fake engines. The
installer writes `dev_mode: true` into `config/runtime.json`; everything that must behave differently reads it
here. Standard library only, so the installer's bare Python can import it. A missing or unreadable
`runtime.json` is a real install (False).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from ns_common.atomic import read_text_retry
from ns_common.paths import Paths

#: The services `ns_studio.context.FAKEABLE` can replace with in-process fakes (a test keeps them equal).
DEV_FAKES: tuple[str, ...] = ("llm", "tts", "whisper", "embed", "clips")

#: What a dev install tells the owner, in the setup window's footer and on the Done screen.
BANNER = "DEV MODE: fake engines, no models"


def _runtime_flags(runtime_file: Path, *names: str) -> list[bool]:
    """For each name, whether the `runtime.json` at `runtime_file` says `"<name>": true`; one read.

    An unreadable or missing file means every answer is False.
    """
    try:
        data = json.loads(read_text_retry(runtime_file, "utf-8-sig"))
    except (OSError, ValueError):
        return [False] * len(names)
    if not isinstance(data, dict):
        return [False] * len(names)
    return [data.get(name) is True for name in names]


def runtime_is_dev(runtime_file: Path) -> bool:
    """True when the `runtime.json` at `runtime_file` says `"dev_mode": true`."""
    return _runtime_flags(runtime_file, "dev_mode")[0]


def is_dev(paths: Paths) -> bool:
    """True when this install (`NIGHTSHIFT_HOME`) is a dev install."""
    return runtime_is_dev(paths.runtime_file)


def fake_footage(paths: Paths) -> bool:
    """True when this install stands the fake clip source in for Pexels and Pixabay.

    A dev install does (`is_dev`); so does a real install whose owner set `"fake_footage": true` in
    `answers.json` for the engine test (docs/DEV_MODE.md, "Real engines, fake voice"): the real models run
    on a PC with no stock-footage account and the footage is placeholder. Callers that decide "is there
    footage" or "which clip sources" ask this, so the two cases cannot drift apart.
    """
    return any(_runtime_flags(paths.runtime_file, "dev_mode", "fake_footage"))


def dev_fakes(paths: Paths) -> frozenset[str]:
    """The services to fake: all of `DEV_FAKES` in a dev install, none otherwise."""
    return frozenset(DEV_FAKES) if is_dev(paths) else frozenset()


# ------------------------------------------------------------------------------ installs made for testing
def test_settings(data: object) -> bool:
    """True when the `runtime.json` content `data` (a parsed object) is that of a dev or an engine-test
    install: `dev_mode`, `fake_footage` or `tiny_story` is `true`, or the voice engine is the fake one."""
    if not isinstance(data, dict):
        return False
    if any(data.get(name) is True for name in ("dev_mode", "fake_footage", "tiny_story")):
        return True
    tts = data.get("tts")
    return isinstance(tts, dict) and tts.get("engine") == "fake"


def _runtime_data(paths: Paths) -> object:
    """The parsed `runtime.json` of `paths`; None when it is missing or not JSON."""
    try:
        return json.loads(read_text_retry(paths.runtime_file, "utf-8-sig"))
    except (OSError, ValueError):
        return None


def install_kind(paths: Paths) -> Literal["test", "normal", "unreadable"]:
    """`test` for a dev install and for an engine-test install (`test_settings`). `unreadable` when
    `runtime.json` is missing, cannot be read or is not a JSON object (it is not taken for a test install).
    `normal` for every other install. The one rule `NS_RELEASE_SOURCE`, `fake_gpu` and `NS_TEST_NIGHT_LIMIT_S`
    follow."""
    data = _runtime_data(paths)
    if not isinstance(data, dict):
        return "unreadable"
    return "test" if test_settings(data) else "normal"


def test_install(paths: Paths) -> bool:
    """True for a dev or an engine-test install (`install_kind`); an unreadable `runtime.json` is neither."""
    return install_kind(paths) == "test"


#: The simulated graphics card (docs/DEV_MODE.md, "A simulated graphics card"): the file a proof writes
#: the card's numbers into, under the home's `run` folder, and the variable that shortens the GPU lease.
FAKE_GPU_FILE = "fake_gpu.json"
FAKE_GPU_LEASE_ENV = "NS_FAKE_GPU_LEASE_S"
#: The lease times `NS_FAKE_GPU_LEASE_S` may set, in seconds (both ends taken).
FAKE_GPU_LEASE_MIN_S = 30
FAKE_GPU_LEASE_MAX_S = 600


def fake_gpu_set(data: object) -> bool:
    """True when the `runtime.json` content `data` says `"fake_gpu": true`, whatever the install."""
    return isinstance(data, dict) and data.get("fake_gpu") is True


def fake_gpu_in(data: object) -> bool:
    """True when the simulated graphics card is in effect for `runtime.json` content `data`: the switch is
    set AND the install is one made for testing. On any other install the switch does nothing (NS-W121)."""
    return fake_gpu_set(data) and test_settings(data)


def fake_gpu(paths: Paths) -> bool:
    """True when this install (`paths`) runs on the simulated graphics card (`fake_gpu_in` of its file)."""
    return fake_gpu_in(_runtime_data(paths))


def fake_gpu_ignored(paths: Paths) -> bool:
    """True when `runtime.json` sets `fake_gpu` on an install that is not made for testing (NS-W121)."""
    data = _runtime_data(paths)
    return fake_gpu_set(data) and not test_settings(data)


def fake_gpu_file(paths: Paths) -> Path:
    """`run\\fake_gpu.json` of the home: the simulated card's numbers (absent: the defaults)."""
    return paths.home / "run" / FAKE_GPU_FILE


def fake_gpu_lease_s(env: Mapping[str, str], default_s: float) -> tuple[float, str | None]:
    """The GPU lease time a proof asked for with `NS_FAKE_GPU_LEASE_S` (whole seconds, 30 to 600), and the
    sentence to log when the value was not taken (`default_s` is used then). Not set: `default_s`, None."""
    raw = env.get(FAKE_GPU_LEASE_ENV)
    if raw is None or not raw.strip():
        return default_s, None
    try:
        value = int(raw.strip())
    except ValueError:
        value = None
    if value is None or not FAKE_GPU_LEASE_MIN_S <= value <= FAKE_GPU_LEASE_MAX_S:
        why = (
            f"{FAKE_GPU_LEASE_ENV}={raw.strip()!r} is not a whole number of seconds from "
            f"{FAKE_GPU_LEASE_MIN_S} to {FAKE_GPU_LEASE_MAX_S}, so the lease stays at {default_s:.0f} s"
        )
        return default_s, why
    return float(value), None


# pytest collects a function named test_* that a test module imports: these two are not tests
test_settings.__test__ = False  # pyright: ignore[reportFunctionMemberAccess]
test_install.__test__ = False  # pyright: ignore[reportFunctionMemberAccess]
