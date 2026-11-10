"""runtime.json for the installer (component C1): baseline from Runtime.default plus the LLM tier.

``ns tune`` needs model paths to exist before it runs, so the baseline is written before tuning (s02 on a
TTS fallback, otherwise s06) and s07 only completes and validates it, never overwrites tuned flags.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from installer.stage2.context import StepContext
from ns_common import first_start
from ns_common.atomic import read_bytes_retry, read_text_retry
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


@dataclass(frozen=True)
class Found:
    """What runtime.json is (``inspect_runtime``).

    ``ok``: it loads (``runtime`` is it). ``missing``. ``damaged``: not JSON, or a field this version knows
    has a wrong type or value (``why`` says which). ``newer``: the ONLY complaints are fields this version
    does not know, so a newer Nightshift wrote it: ``runtime`` is the file without exactly those fields and
    ``removed`` names them (dotted: ``llama.flags.x``). Both kinds of complaint together are ``damaged``.
    """

    state: str
    runtime: Runtime | None = None
    why: str = ""
    removed: tuple[str, ...] = ()


def _where(loc: tuple[int | str, ...]) -> str:
    return ".".join(str(p) for p in loc) or "(the file)"


def _classify(text: str) -> Found:
    try:
        return Found("ok", Runtime.model_validate_json(text))
    except ValidationError as e:
        errors = e.errors()
    except ValueError as e:     # not decodable
        return Found("damaged", why=(str(e).splitlines() or [type(e).__name__])[0])
    if any(err["type"] == "json_invalid" for err in errors):
        return Found("damaged", why="not valid JSON: " + str(errors[0]["msg"]))
    unknown = [err for err in errors if err["type"] == "extra_forbidden"]
    wrong = [err for err in errors if err["type"] != "extra_forbidden"]
    wrong_text = "; ".join(f"{_where(err['loc'])}: {err['msg']}" for err in wrong)[:300]
    names = tuple(_where(err["loc"]) for err in unknown)
    if unknown and wrong:
        return Found(
            "damaged", removed=names,
            why=f"it has fields this version does not know ({', '.join(names)}) and values it cannot accept "
                f"({wrong_text})",
        )
    if wrong:
        return Found("damaged", why=wrong_text)
    try:
        data = json.loads(text)
        for err in unknown:
            node = data
            for step in err["loc"][:-1]:
                node = node[step]
            del node[err["loc"][-1]]
        return Found("newer", Runtime.model_validate(data), removed=names)
    except (ValueError, KeyError, IndexError, TypeError) as e:
        return Found("damaged", why=f"fields this version does not know ({', '.join(names)}) could not be "
                                    f"taken out: {type(e).__name__}")


def inspect_runtime(paths: Paths) -> Found:
    """What runtime.json is (see ``Found``).

    A file that exists but cannot be opened (another program holds it) raises ``OSError``: it is neither
    missing nor known to be damaged, so nothing may be moved or rewritten on that evidence.
    """
    try:
        text = read_text_retry(paths.runtime_file)
    except FileNotFoundError:
        return Found("missing")
    return _classify(text)


def runtime_is_usable(ctx: StepContext) -> bool:
    """True when runtime.json is there and loads as it is (``ok``). False for missing, damaged and newer
    (this version cannot load a newer one as it stands), and when it cannot be read right now: the step's work
    (``ensure_baseline``) then decides, and refuses by name when it must."""
    try:
        return inspect_runtime(ctx.paths).state == "ok"
    except OSError:
        return False


def aside_sentence(kept_as: str, found: Found | None = None) -> str:
    """What the owner is told when setup set a damaged runtime.json aside (step row, report and console)."""
    text = (
        f"Your runtime.json could not be read; it was kept as {kept_as} and a new one was written from your "
        "answers; settings that were only in the old file are not carried over."
    )
    if found is not None and found.removed:     # both kinds of complaint: say both
        text += f" It {found.why}."
    return text


def _few(names: tuple[str, ...], most: int = 8) -> str:
    shown = ", ".join(names[:most])
    return shown + (f" and {len(names) - most} more" if len(names) > most else "")


def newer_sentence(kept_as: str, removed: tuple[str, ...]) -> str:
    """What the owner is told when runtime.json was written by a newer Nightshift (row, report, console)."""
    return (
        f"Your runtime.json was written by a newer Nightshift: this version does not know {_few(removed)}, "
        f"took out only that, and kept every other setting. The file as it was is kept as {kept_as}."
    )


def _refuse(path: Path, what: str, e: OSError) -> NsError:
    return NsError(
        "NS-E125",
        f"{path} {what}: {type(e).__name__}: {e}. Close any program that has runtime.json open (an editor, a "
        "backup tool, a Nightshift window) and make sure the config folder can be written to, then choose "
        "Retry. The file has not been changed.",
    )


def _free_name(path: Path, suffix: str, n: int) -> Path:
    return path.with_name(path.name + suffix + (f".{n}" if n else ""))


def _set_aside(path: Path, why: str, suffix: str = BAD_NAME) -> Path:
    """Rename a damaged ``path`` to ``<name>.bad``, or ``.bad.1``, ``.bad.2`` ... when that name is taken.

    Nothing is overwritten and nothing is deleted. A rename the system refuses (a program holds the file, the
    folder is not writable) is NS-E125 naming the file and what to close; the file is then as it was.
    """
    first_start.guard(path)     # the settings copy of a first start after an older updater, before any change
    for n in range(MAX_ASIDE):
        target = _free_name(path, suffix, n)
        if target.exists():
            continue
        try:
            _rename(path, target)
        except FileExistsError:     # taken between the check and the rename: next name
            continue
        except OSError as e:
            raise _refuse(path, f"is damaged ({why}) and could not be set aside as {target.name}", e) from e
        return target
    raise NsError(
        "NS-E125",
        f"{path} is damaged ({why}) and {MAX_ASIDE} earlier copies of it are already kept next to it, so "
        "setup will not set another aside. Move the runtime.json.bad files out of the config folder, then "
        "choose Retry. The file has not been changed.",
    )


def _keep_copy(path: Path, suffix: str) -> Path:
    """Copy ``path`` to ``<name>.newer`` (``.newer.1`` ... when taken), never over an existing file. A copy
    rather than a rename: the file stays whole until the stripped one replaces it in one step."""
    first_start.guard(path)
    for n in range(MAX_ASIDE):
        target = _free_name(path, suffix, n)
        try:
            data = read_bytes_retry(path)
            with open(target, "xb") as out:
                out.write(data)
        except FileExistsError:
            continue
        except OSError as e:
            what = f"was written by a newer Nightshift and could not be copied to {target.name}"
            raise _refuse(path, what, e) from e
        return target
    raise NsError(
        "NS-E125",
        f"{path} was written by a newer Nightshift and {MAX_ASIDE} copies of it are already kept next to it. "
        "Move the runtime.json.newer files out of the config folder, then choose Retry. "
        "The file has not been changed.",
    )


#: What "this runtime.json is new" makes untrue: the tuning sweep, and the smoke test that ran on its numbers.
FRESH_CLEARS = ("tuned", "smoke", "smoke_version")
#: A newer file's settings are carried over (tuned numbers included), only the smoke test's evidence is not.
CARRIED_CLEARS = ("smoke", "smoke_version")


def write_runtime(ctx: StepContext, rt: Runtime, *, fresh: bool) -> None:
    """The one place setup writes a runtime.json that did not come from a tuning or an owner's setting.

    A baseline from the answers (``fresh``) was never tuned, so the flags that say it was are forgotten
    FIRST: a run killed between the two steps re-tunes, where the other order would leave a baseline that
    the flag calls tuned. Every step that writes a baseline (s02 on the voice fallback, s03, s06 ...) reaches
    this through ``ensure_baseline``, so the tuning step cannot find a flag that describes another file."""
    ctx.clear_flags(*(FRESH_CLEARS if fresh else CARRIED_CLEARS))
    save_runtime(ctx.paths, rt)


def ensure_baseline(ctx: StepContext) -> Runtime:
    """Load runtime.json.

    - missing: written from the answers;
    - damaged (not JSON, or a field this version knows has a wrong value): set aside as ``runtime.json.bad``
      (never overwritten, never deleted), a baseline written from the answers;
    - written by a newer Nightshift (the only complaints are fields this version does not know): copied to
      ``runtime.json.newer``, then rewritten without exactly those fields and with every other setting kept.

    The owner is told on the step's row, in the report and in the log. A baseline clears the flags that say
    tuning ran (``write_runtime``)."""
    path = ctx.paths.runtime_file
    try:
        found = inspect_runtime(ctx.paths)
    except OSError as e:
        raise _refuse(path, "exists but could not be read", e) from e
    if found.state == "ok" and found.runtime is not None:
        return found.runtime
    if found.state == "newer" and found.runtime is not None:
        kept = _keep_copy(path, ".newer")
        sentence = newer_sentence(kept.name, found.removed)
        took = ", ".join(found.removed)
        ctx.log.warn("runtime_newer_stripped", f"{path}: took out {took}; original at {kept}")
        ctx.ui.say(sentence, "yellow")
        ctx.note_step(sentence)
        write_runtime(ctx, found.runtime, fresh=False)
        return load_runtime(ctx.paths)
    if found.state == "damaged":
        kept = _set_aside(path, found.why)
        sentence = aside_sentence(kept.name, found)
        ctx.log.warn("runtime_set_aside", f"{path}: {found.why}; kept as {kept}")
        ctx.ui.say(sentence, "yellow")
        ctx.note_step(sentence)
    write_runtime(ctx, build_runtime(ctx), fresh=True)
    return load_runtime(ctx.paths)


def update_runtime(ctx: StepContext, change: Callable[[Runtime], None]) -> Runtime:
    """Apply ``change`` to runtime.json and save it."""
    rt = ensure_baseline(ctx)
    change(rt)
    save_runtime(ctx.paths, rt)
    return rt
