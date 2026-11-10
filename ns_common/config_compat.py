"""F-10c: a setting a newer version added stays out of its file until it is used; a rollback can take it out.

The settings models are strict (`extra="forbid"`): an older version refuses a whole settings file that holds
a key it never had. So, from 0.1.15 on:

* `disk_dict` is the ONE helper every writer of a strict settings model uses (`save_runtime`,
  `save_install_state`, `save_channel`, the installer's `write_answers`, the dashboard's formats): a field
  listed in `NEW_FIELDS` is written only when its value differs from its default. An install that never uses
  a new feature keeps files the older version reads as its own. The setup window, which runs on the bare
  Python and writes answers.json from a plain dict, uses the same rule through
  `ns_common.new_fields.plain_disk_dict`.
* `for_version` takes out of a settings file what a given older version does not know. `ns update
  --rollback` uses it when it has no copy of the settings to put back (an update made by an older updater, a
  copy missing or damaged) and on the copy it restored. Only the fields in `NEW_FIELDS` are taken out (the
  ones added since 0.1.14): for a rollback to a version older than 0.1.14 nothing more is checked, and a file
  that cannot be written keeps its new keys (the rollback names it as one the older version may refuse).
  A field 0.1.14 already had that accepts more now (`CHANGED_FIELDS`, card 206-carried: a wider Literal) is
  not taken out: a value the older version refuses is replaced by the declared fallback, and named. Only a
  change whose older values can be listed has such a rule; tests/test_config_compat.py fails, and says so,
  when a field changes in any other way (a bound, a pattern, a type).

`NEW_FIELDS` (defined in `ns_common.new_fields`, standard library only, and re-exported here) is the one list
of fields the strict settings models have now and 0.1.14 did not (0.1.14 is the oldest version a rollback from
0.1.15 reaches). tests/test_config_compat.py compares the models with tests/frozen_models_0_1_14.py and
tests/frozen_config_models_0_1_14.py class by class, nested models too, and FAILS when a model gains a field
that is in neither 0.1.14 nor this list: the author of the field decides here, in the release that adds it.

Job files (plan.json, the QC report, the outbox sidecars) are not settings and are not rewritten by a
rollback. Decided for 0.1.15 (H-8b, PR 179):

* `Metadata.profile` and `QcResult.profile` (`MarkedWhenTest`) are left out of the file unless an engine-test
  install set them, which is the same rule as `disk_dict`: an ordinary job folder stays readable by 0.1.14.
* `Plan.target_minutes` was relaxed from `ge=9` to `ge=1` for the tiny-story profile. A tiny-story job's
  plan.json (target under 9 minutes, written only on an engine-test install with `tiny_story` on) is
  therefore refused by 0.1.14 after a rollback: that one job fails to resume on 0.1.14, every other job and
  every setting are unaffected. Accepted: such jobs are test output that cannot be published, and rewriting
  job folders on a rollback would change owners' work to fit a version they are leaving.
"""

from __future__ import annotations

import copy
import json
import types
from collections.abc import Callable, Iterable
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from ns_common.models import Answers, Channel, Format, InstallState, Runtime
from ns_common.new_fields import (
    BASE_VERSION,
    CHANGED_FIELDS,
    NEW_FIELDS,
    ChangedField,
    NewField,
    version_key,
)

__all__ = [
    "BASE_VERSION",
    "CHANGED_FIELDS",
    "ChangedField",
    "FILE_MODELS",
    "NEW_FIELDS",
    "NewField",
    "disk_dict",
    "disk_dict_of",
    "for_version",
    "model_for",
    "version_key",
]

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


def model_for(name: str) -> tuple[type[BaseModel], tuple[str, ...]] | None:
    """The model of the settings file `name` (`runtime.json`, `channels/demo.yaml`), or None."""
    if name in FILE_MODELS:
        return FILE_MODELS[name]
    head = name.split("/", 1)[0] + "/"
    return FILE_MODELS.get(head) if "/" in name else None


#: `CHANGED_FIELDS` by declaring class: the fields 0.1.14 already had that accept other values now.
_CHANGED_BY_MODEL: dict[str, dict[str, ChangedField]] = {}
for _c in CHANGED_FIELDS:
    _CHANGED_BY_MODEL.setdefault(_c.model, {})[_c.field] = _c


def _fields_of(
    table: dict[str, dict[str, Any]], cls: type[BaseModel], extra: Iterable[str]
) -> dict[str, Any]:
    """The entries of `table` (by declaring class) for `cls`, its bases and the `extra` class names."""
    names = [c.__name__ for c in cls.__mro__ if isinstance(c, type) and issubclass(c, BaseModel)]
    out: dict[str, Any] = {}
    for name in [*names, *extra]:
        out.update(table.get(name, {}))
    return out


def _new_fields(cls: type[BaseModel], extra: Iterable[str]) -> dict[str, NewField]:
    return _fields_of(_BY_MODEL, cls, extra)


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
Fix = Callable[[ChangedField, Any], bool]  # (the changed field, its value in the file) -> write its fallback?
_NO_DEFAULT = object()  # a required field: it never holds "its default"


def _walk(
    cls: type[BaseModel],
    extra: Iterable[str],
    data: Any,
    drop: Drop,
    prefix: str,
    dropped: list[str],
    fix: Fix | None = None,
) -> None:
    if not isinstance(data, dict):
        return
    added = _new_fields(cls, extra)
    changed = _fields_of(_CHANGED_BY_MODEL, cls, extra) if fix is not None else {}
    fields = cls.model_fields
    for key in list(data):
        info = fields.get(key)
        new = added.get(key)
        wider = changed.get(key)
        if fix is not None and wider is not None and fix(wider, data[key]):
            data[key] = copy.deepcopy(wider.fallback)
            dropped.append(f"{prefix}{key} set back to {json.dumps(wider.fallback)}")
            continue
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
            _walk(inner, (), value, drop, f"{prefix}{key}.", dropped, fix)
        elif how == "items" and isinstance(value, list):
            for i, item in enumerate(value):
                _walk(inner, (), item, drop, f"{prefix}{key}[{i}].", dropped, fix)
        elif how == "values" and isinstance(value, dict):
            for k, item in value.items():
                _walk(inner, (), item, drop, f"{prefix}{key}.{k}.", dropped, fix)


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
    """Make `data` (the parsed settings file `name`, changed in place) one that `version` reads: every field
    it does not have is taken out (`NEW_FIELDS`), and a value it does not accept in a field it has is
    replaced by the declared fallback (`CHANGED_FIELDS`). Returns what was done, one entry each: the dotted
    name of a field taken out (`tiny_story`, `llama.flags.x`), or `<name> set back to <fallback>`. Empty for
    a file no strict model reads, and the second time. A version that cannot be read (`-`, empty, any text
    without a leading number) counts as older than every release: everything listed is applied."""
    spec = model_for(name)
    if spec is None:
        return []
    target = version_key(version)
    dropped: list[str] = []

    def unknown_to_target(field: NewField, _value: Any, _default: Any) -> bool:
        return version_key(field.since) > target

    def refused_by_target(field: ChangedField, value: Any) -> bool:
        if field.old_reads_all or version_key(field.since) <= target:
            return False
        return any(v not in field.old_values for v in (value if isinstance(value, list) else [value]))

    _walk(spec[0], spec[1], data, unknown_to_target, "", dropped, refused_by_target)
    return dropped
