"""contracts/errors.json holds each code once.

`json.loads` keeps the LAST of two equal keys without a word, and git merges two branches that each add a line
for the same code without a conflict when the lines land in different places (PR 175 and PR 189 both added
NS-E120). The first definition would then vanish, and every place that raises it would show the other one's
message. This loader refuses a repeated key instead.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest


class DuplicateKey(ValueError):
    pass


def refuse_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise DuplicateKey(f"{key} is defined twice")
        out[key] = value
    return out


def load_strict(text: str) -> dict[str, Any]:
    data = json.loads(text, object_pairs_hook=refuse_duplicates)
    assert isinstance(data, dict)
    return data


def test_the_strict_loader_fails_on_a_repeated_code() -> None:
    merged = (
        '{\n  "NS-E120": {"message": "keys file", "hint": "a"},\n'
        '  "NS-E121": {"message": "disk", "hint": "b"},\n'
        '  "NS-E120": {"message": "install folder", "hint": "c"}\n}\n'
    )
    assert json.loads(merged)["NS-E120"]["message"] == "install folder", "plain json keeps the last one"
    with pytest.raises(DuplicateKey, match="NS-E120"):
        load_strict(merged)


def test_errors_json_defines_every_code_once(repo_root: Path) -> None:
    text = (repo_root / "contracts" / "errors.json").read_text(encoding="utf-8")
    data = load_strict(text)  # raises on a code (or a message/hint key) given twice
    keys = re.findall(r'"(NS-[EW]\d{3})"\s*:', text)
    assert len(keys) == len(set(keys)) == len(data), "every code is a key once"
