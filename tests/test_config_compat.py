"""F-10c: a settings field added after 0.1.14 stays out of the file until it is used, and the table of such
fields (`ns_common.config_compat.NEW_FIELDS`) is complete.

The "older version" is 0.1.14, frozen class for class in tests/frozen_models_0_1_14.py (runtime.json,
install_state.json, the channels) and tests/frozen_config_models_0_1_14.py (answers.json, the formats).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from installer.stage2.answers import InstallerAnswers, build_answers, load_raw, write_answers
from installer.stage2.testing import example_answers
from installer.stage2.web import answers_view
from ns_common import config_compat, new_fields
from ns_common.config import (
    load_channel,
    load_install_state,
    load_runtime,
    save_channel,
    save_install_state,
    save_runtime,
)
from ns_common.config_compat import NEW_FIELDS, NewField, disk_dict, disk_dict_of, for_version, version_key
from ns_common.models import Answers, Channel, Format, InstallState, Runtime, StepState, Strict
from ns_common.paths import Paths
from tests import frozen_config_models_0_1_14 as old_cfg
from tests import frozen_models_0_1_14 as old

#: (the model now, the same model in 0.1.14): every strict model that reads a settings file.
PAIRS: list[tuple[type[BaseModel], type[BaseModel]]] = [
    (Runtime, old.Runtime),
    (InstallState, old.InstallState),
    (Channel, old.Channel),
    (Answers, old_cfg.Answers),
    (InstallerAnswers, old_cfg.InstallerAnswers),
    (Format, old_cfg.Format),
]


def declaring_class(cls: type[BaseModel], name: str) -> str:
    """The class whose own body declares field `name` (InstallerAnswers.tiny_story, not Answers)."""
    for c in cls.__mro__:
        if name in vars(c).get("__annotations__", {}):
            return c.__name__
    return cls.__name__


def gained(now: type[BaseModel], then: type[BaseModel] | None) -> set[tuple[str, str]]:
    """(class, field) of every field `now` has that `then` (the same model in 0.1.14) has not, nested too."""
    out: set[tuple[str, str]] = set()
    for name, info in now.model_fields.items():
        if then is None or name not in then.model_fields:
            out.add((declaring_class(now, name), name))
            continue  # a new field is decided as a whole: what it holds is new with it
        inner = config_compat._models_in(info.annotation)
        if inner is not None:
            old_inner = config_compat._models_in(then.model_fields[name].annotation)
            out |= gained(inner[1], old_inner[1] if old_inner else None)
    return out


def table() -> set[tuple[str, str]]:
    return {(f.model, f.field) for f in NEW_FIELDS}


# ------------------------------------------------------------------------------------------ the table
def test_every_field_a_settings_model_gained_since_0_1_14_is_in_the_table() -> None:
    """The tripwire: a new field of a strict settings model must be decided in NEW_FIELDS in its release."""
    found: set[tuple[str, str]] = set()
    for now, then in PAIRS:
        found |= gained(now, then)
    missing = sorted(found - table())
    assert not missing, (
        f"{missing} exist now and not in 0.1.14: add each to ns_common/config_compat.py NEW_FIELDS (with the "
        "release that adds it and why), so the writers leave it out while it holds its default and a "
        "rollback can take it out for an older version."
    )
    stale = sorted(table() - found)
    assert not stale, f"{stale} are in NEW_FIELDS but 0.1.14 already had them (or no settings model has them)"
    assert found == {
        ("Runtime", "tiny_story"), ("InstallerAnswers", "tiny_story"),
        ("Answers", "nightly"), ("InstallerAnswers", "nightly_seen"),  # setup's nightly question (used once)
    }  # what 0.1.15 adds


def test_the_tripwire_catches_a_field_added_deep_inside_a_model() -> None:
    """The comparison really walks nested models: a field added two levels down is found."""

    class NewFlags(old.LlamaFlags):
        rope_scale: float = 1.0

    class NewLlama(old.LlamaCfg):
        flags: NewFlags = NewFlags()

    class NewRuntime(old.Runtime):
        llama: NewLlama = NewLlama()
        streaming: bool = False

    assert gained(NewRuntime, old.Runtime) == {("NewFlags", "rope_scale"), ("NewRuntime", "streaming")}
    steps = gained(InstallState, old.InstallState)  # dict[str, StepState] is walked as well
    assert steps == set()


def test_each_entry_says_when_and_why() -> None:
    for f in NEW_FIELDS:
        assert version_key(f.since) > version_key(config_compat.BASE_VERSION), f
        assert len(f.why) > 20, f


# ---------------------------------------------------------------------------------------- plain-dict writers
#: Every class a NewField may name, by name (InstallerAnswers is not importable from ns_common).
CLASSES: dict[str, type[BaseModel]] = {c.__name__: c for c, _ in PAIRS}


def nested_classes(cls: type[BaseModel]) -> set[str]:
    """The names of the models held inside `cls`, at any depth (not `cls` itself)."""
    out: set[str] = set()
    for info in cls.model_fields.values():
        inner = config_compat._models_in(info.annotation)
        if inner is not None and inner[1].__name__ not in out:
            out |= {inner[1].__name__} | nested_classes(inner[1])
    return out


def test_each_entry_records_the_default_its_model_has() -> None:
    """`plain_disk_dict` (the setup window, no pydantic) compares with `NewField.default`: it must be the
    model's own default, or a plain-dict writer would keep (or drop) the wrong value."""
    for f in NEW_FIELDS:
        info = CLASSES[f.model].model_fields[f.field]
        assert to_jsonable_python(info.get_default(call_default_factory=True)) == f.default, f


def test_the_plain_dict_writers_see_the_same_models_and_no_nested_new_field() -> None:
    """`new_fields.FILE_MODEL_NAMES` names the classes `config_compat.FILE_MODELS` gives each file, and no new
    field sits deeper than the top level of a file a plain-dict writer saves (the helper only looks there):
    a new nested field there must extend `plain_disk_dict` in the release that adds it."""
    for name, models in new_fields.FILE_MODEL_NAMES.items():
        cls, extra = config_compat.FILE_MODELS[name]
        top = {c.__name__ for c in cls.__mro__ if isinstance(c, type) and issubclass(c, BaseModel)}
        assert set(models) <= top | set(extra) and cls.__name__ in models and set(extra) <= set(models)
        deep = set().union(*(nested_classes(CLASSES[m]) for m in models if m in CLASSES))
        assert not {f.model for f in NEW_FIELDS} & deep, name


def test_the_setup_window_writes_a_new_answer_only_when_it_is_in_use(tmp_path: Path) -> None:
    """Finding 7: `answers_view.write_answers` (the setup window, a plain dict) goes through the same rule as
    every other writer of a strict settings file, so 0.1.14 reads its answers.json after a rollback."""
    target = tmp_path / "config" / "answers.json"
    answers_view.write_answers(target, {**example_answers(), "tiny_story": False, "pexels_api_key": "k"})
    data = json.loads(target.read_text(encoding="utf-8"))
    assert "tiny_story" not in data and data["pexels_api_key"] == ""
    old_cfg.InstallerAnswers.model_validate(data)  # 0.1.14's setup reads it
    answers_view.write_answers(target, {**example_answers(), "tiny_story": True})
    assert json.loads(target.read_text(encoding="utf-8"))["tiny_story"] is True  # in use: written
    assert new_fields.plain_disk_dict("runtime.json", {"tiny_story": False}) == {"tiny_story": False}


# ------------------------------------------------------------------------------------------ the writers
def old_loads(paths: Paths) -> None:
    """Every settings file the writers made loads with 0.1.14's models."""
    old.Runtime.model_validate_json(paths.runtime_file.read_text(encoding="utf-8"))
    old.InstallState.model_validate_json(paths.install_state_file.read_text(encoding="utf-8"))
    old.Channel.model_validate(yaml.safe_load(paths.channel_file("demo").read_text(encoding="utf-8")))
    old_cfg.InstallerAnswers.model_validate_json((paths.config / "answers.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("profile", ["gpu", "cpu-dev"])
def test_an_install_that_never_uses_a_new_feature_writes_files_0_1_14_reads(
    tmp_path: Path, profile: str
) -> None:
    paths = Paths(tmp_path / "home")
    save_runtime(paths, Runtime.default(profile))  # type: ignore[arg-type]
    state = InstallState(
        version="0.1.15", app_path="C:\\x\\app", steps={"s01_questions": StepState(status="done")}
    )
    save_install_state(paths, state)
    save_install_state(paths, state)  # twice: the .bak is a file 0.1.14 reads too
    save_channel(paths, Channel(slug="demo", display_name="Demo", niche="true stories"))
    write_answers(paths.config / "answers.json", build_answers(example_answers()))

    old_loads(paths)
    old.InstallState.model_validate_json(
        (paths.config / "install_state.json.bak").read_text(encoding="utf-8")
    )
    assert "tiny_story" not in json.loads(paths.runtime_file.read_text(encoding="utf-8"))
    assert "tiny_story" not in json.loads((paths.config / "answers.json").read_text(encoding="utf-8"))
    # and the new version reads them back as it wrote them
    assert load_runtime(paths) == Runtime.default(profile)  # type: ignore[arg-type]
    assert load_install_state(paths) == state
    assert load_channel(paths, "demo") == Channel(slug="demo", display_name="Demo", niche="true stories")


def test_a_new_setting_in_use_is_written_and_read_back(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    rt = Runtime.default("cpu-dev")
    rt.tiny_story = True
    save_runtime(paths, rt)
    assert json.loads(paths.runtime_file.read_text(encoding="utf-8"))["tiny_story"] is True
    assert load_runtime(paths).tiny_story is True
    answers = paths.config / "answers.json"
    write_answers(answers, build_answers(example_answers(tiny_story=True, allow_cpu=True)))
    assert json.loads(answers.read_text(encoding="utf-8"))["tiny_story"] is True
    assert build_answers(load_raw(answers), saved=True).tiny_story is True


# ------------------------------------------------------------------------------------------ the helper itself
class Inner(Strict):
    keep: int = 0
    added: int = 0


class Outer(Strict):
    one: Inner = Inner()
    maybe: Inner | None = None
    many: list[Inner] = []
    named: dict[str, Inner] = {}
    added_top: str = "x"
    plain: int = 1


@pytest.fixture
def synthetic(monkeypatch: pytest.MonkeyPatch) -> None:
    fields = {
        "Inner": {"added": NewField("Inner", "added", "0.1.16", "a synthetic nested field for this test", 0)},
        "Outer": {
            "added_top": NewField("Outer", "added_top", "0.1.16", "a synthetic top field for this test", "x")
        },
    }
    monkeypatch.setattr(config_compat, "_BY_MODEL", fields)


@pytest.mark.usefixtures("synthetic")
def test_the_helper_leaves_out_new_fields_at_their_default_at_every_depth() -> None:
    m = Outer(maybe=Inner(added=2), many=[Inner(), Inner(added=3)], named={"a": Inner(), "b": Inner(added=4)})
    out = disk_dict(m)
    assert "added_top" not in out and out["plain"] == 1
    assert out["one"] == {"keep": 0}
    assert out["maybe"] == {"keep": 0, "added": 2}  # in use: written
    assert out["many"] == [{"keep": 0}, {"keep": 0, "added": 3}]
    assert out["named"] == {"a": {"keep": 0}, "b": {"keep": 0, "added": 4}}
    assert Outer.model_validate(out) == m  # loads back unchanged
    assert disk_dict(Outer(added_top="y"))["added_top"] == "y"
    raw: dict[str, Any] = {"one": {"keep": 1, "added": 0}, "added_top": "x", "plain": 1}
    assert disk_dict_of(Outer, raw) == {"one": {"keep": 1}, "plain": 1}


@pytest.mark.usefixtures("synthetic")
def test_for_version_takes_out_what_the_target_lacks_and_names_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(config_compat.FILE_MODELS, "outer.json", (Outer, ()))
    data = {
        "one": {"keep": 1, "added": 5},
        "many": [{"added": 0}],
        "named": {"z": {"added": 1}},
        "added_top": "q",
    }
    assert for_version("outer.json", json.loads(json.dumps(data)), "0.1.16") == []  # it has them
    assert for_version("outer.json", json.loads(json.dumps(data)), "0.2.0") == []
    stripped = json.loads(json.dumps(data))
    assert sorted(for_version("outer.json", stripped, "0.1.15")) == [
        "added_top",
        "many[0].added",
        "named.z.added",
        "one.added",
    ]
    assert stripped == {"one": {"keep": 1}, "many": [{}], "named": {"z": {}}}


def test_for_version_on_real_files() -> None:
    rt = {**Runtime.default("cpu-dev").model_dump(mode="json"), "tiny_story": True}
    assert for_version("runtime.json", rt, "0.1.14") == ["tiny_story"] and "tiny_story" not in rt
    old.Runtime.model_validate(rt)
    answers = {**example_answers(), "tiny_story": True, "allow_cpu": True}
    assert for_version("answers.json", answers, "0.1.14") == ["tiny_story"]
    rt2 = {"tiny_story": True}
    assert for_version("runtime.json", rt2, "0.1.15") == [] and rt2 == {"tiny_story": True}
    assert for_version("runtime.json", {"tiny_story": False}, "garbage") == ["tiny_story"]  # unknown: oldest
    assert for_version("schedule.json", {"run_time": "03:00", "x": 1}, "0.1.14") == []  # not a strict model
    assert for_version("channels/demo.yaml", {"slug": "demo"}, "0.1.14") == []


def test_version_key() -> None:
    assert version_key("0.1.15") > version_key("0.1.14") > version_key("0.1.9")
    assert version_key("v0.1.15") == (0, 1, 15) and version_key("0.1.15-rc1") == (0, 1, 15)
    assert version_key("-") == () < version_key("0.0.1")


# ------------------------------------------------------------------- card 206-carried: fields that CHANGED
#: Parts of a field's JSON schema that say nothing about which values a version reads.
NOT_VALUES = ("title", "description", "default", "examples")


def _values_schema(node: Any, defs: dict[str, Any]) -> Any:
    """`node` with every reference to an enum or alias resolved, a reference to a model kept as its name
    (the model is compared field by field on its own) and the `NOT_VALUES` keys left out."""
    if isinstance(node, dict):
        if "$ref" in node:
            name = str(node["$ref"]).rsplit("/", 1)[-1]
            target = defs.get(name, {})
            return {"model": name} if "properties" in target else _values_schema(target, defs)
        return {k: _values_schema(v, defs) for k, v in sorted(node.items()) if k not in NOT_VALUES}
    if isinstance(node, list):
        return [_values_schema(v, defs) for v in node]
    return node


def field_schemas(cls: type[BaseModel]) -> dict[str, Any]:
    """What each field of `cls` accepts: its JSON schema (Literal values, bounds, patterns, types)."""
    schema = cls.model_json_schema()
    defs = schema.get("$defs", {})
    return {n: _values_schema(p, defs) for n, p in schema.get("properties", {}).items()}


def changed(now: type[BaseModel], then: type[BaseModel]) -> set[tuple[str, str]]:
    """(class, field) of every field both `now` and `then` have whose schema differs, nested too."""
    out: set[tuple[str, str]] = set()
    new, old_ = field_schemas(now), field_schemas(then)
    for name, info in now.model_fields.items():
        if name not in then.model_fields:
            continue  # a new field: `gained` and NEW_FIELDS
        if new[name] != old_[name]:
            out.add((declaring_class(now, name), name))
        inner = config_compat._models_in(info.annotation)
        old_inner = config_compat._models_in(then.model_fields[name].annotation)
        if inner is not None and old_inner is not None:
            out |= changed(inner[1], old_inner[1])
    return out


def test_every_changed_field_since_0_1_14_is_declared() -> None:
    """The tripwire for a field 0.1.14 already had: when what it accepts changes (a wider Literal, a looser
    bound, another type), an older version may refuse a value this one writes. Each such field is declared in
    `new_fields.CHANGED_FIELDS` with how an older version reads it."""
    declared = getattr(new_fields, "CHANGED_FIELDS", None)
    assert declared is not None, "ns_common/new_fields.py has no CHANGED_FIELDS: a widened field ships unseen"
    found: set[tuple[str, str]] = set()
    for now, then in PAIRS:
        found |= changed(now, then)
    declared_names = {(f.model, f.field) for f in declared}
    missing = sorted(found - declared_names)
    assert not missing, (
        f"{missing} accept other values now than in 0.1.14: add each to ns_common/new_fields.py "
        "CHANGED_FIELDS with the release, why, and either old_reads_all=True (0.1.14 reads every value this "
        "version writes) or old_values and a fallback (what a rollback writes in place of a value 0.1.14 "
        "refuses). A change whose old values cannot be listed (a bound) needs its own rule in "
        "config_compat.for_version first."
    )
    stale = sorted(declared_names - found)
    assert not stale, f"{stale} are in CHANGED_FIELDS but read the same as in 0.1.14"
    for f in declared:
        assert version_key(f.since) > version_key(config_compat.BASE_VERSION) and len(f.why) > 20, f
        assert f.old_reads_all != bool(f.old_values), f  # exactly one way of going back
        back = f.fallback if isinstance(f.fallback, list) else [f.fallback]
        assert f.old_reads_all or all(v in f.old_values for v in back), f  # the fallback is one it reads


def test_the_tripwire_catches_a_widened_literal() -> None:
    """Box 4: the field name is the same, so the name comparison (`gained`) sees nothing; the schema
    comparison does, at any depth, and is quiet about a changed default or description."""
    from typing import Literal

    from pydantic import Field

    class Then(Strict):
        mode: Literal["a", "b"] = "a"
        size: int = Field(default=3, ge=1, description="how many")
        same: str = "x"

    class Now(Strict):
        mode: Literal["a", "b", "c"] = "a"
        size: int = Field(default=4, ge=1, description="how many of them")
        same: str = "x"

    class Looser(Strict):
        mode: Literal["a", "b"] = "a"
        size: int = Field(default=3, ge=0)
        same: str = "x"

    def holder(choices: Any) -> type[BaseModel]:
        """A model that holds a model of the same name in both versions, as the frozen copy does."""

        class Item(Strict):
            mode: choices = "a"  # type: ignore[valid-type]

        class Holder(Strict):
            inner: dict[str, Item] = {}

        return Holder

    assert gained(Now, Then) == set()  # why names are not enough
    assert changed(Now, Then) == {("Now", "mode")}
    assert changed(Looser, Then) == {("Looser", "size")}
    assert changed(holder(Literal["a", "b", "c"]), holder(Literal["a", "b"])) == {("Item", "mode")}
    assert changed(holder(Literal["a", "b"]), holder(Literal["a", "b"])) == set()
    assert changed(Then, Then) == set()


class Wide(Strict):
    mode: str = "a"  # stands for Literal["a", "b", "c"]: `for_version` works on the parsed file
    modes: list[str] = []
    plain: int = 1


class HoldsWide(Strict):
    one: Wide = Wide()
    named: dict[str, Wide] = {}


def test_for_version_puts_a_value_0_1_14_lacks_back_to_its_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """One bad value among good ones: only the value the older version cannot read is replaced, by the
    declared fallback, and it is named; every other value and file stays as it is."""
    from types import SimpleNamespace

    make = getattr(new_fields, "ChangedField", None) or (lambda *a, **k: SimpleNamespace(args=a, **k))
    entries = {
        "Wide": {
            "mode": make(
                "Wide", "mode", "0.1.16", "a synthetic widened choice for this test",
                old_values=("a", "b"), fallback="a",
            ),
            "modes": make(
                "Wide", "modes", "0.1.16", "a synthetic widened list of choices for this test",
                old_values=("a", "b"), fallback=[],
            ),
            "plain": make(
                "Wide", "plain", "0.1.16", "a synthetic change every older version reads", old_reads_all=True
            ),
        }
    }  # fmt: skip
    monkeypatch.setattr(config_compat, "_CHANGED_BY_MODEL", entries, raising=False)
    monkeypatch.setitem(config_compat.FILE_MODELS, "wide.json", (HoldsWide, ()))
    data = {
        "one": {"mode": "c", "modes": ["a", "c"], "plain": 99},
        "named": {"good": {"mode": "b", "modes": ["a", "b"]}, "bad": {"mode": "c"}},
    }
    same = json.loads(json.dumps(data))
    assert for_version("wide.json", same, "0.1.16") == [] and same == data  # it has the wider field
    fixed = json.loads(json.dumps(data))
    said = for_version("wide.json", fixed, "0.1.14")
    assert fixed == {
        "one": {"mode": "a", "modes": [], "plain": 99},
        "named": {"good": {"mode": "b", "modes": ["a", "b"]}, "bad": {"mode": "a"}},
    }
    assert sorted(said) == [
        'named.bad.mode set back to "a"',
        'one.mode set back to "a"',
        "one.modes set back to []",
    ]
    assert for_version("wide.json", fixed, "0.1.14") == []  # the second time: nothing left to do
    assert for_version("wide.json", json.loads(json.dumps(data)), "-") == said  # unknown: the oldest
    assert disk_dict_of(HoldsWide, json.loads(json.dumps(data))) == data  # a writer never changes a value
