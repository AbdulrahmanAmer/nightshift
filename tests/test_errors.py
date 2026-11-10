import json

import pytest

from ns_common.errors import NsError, describe, load_errors
from ns_common.paths import contracts_dir


def test_errors_json_is_well_formed():
    data = json.loads((contracts_dir() / "errors.json").read_text(encoding="utf-8"))
    for code, d in data.items():
        assert code.startswith("NS-E") or code.startswith("NS-W"), code
        assert set(d) == {"message", "hint"}, code


def _no_repeated_key(pairs):
    """``json`` keeps the last of two equal keys without a word; two merged branches can add the same code."""
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"repeated key {key!r}")
        seen[key] = value
    return seen


def test_errors_json_has_no_code_twice():
    """Git merges two lines that add the same code without a conflict (NS-E120 in two
    branches, 2026-11-09)."""
    text = (contracts_dir() / "errors.json").read_text(encoding="utf-8")
    data = json.loads(text, object_pairs_hook=_no_repeated_key)
    assert len(data) > 50


def test_the_duplicate_key_loader_does_fail_on_a_repeated_code():
    two = '{"NS-E120": {"message": "a", "hint": "b"}, "NS-E120": {"message": "c", "hint": "d"}}'
    with pytest.raises(ValueError, match="repeated key 'NS-E120'"):
        json.loads(two, object_pairs_hook=_no_repeated_key)


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


def test_ns_e702_claims_no_outcome():
    """D-39: NS-E702 is raised by refusals that changed nothing and by a rollback that did not finish, so its
    fixed message names no outcome; the detail says which version is in place. The codes of the one-at-a-time
    rule and of the half-filled environment exist."""
    d = describe("NS-E702")
    assert "rolled back" not in d.message and "previous version" not in d.message
    assert "which version is in place" in d.hint and "ns doctor --bundle" in d.hint
    known = load_errors()
    assert "Another update, rollback or repair" in known["NS-E705"].message
    assert "Nothing was changed" in known["NS-E705"].hint and "Nothing was changed" in known["NS-E706"].hint
    assert "Python environments" in known["NS-E706"].message and known["NS-W210"].kind == "warning"
