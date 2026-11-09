"""D-24 review: a profile written by 0.1.0 to 0.1.14 keeps loading; the name rule applies to typed names.

Before the fix `Voice.name` carried the pattern: one old file made `crud.all_channels` raise NS-E320 and the
nightly (`jobs.resolve_channels`, `cadence.plan_daily`) stopped for every channel.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from ns_common.config import load_channel
from ns_common.db import Db
from ns_common.paths import Paths
from ns_studio import jobs
from ns_studio.channels import cadence, crud
from ns_studio.errors import StudioError
from tests.helpers import make_channel

LONG_NAME = (
    "after-dark-tales-of-the-deep-north-woods-and-lakes-voice"  # 57 chars: s07 built {slug}-voice unbounded
)
assert len(LONG_NAME) > 40


def _env(tmp_home: Path) -> tuple[Paths, Db]:
    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    return paths, db


def _write_old_profile(paths: Paths, slug: str, voice: dict[str, Any], **kw: Any) -> None:
    """A channel yaml the way 0.1.x wrote it, with the voice block replaced by a hand-made (old) one."""
    ch = make_channel(slug=slug, display_name=slug.title(), niche=f"niche of {slug}", **kw)
    data = ch.model_dump(mode="json")
    data["voice"] = voice
    paths.channels_dir.mkdir(parents=True, exist_ok=True)
    paths.channel_file(slug).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


LONG_DESIGN = {
    "mode": "design",
    "speaker": None,
    "description": "warm baritone",
    "name": LONG_NAME,
    "speed": 1.0,
}
SPACED_PRESET = {
    "mode": "preset",
    "speaker": "Ryan",
    "description": None,
    "name": "Night Narrator",
    "speed": 1.0,
}


def test_a_profile_with_a_name_over_40_characters_loads(tmp_home: Path):
    paths, _ = _env(tmp_home)
    _write_old_profile(paths, "long-name", LONG_DESIGN)
    assert load_channel(paths, "long-name").voice.name == LONG_NAME
    assert crud.get_channel(paths, "long-name").voice.name == LONG_NAME


def test_a_preset_profile_that_kept_a_name_with_a_space_loads(tmp_home: Path):
    paths, _ = _env(tmp_home)
    _write_old_profile(paths, "kept-name", SPACED_PRESET)
    ch = crud.get_channel(paths, "kept-name")
    assert (ch.voice.mode, ch.voice.speaker, ch.voice.name) == ("preset", "Ryan", "Night Narrator")


def test_all_channels_lists_every_old_shaped_profile(tmp_home: Path):
    paths, _ = _env(tmp_home)
    _write_old_profile(paths, "good-one", {"mode": "preset", "speaker": "Ryan"})
    _write_old_profile(paths, "d24-victim", SPACED_PRESET)
    _write_old_profile(paths, "long-name", LONG_DESIGN)
    assert [c.slug for c in crud.all_channels(paths)] == ["d24-victim", "good-one", "long-name"]


def test_the_nightly_still_resolves_every_channel_with_old_voice_names(tmp_home: Path):
    paths, _ = _env(tmp_home)
    _write_old_profile(paths, "good-one", {"mode": "preset", "speaker": "Ryan"})
    _write_old_profile(paths, "d24-victim", SPACED_PRESET)
    _write_old_profile(paths, "long-name", LONG_DESIGN)
    env = jobs.open_env(light=True)
    assert [c.slug for c in jobs.resolve_channels(env, None)] == ["d24-victim", "good-one", "long-name"]


def test_one_unreadable_channel_does_not_stop_the_others(tmp_home: Path):
    paths, _ = _env(tmp_home)
    _write_old_profile(paths, "good-one", {"mode": "preset", "speaker": "Ryan"})
    _write_old_profile(paths, "long-name", LONG_DESIGN)
    paths.channel_file("broken").write_text("slug: Bad Slug\n", encoding="utf-8")
    assert [c.slug for c in crud.all_channels(paths)] == ["good-one", "long-name"]
    env = jobs.open_env(light=True)
    assert [c.slug for c in jobs.resolve_channels(env, None)] == ["good-one", "long-name"]


def test_the_skipped_channel_is_named_not_hidden(tmp_home: Path):
    """The bad channel is reported (a list for callers, a WARN in the studio log), never dropped silently."""
    paths, _ = _env(tmp_home)
    _write_old_profile(paths, "good-one", {"mode": "preset", "speaker": "Ryan"})
    paths.channel_file("broken").write_text("slug: Bad Slug\n", encoding="utf-8")
    crud.all_channels(paths)
    problems = crud.broken_channels(paths)
    assert [slug for slug, _ in problems] == ["broken"] and "slug" in problems[0][1]
    logged = [
        json.loads(line)
        for f in paths.logs.glob("studio-*.jsonl")
        for line in f.read_text(encoding="utf-8").splitlines()
    ]
    warn = [r for r in logged if r["event"] == "channel_profile_skipped"]
    assert warn and warn[0]["level"] == "WARN" and warn[0]["data"]["slug"] == "broken"


def test_the_nightly_plan_skips_the_broken_channel_and_plans_the_rest(tmp_home: Path):
    paths, db = _env(tmp_home)
    _write_old_profile(paths, "good-one", {"mode": "preset", "speaker": "Ryan"})
    _write_old_profile(paths, "long-name", LONG_DESIGN)
    paths.channel_file("broken").write_text("slug: Bad Slug\n", encoding="utf-8")
    plan = cadence.plan_daily(paths, db)
    assert {j.channel.slug for j in plan} == {"good-one", "long-name"}


def test_when_every_channel_is_unreadable_the_error_still_says_why(tmp_home: Path):
    paths, _ = _env(tmp_home)
    paths.channel_file("bad").write_text("slug: Bad Slug\n", encoding="utf-8")
    with pytest.raises(StudioError) as ei:
        crud.all_channels(paths)
    assert ei.value.code == "NS-E320" and "bad.yaml" in str(ei.value)


def test_a_channel_edit_keeps_working_on_an_old_name_it_does_not_touch(tmp_home: Path):
    paths, db = _env(tmp_home)
    _write_old_profile(paths, "long-name", LONG_DESIGN)
    edited = crud.edit_channel(paths, db, "long-name", {"voice_notes": "calmer"})
    assert edited.voice_notes == "calmer" and edited.voice.name == LONG_NAME
    assert load_channel(paths, "long-name").voice.name == LONG_NAME


@pytest.mark.parametrize("typed", ["Night Narrator", "a" * 41, "-night", ""])
def test_a_channel_edit_refuses_a_voice_name_typed_now_that_the_rule_refuses(tmp_home: Path, typed: str):
    paths, db = _env(tmp_home)
    _write_old_profile(
        paths, "after-dark", {"mode": "design", "speaker": None, "description": "x", "name": "ok-voice"}
    )
    with pytest.raises(StudioError) as ei:
        crud.edit_channel(paths, db, "after-dark", {"voice.name": typed})
    assert ei.value.code == "NS-E320" and "1 to 40 letters" in str(ei.value)
    assert load_channel(paths, "after-dark").voice.name == "ok-voice"  # nothing was saved


def test_a_channel_edit_accepts_a_valid_new_voice_name(tmp_home: Path):
    paths, db = _env(tmp_home)
    _write_old_profile(
        paths, "after-dark", {"mode": "design", "speaker": None, "description": "x", "name": "ok-voice"}
    )
    crud.edit_channel(paths, db, "after-dark", {"voice.name": "Night-Narrator"})
    assert load_channel(paths, "after-dark").voice.name == "Night-Narrator"


def test_a_new_channel_with_a_name_the_rule_refuses_is_not_added(tmp_home: Path):
    paths, db = _env(tmp_home)
    ch = make_channel(voice={"mode": "design", "speaker": None, "description": "x", "name": LONG_NAME})
    with pytest.raises(StudioError) as ei:
        crud.add_channel(paths, db, ch)
    assert ei.value.code == "NS-E320" and "1 to 40 letters" in str(ei.value)
    assert not paths.channel_file(ch.slug).exists()


# ---- break-it round: the edit that starts to use a name, and the places a skipped profile must show ----


def test_a_preset_channel_that_kept_a_spaced_name_cannot_be_switched_to_design_with_it(tmp_home: Path):
    """The name was never used while the voice was a preset; a designed voice would use it and fail."""
    paths, db = _env(tmp_home)
    _write_old_profile(paths, "after-dark", SPACED_PRESET)
    with pytest.raises(StudioError) as ei:
        crud.edit_channel(paths, db, "after-dark", {"voice.mode": "design", "voice.description": "x"})
    assert ei.value.code == "NS-E320" and "Night Narrator" in str(ei.value)
    assert load_channel(paths, "after-dark").voice.mode == "preset"  # nothing was saved


def test_that_switch_goes_through_with_a_name_the_rule_accepts(tmp_home: Path):
    paths, db = _env(tmp_home)
    _write_old_profile(paths, "after-dark", SPACED_PRESET)
    crud.edit_channel(
        paths,
        db,
        "after-dark",
        {"voice.mode": "design", "voice.description": "x", "voice.name": "Night-Narrator"},
    )
    saved = load_channel(paths, "after-dark").voice
    assert (saved.mode, saved.name) == ("design", "Night-Narrator")


def test_a_designed_channel_may_become_a_clone_and_keeps_its_stored_name(tmp_home: Path):
    paths, db = _env(tmp_home)
    _write_old_profile(paths, "long-name", LONG_DESIGN)
    crud.edit_channel(paths, db, "long-name", {"voice.mode": "clone"})
    assert load_channel(paths, "long-name").voice.name == LONG_NAME


def _broken_beside_a_good_channel(tmp_home: Path) -> Paths:
    paths, _ = _env(tmp_home)
    _write_old_profile(paths, "good-one", {"mode": "preset", "speaker": "Ryan"})
    paths.channel_file("broken").write_text("slug: Bad Slug\n", encoding="utf-8")
    return paths


def test_channel_list_shows_the_unreadable_profile(tmp_home: Path):
    from typer.testing import CliRunner

    from ns_studio.cli import app

    _broken_beside_a_good_channel(tmp_home)
    res = CliRunner().invoke(app, ["channel", "list"])
    assert res.exit_code == 0, res.output
    assert "good-one" in res.output
    assert "broken" in res.output and "UNREADABLE" in res.output


def test_channel_validate_fails_and_names_the_unreadable_profile(tmp_home: Path):
    from typer.testing import CliRunner

    from ns_studio.cli import app

    _broken_beside_a_good_channel(tmp_home)
    res = CliRunner().invoke(app, ["channel", "validate"])
    assert res.exit_code == 1, res.output
    assert "broken: PROBLEM" in res.output and "good-one: ok" in res.output
    only_good = CliRunner().invoke(app, ["channel", "validate", "good-one"])
    assert only_good.exit_code == 0, only_good.output  # asking for one channel is about that channel


def test_the_nightly_report_and_toast_name_the_skipped_profile(tmp_home: Path):
    from datetime import UTC, datetime

    from ns_studio import daily, notifications

    paths = _broken_beside_a_good_channel(tmp_home)
    env = jobs.open_env(light=True)
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    report = daily.run_daily(env, now=now, run_fn=lambda ctx: ["stage"])
    assert [slug for slug, _ in report["unreadable_profiles"]] == ["broken"]
    assert "channel" in (notifications.nightly_message(report, 0, 0) or "")
    nightly = json.loads((paths.logs / "nightly-20261120.json").read_text(encoding="utf-8"))
    assert nightly["unreadable_profiles"][0][0] == "broken"


def test_one_unreadable_profile_is_logged_once_per_process(tmp_home: Path):
    paths = _broken_beside_a_good_channel(tmp_home)
    for _ in range(4):
        crud.all_channels(paths)
    logged = [
        json.loads(line)
        for f in paths.logs.glob("studio-*.jsonl")
        for line in f.read_text(encoding="utf-8").splitlines()
    ]
    assert len([r for r in logged if r["event"] == "channel_profile_skipped"]) == 1
