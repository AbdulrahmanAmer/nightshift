"""D-48: the start gate every command that starts a job, a model or the engine passes FIRST.

While an interrupted update's repair record waits for the program in place, or an update is refreshing the
Python environments, nothing may run from those environments (`venvrepair.home_refusal`, standard library
only: it reads two files). The check sits in each command (`studio run`, the voice, calibrate, clip, series
and assistant commands; the dashboard server; the dashboard's job runner), not in an entry point, so
`studio.exe`, `python -m ns_studio.cli`, a shim and a test runner all pass it; and it asks nothing about fakes
or a running engine, because the command itself runs from the environment the update left half-filled.

Found by the block D proof: `python -m ns_studio.cli run` on a dev install (every service a fake) passed the
old gate, which only refused a command that would start the engine, and ran a job to render_master.
"""

from __future__ import annotations

from installer.stage2.web import venvrepair
from ns_common.errors import NsError
from ns_common.paths import Paths

CODE = venvrepair.CODE


def refusal(paths: Paths | None = None) -> str | None:
    """Why nothing may start now (the sentence), or None."""
    return venvrepair.home_refusal((paths or Paths.from_env()).home)


def job_gate(paths: Paths | None = None) -> None:
    """`NS-W210` with the sentence of `refusal`; call it before anything is opened or written."""
    why = refusal(paths)
    if why is not None:
        raise NsError(CODE, why)
