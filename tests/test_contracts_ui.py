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


# --- CR-C1-UI-3: bins.webview2 and the TBD-release rule -------------------------------------------------

DLLS = [
    "lib/net462/Microsoft.Web.WebView2.Core.dll",
    "lib/net462/Microsoft.Web.WebView2.Wpf.dll",
    "runtimes/win-x64/native/WebView2Loader.dll",
]


def test_manifest_pins_webview2_as_an_optional_nupkg():
    from ns_common.config import load_manifest

    pin = load_manifest().bins["webview2"]
    assert pin is not None
    assert pin.version == "1.0.2903.40"
    assert pin.url == (
        "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/1.0.2903.40/"
        "microsoft.web.webview2.1.0.2903.40.nupkg"
    )
    assert pin.extract == DLLS
    assert pin.dest == "bin\webview2"
    assert pin.optional is True
    assert pin.sha256 == "TBD-release" or len(pin.sha256 or "") == 64


def test_binpin_old_entries_stay_valid():
    pin = m.BinPin.model_validate({"version": "1", "url": "https://x/y.zip", "sha256": "ab" * 32})
    assert pin.extract == [] and pin.dest is None and pin.optional is False


def test_tbd_release_is_accepted_only_for_optional_pins():
    base = {"version": "1", "url": "https://x/y.nupkg", "sha256": "TBD-release"}
    assert m.BinPin.model_validate(base | {"optional": True}).sha256 == "TBD-release"
    with pytest.raises(ValidationError):
        m.BinPin.model_validate(base)


def test_resolve_manifest_check_tolerates_tbd_only_when_optional():
    import httpx

    from tools import resolve_manifest as rm

    def entry(**over):
        return {"version": "1", "url": "https://example.test/x.nupkg", "sha256": "TBD-release"} | over

    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    ok = rm.check({"profiles": {}, "bins": {"webview2": entry(optional=True)}}, client)
    assert ok == []
    bad = rm.check({"profiles": {}, "bins": {"webview2": entry()}}, client)
    assert any("webview2" in p and "sha256" in p for p in bad)
