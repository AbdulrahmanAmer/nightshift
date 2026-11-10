"""runtime.json for the installer (component C1): baseline from Runtime.default plus the LLM tier.

``ns tune`` needs model paths to exist before it runs, so the baseline is written before tuning (s02 on a
TTS fallback, otherwise s06) and s07 only completes and validates it, never overwrites tuned flags.

One mechanism for an existing file (setup-torn-runtime). ``inspect_runtime`` is the ONE place that decides
what the file is, and ``Found.runtime`` is the ONE meaning of "the settings setup keeps from it"; every step
gets the file through ``existing_runtime`` (s01's comparison, s03) or ``ensure_baseline`` (every step that
needs a file that loads), and a step's ``check`` through ``runtime_is_usable`` / ``runtime_settled``.

==========  =========================================  ==================================================
file        what setup believes the settings are       what ``ensure_baseline`` leaves
==========  =========================================  ==================================================
ok          the file                                   the file, untouched
recode      the file (byte order mark, UTF-16)         the same settings as plain UTF-8
newer       the file without the unknown names         those settings; the original as ``.newer``
missing     none                                       the baseline of the answers
damaged     none                                       the baseline; the original as ``.bad``
other kind  none (s06, once the model is there)        the baseline; the original as ``.other``
unreadable  not known: NS-E125, nothing is guessed     nothing is changed
==========  =========================================  ==================================================

The invariant: the final runtime.json is what a fresh install with these answers writes (``build_runtime``)
plus exactly the settings of the existing file that setup keeps by design: every one when the answers did not
change and the file is of this kind of install; all but ``llama.model_path`` and ``llama.flags`` when the tier
or the custom model changed; all but ``fake_footage``, ``tiny_story`` and ``tts.engine`` when an engine-test
answer changed; none when the file was made for another kind of install (real or dev, GPU or not), is
missing or is damaged. What the answers changed is recorded by s01 (``PENDING_FLAG``) against the settings
``Found.runtime`` names and applied by s06 (``ensure_baseline(follow_answers=True)``) to the settings the same
field names then; the kind of install is compared by s06 itself on every run, so no state of the old file and
no route past s01 can make an answer be ignored.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from installer.stage2.context import StepContext
from installer.stage2.devmode import OTHER_FLAG
from ns_common import first_start
from ns_common.atomic import read_bytes_retry
from ns_common.config import load_runtime, save_runtime
from ns_common.errors import NsError
from ns_common.models import HfFile, Runtime, WhisperCfg
from ns_common.paths import Paths

#: Where a runtime.json that cannot be read is kept: ``runtime.json.bad``, then ``.bad.1``, ``.bad.2`` ...
BAD_NAME = ".bad"
#: The most set-aside copies one folder is asked to hold before setup stops instead of counting on.
MAX_ASIDE = 1000
_rename = os.rename     # a seam for the tests: on Windows it refuses to replace a file that is already there
#: Wizard flag: what runtime.json still owes the answers, ``{"whole": bool, "llama": bool, "engine": bool}``.
#: Set by s01 (``reinstall._runtime``) when the answers ask for it, applied and cleared by s06
#: (``ensure_baseline(follow_answers=True)``) once the model file is in place; a baseline owes nothing.
PENDING_FLAG = "runtime_pending"


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
    ``recode``: it reads fine but is not plain UTF-8 (a byte order mark, or UTF-16): every setting is kept and
    the file is written again as plain UTF-8, which is all the rest of Nightshift reads; ``why`` says which.
    ``unreadable``: it is there and cannot be read right now (another program holds it, no permission, a
    folder of that name): ``error`` is what the system said. Nothing is known about its settings.

    ``runtime`` is the one meaning of "the settings setup keeps from the existing file": set for ``ok``,
    ``recode`` and ``newer``, None for every other state. No step tests the state to decide that.
    """

    state: str
    runtime: Runtime | None = None
    why: str = ""
    removed: tuple[str, ...] = ()
    error: OSError | None = None


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
    except (ValueError, KeyError, IndexError, TypeError, RecursionError) as e:
        return Found("damaged", why=f"fields this version does not know ({', '.join(names)}) could not be "
                                    f"taken out: {type(e).__name__}")


#: A real runtime.json is a few kilobytes. A file over this is not one (garbage, a wrong file): not read.
MAX_BYTES = 1_000_000


def _decode(data: bytes) -> tuple[str, str]:
    """(text, how) of the file's bytes; how is empty for plain UTF-8. A UTF-8 byte order mark and UTF-16 with
    a byte order mark (what some Windows editors save) are unambiguous text: read, and the file is written as
    plain UTF-8 (``recode``), nothing lost. Any other bytes raise ``UnicodeDecodeError``."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8"), "UTF-8 with a byte order mark"
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16"), "UTF-16"
    return data.decode("utf-8"), ""


def inspect_runtime(paths: Paths) -> Found:
    """What runtime.json is (see ``Found``). Every way reading it can go is one deliberate answer:

    - not there, or gone between two looks: ``missing``
    - plain UTF-8, UTF-8 with a byte order mark, UTF-16 with one: read (the last two are ``recode``)
    - other bytes, empty or whitespace only, not JSON, JSON that is not an object, over ``MAX_BYTES``, a known
      field with a bad value: ``damaged``
    - only unknown fields: ``newer``
    - a folder of that name, or a file that cannot be opened (another program holds it, no permission):
      ``unreadable``, which ``existing_runtime`` turns into NS-E125; nothing is moved or rewritten then.

    It never raises for anything the file or the file system does.
    """
    path = paths.runtime_file
    try:
        if path.is_dir():
            raise IsADirectoryError(21, "is a folder")
        size = path.stat().st_size
        if size > MAX_BYTES:
            return Found("damaged", why=f"it is {size} bytes, far too large for a settings file")
        data = read_bytes_retry(path)
    except FileNotFoundError:
        return Found("missing")
    except OSError as e:
        return Found("unreadable", why=f"{type(e).__name__}: {e}", error=e)
    if len(data) > MAX_BYTES:
        return Found("damaged", why=f"it is {len(data)} bytes, far too large for a settings file")
    try:
        text, how = _decode(data)
    except UnicodeDecodeError:
        return Found("damaged", why="it is not text this version can read (not UTF-8, and not UTF-16 with a "
                                    "byte order mark)")
    found = _classify(text)
    if found.state == "ok" and how:
        return Found("recode", found.runtime, why=how)
    return found


def existing_runtime(ctx: StepContext) -> Found:
    """What runtime.json is, for a step that acts on it: ``inspect_runtime``, and NS-E125 naming the file
    when it cannot be read right now. A step never goes on as if such a file were missing: its settings and
    what the answers would change in them are not known, so nothing is guessed and nothing is changed."""
    found = inspect_runtime(ctx.paths)
    if found.state == "unreadable" and found.error is not None:
        raise _refuse(ctx.paths.runtime_file, "exists but could not be read", found.error) from found.error
    return found


def runtime_is_usable(ctx: StepContext) -> bool:
    """For a step's ``check`` (which never changes a file and never raises): True when runtime.json is there
    and loads as it is (``ok``). False for every other state: the step's work (``ensure_baseline``) then
    deals with it, and refuses by name when it must."""
    return inspect_runtime(ctx.paths).state == "ok"


def _kind(rt: Runtime) -> tuple[str, bool]:
    return rt.profile, rt.dev_mode


def install_kind(ctx: StepContext) -> tuple[str, bool]:
    """The kind of install this run makes, as ``build_runtime`` writes it: (profile, dev_mode)."""
    return ("cpu-dev" if ctx.profile == "cpu-dev" else "gpu"), ctx.dev_mode


def runtime_settled(ctx: StepContext) -> bool:
    """For the tuning step's ``check``: True when runtime.json needs nothing more from setup. It loads as it
    is, it was made for this kind of install (real or dev, GPU or not), and nothing that the answers changed
    is still owed to it. Compared on every run, whether or not the questions step ran."""
    found = inspect_runtime(ctx.paths)
    return (
        found.state == "ok" and found.runtime is not None
        and _kind(found.runtime) == install_kind(ctx)
        and not ctx.flag(PENDING_FLAG)
    )


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


def newer_sentence(kept_as: str, removed: tuple[str, ...], *, whole: bool = False, changed: str = "") -> str:
    """What the owner is told when runtime.json has settings this version does not know (a newer Nightshift
    wrote it, or a hand edit misspelt a name): row, report and console. ``whole``: the same step built the
    file again from the answers (another kind of install); ``changed``: what the answers changed in it."""
    start = (
        f"Your runtime.json has settings this version does not know (a newer Nightshift, or a hand edit): "
        f"{_few(removed)}"
    )
    if whole:
        return (
            f"{start}, and it was made for another kind of install (real or dev, GPU or not): a new one was "
            f"built from your answers and no setting of the old file is carried over. The file as it was is "
            f"kept as {kept_as}. Tuning runs again for it (1 to 3 minutes)."
        )
    other = f", except what your answers changed ({changed})" if changed else ""
    return (
        f"{start}; they were removed and every other setting was kept{other}. The file as it was is kept as "
        f"{kept_as}. Tuning runs again for it (1 to 3 minutes)."
    )


#: Where a runtime.json made for another kind of install is kept: ``runtime.json.other``, ``.other.1`` ...
OTHER_NAME = ".other"


def switched_sentence(kept_as: str) -> str:
    """What the owner is told (console, the tuning step's row, report) after a switch between a dev and a
    real install set the old mode's runtime.json aside before the steps ran."""
    return (
        "The settings of the other kind of install (real or dev) were not carried over: config\\runtime.json "
        f"is written new from your answers. The old file is kept as {kept_as}. A port or an engine you "
        "changed by hand or in Settings, and the tuned numbers, are in it: set them again in Settings, or "
        "copy them from that file."
    )


def _tell_switched(ctx: StepContext) -> None:
    """The tuning step: name the file a mode switch set aside (``devmode.reset_for_mode_switch``), once."""
    name = ctx.flag(OTHER_FLAG)
    if name is None:
        return
    if isinstance(name, str) and (ctx.paths.config / name).is_file():
        sentence = switched_sentence(name)
        ctx.log.warn("runtime_other_kept", f"{ctx.paths.runtime_file}: the other mode's file is at {name}")
        ctx.ui.say(sentence, "yellow")
        ctx.note_step(sentence)
    ctx.clear_flags(OTHER_FLAG)


def whole_sentence(kept_as: str | None) -> str:
    """What the owner is told (console, row, report) when the file is built again because it was made for
    another kind of install. ``kept_as``: the name the old file is kept under (None: it was gone by then)."""
    start = (
        "config\\runtime.json is built again from your answers (it was made for another kind of install). "
    )
    if kept_as is None:
        return start + "Settings that were only in the old file are not carried over."
    return start + (
        f"The file as it was is kept as {kept_as}. Settings that were only in it are not carried over: a "
        "port or an engine you changed by hand or in Settings, and the tuned numbers. Set them again in "
        "Settings, or copy them from that file."
    )


def _refuse(path: Path, what: str, e: OSError) -> NsError:
    if isinstance(e, IsADirectoryError):
        return NsError(
            "NS-E125",
            f"{path} is a folder, not a file. Nightshift will not move a folder: rename or remove the folder "
            "runtime.json in the config folder, then choose Retry. Nothing has been changed.",
        )
    return NsError(
        "NS-E125",
        f"{path} {what}: {type(e).__name__}: {e}. Close any program that has runtime.json open (an editor, a "
        "backup tool, a Nightshift window) and make sure the config folder can be written to, then choose "
        "Retry. The file has not been changed.",
    )


def _free_name(path: Path, suffix: str, n: int) -> Path:
    return path.with_name(path.name + suffix + (f".{n}" if n else ""))


def _set_aside(path: Path, why: str, suffix: str = BAD_NAME) -> Path | None:
    """Rename a damaged ``path`` to ``<name>.bad``, or ``.bad.1``, ``.bad.2`` ... when that name is taken.

    None when the file is gone by then. Nothing is overwritten and nothing is deleted. A rename the system
    refuses (a program holds the file, the
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
        except FileNotFoundError:   # gone since it was read: there is nothing to keep
            return None
        except OSError as e:
            raise _refuse(path, f"is damaged ({why}) and could not be set aside as {target.name}", e) from e
        return target
    raise NsError(
        "NS-E125",
        f"{path} is damaged ({why}) and {MAX_ASIDE} earlier copies of it are already kept next to it, so "
        "setup will not set another aside. Move the runtime.json.bad files out of the config folder, then "
        "choose Retry. The file has not been changed.",
    )


def _keep_copy(path: Path, suffix: str, why: str = "was written by a newer Nightshift") -> Path | None:
    """Copy ``path`` to ``<name>.newer`` (``.newer.1`` ... when taken), never over an existing file. A copy
    rather than a rename: the file stays whole until the new one replaces it in one step. The same for a
    file of another kind of install (``.other``); ``why`` is what an NS-E125 says about the file."""
    first_start.guard(path)
    for n in range(MAX_ASIDE):
        target = _free_name(path, suffix, n)
        try:
            data = read_bytes_retry(path)
            with open(target, "xb") as out:
                out.write(data)
        except FileExistsError:
            continue
        except FileNotFoundError:   # gone since it was read: no original to keep
            return None
        except OSError as e:
            what = f"{why} and could not be copied to {target.name}"
            raise _refuse(path, what, e) from e
        return target
    raise NsError(
        "NS-E125",
        f"{path} {why} and {MAX_ASIDE} copies of it are already kept next to it. "
        f"Move the runtime.json{suffix} files out of the config folder, then choose Retry. "
        "The file has not been changed.",
    )


#: What "this runtime.json is new" makes untrue: the tuning sweep, and the smoke test that ran on its numbers.
#: A file with unknown fields counts: a setting's meaning may have changed within its accepted range.
FRESH_CLEARS = ("tuned", "smoke", "smoke_version")


def write_runtime(ctx: StepContext, rt: Runtime, *, clears: tuple[str, ...] = FRESH_CLEARS) -> None:
    """The one place setup writes a runtime.json that did not come from a tuning or an owner's setting.

    A baseline from the answers, or a file that had to lose settings, was never tuned by this version, so
    the flags that say it was are forgotten FIRST: a run killed between the two steps re-tunes, where the
    other order would leave a file the flag calls tuned. Every step that writes one (s02 on the voice
    fallback, s03, s06 ...) reaches this through ``ensure_baseline``, so the tuning step cannot find a flag
    that describes another file. A file only written again as plain UTF-8 changes no setting: ``clears=()``.
    """
    ctx.clear_flags(*clears)
    save_runtime(ctx.paths, rt)


@dataclass(frozen=True)
class _Follow:
    """What the answers change in the kept settings (``_follow``)."""

    runtime: Runtime                    # the settings after the change (the kept ones when nothing changed)
    lines: tuple[str, ...] = ()         # said on the console, one per thing done
    whole: bool = False                 # built again from the answers
    changed: str = ""                   # in words, for the sentence of a file with unknown settings
    waiting: str = ""                   # the model the answers name is not on disk: nothing changed yet
    settled: bool = True                # nothing is owed any more

    @property
    def applied(self) -> bool:
        """True when the settings were changed."""
        return self.whole or bool(self.lines)


def _follow(ctx: StepContext, rt: Runtime) -> _Follow:
    """Make the kept settings follow the answers (s06, once s03 has verified the models): what s01 recorded
    in ``PENDING_FLAG``, and the kind of install, which is compared here whatever s01 saw or did not see.

    The language model is repointed only when its file is in models\\llm; otherwise the settings keep the
    model they have and the work stays owed (the nightly run never starts llama-server on a missing file).
    """
    flag = ctx.flag(PENDING_FLAG)
    owed: dict[str, bool] = dict(flag) if isinstance(flag, dict) else {}
    whole = bool(owed.get("whole")) or _kind(rt) != install_kind(ctx)
    llama, engine = bool(owed.get("llama")), bool(owed.get("engine"))
    if not (whole or llama or engine):
        return _Follow(rt)
    fresh = build_runtime(ctx)
    was, wanted = Path(rt.llama.model_path).name, Path(fresh.llama.model_path)
    if (whole or llama) and not wanted.is_file():
        if whole:
            waiting = (
                "config\\runtime.json was made for another kind of install (real or dev, GPU or not) and is "
                f"still in use as it is, because {wanted.name} is not in models\\llm yet; it is built again "
                "from your answers once the file is there. Run setup again (INSTALL.cmd)."
            )
        else:
            waiting = (
                f"config\\runtime.json keeps using {was} because {wanted.name} is not in models\\llm yet; "
                "it is switched once the file is there."
            )
        return _Follow(rt, waiting=waiting, settled=False)
    if whole:
        return _Follow(fresh, whole=True)
    out = rt.model_copy(deep=True)
    lines: list[str] = []
    changed: list[str] = []
    if llama:
        # always the flags too: s03 already pointed model_path at a custom file, with the old tier's flags
        out.llama.model_path, out.llama.flags = fresh.llama.model_path, fresh.llama.flags
        had = f" (it had {was})" if was != wanted.name else ""
        lines.append(
            f"Language model: config\\runtime.json now uses {wanted.name}{had} with that model's "
            "settings, and tuning runs again."
            + (f" {was} stays in models\\llm; delete it by hand to free space." if had else "")
        )
        changed.append("the language model")
    if engine:
        out.fake_footage, out.tiny_story = fresh.fake_footage, fresh.tiny_story
        out.tts.engine = fresh.tts.engine
        lines.append(
            "Engine test: config\\runtime.json now follows your fake_voice, fake_footage and tiny_story "
            "answers."
        )
        changed.append("the engine test")
    return _Follow(out, lines=tuple(lines), changed=" and ".join(changed))


def ensure_baseline(ctx: StepContext, *, follow_answers: bool = False) -> Runtime:
    """Leave a runtime.json that loads, and return it. The one way every step gets the file.

    - missing: written from the answers;
    - damaged (not readable text, not JSON, not an object, too large, or a field this version knows has a
      wrong value): set aside as ``runtime.json.bad`` (never overwritten, never deleted), a baseline written
      from the answers;
    - settings this version does not know (and nothing else wrong): copied to ``runtime.json.newer``, then
      rewritten without exactly those, every other setting kept, tuning to run again;
    - a byte order mark or UTF-16: the same settings, written again as plain UTF-8;
    - a folder of that name, or a file that cannot be opened: NS-E125 naming it, nothing changed.

    ``follow_answers`` (the tuning step, after the models are verified): in the same pass the kept settings
    are made to follow the answers (``_follow``), so the file is written once and what the owner is told is
    true of the file that is left. A baseline is made from the answers and owes them nothing.

    The owner is told on the step's row, in the report and in the log (not for the plain re-encoding and not
    for a model or engine-test change the answers asked for, which the console names)."""
    path = ctx.paths.runtime_file
    found = existing_runtime(ctx)
    if follow_answers:
        _tell_switched(ctx)     # after the refusal above: a Retry still has it to say
    kept_rt = found.runtime
    if kept_rt is None:     # missing or damaged: nothing is kept
        if found.state == "damaged":
            kept = _set_aside(path, found.why)
            if kept is not None:
                sentence = aside_sentence(kept.name, found)
                ctx.log.warn("runtime_set_aside", f"{path}: {found.why}; kept as {kept}")
                ctx.ui.say(sentence, "yellow")
                ctx.note_step(sentence)
        write_runtime(ctx, build_runtime(ctx))
        ctx.clear_flags(PENDING_FLAG)       # after the file is there: a kill in between applies it again
        return load_runtime(ctx.paths)
    follow = _follow(ctx, kept_rt) if follow_answers else _Follow(kept_rt, settled=False)
    if follow.waiting:
        ctx.log.warn("runtime_answers_waiting", follow.waiting)
        ctx.ui.say(follow.waiting, "yellow")
        ctx.note_step(follow.waiting)
    if found.state == "ok" and not follow.applied:
        if follow.settled:
            ctx.clear_flags(PENDING_FLAG)   # a flag that names nothing to do
        return kept_rt
    if found.state == "newer":
        kept = _keep_copy(path, ".newer")
        if kept is not None:
            sentence = newer_sentence(kept.name, found.removed, whole=follow.whole, changed=follow.changed)
            took = ", ".join(found.removed)
            ctx.log.warn("runtime_newer_stripped", f"{path}: took out {took}; original at {kept}")
            ctx.ui.say(sentence, "yellow")
            ctx.note_step(sentence)
    if found.state == "recode":
        ctx.log.info("runtime_recoded", f"{path} was {found.why}; written again as plain UTF-8")
        same = "" if follow.applied else " (no setting changed)"
        ctx.ui.say(f"runtime.json was {found.why}; it is written again as plain UTF-8{same}.")
    if follow.whole and found.state != "newer":     # a newer file is kept as .newer and its sentence says it
        kept = _keep_copy(path, OTHER_NAME, "was made for another kind of install")
        sentence = whole_sentence(kept.name if kept is not None else None)
        ctx.log.warn("runtime_rebuilt", f"{path}: made for another kind of install; original at {kept}")
        ctx.ui.say(sentence, "yellow")
        ctx.note_step(sentence)
    for line in follow.lines:
        ctx.ui.say(line)
    new_settings = found.state == "newer" or follow.applied     # a plain re-encoding changes none
    write_runtime(ctx, follow.runtime, clears=FRESH_CLEARS if new_settings else ())
    if follow.settled:
        ctx.clear_flags(PENDING_FLAG)       # after the file is there: a kill in between applies it again
    return load_runtime(ctx.paths)


def update_runtime(ctx: StepContext, change: Callable[[Runtime], None]) -> Runtime:
    """Apply ``change`` to runtime.json and save it."""
    rt = ensure_baseline(ctx)
    change(rt)
    save_runtime(ctx.paths, rt)
    return rt
