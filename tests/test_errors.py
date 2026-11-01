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
