"""D-24: one voice-name rule (ns_common), refused where the name is typed, never silently lost."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

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

    assert m.Voice.model_json_schema()["properties"]["name"]["anyOf"][0]["pattern"] == m.VOICE_NAME_PATTERN
    for rule in (voice_setup.NAME_RE, svc_voices.NAME_RE, voices._NAME_RE):
        assert rule.pattern == m.VOICE_NAME_PATTERN


@pytest.mark.parametrize(
    "name", ["Night Narrator", "", " night", "-night", "_night", "a" * 41, "caf\u00e9", "a.b"]
)
def test_the_voice_model_refuses_a_name_the_voice_service_would_refuse(name):
    with pytest.raises(ValidationError):
        m.Voice(mode="design", description="x", name=name)


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
