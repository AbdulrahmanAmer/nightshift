import json

import pytest

from ns_common.errors import NsError, describe, load_errors
from ns_common.paths import contracts_dir


def test_errors_json_is_well_formed():
    data = json.loads((contracts_dir() / "errors.json").read_text(encoding="utf-8"))
    for code, d in data.items():
        assert code.startswith("NS-E") or code.startswith("NS-W"), code
        assert set(d) == {"message", "hint"}, code


def test_describe_known_code():
    d = describe("NS-E102")
    assert d.kind == "error"
    assert "NVIDIA" in d.message


def test_nserror_str_and_data():
    err = NsError("NS-E301", "timeout after 600s", url="http://127.0.0.1:8080")
    assert str(err).startswith("NS-E301: ")
    assert "timeout after 600s" in str(err)
    assert err.data == {"url": "http://127.0.0.1:8080"}
    assert err.definition.kind == "error"


def test_warning_kind():
    assert describe("NS-W401").kind == "warning"


def test_unknown_code_raises():
    with pytest.raises(KeyError):
        describe("NS-E999")
    assert "NS-E999" in load_errors() or True  # load_errors never raises


def test_studio_and_transcription_codes_exist():
    for code in (
        "NS-E320", "NS-E321", "NS-E322", "NS-E323", "NS-E324", "NS-E325", "NS-E326", "NS-E408", "NS-E409",
    ):
        d = describe(code)
        assert d.kind == "error" and d.message and d.hint, code
    assert describe("NS-E408").message == "Transcription failed."


def test_clip_rights_and_unexpected_codes_exist():
    assert describe("NS-E410").kind == "error" and "right" in describe("NS-E410").message
    assert describe("NS-E327").kind == "error"


def test_fake_voice_tone_burst_warning_has_its_own_code_next_to_the_other_voice_warnings():
    d = describe("NS-W303")  # CR-H7-1: "the fake voice made tone bursts"
    assert d.kind == "warning"
    assert "tone bursts" in d.message and "speech recognition" in d.message
    assert "Speech" in d.hint and "NS-E403" in d.hint and "test voice" in d.hint
    # the 3xx family is the text-and-voice stages; NS-W301 and NS-W302 are the engine's own warnings and are
    # not reused for this one
    assert describe("NS-W301").message != d.message and describe("NS-W302").message != d.message
