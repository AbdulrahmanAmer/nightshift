"""F-10c: the one list of settings fields added since 0.1.14, and the plain-dict rule for writers without
pydantic.

`NEW_FIELDS` is the table `ns_common.config_compat` works from (it re-exports it): every field the strict
settings models have now and 0.1.14 did not. It lives here, standard library only, because the setup window
runs on the bare Python and writes `answers.json` from a plain dict (`installer/stage2/web/answers_view.py`):
`plain_disk_dict` applies the same rule as `config_compat.disk_dict` (a new field is written only when it is
not its default) to the top-level fields of such a dict. tests/test_config_compat.py keeps each `default`
equal to the model's own default and fails when a new field is added to a model a plain-dict writer saves
at a depth this helper does not reach.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: The release `NEW_FIELDS` starts from: every field of the strict settings models that 0.1.14 had is known
#: to every version a rollback can reach, so it never needs taking out.
BASE_VERSION = "0.1.14"


@dataclass(frozen=True)
class NewField:
    """A field of a strict settings model that `BASE_VERSION` did not have."""

    model: str  # the class that declares it (its name: `installer.stage2.answers` is not importable here)
    field: str
    since: str  # the first release that has it
    why: str
    default: Any  # its default as JSON (what `plain_disk_dict` compares; a test keeps it the model's own)


NEW_FIELDS: tuple[NewField, ...] = (
    NewField(
        "Runtime",
        "tiny_story",
        "0.1.15",
        "H-8: the engine-test story profile switch in runtime.json; false in every normal install.",
        False,
    ),
    NewField(
        "InstallerAnswers",
        "tiny_story",
        "0.1.15",
        "H-8: the answers.json switch behind Runtime.tiny_story; false unless an engine test asks for it.",
        False,
    ),
)

#: The model class names whose top-level fields a settings file holds, for writers of plain dicts (a test
#: keeps this equal to `config_compat.FILE_MODELS`).
FILE_MODEL_NAMES: dict[str, tuple[str, ...]] = {
    "answers.json": ("Answers", "InstallerAnswers"),
}


def version_key(text: str) -> tuple[int, ...]:
    """`0.1.15` -> (0, 1, 15); a text with no leading number -> () (older than every release)."""
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", text or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def plain_disk_dict(name: str, data: dict[str, Any]) -> dict[str, Any]:
    """What a plain-dict writer of the settings file `name` writes: `data` (changed in place) without the
    top-level `NEW_FIELDS` that still hold their default. The same rule as `config_compat.disk_dict`."""
    models = FILE_MODEL_NAMES.get(name, ())
    for f in NEW_FIELDS:
        if f.model in models and f.field in data and data[f.field] == f.default:
            del data[f.field]
    return data
