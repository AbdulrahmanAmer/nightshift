import json
from typing import Any

import pytest

from ns_common.errors import NsError, describe, load_errors
from ns_common.paths import contracts_dir


def test_errors_json_is_well_formed():
    data = json.loads((contracts_dir() / "errors.json").read_text(encoding="utf-8"))
    for code, d in data.items():
        assert code.startswith("NS-E") or code.startswith("NS-W"), code
        assert set(d) == {"message", "hint"}, code


def _no_repeated_key(pairs):
    """``json`` keeps the last of two equal keys without a word; two merged branches can add the same code."""
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"repeated key {key!r}")
        seen[key] = value
    return seen


def test_errors_json_has_no_code_twice():
    """Git merges two lines that add the same code without a conflict (NS-E120 in two
    branches, 2026-11-09)."""
    text = (contracts_dir() / "errors.json").read_text(encoding="utf-8")
    data = json.loads(text, object_pairs_hook=_no_repeated_key)
    assert len(data) > 50


def test_the_duplicate_key_loader_does_fail_on_a_repeated_code():
    two = '{"NS-E120": {"message": "a", "hint": "b"}, "NS-E120": {"message": "c", "hint": "d"}}'
    with pytest.raises(ValueError, match="repeated key 'NS-E120'"):
        json.loads(two, object_pairs_hook=_no_repeated_key)


def test_describe_known_code():
    d = describe("NS-E102")
    assert d.kind == "error"
    assert "NVIDIA" in d.message


def test_nserror_str_and_data():
    err = NsError("NS-E301", "timeout after 600s", url="http://127.0.0.1:8080")
    assert str(err).startswith("NS-E301: ")
    assert "timeout after 600s" in str(err)
    assert err.data == {"url": "http://127.0.0.1:8080"}
    assert err.definition.kind == "error"


def test_warning_kind():
    assert describe("NS-W401").kind == "warning"


def test_unknown_code_raises():
    with pytest.raises(KeyError):
        describe("NS-E999")
    assert "NS-E999" in load_errors() or True  # load_errors never raises


def test_studio_and_transcription_codes_exist():
    for code in (
        "NS-E320", "NS-E321", "NS-E322", "NS-E323", "NS-E324", "NS-E325", "NS-E326", "NS-E408", "NS-E409",
    ):
        d = describe(code)
        assert d.kind == "error" and d.message and d.hint, code
    assert describe("NS-E408").message == "Transcription failed."


def test_clip_rights_and_unexpected_codes_exist():
    assert describe("NS-E410").kind == "error" and "right" in describe("NS-E410").message
    assert describe("NS-E327").kind == "error"


def test_fake_voice_tone_burst_warning_has_its_own_code_next_to_the_other_voice_warnings():
    d = describe("NS-W303")  # CR-H7-1: "the fake voice made tone bursts"
    assert d.kind == "warning"
    assert "tone bursts" in d.message and "speech recognition" in d.message
    assert "Speech" in d.hint and "NS-E403" in d.hint and "test voice" in d.hint
    # the 3xx family is the text-and-voice stages; NS-W301 and NS-W302 are the engine's own warnings and are
    # not reused for this one
    assert describe("NS-W301").message != d.message and describe("NS-W302").message != d.message


def test_ns_e702_claims_no_outcome():
    """D-39: NS-E702 is raised by refusals that changed nothing and by a rollback that did not finish, so its
    fixed message names no outcome; the detail says which version is in place. The codes of the one-at-a-time
    rule and of the half-filled environment exist."""
    d = describe("NS-E702")
    assert "rolled back" not in d.message and "previous version" not in d.message
    assert "which version is in place" in d.hint and "ns doctor --bundle" in d.hint
    known = load_errors()
    assert "Another update, rollback or repair" in known["NS-E705"].message
    assert "Nothing was changed" in known["NS-E705"].hint and "Nothing was changed" in known["NS-E706"].hint
    assert "Python environments" in known["NS-E706"].message and known["NS-W210"].kind == "warning"


#: FN-3: the `ns` and `studio` commands the owner is told to type (they work in any new terminal since then),
#: from the sentences in code that name one (the card's table C), beside every text of errors.json.
TABLE_C = (
    "ns schedule install --enable", "ns tune", "studio voice design", "studio calibrate --channel x",
    "studio status", "studio channel add", "studio review", "studio channel list",
)
#: Words after "ns" or "studio" in a text that are not a command (NS-E112 names the studio venv folder;
#: NS-W119 and NS-W120 name the switch "... the ns and studio commands ...").
NOT_COMMANDS = {("studio", "venv"), ("ns", "and")}


def test_every_named_command_exists():
    """Each `ns <x>` and `studio <x>` an error text or a sentence in code names is a command of that program
    (and `<x> <y>` a command of its group), so the way out a message gives can be typed as written."""
    import re

    import click
    import typer.main

    from ns_studio.cli import app as studio_app
    from ns_supervisor.cli import app as ns_app

    programs = {"ns": typer.main.get_command(ns_app), "studio": typer.main.get_command(studio_app)}
    data = json.loads((contracts_dir() / "errors.json").read_text(encoding="utf-8"))
    texts = [f"{d['message']} {d['hint']}" for d in data.values()] + list(TABLE_C)
    named = re.compile(r"(?<![\w.\\/-])(ns|studio) ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?")
    found = [m.groups() for text in texts for m in named.finditer(text)]
    assert len(found) >= 28, found
    for tool, first, second in found:
        if (tool, first) in NOT_COMMANDS:
            continue
        program: Any = programs[tool]
        command: Any = program.get_command(click.Context(program), first)
        assert command is not None, f"'{tool} {first}' is named but is no command of {tool}"
        if hasattr(command, "get_command") and second:      # a group: its command too
            sub = command.get_command(click.Context(command), second)
            assert sub is not None, f"'{tool} {first} {second}' is named but is no command of {tool} {first}"
