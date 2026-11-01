import pytest

import ns_studio.errors as studio_errors
from ns_common.errors import describe
from ns_studio.errors import StudioError

CODES = [f"NS-E{n}" for n in range(320, 327)]


@pytest.mark.parametrize("code", CODES)
def test_studio_error_uses_the_registry_text(code: str):
    err = StudioError(code, "detail")
    definition = describe(code)
    assert (err.message, err.hint) == (definition.message, definition.hint)
    assert err.hint
    assert str(err) == f"{code}: {definition.message} (detail)"


def test_no_local_fallback_table_remains():
    assert not hasattr(studio_errors, "_LOCAL")


def test_unknown_code_is_a_key_error():
    with pytest.raises(KeyError):
        StudioError("NS-E999")
