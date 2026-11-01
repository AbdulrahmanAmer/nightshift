"""s04_venvs: studio and supervisor environments (component C1, spec 21.4).

``studio`` gets the app with the ``studio`` extra (plus ``supervisor`` so ``ns`` imports resolve there),
the cuBLAS/cuDNN wheels faster-whisper needs on Windows, and the spaCy model wheel. ``supervisor`` gets
``[supervisor,tts]``. The PyTorch ``tts`` venv is created only when s02 or s08 flagged the fallback.
"""

from __future__ import annotations

from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.errors import make_error
from installer.stage2.fallback import ensure_tts_venv

# Ranges, not pins: the manifest has no entries for these yet (change request CR-C1-4).
GPU_DLL_WHEELS = ("nvidia-cublas-cu12>=12.8,<13", "nvidia-cudnn-cu12>=9,<10")
SPACY_MODEL_VERSION = "3.8.0"


def spacy_wheel_url(model: str, version: str = SPACY_MODEL_VERSION) -> str:
    """Release URL of the spaCy model wheel (spec 21.3)."""
    return (
        f"https://github.com/explosion/spacy-models/releases/download/{model}-{version}/"
        f"{model}-{version}-py3-none-any.whl"
    )


class S04Venvs(Step):
    """Create and fill the Python environments."""

    id = "s04_venvs"
    title = "Python environments"

    def check(self, ctx: StepContext) -> bool:
        """Environments exist and were filled (and the tts venv too when the fallback is flagged)."""
        if not ctx.flag("venvs_installed", False):
            return False
        if not (ctx.venv_python("studio").exists() and ctx.venv_python("supervisor").exists()):
            return False
        return not ctx.flag("tts_fallback", False) or (
            bool(ctx.flag("tts_venv_installed", False)) and ctx.venv_python("tts").exists()
        )

    def _venv(self, ctx: StepContext, name: str) -> None:
        py = ctx.venv_python(name)
        if py.exists():
            return
        path = ctx.paths.venvs / name
        res = ctx.run_uv(
            ["venv", "--python", ctx.manifest.python_version, str(path)], tag=f"uv venv {name}",
            meta={"path": str(path)},
        )
        if not res.ok:
            raise make_error("NS-E105", f"uv venv {name} failed (exit {res.returncode})")

    def _pip(self, ctx: StepContext, name: str, packages: list[str], tag: str) -> None:
        res = ctx.run_uv(
            ["pip", "install", "--python", str(ctx.venv_python(name)), *packages], tag=f"uv pip install {tag}"
        )
        if not res.ok:
            raise make_error("NS-E105", f"installing {tag} failed (exit {res.returncode})")

    def run(self, ctx: StepContext) -> None:
        """Create the venvs and install the packages."""
        app = str(ctx.app_path)
        self._venv(ctx, "studio")
        self._pip(ctx, "studio", ["-e", f"{app}[studio,supervisor]"], "studio app")
        if ctx.profile == "gpu":
            self._pip(ctx, "studio", list(GPU_DLL_WHEELS), "studio gpu dlls")
        wheel = spacy_wheel_url(ctx.manifest.spacy_model)
        self._pip(ctx, "studio", [wheel], "studio spacy model")

        self._venv(ctx, "supervisor")
        self._pip(ctx, "supervisor", ["-e", f"{app}[supervisor,tts]"], "supervisor app")

        if ctx.flag("tts_fallback", False):
            ensure_tts_venv(ctx)
        ctx.set_flag("venvs_installed", True)

    def verify(self, ctx: StepContext) -> None:
        """The venv interpreters exist and the studio venv imports the app."""
        for name in ("studio", "supervisor"):
            if not ctx.venv_python(name).exists():
                raise make_error("NS-E105", f"{name} environment is missing")
        res = ctx.runner.run(
            [str(ctx.venv_python("studio")), "-c", "import ns_common, ns_studio, ns_supervisor"],
            cwd=ctx.app_path, timeout=300, tag="studio import check",
        )
        if not res.ok:
            raise make_error("NS-E105", f"the studio environment cannot import Nightshift: {res.text[-200:]}")
        if ctx.flag("tts_fallback", False) and not ctx.venv_python("tts").exists():
            raise make_error("NS-E108", "the tts environment was not created")
