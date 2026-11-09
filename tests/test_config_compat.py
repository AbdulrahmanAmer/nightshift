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

from installer.stage2.answers import InstallerAnswers, build_answers, load_raw, write_answers
from installer.stage2.testing import example_answers
from ns_common import config_compat
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
    assert found == {("Runtime", "tiny_story"), ("InstallerAnswers", "tiny_story")}  # what 0.1.15 adds


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
        "Inner": {"added": NewField("Inner", "added", "0.1.16", "a synthetic nested field for this test")},
        "Outer": {
            "added_top": NewField("Outer", "added_top", "0.1.16", "a synthetic top field for this test")
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
