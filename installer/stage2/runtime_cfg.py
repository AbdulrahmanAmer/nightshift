"""runtime.json for the installer (component C1): baseline from Runtime.default plus the LLM tier.

``ns tune`` needs model paths to exist before it runs, so the baseline is written before tuning (s02 on a
TTS fallback, otherwise s06) and s07 only completes and validates it, never overwrites tuned flags.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from installer.stage2.context import StepContext
from ns_common.atomic import read_text_retry
from ns_common.config import load_runtime, save_runtime
from ns_common.errors import NsError
from ns_common.models import HfFile, Runtime, WhisperCfg
from ns_common.paths import Paths

#: Where a runtime.json that cannot be read is kept: ``runtime.json.bad``, then ``.bad.1``, ``.bad.2`` ...
BAD_NAME = ".bad"
#: The most set-aside copies one folder is asked to hold before setup stops instead of counting on.
MAX_ASIDE = 1000
_rename = os.rename     # a seam for the tests: on Windows it refuses to replace a file that is already there


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
    # The voice engine. qwentts.cpp whenever s02 found its binary, on both profiles: Runtime.default of
    # cpu-dev says "reference", which runs in the PyTorch venv that s04 creates only on the fallback, so a
    # real install on a PC without a graphics card pointed its voice service at a venv that did not exist.
    # The PyTorch reference engine on the fallback (NS-W301); the fake engine for an engine test.
    rt.tts.engine = "reference" if ctx.flag("tts_fallback", False) else "qwentts"
    if ctx.fake_voice:
        rt.tts.engine = "fake"
    rt.fake_footage = ctx.fake_footage      # engine test: placeholder footage instead of a stock-footage key
    rt.tiny_story = ctx.tiny_story          # engine test: the text stages' thresholds of a 1 to 2 GB model
    if ctx.dev_mode:
        # docs/DEV_MODE.md: fake engines. The voice service runs in the supervisor venv (there is no PyTorch
        # venv), and every consumer of runtime.json reads dev_mode to start the fakes.
        rt.dev_mode = True
        rt.tts.engine = "qwentts"
    return rt


def inspect_runtime(paths: Paths) -> tuple[Runtime | None, str | None]:
    """What runtime.json is: ``(runtime, None)`` when it loads, ``(None, None)`` when it is missing and
    ``(None, reason)`` when it is there but is not JSON or fails the model (the reason is one line).

    A file that exists but cannot be opened (another program holds it) raises ``OSError``: it is neither
    missing nor known to be damaged, so nothing may be moved or rewritten on that evidence.
    """
    try:
        text = read_text_retry(paths.runtime_file)
    except FileNotFoundError:
        return None, None
    try:
        return Runtime.model_validate_json(text), None
    except ValueError as e:      # not JSON, or JSON that fails the model (pydantic's errors are ValueErrors)
        first = (str(e).splitlines() or [type(e).__name__])[0]
        return None, first


def runtime_is_usable(ctx: StepContext) -> bool:
    """True when runtime.json is there and loads. False for missing and damaged, and when it cannot be read
    right now: the step's work (``ensure_baseline``) then decides, and refuses by name when it must."""
    try:
        return inspect_runtime(ctx.paths)[0] is not None
    except OSError:
        return False


def aside_sentence(kept_as: str) -> str:
    """What the owner is told when setup set a damaged runtime.json aside (step row, report and console)."""
    return (
        f"Your runtime.json could not be read; it was kept as {kept_as} and a new one was written from your "
        "answers; settings that were only in the old file are not carried over."
    )


def _set_aside(path: Path, why: str) -> Path:
    """Rename a damaged ``path`` to ``<name>.bad``, or ``.bad.1``, ``.bad.2`` ... when that name is taken.

    Nothing is overwritten and nothing is deleted. A rename the system refuses (a program holds the file, the
    folder is not writable) is NS-E125 naming the file and what to close; the file is then as it was.
    """
    for n in range(MAX_ASIDE):
        target = path.with_name(path.name + BAD_NAME + (f".{n}" if n else ""))
        if target.exists():
            continue
        try:
            _rename(path, target)
        except FileExistsError:     # taken between the check and the rename: next name
            continue
        except OSError as e:
            raise NsError(
                "NS-E125",
                f"{path} is damaged ({why}) and could not be set aside as {target.name}: "
                f"{type(e).__name__}: {e}. Close any program that has runtime.json open (an editor, a backup "
                "tool, a Nightshift window) and make sure the config folder can be written to, then choose "
                "Retry. The file has not been changed.",
            ) from e
        return target
    raise NsError(
        "NS-E125",
        f"{path} is damaged ({why}) and {MAX_ASIDE} earlier copies of it are already kept next to it, so "
        "setup will not set another aside. Move the runtime.json.bad files out of the config folder, then "
        "choose Retry. The file has not been changed.",
    )


def ensure_baseline(ctx: StepContext) -> Runtime:
    """Load runtime.json. A missing one is written from the answers. One that cannot be read (not JSON, or
    failing the model) is first set aside as ``runtime.json.bad`` (never overwritten, never deleted) and the
    owner is told, on the step's row, in the report and in the log; then the baseline is written."""
    found, why = inspect_runtime(ctx.paths)
    if found is not None:
        return found
    if why is not None:
        kept = _set_aside(ctx.paths.runtime_file, why)
        sentence = aside_sentence(kept.name)
        ctx.log.warn("runtime_set_aside", f"{ctx.paths.runtime_file}: {why}; kept as {kept}")
        ctx.ui.say(sentence, "yellow")
        ctx.note_step(sentence)
    save_runtime(ctx.paths, build_runtime(ctx))
    return load_runtime(ctx.paths)


def update_runtime(ctx: StepContext, change: Callable[[Runtime], None]) -> Runtime:
    """Apply ``change`` to runtime.json and save it."""
    rt = ensure_baseline(ctx)
    change(rt)
    save_runtime(ctx.paths, rt)
    return rt
