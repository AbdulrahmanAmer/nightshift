"""contracts/errors.json is read as a plain JSON object, and a JSON object silently keeps the LAST of two
equal keys: a second `"NS-E104": {...}` pasted into the file would replace the first one without any error.
This test reads the file with a loader that refuses a repeated key, and checks every key is an error code."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
ERRORS = ROOT / "contracts" / "errors.json"
CODE = re.compile(r"^NS-[EW]\d{3}$")


class DuplicateKey(ValueError):
    pass


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise DuplicateKey(f"duplicate key {key!r}")
        seen.add(key)
    return dict(pairs)


def strict_load(text: str) -> dict[str, Any]:
    return json.loads(text, object_pairs_hook=_no_duplicates)


def test_the_strict_loader_fails_on_a_repeated_key_at_any_depth():
    with pytest.raises(DuplicateKey, match="NS-E104"):
        strict_load('{"NS-E104": {"message": "a", "hint": "b"}, "NS-E105": {}, "NS-E104": {"message": "c"}}')
    with pytest.raises(DuplicateKey, match="hint"):
        strict_load('{"NS-E104": {"message": "a", "hint": "b", "hint": "c"}}')
    ok = strict_load('{"NS-E104": {"message": "a", "hint": "b"}}')
    assert ok == {"NS-E104": {"message": "a", "hint": "b"}}


def test_errors_json_has_no_duplicate_key():
    strict_load(ERRORS.read_text(encoding="utf-8"))


def test_every_key_of_errors_json_is_an_error_or_warning_code():
    codes = strict_load(ERRORS.read_text(encoding="utf-8"))
    assert codes, "contracts/errors.json is empty"
    bad = sorted(k for k in codes if not CODE.match(k))
    assert not bad, f"keys that are not NS-E### / NS-W### codes: {bad}"
    for code, entry in codes.items():
        assert isinstance(entry, dict) and set(entry) == {"message", "hint"}, code
        assert all(isinstance(v, str) and v.strip() for v in entry.values()), code
