"""C3: contracts added for the installer UI and the dashboard redesign (CR-C1-UI-* and CR-C3-UI-*)."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ns_common import models as m

ROOT = Path(__file__).resolve().parents[1]
MIN_ANSWERS = {"channel": {"display_name": "x", "niche": "y"}}


# --- CR-C1-UI-1: installer answers ---------------------------------------------------------------------


def test_answers_ui_fields_default_so_old_files_stay_valid():
    a = m.Answers.model_validate(MIN_ANSWERS)
    assert a.owner_name is None
    assert a.ui_theme == "dark"
    assert a.models_source_kind == "internet"


def test_answers_ui_fields_accept_the_documented_values():
    for theme in ("light", "dark", "soft"):
        assert m.Answers.model_validate(MIN_ANSWERS | {"ui_theme": theme}).ui_theme == theme
    for kind in ("internet", "folder", "drive_zip"):
        assert m.Answers.model_validate(MIN_ANSWERS | {"models_source_kind": kind}).models_source_kind == kind
    assert m.Answers.model_validate(MIN_ANSWERS | {"owner_name": "Sam"}).owner_name == "Sam"


@pytest.mark.parametrize(
    "bad",
    [
        {"ui_theme": "neon"},
        {"models_source_kind": "usb"},
        {"models_source_kind": "drive-zip"},
        {"owner_name": "x" * 41},
    ],
)
def test_answers_ui_fields_reject_other_values(bad):
    with pytest.raises(ValidationError):
        m.Answers.model_validate(MIN_ANSWERS | bad)


def test_answers_example_lists_the_ui_fields():
    example = json.loads((ROOT / "installer" / "answers.example.json").read_text(encoding="utf-8"))
    assert example["owner_name"] is None
    assert example["ui_theme"] == "dark"
    assert example["models_source_kind"] == "internet"


# --- CR-C1-UI-2: error codes --------------------------------------------------------------------------


def test_installer_ui_error_codes_exist_with_message_and_fix():
    from ns_common.errors import describe

    av = describe("NS-E113")
    assert av.kind == "error" and "Antivirus" in av.message and "allow list" in av.hint
    video = describe("NS-W403")
    assert video.kind == "warning" and "test video" in video.message
    assert "Diagnostics" in video.hint
