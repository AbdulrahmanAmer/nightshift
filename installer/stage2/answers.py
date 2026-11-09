"""Installer answers (component C1): the ``answers.json`` file, its search path and secret blanking."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from installer.stage2.web.modelcheck import MAX_BYTES, shape_problem, too_big_message
from ns_common.models import Answers
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
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise AnswersError(f"cannot read {path}: {e}") from e
    if not isinstance(data, dict):
        raise AnswersError(f"{path} must contain a JSON object")
    for key in _BLANK_AS_NONE:
        if isinstance(data.get(key), str) and not data[key].strip():
            data[key] = None
    return data


def build_answers(raw: dict[str, Any]) -> InstallerAnswers:
    """Validate a merged answers dict. Raises AnswersError with a readable message."""
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
    """Write answers with blank secrets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dump_blanked(ans), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def blank_secrets_in_file(path: Path) -> bool:
    """Blank the API keys inside an existing answers file. Returns True when the file changed."""
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
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
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return changed


def slugify(name: str) -> str:
    """Channel slug from a display name (matches ns_common SLUG_RE)."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "channel"
