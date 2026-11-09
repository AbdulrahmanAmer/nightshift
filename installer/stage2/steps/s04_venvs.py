"""s04_venvs: studio and supervisor environments (component C1, spec 21.4).

``studio`` gets the app with the ``studio`` extra (plus ``supervisor`` so ``ns`` imports resolve there),
the cuBLAS/cuDNN wheels faster-whisper needs on Windows (hash-locked: ``python.whisper_cuda`` in
``manifest.json``) and the spaCy model wheel (hash-locked: ``python.spacy_wheel``). ``supervisor`` gets
``[supervisor,tts,dashboard]``. The PyTorch ``tts`` venv is created only when s02 or s08 flagged the fallback.

A dev install (``ctx.dev_mode``, docs/DEV_MODE.md) installs the ``dev-install`` extra instead of
``studio``: the same packages without faster-whisper, PyAV and CTranslate2, and never the cuBLAS/cuDNN wheels
or the ``tts`` venv.
"""

from __future__ import annotations

import os
from pathlib import Path

from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.errors import make_error, uv_error
from installer.stage2.fallback import ensure_tts_venv
from ns_common.uv import uv_env, venv_args
from ns_common.venv_extras import extras_suffix

#: The wheels uv unpacks for the studio, supervisor and PyTorch venvs (cuBLAS, cuDNN, torch).
UV_CACHE_NEED_GB = 8
#: A dev install unpacks no torch, cuBLAS or cuDNN: spaCy, NumPy, OpenCV and the web stack.
UV_CACHE_NEED_DEV_GB = 2

#: Used only by a manifest without ``python.spacy_wheel`` (no hash to check); the repo manifest pins it.
SPACY_MODEL_VERSION = "3.8.0"

#: What each venv must be able to load before s04 may call it done (``import:`` loads the module, ``find:``
#: only looks it up: spaCy and torch take seconds to load). The first-party packages come from the editable
#: install of the app, the part a killed run leaves undone. ``ns_supervisor.cli`` is the module ``ns`` runs: it
#: pulls in typer, pydantic, httpx, yaml, rich, numpy and psutil, which the studio and supervisor venvs get from
#: ``pip install -e app[...]`` (the top-level ``ns_*`` packages import nothing third-party). The tts venv is
#: installed with ``--no-deps``, so it is asked only for its own packages.
VENV_PROBES: dict[str, tuple[str, ...]] = {
    "studio": ("import:ns_common", "import:ns_studio", "import:ns_supervisor.cli", "find:spacy"),
    "supervisor": ("import:ns_common", "import:ns_supervisor.cli", "import:ns_dashboard", "find:fastapi",
                   "find:uvicorn"),
    "tts": ("import:ns_common", "import:ns_tts", "find:torch"),
}

#: Run by ``python -I -c``: exits 1 and names what could not be loaded. ``-I`` keeps the current folder and
#: the user's site-packages out of ``sys.path``; without it ``import ns_studio`` succeeds in an empty venv
#: when the working directory is the program folder, which is the folder the packages come from.
PROBE_CODE = """\
import importlib
import importlib.util
import sys

bad = []
for spec in sys.argv[1:]:
    kind, _, name = spec.partition(":")
    try:
        if kind == "import":
            importlib.import_module(name)
            found = True
        else:
            found = importlib.util.find_spec(name) is not None
    except Exception:
        found = False
    if not found:
        bad.append(name)
if bad:
    sys.stderr.write("cannot load: " + ", ".join(bad))
    sys.exit(1)
"""


def spacy_wheel_url(model: str, version: str = SPACY_MODEL_VERSION) -> str:
    """Release URL of the spaCy model wheel (spec 21.3)."""
    return (
        f"https://github.com/explosion/spacy-models/releases/download/{model}-{version}/"
        f"{model}-{version}-py3-none-any.whl"
    )


def check_cache_space(ctx: StepContext) -> None:
    """NS-E104 when uv's cache cannot be on the install drive and the system drive is short of room.

    ``uv_env`` puts the cache under ``<home>/cache/uv``; only when that folder cannot be made does uv fall
    back to ``%LOCALAPPDATA%`` on C:, which a small system drive cannot hold.
    """
    if "UV_CACHE_DIR" in uv_env(ctx.paths.home):
        return
    drive = Path(os.environ.get("SystemDrive", "C:") + os.sep)
    free = ctx.free_gb_fn(drive)
    need = UV_CACHE_NEED_DEV_GB if ctx.dev_mode else UV_CACHE_NEED_GB
    if free < need:
        raise make_error(
            "NS-E104",
            f"uv's cache cannot be placed on the install drive and {drive} has {free:.0f} GB free; "
            f"{need} GB are needed",
        )


def venv_names(ctx: StepContext) -> tuple[str, ...]:
    """The environments this install has: studio and supervisor, and tts when the fallback is flagged."""
    return ("studio", "supervisor", "tts") if ctx.flag("tts_fallback", False) else ("studio", "supervisor")


def probe_argv(ctx: StepContext, name: str) -> list[str]:
    """The command that asks the venv ``name`` whether it holds what the install put there."""
    specs = list(VENV_PROBES[name])
    if name == "studio":
        specs.append(f"find:{ctx.manifest.spacy_model}")
    return [str(ctx.venv_python(name)), "-I", "-c", PROBE_CODE, *specs]


def cannot_load(ctx: StepContext, name: str) -> str | None:
    """None when the venv ``name`` loads the product, else why not (the interpreter, or what is missing).

    This asks the environment itself. A flag, or an interpreter that exists, says nothing about the packages:
    an install killed after ``uv venv`` leaves an interpreter with an empty site-packages (D-15).
    """
    if not ctx.venv_python(name).exists():
        return f"the {name} environment has no interpreter"
    res = ctx.runner.run(
        probe_argv(ctx, name), cwd=ctx.paths.home, timeout=300, tag=f"{name} import check",
        meta={"venv": name},
    )
    if res.ok:
        return None
    return f"the {name} environment cannot load Nightshift: {res.text[-200:] or 'no answer'}"


class S04Venvs(Step):
    """Create and fill the Python environments."""

    id = "s04_venvs"
    title = "Python environments"
    # the done-state goes before the work starts and comes back only after every venv is filled; a new set of
    # environments also voids the smoke test that tested the old ones
    clears_on_run = ("venvs_installed", "venvs_version", "venvs_app", "tts_venv_installed", "smoke",
                     "smoke_version")

    def check(self, ctx: StepContext) -> bool:
        """The environments hold the packages: recorded as done for this version and program folder, and the
        interpreters of every venv load the product (a probe, not the flag)."""
        if not ctx.flag("venvs_installed", False) or ctx.flag("venvs_version") != ctx.manifest.version:
            return False
        if ctx.flag("venvs_app") != str(ctx.app_path):
            return False                    # the editable installs point at another program folder
        if ctx.flag("tts_fallback", False) and not ctx.flag("tts_venv_installed", False):
            return False
        for name in venv_names(ctx):
            problem = cannot_load(ctx, name)
            if problem is not None:
                ctx.log.info("venv_check_failed", venv=name, why=problem)
                return False
        return True

    def _venv(self, ctx: StepContext, name: str) -> None:
        py = ctx.venv_python(name)
        if py.exists():
            return
        path = ctx.paths.venvs / name
        res = ctx.run_uv(
            venv_args(path, ctx.manifest.python_version), tag=f"uv venv {name}", meta={"path": str(path)},
        )
        if not res.ok:
            raise uv_error(f"uv venv {name} failed", res)

    def _pip(self, ctx: StepContext, name: str, packages: list[str], tag: str) -> None:
        res = ctx.run_uv(
            ["pip", "install", "--python", str(ctx.venv_python(name)), *packages], tag=f"uv pip install {tag}"
        )
        if not res.ok:
            raise uv_error(f"installing {tag} failed", res)

    def _pip_locked(self, ctx: StepContext, name: str, lines: list[str], tag: str, file_stem: str) -> None:
        """Install requirement lines that each carry ``--hash=sha256:...`` (hash-checking mode, no deps).

        The lines are written to ``logs/pins-<file_stem>.txt`` first, so the diagnostics zip shows what
        was pinned.
        """
        pin_file = ctx.paths.logs / f"pins-{file_stem}.txt"
        pin_file.parent.mkdir(parents=True, exist_ok=True)
        pin_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._pip(ctx, name, ["--no-deps", "--require-hashes", "-r", str(pin_file)], tag)

    def _spacy_model(self, ctx: StepContext) -> None:
        """The spaCy model wheel, checked against the manifest's sha256 (unchecked URL for old manifests)."""
        model = ctx.manifest.spacy_model
        pin = ctx.manifest.python.spacy_wheel
        if pin is None or not pin.sha256:
            self._pip(ctx, "studio", [spacy_wheel_url(model)], "studio spacy model")
            return
        self._pip_locked(
            ctx, "studio", [f"{model} @ {pin.url} --hash=sha256:{pin.sha256}"], "studio spacy model", "spacy"
        )

    def run(self, ctx: StepContext) -> None:
        """Create the venvs and install the packages."""
        check_cache_space(ctx)
        app = str(ctx.app_path)
        self._venv(ctx, "studio")
        dev = ctx.dev_mode      # docs/DEV_MODE.md: the dev-install extra, no speech or GPU wheels
        self._pip(ctx, "studio", ["-e", f"{app}{extras_suffix('studio', dev=dev)}"], "studio app")
        if ctx.profile == "gpu":
            pins = ctx.manifest.python.whisper_cuda
            if not pins:
                raise make_error("NS-E105", "manifest.json has no python.whisper_cuda pins")
            self._pip_locked(ctx, "studio", pins, "studio gpu dlls", "whisper-cuda")
        self._spacy_model(ctx)

        self._venv(ctx, "supervisor")
        self._pip(ctx, "supervisor", ["-e", f"{app}{extras_suffix('supervisor', dev=dev)}"], "supervisor app")

        if ctx.flag("tts_fallback", False):
            ensure_tts_venv(ctx)
        # the done-state is written last, after every venv was filled (the wizard cleared it before the work)
        ctx.set_flag("venvs_installed", True)
        ctx.set_flag("venvs_version", ctx.manifest.version)
        ctx.set_flag("venvs_app", str(ctx.app_path))

    def verify(self, ctx: StepContext) -> None:
        """Every venv has its interpreter and loads the product (studio, supervisor and the flagged tts)."""
        for name in ("studio", "supervisor"):
            if not ctx.venv_python(name).exists():
                raise make_error("NS-E105", f"{name} environment is missing")
        if ctx.flag("tts_fallback", False) and not ctx.venv_python("tts").exists():
            raise make_error("NS-E108", "the tts environment was not created")
        for name in venv_names(ctx):
            problem = cannot_load(ctx, name)
            if problem is not None:
                raise make_error("NS-E108" if name == "tts" else "NS-E105", problem)
