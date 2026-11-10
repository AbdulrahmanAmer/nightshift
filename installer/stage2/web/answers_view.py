"""The Choices form: validation, and the camelCase <-> snake_case mapping to answers.json.

Standard library only (docs/design/installer-ui.md 2.3 and 5.6). The wizard validates again with pydantic
(``installer.stage2.answers.build_answers``), so this module only has to be strict enough to give the window
instant, plain-spoken field errors. API keys are never written here: they live in memory until the wizard
moves them into the Windows keyring.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import ns_common.diskneed as diskneed
import ns_common.first_start as first_start
from installer.stage2.web import nightly_view, shortcuts_view
from installer.stage2.web.model import PRESET_VOICES, TIERS
from installer.stage2.web.modelcheck import MAX_BYTES, shape_problem, too_big_message
from installer.stage2.web.nightly_view import valid_seen
from installer.stage2.web.pathcheck import DRIVE_PATH_RE as DRIVE_PATH_RE
from installer.stage2.web.pathcheck import home_problem
from installer.stage2.web.plan import home_need
from ns_common.atomic import copy_atomic, read_text_retry, write_text_atomic
from ns_common.new_fields import plain_disk_dict

RUN_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_HOME_LEN = 120
KINDS_UI = ("internet", "folder", "drive-zip")
THEMES = ("dark", "light", "soft")
TIER_NAMES = {"small": "Small", "balanced": "Balanced", "best": "Best"}
SERVICES = {"pexelsKey": "Pexels", "pixabayKey": "Pixabay"}
# Both keys are optional: setup never blocks on them. A pasted key is still checked live (/api/check-key)
# and a rejected one is a field error; without any key the studio's clips stage asks for one (NS-E502)
# and Settings takes it. Pexels stopped handing out new API keys in 2026, so Pixabay is the one to add.


def kind_to_file(kind: str) -> str:
    """UI spelling (drive-zip) to the answers.json spelling (drive_zip)."""
    return kind.replace("-", "_")


def kind_from_file(kind: str) -> str:
    """answers.json spelling to the UI spelling."""
    return kind.replace("_", "-")


def _err(field: str, message: str, code: str | None = None) -> dict[str, Any]:
    return {"field": field, "message": message, "code": code}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


class Env:
    """What validation needs to know about this PC (injectable for tests)."""

    def __init__(
        self,
        *,
        ram_gb: float,
        tiers: dict[str, Any],
        drive_letters: set[str] | None = None,
        free_gb: Callable[[str], float | None] | None = None,
        isdir: Callable[[str], bool] = os.path.isdir,
        keys_stored: dict[str, bool] | None = None,
        layout_problem: Callable[[str], str | None] | None = None,
        dev: bool = False,
        need_fn: Callable[[str, str, dict[str, Any] | None, dict[str, Any]], diskneed.Need] | None = None,
    ) -> None:
        self.dev = dev              # a dev install needs 10 GB free, not 60 (docs/DEV_MODE.md)
        self.ram_gb = ram_gb
        self.tiers = tiers
        self.drive_letters = drive_letters
        self.free_gb = free_gb or (lambda _p: None)
        self.isdir = isdir
        self.keys_stored = keys_stored or {}
        self.layout_problem = layout_problem or (lambda _home: None)
        self.need_fn = need_fn      # (home, tier, custom, draft) -> the space that install needs (H-6)

    def need_for(
        self, home: str, tier: str, custom: dict[str, Any] | None, draft: dict[str, Any]
    ) -> diskneed.Need:
        """The space this install needs at ``home``: the window's own sum when given, else the defaults."""
        if self.need_fn is not None:
            return self.need_fn(home, tier, custom, draft)
        return home_need(diskneed.shipped_manifest(), home, tier, custom=custom, dev=self.dev)


def validate(draft: dict[str, Any], env: Env) -> list[dict[str, Any]]:
    """Every problem with the draft, in form order. An empty list means the draft is acceptable."""
    errors: list[dict[str, Any]] = []
    channel = draft.get("channel") if isinstance(draft.get("channel"), dict) else {}
    owner = draft.get("ownerName")
    if owner is not None and len(_text(owner)) > 40:
        errors.append(_err("ownerName", "Use 40 characters or fewer"))
    name = _text(channel.get("displayName"))
    if not 1 <= len(name) <= 60:
        errors.append(_err("channel.displayName", "Give the channel a name"))
    niche = _text(channel.get("niche"))
    if not 2 <= len(niche) <= 60:
        errors.append(_err("channel.niche", "Say in a few words what the channel is about"))
    language = _text(channel.get("language")) or "en"
    if not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?", language):
        errors.append(_err("channel.language", "Use a language code such as en"))

    tier = draft.get("llmTier")
    if tier not in TIERS:
        errors.append(_err("llmTier", "Pick Small, Balanced or Best"))
        tier = "small"
    custom = draft.get("llmCustom")
    custom_entry: dict[str, Any] | None = None
    if custom is not None:
        problem = custom_problem(custom)
        if problem:
            errors.append(_err("llmCustom", problem))
        else:
            custom_entry = {
                "repo": _text(custom["repo"]), "file": _text(custom["file"]), "size": int(custom["size"]),
            }
    errors += _home_errors(_text(draft.get("homePath")), str(tier), env, custom_entry, draft)
    info = env.tiers.get(str(tier), {})
    if custom is None and tier != "small" and not info.get("allowed", True):
        reason = info.get("reason") or "Not available on this PC"
        message = (
            f"{TIER_NAMES[str(tier)]} needs 32 GB of memory. This PC has {env.ram_gb:g} GB"
            if "memory" in reason.lower() else reason
        )
        errors.append(_err("llmTier", message))

    voice = draft.get("voice") if isinstance(draft.get("voice"), dict) else {}
    if voice.get("speaker") not in PRESET_VOICES:
        errors.append(_err("voice.speaker", "Pick one of the four narrators"))
    source = draft.get("modelsSource") if isinstance(draft.get("modelsSource"), dict) else {}
    kind = source.get("kind", "internet")
    if kind not in KINDS_UI:
        errors.append(_err("modelsSource.path", "Pick where the model files come from"))
    elif kind != "internet" and not (_text(source.get("path")) and env.isdir(_text(source.get("path")))):
        errors.append(_err("modelsSource.path", "That folder was not found"))
    if not RUN_TIME_RE.match(_text(draft.get("runTime"))):
        errors.append(_err("runTime", "Use the 24 hour clock, for example 03:00"))
    if draft.get("mastersPerWeek") not in (1, 2, 3):
        errors.append(_err("mastersPerWeek", "Pick 1, 2 or 3 long videos per week"))
    return errors


def custom_problem(custom: Any) -> str | None:
    """Why a custom model entry (resolved by /api/model-check) is not acceptable; None when it is fine."""
    if not isinstance(custom, dict):
        return "Check the model file first"
    repo, file, size = custom.get("repo"), custom.get("file"), custom.get("size")
    if not isinstance(repo, str) or not isinstance(file, str):
        return "Check the model file first"
    problem = shape_problem(repo.strip(), file.strip())
    if problem:
        return problem
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        return "Check the model file first"
    return too_big_message(size) if size > MAX_BYTES else None


def _home_errors(
    home: str, tier: str, env: Env, custom: dict[str, Any] | None = None, draft: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    problem = home_problem(home, MAX_HOME_LEN) or env.layout_problem(home)
    if problem:
        return [_err("homePath", problem)]
    if env.drive_letters is not None and home[0].upper() not in env.drive_letters:
        return [_err("homePath", "Pick a folder on a fixed drive of this PC")]
    free = env.free_gb(home)
    if free is None:
        return []
    need = env.need_for(home, tier, custom, draft or {})
    if free * diskneed.GIB < need.total:
        pick = f"Pick a folder on a drive with {need.gb} GB free. This one has {free:.0f} GB"
        return [_err("homePath", pick, "NS-E104")]
    return []


# ---------------------------------------------------------------------------------------------------------
# answers.json
# ---------------------------------------------------------------------------------------------------------
def draft_to_answers(
    draft: dict[str, Any], *, allow_cpu: bool = False, models_source: str | None = None
) -> dict[str, Any]:
    """The answers.json content for an accepted draft: snake_case, with blank API keys.

    ``models_source`` replaces the draft's folder (the Drive zips are unpacked into a cache folder first).
    """
    channel = draft["channel"]
    source = draft.get("modelsSource") or {"kind": "internet", "path": None}
    kind = kind_to_file(str(source.get("kind", "internet")))
    path = models_source if models_source is not None else (_text(source.get("path")) or None)
    owner = _text(draft.get("ownerName")) or None
    custom = draft.get("llmCustom")
    llm_custom = (
        {"repo": _text(custom["repo"]), "file": _text(custom["file"]), "size": int(custom["size"])}
        if isinstance(custom, dict) else None
    )
    return {
        "home_path": _text(draft["homePath"]),
        "models_source": path if kind != "internet" else None,
        "models_source_kind": kind,
        "llm_tier": "small" if llm_custom else draft["llmTier"],
        "llm_custom": llm_custom,
        "channel": {
            "display_name": _text(channel["displayName"]),
            "niche": _text(channel["niche"]),
            "language": _text(channel.get("language")) or "en",
            # no seed_keywords: the Choices screen has no such field, and a key the window writes is a choice
            # it made (D-14): an empty list would wipe the earlier install's seeds and the owner's edits. The
            # window's wizard never asks, so a first install starts with none
        },
        "voice": {"mode": "preset", "speaker": draft["voice"]["speaker"], "description": None, "name": None,
                  "speed": 1.0},
        "voice_ref_audio_path": None,
        "voice_ref_text": None,
        "voice_rights_confirmed": False,
        "pexels_api_key": "",
        "pixabay_api_key": "",
        "masters_per_week": int(draft.get("mastersPerWeek", 1)),
        "run_time": _text(draft["runTime"]),
        "install_code": bool(draft.get("installCode", True)),
        "include_voicedesign": bool(draft.get("includeVoicedesign", False)),
        "allow_cpu": bool(allow_cpu),
        "restore_from": None,
        "owner_name": owner,
        "ui_theme": draft.get("theme", "dark") if draft.get("theme") in THEMES else "dark",
        # The switch "Run Nightshift by itself every night", with the state the window showed beside it
        # (`nightlySeen`): the scheduler step applies the answer only while Windows still has that state. A
        # page without the switch sends neither, which changes nothing on an install and is off on a new one.
        "nightly": draft["nightly"] if isinstance(draft.get("nightly"), bool) else None,
        "nightly_seen": valid_seen(draft.get("nightlySeen")),
        # The switch "Make the Desktop shortcuts open this install": only a yes is written; the scheduler
        # step uses it once and removes it. Off, or a page without the switch, keeps another install's.
        "shortcuts_here": True if draft.get("shortcutsHere") is True else None,
    }


#: What `answers_to_view` shows when nothing was read: the shortcuts are, or will be, this install's.
NO_OTHER_INSTALL: dict[str, Any] = {"state": "mine", "path": None, "line": None}


def folder_now(home: Path, app_path: Path | None, desktop: Path | None) -> dict[str, Any]:
    """What the window shows for the install folder `home`: Automatic runs there (``nightlySeen``) and whose
    the Desktop shortcuts are seen from there (``shortcutsNow``). Both belong to the FOLDER, so the window
    reads them again for every folder the owner picks (the folder check carries them), not once at load."""
    return {
        "nightlySeen": nightly_view.current_choice(Path(home)),
        "shortcutsNow": shortcuts_view.for_window(Path(home), app_path, desktop),
    }


def _wants_voicedesign(raw: dict[str, Any]) -> bool:
    """The checkbox: the explicit answer, else on when the stored voice is a designed one."""
    flag = raw.get("include_voicedesign")
    if flag is not None:
        return bool(flag)
    return (raw.get("voice") or {}).get("mode") == "design"


def answers_to_view(
    raw: dict[str, Any], *, pexels_set: bool = False, pixabay_set: bool = False, default_tier: str = "small",
    nightly_now: str = "new", shortcuts_now: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """AnswersView (camelCase) from an answers.json dict; ``default_tier`` fills a missing ``llm_tier``.

    ``nightly_now`` is what Automatic runs are in the install folder (``nightly_view.current_choice``). The
    switch starts where they are: on when they are on, else off. An answer in ``raw`` moves it only when it
    is the window's own earlier one (it carries ``nightly_seen``; a resumed setup shows what was chosen) or
    the folder holds no install (a prefilled answers file may ask for it there); over an install an answers
    file never turns the switch.

    ``shortcuts_now`` is whose the Desktop shortcuts are, seen from the install folder
    (``shortcuts_view.for_window``; `folder_now` reads both for a folder). The switch "Make the Desktop
    shortcuts open this install" exists only while another install holds them, and is on only for a yes.
    """
    theirs = shortcuts_now if shortcuts_now and shortcuts_now.get("state") in shortcuts_view.KEPT else None
    seen = valid_seen(raw.get("nightly_seen")) or (valid_seen(nightly_now) or "new")
    answered = isinstance(raw.get("nightly"), bool) and (valid_seen(raw.get("nightly_seen")) or seen == "new")
    nightly = bool(raw["nightly"]) if answered else seen.startswith("on@")
    channel = raw.get("channel") or {}
    voice = raw.get("voice") or {}
    kind = kind_from_file(str(raw.get("models_source_kind") or "internet"))
    return {
        "ownerName": raw.get("owner_name"),
        "channel": {
            "displayName": channel.get("display_name", ""),
            "niche": channel.get("niche", ""),
            "language": channel.get("language", "en"),
        },
        "homePath": raw.get("home_path") or "",
        "llmTier": raw.get("llm_tier") or default_tier,
        "llmCustom": raw.get("llm_custom") or None,
        "voice": {"speaker": voice.get("speaker") or PRESET_VOICES[0]},
        "modelsSource": {"kind": kind if kind in KINDS_UI else "internet", "path": raw.get("models_source")},
        "runTime": raw.get("run_time", "03:00"),
        "mastersPerWeek": int(raw.get("masters_per_week", 1)),
        "installCode": bool(raw.get("install_code", True)),
        "includeVoicedesign": _wants_voicedesign(raw),
        "theme": raw.get("ui_theme", "dark"),
        "nightly": nightly,
        "nightlySeen": seen,
        "shortcutsHere": theirs is not None and raw.get("shortcuts_here") is True,
        "shortcutsNow": dict(theirs) if theirs is not None else dict(NO_OTHER_INSTALL),
        "pexelsKeySet": pexels_set or bool(raw.get("pexels_api_key")),
        "pixabayKeySet": pixabay_set or bool(raw.get("pixabay_api_key")),
    }


def read_answers(path: Path) -> dict[str, Any]:
    """Read an answers.json (UTF-8, with or without a BOM). Raises ValueError when it is not an object."""
    data = json.loads(read_text_retry(path, "utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def write_answers(path: Path, data: dict[str, Any]) -> Path:
    """Write an answers.json. The keys are always blank, and an answer added after 0.1.14 is written only
    when it is in use (`ns_common.new_fields.plain_disk_dict`, the plain-dict form of
    `config_compat.disk_dict`, F-10c), so an older setup can read the file after a rollback."""
    clean = plain_disk_dict("answers.json", dict(data))
    clean["pexels_api_key"] = ""
    clean["pixabay_api_key"] = ""
    path.parent.mkdir(parents=True, exist_ok=True)
    first_start.guard(path)
    write_text_atomic(path, json.dumps(clean, indent=2, ensure_ascii=False) + "\n")
    return path


#: The earlier install's ``config/answers.json``, kept by the window before it writes the new choices over it
#: (D-14). The wizard compares the new answers with it, rebuilds what comes from the ones that changed, and
#: deletes it (``installer/stage2/reinstall.py``).
KEPT_NAME = "answers.kept.json"


def keep_earlier_answers(target: Path) -> None:
    """Before new choices replace ``target`` (config/answers.json) of an earlier install, keep that file.

    The oldest copy is the one that counts: a setup that stopped before it applied the choices must still
    compare the next ones with the answers the kept files were made from.
    """
    kept = target.with_name(KEPT_NAME)
    if target.is_file() and not kept.exists():
        copy_atomic(target, kept)  # a copy torn by a crash would be the baseline for ever


def keys_in(raw: dict[str, Any]) -> dict[str, str | None]:
    """The API keys an answers.json carries (unattended runs); empty strings count as absent."""
    return {
        "pexels_api_key": (raw.get("pexels_api_key") or None),
        "pixabay_api_key": (raw.get("pixabay_api_key") or None),
    }
