"""PyTorch TTS fallback (component C1): venv, packages and safetensors for the reference engine.

Used when no qwentts.cpp binary exists (``s02``) or its smoke test fails (``s08``). Spec 21.4: the ``tts``
venv gets torch cu128 and ``qwen-tts`` from the manifest pins, then ``-e <app>[tts] --no-deps``.
"""

from __future__ import annotations

from installer.stage2.context import StepContext
from installer.stage2.downloader import DownloadItem, hf_url, plan_from_hf_tree
from installer.stage2.errors import make_error, uv_error
from ns_common.uv import venv_args
from ns_common.venv_extras import extras_suffix

INDEX_SEP = " --index-url "


def fallback_items(ctx: StepContext) -> list[DownloadItem]:
    """Safetensors files of the manifest's ``tts_fallback_repos`` under models\\tts\\hf (HF tree API)."""
    dest_root = ctx.paths.models_tts_hf
    if ctx.simulate:
        items: list[DownloadItem] = []
        for repo in ctx.manifest.tts_fallback_repos:
            for fname in ("config.json", "model.safetensors"):
                items.append(
                    DownloadItem(
                        name=f"{repo}/{fname}",
                        url=hf_url(repo, ctx.manifest.tts_fallback_revisions.get(repo, "main"), fname),
                        dest=dest_root / repo.split("/")[1] / fname,
                    )
                )
        return items
    client = ctx.http_factory()
    try:
        items = []
        for repo in ctx.manifest.tts_fallback_repos:
            revision = ctx.manifest.tts_fallback_revisions.get(repo, "main")
            items += plan_from_hf_tree(repo, revision, dest_root / repo.split("/")[1], client)
        return items
    finally:
        client.close()


def parse_pin(entry: str) -> tuple[str, str | None]:
    """Split a manifest ``tts_packages`` entry into (spec, index url)."""
    if INDEX_SEP in entry:
        spec, index = entry.split(INDEX_SEP, 1)
        return spec.strip(), index.strip()
    return entry.strip(), None


def ensure_tts_venv(ctx: StepContext) -> None:
    """Create venvs\\tts with the pinned PyTorch cu128 stack and check it sees the GPU (NS-E108)."""
    venv = ctx.paths.venvs / "tts"
    py = ctx.venv_python("tts")
    ctx.ui.say("Setting up the PyTorch voice engine (fallback) ...")
    if not py.exists():
        res = ctx.run_uv(
            venv_args(venv, ctx.manifest.python_version), tag="uv venv tts", meta={"path": str(venv)}
        )
        if not res.ok:
            raise uv_error("uv venv for the tts environment failed", res)
    for entry in ctx.manifest.tts_packages:
        spec, index = parse_pin(entry)
        args = ["pip", "install", "--python", str(py), spec]
        if index:
            args += ["--index-url", index]
        res = ctx.run_uv(args, tag=f"uv pip install tts {spec.split('==')[0]}")
        if not res.ok:
            raise uv_error(f"installing {spec} failed", res)
    res = ctx.run_uv(
        ["pip", "install", "--python", str(py), "--no-deps", "-e", f"{ctx.app_path}{extras_suffix('tts')}"],
        tag="uv pip install tts app",
    )
    if not res.ok:
        raise uv_error("installing the app into the tts environment failed", res)
    if ctx.profile == "gpu":
        probe = (
            "import sys, torch; "
            "ok = torch.cuda.is_available() and torch.cuda.get_device_capability() >= (12, 0); "
            "sys.exit(0 if ok else 1)"
        )
        res = ctx.runner.run([str(py), "-c", probe], timeout=300, tag="tts torch check")
        if not res.ok:
            raise make_error("NS-E108", "torch.cuda.is_available() is false or compute capability < 12.0")
    ctx.set_flag("tts_venv_installed", True)
