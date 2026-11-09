"""D-24: one voice-name rule (ns_common), refused where the name is typed, never silently lost."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from installer.stage2.answers import AnswersError, build_answers, default_voice_name, slugify
from installer.stage2.steps.s01_questions import S01Questions
from installer.stage2.steps.s07_configs import channel_from_answers, pending_voice_file, queue_voice
from installer.stage2.testing import FakeUi, example_answers, make_test_context
from ns_common import models as m
from ns_studio import jobs, voice_setup

SPACED = {"mode": "design", "description": "a warm, slow baritone", "name": "Night Narrator", "speaker": None}


class Recorder:
    """A `post` that stands in for the TTS service and writes the voice like the real one."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[dict[str, Any]] = []

    def __call__(self, base_url: str, route: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(body)
        folder = self.root / body["name"]
        folder.mkdir(parents=True)
        (folder / "voice.json").write_text(json.dumps({"name": body["name"]}), encoding="utf-8")
        return {"name": body["name"]}


def test_voice_name_has_one_pattern_in_ns_common_and_every_validator_uses_it():
    from ns_dashboard import svc_voices
    from ns_tts import voices

    for rule in (voice_setup.NAME_RE, svc_voices.NAME_RE, voices._NAME_RE):
        assert rule.pattern == m.VOICE_NAME_PATTERN


@pytest.mark.parametrize(
    "name", ["Night Narrator", "", " night", "-night", "_night", "a" * 41, "caf\u00e9", "a.b", "a" * 53]
)
def test_the_voice_model_still_loads_a_stored_name_the_rule_refuses(name):
    """The rule is for names being typed; a profile already on disk must keep loading (D-24 review)."""
    assert m.Voice(mode="design", description="x", name=name).name == name
    assert "pattern" not in m.Voice.model_json_schema()["properties"]["name"]["anyOf"][0]


@pytest.mark.parametrize("name", [None, "n", "Night-Narrator", "night_narrator", "9lives", "a" * 40])
def test_the_voice_model_accepts_a_valid_name(name):
    assert m.Voice(mode="design", description="x", name=name).name == name


def test_answers_refuse_a_voice_name_with_a_space_and_say_what_to_type():
    with pytest.raises(AnswersError) as ei:
        build_answers(example_answers(voice=SPACED))
    text = str(ei.value)
    assert "Night Narrator" in text and "1 to 40 letters, digits, - or _" in text
    assert "Night-Narrator" in text  # the suggestion; the typed name is quoted, not changed


def test_answers_accept_a_valid_voice_name():
    ans = build_answers(example_answers(voice={**SPACED, "name": "Night-Narrator"}))
    assert ans.voice.name == "Night-Narrator"


def test_setup_refuses_the_spaced_name_at_s01_and_writes_nothing(tmp_path: Path):
    ctx = make_test_context(
        tmp_path,
        raw_answers=example_answers(home_path=str(tmp_path / "home"), voice=SPACED),
        ui=FakeUi(interactive=False),
    )
    with pytest.raises(AnswersError, match="Night Narrator"):
        S01Questions().run(ctx)
    assert not pending_voice_file(ctx).exists()


def test_the_suggested_name_gets_through_setup_and_the_voice_is_created(tmp_path: Path, tmp_home: Path):
    """Ends at the effect: s01 accepts it, s07 queues it, the studio creates the voice with that name."""
    voice = {**SPACED, "name": "Night-Narrator"}
    ctx = make_test_context(
        tmp_path,
        raw_answers=example_answers(home_path=str(tmp_path / "home"), voice=voice),
        ui=FakeUi(interactive=False),
    )
    S01Questions().run(ctx)
    ans = ctx.ensure_answers()
    assert queue_voice(ctx, ans, channel_from_answers(ans))
    queued = json.loads(pending_voice_file(ctx).read_text(encoding="utf-8"))
    assert queued["name"] == "Night-Narrator"

    home_cfg = tmp_home / "config"
    home_cfg.mkdir(parents=True, exist_ok=True)
    (home_cfg / "pending_voice.json").write_text(json.dumps(queued), encoding="utf-8")
    env = jobs.open_env(frozenset({"llm"}))
    post = Recorder(env.paths.models_tts_voices)
    assert voice_setup.apply_pending(env, post=post) == "Night-Narrator"
    assert (env.paths.models_tts_voices / "Night-Narrator" / "voice.json").is_file()
    assert not (home_cfg / "pending_voice.json").exists()


@pytest.mark.parametrize(
    "display", ["After Dark Tales", "x" * 60, "Los Cuentos de la Noche Oscura y Fria del Norte", "\u591c"]
)
def test_the_default_voice_name_is_always_a_valid_one(display):
    name = default_voice_name(display)
    assert re.fullmatch(m.VOICE_NAME_PATTERN, name), name
    assert name.endswith("-voice")
    assert name == f"{slugify(display)}-voice" or len(slugify(display)) > 34


def test_a_long_channel_name_still_queues_a_designed_voice(tmp_path: Path):
    ans = build_answers(
        example_answers(
            channel={"display_name": "Los Cuentos de la Noche Oscura y Fria del Norte", "niche": "horror"},
            voice={"mode": "design", "description": "calm", "speaker": None},
        )
    )
    channel = channel_from_answers(ans)
    assert re.fullmatch(m.VOICE_NAME_PATTERN, channel.voice.name or "")


# ---- a saved answers.json from an earlier install is read again, never refused (D-24 break-it round) ----

OLD_LONG = "after-dark-tales-of-the-deep-north-woods-and-lakes-voice"  # 57 chars: the 0.1.x unbounded default
OLD_SHAPES = {
    "long design name": {"mode": "design", "description": "warm baritone", "name": OLD_LONG, "speaker": None},
    "design name with a space": {
        "mode": "design",
        "description": "warm baritone",
        "name": "Night Narrator",
        "speaker": None,
    },
    "preset that kept a spaced name": {"mode": "preset", "speaker": "Ryan", "name": "Night Narrator"},
}


def _saved_answers(ctx, voice: dict[str, Any]) -> Path:
    """config/answers.json the way an earlier setup left it."""
    f = ctx.paths.config / "answers.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(example_answers(home_path=str(ctx.paths.home), voice=voice)), encoding="utf-8")
    return f


def test_a_preset_voice_with_a_leftover_name_is_not_refused():
    ans = build_answers(example_answers(voice=OLD_SHAPES["preset that kept a spaced name"]))
    assert ans.voice.mode == "preset" and ans.voice.speaker == "Ryan"


def test_a_name_typed_now_is_still_refused_for_a_clone_voice():
    voice = {"mode": "clone", "name": "my voice", "speaker": None}
    raw = example_answers(
        voice=voice, voice_ref_audio_path="a.wav", voice_ref_text="hi", voice_rights_confirmed=True
    )
    with pytest.raises(AnswersError, match="my voice"):
        build_answers(raw)


@pytest.mark.parametrize("shape", sorted(OLD_SHAPES))
def test_a_resume_reads_the_saved_answers_of_an_older_install(tmp_path: Path, shape: str):
    """ensure_answers is what s03, s07, s09, s10 and v01 call; the saved name is kept as it is."""
    ctx = make_test_context(tmp_path, ui=FakeUi(interactive=False))
    _saved_answers(ctx, OLD_SHAPES[shape])
    ans = ctx.ensure_answers()
    assert ans.voice.name == OLD_SHAPES[shape]["name"]


@pytest.mark.parametrize("shape", sorted(OLD_SHAPES))
def test_s01_check_accepts_the_saved_answers_of_an_older_install(tmp_path: Path, shape: str):
    ctx = make_test_context(tmp_path, ui=FakeUi(interactive=False))
    _saved_answers(ctx, OLD_SHAPES[shape])
    assert S01Questions().check(ctx) is True
    assert ctx.answers is not None and ctx.answers.voice.name == OLD_SHAPES[shape]["name"]


@pytest.mark.parametrize("shape", sorted(OLD_SHAPES))
def test_s07_check_and_the_download_plan_read_the_saved_answers_of_an_older_install(
    tmp_path: Path, shape: str
):
    from installer.stage2 import verify_downloads
    from installer.stage2.steps.s07_configs import S07Configs

    ctx = make_test_context(tmp_path, ui=FakeUi(interactive=False))
    _saved_answers(ctx, OLD_SHAPES[shape])
    assert S07Configs().check(ctx) is False  # nothing is configured yet; it answers, it does not raise
    items = verify_downloads.plan_for_install(ctx.paths, ctx.app_path)
    assert items  # the models setup would fetch for these answers


def test_the_same_old_name_given_to_setup_now_is_refused(tmp_path: Path):
    """The window or --answers supplies it in this run: that is a name typed now."""
    ctx = make_test_context(
        tmp_path,
        raw_answers=example_answers(home_path=str(tmp_path / "home"), voice=OLD_SHAPES["long design name"]),
        ui=FakeUi(interactive=False),
    )
    with pytest.raises(AnswersError, match="cannot be used"):
        S01Questions().run(ctx)


# ---- an update over kept answers with an old-shaped voice name goes through s01.run (D-24 round 2) ----


def _update(tmp_path: Path, new: dict[str, Any]):
    """A real S01Questions().run for answers supplied from a file outside config, over a home whose saved
    answers.json is the earlier install's (the way an update, repair or reinstall reaches s01)."""
    ctx = make_test_context(tmp_path, raw_answers=new, ui=FakeUi(interactive=False))
    src = tmp_path / "new-answers.json"
    src.write_text(json.dumps(new), encoding="utf-8")
    ctx.answers_path = src
    return ctx


def _saved(ctx) -> dict[str, Any]:
    return json.loads((ctx.paths.config / "answers.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("shape", sorted(OLD_SHAPES))
def test_an_update_that_changes_only_the_run_time_keeps_an_old_voice_name(tmp_path: Path, shape: str):
    earlier = make_test_context(tmp_path, ui=FakeUi(interactive=False))
    _saved_answers(earlier, OLD_SHAPES[shape])
    ctx = _update(tmp_path, {"run_time": "05:45"})
    S01Questions().run(ctx)  # used to raise AnswersError "The voice name '...' cannot be used"
    saved = _saved(ctx)
    assert saved["run_time"] == "05:45"  # the change the owner asked for reached the saved answers
    assert saved["voice"]["name"] == OLD_SHAPES[shape]["name"]  # the old voice stays as it was
    assert ctx.answers is not None and ctx.answers.run_time == "05:45"


def test_an_answers_file_copied_from_the_saved_one_with_a_new_run_time_is_not_a_voice_change(tmp_path: Path):
    earlier = make_test_context(tmp_path, ui=FakeUi(interactive=False))
    f = _saved_answers(earlier, OLD_SHAPES["long design name"])
    copy = {**json.loads(f.read_text(encoding="utf-8")), "run_time": "05:45"}  # names the same voice again
    ctx = _update(tmp_path, copy)
    S01Questions().run(ctx)
    assert _saved(ctx)["run_time"] == "05:45" and _saved(ctx)["voice"]["name"] == OLD_LONG


@pytest.mark.parametrize("shape", sorted(OLD_SHAPES))
def test_an_update_that_types_a_bad_voice_name_is_still_refused_and_changes_nothing(
    tmp_path: Path, shape: str
):
    earlier = make_test_context(tmp_path, ui=FakeUi(interactive=False))
    _saved_answers(earlier, OLD_SHAPES[shape])
    before = _saved(earlier)
    typed = {"mode": "design", "description": "a new voice", "name": "Another Bad Name", "speaker": None}
    ctx = _update(tmp_path, {"run_time": "05:45", "voice": typed})
    with pytest.raises(AnswersError, match="Another Bad Name"):
        S01Questions().run(ctx)
    assert _saved(ctx) == before  # the saved answers are untouched


def test_an_update_that_changes_the_description_but_keeps_the_old_bad_name_is_refused(tmp_path: Path):
    """The voice is edited, so it would be created again under the name: the rule applies to it."""
    earlier = make_test_context(tmp_path, ui=FakeUi(interactive=False))
    _saved_answers(earlier, OLD_SHAPES["long design name"])
    typed = {**OLD_SHAPES["long design name"], "description": "a different, brighter voice"}
    ctx = _update(tmp_path, {"voice": typed})
    with pytest.raises(AnswersError, match="cannot be used"):
        S01Questions().run(ctx)


def test_an_update_may_replace_an_old_bad_name_with_a_good_one(tmp_path: Path):
    earlier = make_test_context(tmp_path, ui=FakeUi(interactive=False))
    _saved_answers(earlier, OLD_SHAPES["long design name"])
    typed = {**OLD_SHAPES["long design name"], "name": "after-dark-voice"}
    ctx = _update(tmp_path, {"voice": typed})
    S01Questions().run(ctx)
    assert _saved(ctx)["voice"]["name"] == "after-dark-voice"
