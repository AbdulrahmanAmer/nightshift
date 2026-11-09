# ruff: noqa: E501
"""D-27: the Assistant checks a changing tool's arguments BEFORE it shows a confirm card.

The defect: a small model proposed `start_story_job` with `schedule_at: "2026-11-09T12:54Z', "`; the card was shown,
the owner pressed Yes and got "That did not work ... 'schedule_at' must be a date and time". Now every changing tool
runs a dry run that makes the same checks the real run makes; what fails goes back to the model as a tool error in
plain words and no card (and no pending action) exists. The tests are parametrized over the registry, so a new
changing tool is covered the day it is added: it needs a case here or the suite fails.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ns_dashboard.assistant.llm import ScriptedToolLlm, StaticProvider, call, say
from ns_dashboard.assistant.loop import refusal_text, run_tool
from ns_dashboard.assistant.registry import ToolContext
from ns_dashboard.assistant.tools import SCHEMA_ONLY, build_registry
from ns_dashboard.errors import AdapterUnavailable, ApiError
from ns_dashboard.fakes import FakeSupervisor
from ns_dashboard.supervisor_link import SupervisorLink
from tests.assistant_eval import fixture_home

SCRIPT = (
    "The porch light flickered twice before it died. I stood very still and counted the seconds, the way my "
    "grandmother taught me, because she always said the dark is only honest when it is quiet. Somewhere behind "
    "me the floorboards gave one long, patient creak. Then a voice I knew very well whispered my name from the "
    "empty kitchen."
)
BAD_SCHEDULE = "2026-11-09T12:54Z', "  # the value the real Qwen3-1.7B wrote on the engine test (defect D-27)

#: For every changing tool that has a dry run: arguments that pass the JSON schema but that the real run refuses
#: (in the fixture home the engine does not answer), and a phrase the plain-words error must contain. A new changing
#: tool without a row fails `test_every_...`; `test_every_invalid_row_passes_the_json_schema` keeps the rows honest.
INVALID: dict[str, tuple[dict[str, Any], str]] = {
    "warmup_engine": ({"service": "llama"}, "engine is not running"),
    "shutdown_engine": ({"grace_s": 5}, "engine is not running"),
    "create_channel": ({"profile": {"slug": "Not A Slug"}}, "channel"),
    "edit_channel": ({"slug": "no-such-channel", "changes": {"niche": "x"}}, "no-such-channel"),
    "pause_channel": ({"slug": "no-such-channel"}, "no-such-channel"),
    "resume_channel": ({"slug": "no-such-channel"}, "no-such-channel"),
    "start_story_job": (
        {"channel": "after-dark", "schedule_at": BAD_SCHEDULE},
        "'schedule_at' must be a date and time",
    ),
    "start_script_job": ({"channel": "no-such-channel", "script": SCRIPT}, "no-such-channel"),
    "start_clip_job": ({"channel": "after-dark", "path": "D:/no/such/video.mp4"}, "is not a video file"),
    "rerun_clips": ({"job_id": "no-such-job", "template": "auto"}, "clip job was not found"),
    "resume_job": ({"job_id": "no-such-job"}, "story job was not found"),
    "cancel_job": ({"job_id": "no-such-job"}, "job was not found"),
    "import_analytics": ({"path": "D:/no/such/analytics.csv"}, "is not a file"),
    "set_schedule": ({}, "schedule_time"),
    "design_voice": (
        {"name": "bad name!", "description": "warm and calm", "test_line": "Hello there."},
        "Voice names",
    ),
    "clone_voice": (
        {"name": "okname", "sample_path": "D:/no/such/voice.wav", "test_line": "Hello there."},
        "not a recording",
    ),
    "add_idea": (
        {"channel": "no-such-channel", "premise": "A lighthouse keeper gets letters from the future."},
        "no-such-channel",
    ),
    "set_idea_status": ({"idea_id": 99999, "status": "approved"}, "idea was not found"),
    "fetch_footage": ({"q": "   "}, "search word"),
    "duplicate_format": ({"duplicate_of": "no_such_format"}, "no_such_format"),
    "edit_format": ({"id": "no_such_format", "changes": {"name": "x"}}, "no_such_format"),
    "workshop_start": ({"channel": "no-such-channel"}, "no-such-channel"),
    "workshop_concepts": ({"slug": "no-such-story"}, "no-such-story"),
    "workshop_regenerate": ({"slug": "no-such-story", "letter": "A", "feedback": "darker"}, "no-such-story"),
    "workshop_choose": ({"slug": "no-such-story", "letter": "A"}, "no-such-story"),
    "workshop_map": ({"slug": "no-such-story", "parts": 5}, "no-such-story"),
    "workshop_write_part": ({"slug": "no-such-story", "n": 1}, "no-such-story"),
    "workshop_revise_part": ({"slug": "no-such-story", "n": 1, "feedback": "shorter"}, "no-such-story"),
    "workshop_approve": ({"slug": "no-such-story"}, "no-such-story"),
    "workshop_design": ({"slug": "no-such-story", "visual_treatment": "dark"}, "no-such-story"),
    "workshop_produce": ({"slug": "no-such-story"}, "no-such-story"),
}
#: Changing tools whose refusal is a state, not an argument, with the test that pins it.
STATE_ONLY: dict[str, str] = {
    "start_verify": "test_a_job_is_not_offered_while_shutting_down",
    "make_diagnostics": "test_diagnostics_are_refused_before_the_card_when_the_doctor_is_missing",
}

#: Changing tools with arguments that are valid: they still get their card.
VALID: dict[str, dict[str, Any]] = {
    "start_story_job": {"channel": "after-dark", "format": "shorts_only", "count": 1},
    "start_script_job": {
        "channel": "after-dark",
        "format": "shorts_only",
        "script": SCRIPT,
        "title": "The Porch",
    },
    "pause_channel": {"slug": "after-dark"},
    "resume_channel": {"slug": "after-dark"},
    "add_idea": {"channel": "after-dark", "premise": "A ferry captain finds a passenger who never boarded."},
    "set_schedule": {"schedule_time": "03:30"},
    "start_verify": {"quick": True},
    "workshop_start": {"channel": "after-dark", "premise": "A ferry that arrives before it leaves"},
    "make_diagnostics": {},
}


def changing_tools() -> list[str]:
    return sorted(t.spec.name for t in build_registry().all() if t.spec.side_effect)


@pytest.fixture
def home(tmp_home: Path) -> fixture_home.FixtureHome:
    return fixture_home.build(tmp_home)


def turn(home: fixture_home.FixtureHome, *steps: dict[str, Any]) -> list[dict[str, Any]]:
    """One chat turn on the scripted model: the given tool calls, then a closing sentence."""
    llm = ScriptedToolLlm([*steps, say("Done for now.")])
    home.dash.assistant.provider = StaticProvider(llm)
    return list(home.dash.assistant.chat("do it", None, {}))


def turn_with(home: fixture_home.FixtureHome, *script: dict[str, Any]) -> list[dict[str, Any]]:
    """One chat turn on exactly the given model messages (no closing sentence added)."""
    home.dash.assistant.provider = StaticProvider(ScriptedToolLlm(list(script)))
    return list(home.dash.assistant.chat("do it", None, {}))


def cards(home: fixture_home.FixtureHome) -> int:
    row = home.dash.assistant_store.db.one("SELECT COUNT(*) AS n FROM actions")
    assert row is not None
    return int(row["n"])


def result_of(events: list[dict[str, Any]]) -> dict[str, Any]:
    results = [e["payload"] for e in events if e["kind"] == "tool_result"]
    assert len(results) == 1, results
    return results[0]


# ------------------------------------------------------------------ the defect itself


def test_start_story_job_with_a_bad_schedule_gives_a_tool_error_and_no_card(home: fixture_home.FixtureHome):
    """D-27 as filed: the model's `schedule_at` is not a date; before the fix a card was shown and Yes failed."""
    events = turn(home, call("start_story_job", {"channel": "after-dark", "schedule_at": BAD_SCHEDULE}))
    assert not [e for e in events if e["kind"] == "confirm"]
    result = result_of(events)
    assert (
        result["ok"] is False
        and "'schedule_at' must be a date and time such as 2026-11-03T02:00" in result["error"]
    )
    assert cards(home) == 0
    assert home.dash.assistant_store.db.query("SELECT 1 FROM actions WHERE tool = 'start_story_job'") == []
    assert home.dash.jobs.list_jobs("queued", "run", 10) == []  # and nothing was queued


def test_the_model_sees_the_error_in_plain_words_and_can_correct_itself(home: fixture_home.FixtureHome):
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT02:00")
    llm = ScriptedToolLlm(
        [
            call("start_story_job", {"channel": "after-dark", "schedule_at": BAD_SCHEDULE}, call_id="first"),
            call("start_story_job", {"channel": "after-dark", "schedule_at": tomorrow}, call_id="second"),
            say("I asked you to confirm one video for tomorrow at 02:00."),
        ]
    )
    home.dash.assistant.provider = StaticProvider(llm)
    events = list(home.dash.assistant.chat("a video for tomorrow at two", None, {}))
    told = next(m for m in llm.requests[1] if m.get("role") == "tool" and m.get("tool_call_id") == "first")
    assert (
        "must be a date and time" in told["content"] and "Nothing was shown to the owner" in told["content"]
    )
    shown = [e["payload"] for e in events if e["kind"] == "confirm"]
    assert len(shown) == 1 and tomorrow in shown[0]["text"]  # the corrected call got the one card
    assert cards(home) == 1


def test_a_schedule_more_than_31_days_away_is_refused_before_the_card(home: fixture_home.FixtureHome):
    far = (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%dT02:00")
    events = turn(home, call("start_story_job", {"channel": "after-dark", "schedule_at": far}))
    assert "at most 31 days" in result_of(events)["error"]
    assert cards(home) == 0


def test_a_paused_channel_is_refused_before_the_card(home: fixture_home.FixtureHome):
    home.dash.channels.pause("after-dark", "test")
    events = turn(home, call("start_story_job", {"channel": "after-dark"}))
    assert "paused" in result_of(events)["error"]
    assert cards(home) == 0


def test_a_story_job_is_refused_before_the_card_when_no_footage_key_is_stored(home: fixture_home.FixtureHome):
    """The run itself would stop with NS-E502 an hour in; the owner hears it before the card (gap L3 made earlier)."""
    home.state.fake_services = False
    assert home.state.adapters.keyring.get("pexels_api_key") is None  # a fresh home holds no key
    events = turn(home, call("start_story_job", {"channel": "after-dark"}))
    result = result_of(events)
    assert result["ok"] is False and "footage" in result["error"].lower()
    assert cards(home) == 0


# ------------------------------------------------------------------ every changing tool, from the registry


@pytest.mark.parametrize("name", sorted(INVALID))
def test_arguments_the_real_run_refuses_give_a_tool_error_and_no_confirm_event(
    home: fixture_home.FixtureHome, name: str
):
    args, phrase = INVALID[name]
    events = turn(home, call(name, args))
    assert not [e for e in events if e["kind"] == "confirm"], (
        f"{name} showed a card for arguments it cannot run"
    )
    result = result_of(events)
    assert result["ok"] is False, result
    assert phrase in result["error"], result["error"]
    assert cards(home) == 0
    # the effect behind it (rule 5): the real run refuses the same arguments, so the card would have been a trap
    td = home.dash.registry.get(name)
    assert td is not None
    ok, real = run_tool(td, ToolContext(home.dash, {}), args)
    assert ok is False, f"{name}: the dry run refused but the real run accepted {args}: {real}"


def test_every_changing_tool_has_a_dry_run_or_a_reason_and_a_case_here():
    """A new changing tool must either validate before its card or say why the schema is all there is to check."""
    reg = build_registry()
    changing = {t.spec.name: t for t in reg.all() if t.spec.side_effect}
    bare = {n for n, t in changing.items() if t.dry_run is None}
    assert bare == set(SCHEMA_ONLY), (
        f"changing tools without a dry run must be listed in SCHEMA_ONLY with a reason: {sorted(bare ^ set(SCHEMA_ONLY))}"
    )
    assert all(reason.strip() for reason in SCHEMA_ONLY.values())
    missing = sorted(set(changing) - set(INVALID) - set(SCHEMA_ONLY) - set(STATE_ONLY))
    assert not missing, f"add an INVALID case (or a STATE_ONLY test) for: {missing}"
    stale = sorted((set(INVALID) | set(STATE_ONLY)) - set(changing))
    assert not stale, f"INVALID or STATE_ONLY name tools that no longer exist: {stale}"
    assert not set(INVALID) & set(STATE_ONLY)
    here = Path(__file__).read_text(encoding="utf-8")
    assert all(f"def {test}(" in here for test in STATE_ONLY.values()), STATE_ONLY


def test_every_invalid_row_passes_the_json_schema():
    """A row the schema already rejects would pass without any dry run (the 34-row test this replaces did)."""
    reg = build_registry()
    for name, (args, _phrase) in INVALID.items():
        assert reg.validate(name, args) is None, (name, reg.validate(name, args))


@pytest.mark.parametrize("name", sorted(VALID))
def test_valid_arguments_still_get_their_card(home: fixture_home.FixtureHome, name: str):
    events = turn(home, call(name, VALID[name]))
    shown = [e["payload"] for e in events if e["kind"] == "confirm"]
    assert [s["tool"] for s in shown] == [name], [e for e in events if e["kind"] == "tool_result"]
    action = home.dash.assistant_store.get_action(shown[0]["action_id"])
    assert action is not None and action["status"] == "pending"


def test_valid_arguments_with_ids_from_the_home_get_their_card(home: fixture_home.FixtureHome):
    idea = home.dash.library.list_ideas("after-dark", None, None, 5)[0]["idea_id"]
    slug = home.ids["series_slug"]
    for name, args in (
        ("set_idea_status", {"idea_id": idea, "status": "approved"}),
        ("workshop_concepts", {"slug": slug}),
        ("edit_channel", {"slug": "after-dark", "changes": {"voice_notes": "calmer"}}),
    ):
        events = turn(home, call(name, args))
        assert [e["payload"]["tool"] for e in events if e["kind"] == "confirm"] == [name], (name, events)


def test_workshop_checks_know_the_state_of_the_story(home: fixture_home.FixtureHome):
    """A story that exists but has no chosen concept, series or design cannot map, write, approve or produce yet."""
    slug = home.ids["series_slug"]
    for name, args, phrase in (
        ("workshop_map", {"slug": slug, "parts": 3}, "choose a concept first"),
        ("workshop_write_part", {"slug": slug, "n": 1}, "series.md"),
        ("workshop_approve", {"slug": slug}, "series.md"),
        ("workshop_produce", {"slug": slug}, "series.md"),
        ("workshop_choose", {"slug": slug, "letter": "B"}, "no draft B"),
        ("workshop_regenerate", {"slug": slug, "letter": "A", "feedback": "x"}, "no draft A"),
    ):
        events = turn(home, call(name, args))
        assert not [e for e in events if e["kind"] == "confirm"], name
        assert phrase in result_of(events)["error"], (name, result_of(events))
    assert cards(home) == 0


def test_warmup_and_shutdown_get_their_card_only_when_the_engine_answers(home: fixture_home.FixtureHome):
    """D-27 for the engine tools: with the engine off the card was a Yes that ended in "not running"."""
    with FakeSupervisor() as sup:
        home.state.link = SupervisorLink(sup.url)
        for name, args in (("warmup_engine", {"service": "llama"}), ("shutdown_engine", {"grace_s": 5})):
            events = turn(home, call(name, args))
            shown = [e["payload"]["tool"] for e in events if e["kind"] == "confirm"]
            assert shown == [name], (name, [e for e in events if e["kind"] == "tool_result"])
        assert not [
            p for m, p, _b in sup.calls if m == "POST"
        ]  # looking asked the engine for its status only
    assert cards(home) == 2


def test_diagnostics_are_refused_before_the_card_when_the_doctor_is_missing(
    home: fixture_home.FixtureHome, monkeypatch: pytest.MonkeyPatch
):
    def missing(*_a: Any, **_k: Any) -> None:
        raise AdapterUnavailable("ns_supervisor.doctor.build_bundle is not installed yet")

    monkeypatch.setattr(home.state.adapters.doctor, "available", missing)
    events = turn(home, call("make_diagnostics", {}))
    result = result_of(events)
    assert result["ok"] is False and "not installed yet" in result["error"] and "shortcut" in result["error"]
    assert not [e for e in events if e["kind"] == "confirm"] and cards(home) == 0
    td = home.dash.registry.get("make_diagnostics")
    assert td is not None
    monkeypatch.setattr(home.state.adapters.doctor, "bundle", missing)
    ok, real = run_tool(td, ToolContext(home.dash, {}), {})
    assert (
        ok is False and real["error"]["code"] == "diagnostics_unavailable"
    )  # the Yes would have met the same 501


def test_a_job_is_not_offered_while_shutting_down(home: fixture_home.FixtureHome):
    home.state.draining = True
    for name, args in (("start_verify", {"quick": True}), ("start_story_job", {"channel": "after-dark"})):
        events = turn(home, call(name, args))
        assert "shutting down" in result_of(events)["error"], name
    assert cards(home) == 0


def test_a_cancel_of_a_finished_job_is_refused_before_the_card(home: fixture_home.FixtureHome):
    events = turn(home, call("cancel_job", {"job_id": home.ids["failed_job"]}))
    assert "already ended" in result_of(events)["error"]
    assert cards(home) == 0


def test_channel_problems_are_listed_field_by_field(home: fixture_home.FixtureHome):
    """`invalid_profile` carries its list of problems in `details`; the model needs them in the message."""
    events = turn(home, call("create_channel", {"profile": {"slug": "Not A Slug"}}))
    error = result_of(events)["error"]
    assert (
        "need fixing" in error
        and error.count(":") >= 1
        and len(error) > len("Some channel settings need fixing.") + 10
    )


def test_refusal_text_puts_message_details_and_hint_in_one_plain_sentence():
    e = ApiError(
        422,
        "invalid_profile",
        "Some settings need fixing",
        details=[{"field": "niche", "message": "Say what the channel is about"}, "plain note"],
        hint="Open Channels",
    )
    assert (
        refusal_text(e)
        == "Some settings need fixing. niche: Say what the channel is about. plain note. Open Channels."
    )
    assert refusal_text(ApiError(404, "not_found", "That job was not found.")) == "That job was not found."


def test_a_check_that_breaks_never_becomes_a_card(
    home: fixture_home.FixtureHome, monkeypatch: pytest.MonkeyPatch
):
    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("disk gone")

    monkeypatch.setattr(home.dash.jobs, "check_run", boom)
    events = turn(home, call("start_story_job", {"channel": "after-dark"}))
    assert "could not be checked" in result_of(events)["error"] and "disk gone" in result_of(events)["error"]
    assert cards(home) == 0


# ------------------------------------------------------------------ the owner hears the refusal


def test_a_model_that_repeats_the_bad_call_until_it_runs_out_of_steps_still_tells_the_owner_why(
    home: fixture_home.FixtureHome,
):
    """A small model may repeat the call; the stop note carries the plain-words reason instead of hiding it."""
    bad = call("start_story_job", {"channel": "after-dark", "schedule_at": BAD_SCHEDULE})
    llm = ScriptedToolLlm([bad, bad, bad])
    home.dash.assistant.provider = StaticProvider(llm)
    home.dash.assistant.max_steps = 3
    events = list(home.dash.assistant.chat("a video at 12:54", None, {}))
    messages = [e["payload"]["text"] for e in events if e["kind"] == "message"]
    assert len(messages) == 1
    assert "I stopped after several steps" in messages[0]
    assert "'schedule_at' must be a date and time" in messages[0], messages
    assert cards(home) == 0


def test_a_model_that_answers_nothing_after_a_refusal_still_tells_the_owner_why(
    home: fixture_home.FixtureHome,
):
    """Empty content after the refusal used to show "I have nothing to add."; the owner now gets the reason."""
    events = turn_with(home, call("cancel_job", {"job_id": "no-such-job"}), say(""))
    messages = [e["payload"]["text"] for e in events if e["kind"] == "message"]
    assert (
        len(messages) == 1 and "nothing to confirm" in messages[0] and "job was not found" in messages[0]
    ), messages
    assert "nothing to add" not in messages[0]
    stored = [
        m["content"] for m in home.dash.assistant_store.messages(events[0]["payload"]["conversation_id"])
    ]
    assert messages[0] in stored  # a reloaded conversation shows it too


def test_an_answer_the_model_wrote_is_not_replaced_by_the_refusal(home: fixture_home.FixtureHome):
    events = turn_with(
        home,
        call("cancel_job", {"job_id": "no-such-job"}),
        say("That job does not exist; which one do you mean?"),
    )
    messages = [e["payload"]["text"] for e in events if e["kind"] == "message"]
    assert messages == ["That job does not exist; which one do you mean?"]


def test_a_refusal_before_a_card_is_not_repeated_at_the_end_of_the_turn(home: fixture_home.FixtureHome):
    """The model corrected itself and the owner has the card: an empty closing message stays as it was."""
    events = turn_with(
        home,
        call("cancel_job", {"job_id": "no-such-job"}),
        call("pause_channel", {"slug": "after-dark"}),
        say(""),
    )
    messages = [e["payload"]["text"] for e in events if e["kind"] == "message"]
    assert messages == ["I have nothing to add."]
    assert [e["payload"]["tool"] for e in events if e["kind"] == "confirm"] == ["pause_channel"]


# ------------------------------------------------------------------ the two tool descriptions


def test_the_story_and_script_tools_point_at_each_other():
    """D-27: a small model picked `start_story_job` for a pasted story; each description now names the other tool."""
    reg = build_registry()
    story, script = reg.get("start_story_job"), reg.get("start_script_job")
    assert story is not None and script is not None
    assert "start_script_job" in story.spec.description and "start_story_job" in script.spec.description
    assert "NEW story" in story.spec.description and "wrote or pasted" in script.spec.description
    assert len(story.spec.description) <= 160 and len(script.spec.description) <= 160
