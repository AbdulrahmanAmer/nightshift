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

def test_manifest_pins_webview2_with_a_real_hash():
    from ns_common.config import load_manifest

    pin = load_manifest().bins["webview2"]
    assert pin is not None
    assert pin.version == "1.0.2903.40"
    assert pin.url == (
        "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/1.0.2903.40/"
        "microsoft.web.webview2.1.0.2903.40.nupkg"
    )
    assert pin.sha256 == "ef128016dd1e51c59178c827ed5b8aa3322c57afa8675d930f8109505542ad74"
    assert pin.optional is False


def test_binpin_old_entries_stay_valid():
    pin = m.BinPin.model_validate({"version": "1", "url": "https://x/y.zip", "sha256": "ab" * 32})
    assert pin.optional is False


def test_manifest_uses_tbd_release_only_on_optional_pins():
    from ns_common.config import load_manifest

    for name, pin in load_manifest().bins.items():
        if pin is not None and pin.sha256 == "TBD-release":
            assert pin.optional, f"bins.{name}: TBD-release is allowed only on optional entries"


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


# --- CR-C3-UI-4: Status.gpu ----------------------------------------------------------------------------


def test_status_gpu_is_optional_and_old_fields_stay():
    base = {
        "engine": {"state": "ready", "vram_gb": 11.5},
        "services": {"llama": {"state": "ready", "vram_mb": 9100}},
    }
    old = m.Status.model_validate(base)
    assert old.gpu is None
    assert old.engine.vram_gb == 11.5 and old.services["llama"].vram_mb == 9100
    gpu = {"name": "NVIDIA GeForce RTX 4080", "load_pct": 37.5, "vram_used_mb": 9300, "vram_total_mb": 16376}
    new = m.Status.model_validate(base | {"gpu": gpu})
    assert new.gpu is not None and new.gpu.temp_c is None and new.gpu.vram_total_mb == 16376
    assert m.Status.model_validate(base | {"gpu": {"name": "x"}}).gpu == m.GpuStatus(name="x")


def test_status_gpu_needs_a_name():
    with pytest.raises(ValidationError):
        m.Status.model_validate({"engine": {}, "gpu": {"load_pct": 1.0}})


def test_the_dashboard_status_object_validates_against_the_model(tmp_home):
    from ns_dashboard.fakes import seed_channel, seed_output
    from tests.test_dashboard_api import make_env

    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths)
    seed_output(state.db, state.paths)
    status = m.Status.model_validate(client.get("/api/status").json())
    assert status.channels and status.channels[0].slug == "after-dark"
    assert status.gpu is None


# --- CR-C3-UI-5: job request schedule_at ----------------------------------------------------------------


def test_job_run_request_schedule_at_null_means_run_now():
    req = m.JobRunRequest.model_validate({"channel": "after-dark"})
    assert req.schedule_at is None and req.format is None and req.count is None
    assert m.JobRunRequest.model_validate({"channel": "a", "schedule_at": None}).schedule_at is None


def test_job_run_request_parses_an_iso_time():
    body = {"channel": "a", "format": "shorts_only", "schedule_at": "2026-11-05T21:30:00"}
    req = m.JobRunRequest.model_validate(body)
    assert req.schedule_at is not None and (req.schedule_at.hour, req.schedule_at.minute) == (21, 30)
    with pytest.raises(ValidationError):
        m.JobRunRequest.model_validate({"channel": "a", "schedule_at": "tomorrow evening"})
    with pytest.raises(ValidationError):
        m.JobRunRequest.model_validate({"channel": "a", "count": 0})


# --- CR-C3-UI-6: channel stats qc_pass_rate -------------------------------------------------------------


def test_channel_stats_qc_fields_default_and_range():
    s = m.ChannelStats.model_validate({"week": 2, "watched_pct": 64, "spark": [38, 44]})
    assert s.qc_pass_rate is None and s.qc_pass_target == 0.9
    assert m.ChannelStats.model_validate({"qc_pass_rate": 0.75, "qc_pass_target": 0.8}).qc_pass_rate == 0.75
    for bad in ({"qc_pass_rate": 1.2}, {"qc_pass_rate": -0.1}, {"qc_pass_target": 90}):
        with pytest.raises(ValidationError):
            m.ChannelStats.model_validate(bad)


# --- CR-C3-UI-7: voice design and clone endpoints -------------------------------------------------------


def test_voice_design_request_and_response():
    body = {"name": "Mara", "description": "calm, low, a bit hoarse", "test_line": "Hi."}
    assert m.VoiceDesignRequest.model_validate(body).name == "Mara"
    res = m.VoiceMakeResponse.model_validate({"voice_id": "mara", "preview_wav_path": "cache/mara.wav"})
    assert res.voice_id == "mara" and res.preview_wav_path == "cache/mara.wav"
    with pytest.raises(ValidationError):
        m.VoiceDesignRequest.model_validate(body | {"name": ""})
    with pytest.raises(ValidationError):
        m.VoiceDesignRequest.model_validate({"name": "Mara", "description": "x"})


def test_voice_clone_needs_consent_true():
    ok = {"name": "Me", "sample_path": "voices/me.wav", "consent": True, "test_line": "Hello there."}
    assert m.VoiceCloneRequest.model_validate(ok).consent is True
    for bad in (ok | {"consent": False}, {k: v for k, v in ok.items() if k != "consent"}):
        with pytest.raises(ValidationError):
            m.VoiceCloneRequest.model_validate(bad)


def test_voice_sample_error_code_is_in_the_tts_block():
    from ns_common.errors import describe

    d = describe("NS-E312")
    assert d.kind == "error" and "too short or noisy" in d.message and d.hint
