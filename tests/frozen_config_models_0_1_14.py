"""The other strict settings models of Nightshift 0.1.14, frozen: answers.json and formats/*.yaml.

The companion of `tests/frozen_models_0_1_14.py` (runtime.json, install_state.json and the channels), which
F-10a froze and which stays as it is. These are the classes of tag v0.1.14, class for class:
`AnswersChannel`, `LlmCustom`, `Answers` and the `Format` family from `ns_common/models.py`, and
`InstallerAnswers` from `installer/stage2/answers.py` (the model setup reads answers.json with). Only the
imports and the spacing of comments differ (ruff format). F-10c compares the current models with these to
find every field a newer version added (`ns_common.config_compat.NEW_FIELDS`), and loads what a rollback
leaves behind with them. Do not edit: a newer copy of the same kind (frozen 0.1.15) goes in a new file.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from tests.frozen_models_0_1_14 import Strict, Voice


# ---------- answers.json (ns_common/models.py at v0.1.14) ----------
class AnswersChannel(Strict):
    display_name: str
    niche: str
    language: str = "en"
    seed_keywords: list[str] = []


class LlmCustom(Strict):
    repo: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
    file: str = Field(min_length=6, max_length=240)
    size: int = Field(gt=0)


class Answers(Strict):
    channel: AnswersChannel
    voice: Voice = Voice()
    voice_ref_audio_path: str | None = None
    voice_ref_text: str | None = None
    voice_rights_confirmed: bool = False
    pexels_api_key: str | None = None
    pixabay_api_key: str | None = None
    masters_per_week: int = 1
    run_time: str = Field(default="03:00", pattern=r"^\d{2}:\d{2}$")
    install_code: bool = True
    allow_cpu: bool = False
    # installer-only keys for unattended installs (spec 5 and 1.3)
    home_path: str | None = None
    models_source: str | None = None
    llm_tier: Literal["small", "balanced", "best"] = "small"
    llm_custom: LlmCustom | None = None  # CR-C1-UI-9: overrides the tier's model when set
    restore_from: str | None = None
    # installer UI (CR-C1-UI-1): greeting name, default dashboard theme, where the models come from
    owner_name: str | None = Field(default=None, max_length=40)
    ui_theme: Literal["light", "dark", "soft"] = "dark"
    models_source_kind: Literal["internet", "folder", "drive_zip"] = "internet"


# ---------- answers.json as setup reads it (installer/stage2/answers.py at v0.1.14) ----------
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


# ---------- formats/*.yaml (ns_common/models.py at v0.1.14) ----------
class FormatCanvas(Strict):
    w: int = Field(ge=1)
    h: int = Field(ge=1)
    fps: int = Field(default=30, ge=1)


class FormatDuration(Strict):
    min_s: float = Field(ge=0)
    max_s: float = Field(gt=0)
    buckets: list[int] | None = None


class FormatCaptions(Strict):
    styles_allowed: list[str] = []
    anchor_y_pct: float = Field(default=70.0, ge=0, le=100)
    words_on_screen: int = Field(default=4, ge=1)


# One panel of the output frame. `source`: full | facecam | face:<n> | screen | blur_fill.
class FormatRegion(Strict):
    name: str
    x: int = Field(default=0, ge=0)
    y: int = Field(default=0, ge=0)
    w: int = Field(ge=1)
    h: int = Field(ge=1)
    source: str = Field(default="full", pattern=r"^(full|facecam|screen|blur_fill|face:\d+)$")


class FormatLayout(Strict):
    regions: list[FormatRegion] = Field(min_length=1)
    seam_captions: bool = False


class FormatMargins(Strict):
    top: int = Field(default=0, ge=0)
    bottom: int = Field(default=0, ge=0)


class FormatShots(Strict):
    min_s: float = Field(gt=0)
    max_s: float = Field(gt=0)


class FormatEncoder(Strict):
    preset: str = "p4"


class FormatSheet(Strict):
    hashtags_required: list[str] = []
    description_rules: str = ""


class Format(Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    kind: Literal["short", "long", "square", "clip"]
    canvas: FormatCanvas
    duration: FormatDuration
    captions: FormatCaptions
    layout: FormatLayout
    safe_margins: FormatMargins = FormatMargins()
    shots: FormatShots | None = None
    encoder: FormatEncoder | None = None
    sheet: FormatSheet | None = None
