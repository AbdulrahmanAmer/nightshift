"""C3: which optional-dependency groups of the app each venv gets.

Setup (installer s04) and `ns update` (re-sync) both read this table, so an update installs exactly what setup
installed. A release that adds a dependency to the `dashboard` or `tts` extra then reaches the venv
that runs it.

A dev install (`INSTALL.cmd --dev`, docs/DEV_MODE.md) swaps the `studio` extra for `dev-install`: the same
packages without faster-whisper, PyAV and CTranslate2 (the GPU and speech wheels), and has no `tts` venv.
"""

from __future__ import annotations

#: venv name -> extras of the `nightshift` package installed into it (`pip install -e <app>[a,b]`).
VENV_EXTRAS: dict[str, tuple[str, ...]] = {
    "studio": ("studio", "supervisor"),
    "supervisor": ("supervisor", "tts", "dashboard"),
    "tts": ("tts",),
}

#: The same table for a dev install: no speech or GPU wheels, no PyTorch voice venv.
DEV_VENV_EXTRAS: dict[str, tuple[str, ...]] = {
    "studio": ("dev-install", "supervisor"),
    "supervisor": ("supervisor", "tts", "dashboard"),
}

#: venvs installed with `--no-deps` (the fallback TTS venv pins torch itself).
NO_DEPS: frozenset[str] = frozenset({"tts"})


def extras_suffix(venv: str, *, dev: bool = False) -> str:
    """The `[a,b]` suffix for `venv` ("" for an unknown venv); `dev` picks the dev install's table."""
    extras = (DEV_VENV_EXTRAS if dev else VENV_EXTRAS).get(venv, ())
    return f"[{','.join(extras)}]" if extras else ""
