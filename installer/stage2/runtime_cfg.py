"""runtime.json for the installer (component C1): baseline from Runtime.default plus the LLM tier.

``ns tune`` needs model paths to exist before it runs, so the baseline is written before tuning (s02 on a
TTS fallback, otherwise s06) and s07 only completes and validates it, never overwrites tuned flags.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from installer.stage2.context import StepContext
from ns_common.config import load_runtime, save_runtime
from ns_common.models import HfFile, Runtime, WhisperCfg


def llm_file(ctx: StepContext) -> HfFile:
    """The manifest LLM entry for the chosen profile and tier, or the owner's custom GGUF (no pinned hash)."""
    custom = ctx.llm_custom
    if custom is not None:
        return HfFile(repo=custom.repo, file=custom.file, sha256=None, bytes=custom.size)
    pm = ctx.manifest.profiles[ctx.profile]
    if ctx.profile == "gpu":
        if ctx.tier == "balanced" and pm.llm_balanced is not None:
            return pm.llm_balanced
        if ctx.tier == "best" and pm.llm_best is not None:
            return pm.llm_best
    return pm.llm


def build_runtime(ctx: StepContext) -> Runtime:
    """Runtime for this profile and tier with the installed model paths."""
    profile = "cpu-dev" if ctx.profile == "cpu-dev" else "gpu"
    rt = Runtime.default(profile)
    pm = ctx.manifest.profiles[profile]
    rt.llama.model_path = str(ctx.paths.models_llm / Path(llm_file(ctx).file).name)
    rt.embed.model_path = str(ctx.paths.models_embed / Path(pm.embed.file).name)
    rt.tts.model_dir = str(ctx.paths.models_tts)
    wdir = str(ctx.paths.models_whisper / pm.whisper_model)
    if profile == "gpu":
        rt.whisper = WhisperCfg(model=wdir, device="cuda", compute_type="int8")
        tier = "small" if ctx.llm_custom is not None else ctx.tier      # a custom GGUF: plain dense flags
        if tier in ("balanced", "best"):
            rt.llama.flags.kv_type = "q8_0"
            rt.llama.flags.n_cpu_moe = 8 if tier == "balanced" else 12
            rt.llama.flags.spec_type = "draft-mtp"
            rt.llama.flags.spec_draft_n_max = 3
        if tier == "best":
            rt.llama.flags.ctx = 32768
    else:
        rt.whisper = WhisperCfg(model=wdir, device="cpu", compute_type="int8")
    if ctx.flag("tts_fallback", False):
        rt.tts.engine = "reference"
    return rt


def ensure_baseline(ctx: StepContext) -> Runtime:
    """Load runtime.json, writing the baseline first when it does not exist."""
    if not ctx.paths.runtime_file.exists():
        save_runtime(ctx.paths, build_runtime(ctx))
    return load_runtime(ctx.paths)


def update_runtime(ctx: StepContext, change: Callable[[Runtime], None]) -> Runtime:
    """Apply ``change`` to runtime.json and save it."""
    rt = ensure_baseline(ctx)
    change(rt)
    save_runtime(ctx.paths, rt)
    return rt
