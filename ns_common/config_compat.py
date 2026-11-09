"""F-10c: a setting a newer version added stays out of its file until it is used; a rollback can take it out.

The settings models are strict (`extra="forbid"`): an older version refuses a whole settings file that holds
a key it never had. So, from 0.1.15 on:

* `disk_dict` is the ONE helper every writer of a strict settings model uses (`save_runtime`,
  `save_install_state`, `save_channel`, the installer's `write_answers`, the dashboard's formats): a field
  listed in `NEW_FIELDS` is written only when its value differs from its default. An install that never uses
  a new feature keeps files the older version reads as its own.
* `for_version` takes out of a settings file what a given older version does not know. `ns update
  --rollback` uses it when it has no copy of the settings to put back (an update made by an older updater, a
  copy missing or damaged) and on the copy it restored, so the older version can always start.

`NEW_FIELDS` is the one list of fields the strict settings models have now and 0.1.14 did not (0.1.14 is the
oldest version a rollback from 0.1.15 reaches; older ones are not checked). tests/test_config_compat.py
compares the models with tests/frozen_models_0_1_14.py and tests/frozen_config_models_0_1_14.py class by
class, nested models too, and FAILS when a model gains a field that is in neither 0.1.14 nor this list: the
author of the field decides here, in the release that adds it.

Job files (plan.json, the QC report, the outbox sidecars) are not settings: H-8b's `MarkedWhenTest` already
leaves its `profile` out unless an engine-test install set it, which is the same rule.
"""

from __future__ import annotations

import re
import types
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from ns_common.models import Answers, Channel, Format, InstallState, Runtime

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


NEW_FIELDS: tuple[NewField, ...] = (
    NewField(
        "Runtime",
        "tiny_story",
        "0.1.15",
        "H-8: the engine-test story profile switch in runtime.json; false in every normal install.",
    ),
    NewField(
        "InstallerAnswers",
        "tiny_story",
        "0.1.15",
        "H-8: the answers.json switch behind Runtime.tiny_story; false unless an engine test asks for it.",
    ),
)

#: The model each settings file holds (a `channels/` or `formats/` name stands for every yaml in that folder),
#: with the class names whose fields it may also carry: answers.json is `installer.stage2.answers.
#: InstallerAnswers`, the installer's subclass of `Answers` that this layer cannot import.
FILE_MODELS: dict[str, tuple[type[BaseModel], tuple[str, ...]]] = {
    "runtime.json": (Runtime, ()),
    "install_state.json": (InstallState, ()),
    "install_state.json.bak": (InstallState, ()),
    "answers.json": (Answers, ("InstallerAnswers",)),
    "channels/": (Channel, ()),
    "formats/": (Format, ()),
}

_BY_MODEL: dict[str, dict[str, NewField]] = {}
for _f in NEW_FIELDS:
    _BY_MODEL.setdefault(_f.model, {})[_f.field] = _f


def version_key(text: str) -> tuple[int, ...]:
    """`0.1.15` -> (0, 1, 15); a text with no leading number -> () (older than every release)."""
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", text or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def model_for(name: str) -> tuple[type[BaseModel], tuple[str, ...]] | None:
    """The model of the settings file `name` (`runtime.json`, `channels/demo.yaml`), or None."""
    if name in FILE_MODELS:
        return FILE_MODELS[name]
    head = name.split("/", 1)[0] + "/"
    return FILE_MODELS.get(head) if "/" in name else None


def _new_fields(cls: type[BaseModel], extra: Iterable[str]) -> dict[str, NewField]:
    names = [c.__name__ for c in cls.__mro__ if isinstance(c, type) and issubclass(c, BaseModel)]
    out: dict[str, NewField] = {}
    for name in [*names, *extra]:
        out.update(_BY_MODEL.get(name, {}))
    return out


def _models_in(annotation: Any) -> tuple[str, type[BaseModel]] | None:
    """How a field holds a model: ("one", M) for M or M | None, ("items", M) for a list or tuple of M,
    ("values", M) for a dict of M; None when it holds no model."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "one", annotation
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, types.UnionType):
        for arg in args:
            found = _models_in(arg)
            if found is not None:
                return found
        return None
    if origin in (list, tuple, set, frozenset) and args:
        inner = _models_in(args[0])
        return ("items", inner[1]) if inner is not None and inner[0] == "one" else None
    if origin is dict and len(args) == 2:
        inner = _models_in(args[1])
        return ("values", inner[1]) if inner is not None and inner[0] == "one" else None
    return None


Drop = Callable[[NewField, Any, Any], bool]  # (the field, its value in the file, its default) -> take it out?
_NO_DEFAULT = object()  # a required field: it never holds "its default"


def _walk(
    cls: type[BaseModel], extra: Iterable[str], data: Any, drop: Drop, prefix: str, dropped: list[str]
) -> None:
    if not isinstance(data, dict):
        return
    added = _new_fields(cls, extra)
    fields = cls.model_fields
    for key in list(data):
        info = fields.get(key)
        new = added.get(key)
        if new is not None:
            # (a field of the installer's subclass is not one of `Answers`: it has no default to compare here)
            default = (
                _NO_DEFAULT
                if info is None or info.is_required()
                else to_jsonable_python(info.get_default(call_default_factory=True))
            )
            if drop(new, data[key], default):
                del data[key]
                dropped.append(prefix + key)
                continue
        if info is None:
            continue
        held = _models_in(info.annotation)
        if held is None:
            continue
        how, inner = held
        value = data[key]
        if how == "one":
            _walk(inner, (), value, drop, f"{prefix}{key}.", dropped)
        elif how == "items" and isinstance(value, list):
            for i, item in enumerate(value):
                _walk(inner, (), item, drop, f"{prefix}{key}[{i}].", dropped)
        elif how == "values" and isinstance(value, dict):
            for k, item in value.items():
                _walk(inner, (), item, drop, f"{prefix}{key}.{k}.", dropped)


def _at_default(_field: NewField, value: Any, default: Any) -> bool:
    return value == default


def disk_dict(model: BaseModel, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """What a strict settings model writes to its file: `model.model_dump(mode="json")` (or `data`, a dump of
    it the caller has prepared, such as the answers with blank keys) without the `NEW_FIELDS` that still hold
    their default. The model loads it back unchanged; the older version loads it as long as no new feature
    is in use."""
    out = model.model_dump(mode="json") if data is None else data
    _walk(type(model), (), out, _at_default, "", [])
    return out


def disk_dict_of(cls: type[BaseModel], data: dict[str, Any]) -> dict[str, Any]:
    """`disk_dict` for a writer that holds the plain dict of a `cls` (the dashboard's formats): the same rule,
    in place, without building the model."""
    _walk(cls, (), data, _at_default, "", [])
    return data


def for_version(name: str, data: Any, version: str) -> list[str]:
    """Take out of `data` (the parsed settings file `name`, changed in place) every field that `version` does
    not have; returns their dotted names (`tiny_story`, `llama.flags.x`). Empty for a file no strict model
    reads. A version that cannot be read counts as older than every release (everything listed goes)."""
    spec = model_for(name)
    if spec is None:
        return []
    target = version_key(version)
    dropped: list[str] = []

    def unknown_to_target(field: NewField, _value: Any, _default: Any) -> bool:
        return version_key(field.since) > target

    _walk(spec[0], spec[1], data, unknown_to_target, "", dropped)
    return dropped
