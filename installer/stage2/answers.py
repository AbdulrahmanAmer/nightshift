"""Installer answers (component C1): the ``answers.json`` file, its search path and secret blanking."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from installer.stage2.web.modelcheck import MAX_BYTES, shape_problem, too_big_message
from ns_common import first_start
from ns_common.atomic import read_text_retry, write_text_atomic
from ns_common.config_compat import disk_dict
from ns_common.models import VOICE_NAME_PATTERN, VOICE_NAME_RULE, Answers, suggest_voice_name
from ns_common.paths import Paths
from ns_common.secrets_names import KEY_NAMES

SECRET_KEYS = KEY_NAMES
_BLANK_AS_NONE = (*KEY_NAMES, "models_source", "home_path", "restore_from")


class AnswersError(ValueError):
    """answers.json is unreadable or invalid."""


class InstallerAnswers(Answers):
    """``ns_common`` Answers plus the installer-only fields (change request CR-C1-2)."""

    home_path: str | None = None
    models_source: str | None = None
    llm_tier: Literal["small", "balanced", "best"] = "small"
    restore_from: str | None = None
    # Download the VoiceDesign talker now. None (absent) means "infer": yes when the voice is a designed one,
    # because that voice cannot be created without the model; an explicit false is honoured (NS-W114).
    include_voicedesign: bool | None = None
    # Engine test (docs/DEV_MODE.md, "Real engines, fake voice"): the real language model, embeddings and
    # speech recognition with the fake voice engine. No voice model and no qwentts.cpp are downloaded and
    # runtime.json says `tts.engine: fake`. An answers.json setting only; the setup window never asks it.
    fake_voice: bool = False
    # Engine test, the twin of `fake_voice`: the fake clip source stands in for Pexels and Pixabay, so story
    # jobs run end to end without a stock-footage key (the footage is placeholder). runtime.json says
    # `fake_footage: true`. An answers.json setting only; the setup window never asks it.
    fake_footage: bool = False
    # Engine test: the story text stages use the thresholds a 1 to 2 GB language model can meet (word budgets,
    # chapter and beat counts, the length and reading-grade floors; `ns_studio.text.story_profile`), so a real
    # tiny model can get a story past the text validators. runtime.json says `tiny_story: true`. Needs
    # `allow_cpu`. An answers.json setting only: the setup window never asks it, the dashboard cannot set it.
    tiny_story: bool = False
    # What the setup window showed for Automatic runs when the owner answered `nightly`: `new` (no install
    # there), `off`, or `on@HH:MM`. Only the window writes it. The scheduler step applies the window's answer
    # only while Windows still has that state (a Settings change made meanwhile wins), then removes both keys.
    nightly_seen: str | None = Field(default=None, pattern=r"^(new|off|on@([01]\d|2[0-3]):[0-5]\d)$")
    # A second Nightshift on a PC (card second-install-takeover): true is the owner's yes, in this run, to
    # "make the Desktop shortcuts open this install" when they open another Nightshift folder (NIGHTSHIFT_HOME
    # and the toast registration follow the same yes). Absent or false keeps them. Used once by the scheduler
    # step and then removed, so an old answers file never takes them back at a later Repair.
    shortcuts_here: bool | None = None


def wants_voicedesign(ans: InstallerAnswers) -> bool:
    """Whether setup fetches the VoiceDesign talker: the explicit answer, else yes for a designed voice."""
    if ans.include_voicedesign is not None:
        return ans.include_voicedesign
    return ans.voice.mode == "design"


def find_answers(app_path: Path, paths: Paths, explicit: Path | None = None) -> Path | None:
    """Locate answers.json: explicit path, then next to INSTALL.cmd, then in the home config folder."""
    if explicit is not None:
        return explicit
    for cand in (app_path / "answers.json", paths.config / "answers.json"):
        if cand.is_file():
            return cand
    return None


def load_raw(path: Path) -> dict[str, Any]:
    """Read an answers file into a dict; blank strings become None. Raises AnswersError."""
    try:
        data = json.loads(read_text_retry(path, "utf-8-sig"))
    except (OSError, ValueError) as e:
        raise AnswersError(f"cannot read {path}: {e}") from e
    if not isinstance(data, dict):
        raise AnswersError(f"{path} must contain a JSON object")
    for key in _BLANK_AS_NONE:
        if isinstance(data.get(key), str) and not data[key].strip():
            data[key] = None
    return data


def _check_voice_name(raw: dict[str, Any]) -> None:
    """Refuse a voice name the voice service would refuse, here, where the answers are read (defect D-24).

    The name is never changed behind the owner's back: the message quotes what was typed and offers one.
    A preset voice never uses the name, so a leftover one is not checked.
    """
    voice = raw.get("voice")
    if not isinstance(voice, dict) or voice.get("mode", "preset") == "preset":
        return
    typed = voice.get("name")
    if typed is None or (isinstance(typed, str) and re.fullmatch(VOICE_NAME_PATTERN, typed)):
        return
    shown = typed if isinstance(typed, str) else repr(typed)
    hint = f" Try '{suggest_voice_name(typed)}'." if isinstance(typed, str) else ""
    raise AnswersError(f"The voice name '{shown}' cannot be used: {VOICE_NAME_RULE}.{hint}")


def default_voice_name(display_name: str) -> str:
    """Voice name when none was given: the channel slug plus '-voice', at most 40 characters."""
    return f"{slugify(display_name)[:34].strip('-')}-voice"


def build_answers(raw: dict[str, Any], *, saved: bool = False) -> InstallerAnswers:
    """Validate a merged answers dict. Raises AnswersError with a readable message.

    `saved` is True for the answers.json an earlier run wrote (a resume, repair, update or reinstall reads it
    again): a voice name in it was accepted then, 0.1.x accepted names the rule now refuses, and the owner
    typed nothing now, so the name rule is applied to answers typed or supplied in this run only.
    """
    if not saved:
        _check_voice_name(raw)
    try:
        ans = InstallerAnswers.model_validate(raw)
    except ValueError as e:
        raise AnswersError(str(e)) from e
    if not 1 <= ans.masters_per_week <= 3:
        raise AnswersError("masters_per_week must be 1, 2 or 3")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", ans.run_time):
        raise AnswersError("run_time must be HH:MM (24 hour clock)")
    if ans.voice.mode == "clone" and not (
        ans.voice_rights_confirmed and ans.voice_ref_audio_path and ans.voice_ref_text
    ):
        raise AnswersError(
            "a cloned voice needs voice_ref_audio_path, voice_ref_text and voice_rights_confirmed=true"
        )
    if ans.voice.mode == "design" and not ans.voice.description:
        raise AnswersError("a designed voice needs voice.description")
    if ans.tiny_story and not ans.allow_cpu:
        raise AnswersError(
            "tiny_story is for an engine test on a PC with no NVIDIA card: set allow_cpu to true as well, "
            "or remove tiny_story"
        )
    if ans.llm_custom is not None:
        problem = shape_problem(ans.llm_custom.repo, ans.llm_custom.file)
        if problem:
            raise AnswersError(f"llm_custom: {problem}")
        if ans.llm_custom.size > MAX_BYTES:
            raise AnswersError(f"llm_custom: {too_big_message(ans.llm_custom.size)}")
    return ans


def dump_blanked(ans: InstallerAnswers) -> dict[str, Any]:
    """Answers as a JSON-ready dict with the API keys blanked."""
    data = ans.model_dump(mode="json")
    for key in SECRET_KEYS:
        data[key] = ""
    return data


def write_answers(path: Path, ans: InstallerAnswers) -> Path:
    """Write answers with blank secrets. An answer added after 0.1.14 is written only when it is in use, so
    an older setup can read the file after a rollback (`config_compat.disk_dict`, F-10c)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    first_start.guard(path)
    data = disk_dict(ans, dump_blanked(ans))
    write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return path


#: Setup input that is used once (the scheduler step, s09) and then removed from the saved answers: it
#: answers a question about the moment setup ran, and must never be read again by a later run.
SETUP_INPUT = ("nightly", "nightly_seen", "shortcuts_here")


def forget_setup_input(path: Path) -> bool:
    """Remove `SETUP_INPUT` from a saved answers file. Returns True when the file changed. A file that is
    missing or unreadable is left alone (the step that calls this already acted on the answers it loaded)."""
    try:
        data = json.loads(read_text_retry(path, "utf-8-sig"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or not any(key in data for key in SETUP_INPUT):
        return False
    for key in SETUP_INPUT:
        data.pop(key, None)
    first_start.guard(path)
    write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return True


def blank_secrets_in_file(path: Path) -> bool:
    """Blank the API keys inside an existing answers file. Returns True when the file changed."""
    if not path.is_file():
        return False
    try:
        data = json.loads(read_text_retry(path, "utf-8-sig"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    changed = False
    for key in SECRET_KEYS:
        if data.get(key):
            data[key] = ""
            changed = True
    if changed:
        first_start.guard(path)
        write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return changed


def slugify(name: str) -> str:
    """Channel slug from a display name (matches ns_common SLUG_RE)."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "channel"
