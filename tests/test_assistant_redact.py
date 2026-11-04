# ruff: noqa: E501
"""Anything that looks like a pasted API key is blanked before it is stored or sent to the model."""

from __future__ import annotations

from pathlib import Path

from ns_dashboard.assistant.llm import ScriptedToolLlm, say
from ns_dashboard.assistant.redact import MARK, redact_obj, redact_text
from tests.test_dashboard_assistant import make, run_chat

PEXELS = "5634" + "92ad6f91" * 6 + "7000"  # 56 letters and digits
PIXABAY = "12345678-" + "0123456789abcdef01234567"
VENDOR = "sk-" + "A1b2C3d4E5f6G7h8I9j0"


def test_bare_labelled_and_vendor_keys_are_blanked():
    assert redact_text(f"use {PEXELS} please") == f"use {MARK} please"
    assert redact_text(f"my Pexels key is {PEXELS}") == f"my Pexels key is {MARK}"
    assert redact_text(f"Here's my Pexels key: {PEXELS}") == f"Here's my Pexels key: {MARK}"
    assert redact_text(f"pixabay {PIXABAY} done") == f"pixabay {MARK} done"
    assert redact_text(f"api_key = {VENDOR}") == f"api_key = {MARK}"
    assert redact_text("Authorization: Bearer abcdef0123456789abcdef") == f"Authorization: {MARK}"
    for key in (PEXELS, PIXABAY, VENDOR):
        assert key not in redact_text(f"a {key} b")


def test_ordinary_text_ids_and_paths_are_kept():
    keep = [
        "Why did last night's run fail?",
        "after-dark-20261101-ab12/s1 is waiting",
        r"C:\Users\x\Nightshift\outbox\after-dark\abcdef0123456789abcdef0123456789abcdef\video.mp4",
        "The key difference between the two voices is speed.",
        "token: authentication-required",
        "sha is short: abc123",
        "A" * 40,  # a long word without digits is not a key
    ]
    for text in keep:
        assert redact_text(text) == text, text


def test_redaction_is_idempotent_and_reaches_nested_values():
    once = redact_text(f"key: {PEXELS}")
    assert redact_text(once) == once
    data = {"a": [f"x {PEXELS}", {"b": PIXABAY}], "n": 3, "ok": True}
    out = redact_obj(data)
    assert PEXELS not in str(out) and PIXABAY not in str(out) and out["n"] == 3 and out["ok"] is True


def test_a_pasted_key_reaches_neither_the_database_nor_the_model(tmp_home: Path):
    llm = ScriptedToolLlm([say("Keys go in Settings > Stock footage keys."), say("Anything else?")])
    dash, _state, _ = make(tmp_home, llm)
    events = run_chat(dash, f"Here's my Pexels key: {PEXELS}", context={"page": "settings", "note": f"x {PIXABAY}"})
    cid = events[0]["payload"]["conversation_id"]
    run_chat(dash, "thanks", conversation_id=cid)
    db = dash.assistant_store.db
    stored = "\n".join(
        str(r["t"] or "")
        for sql in (
            "SELECT content AS t FROM messages", "SELECT content AS t FROM assistant_messages",
            "SELECT title AS t FROM conversations", "SELECT context_json AS t FROM messages",
        )
        for r in db.query(sql)
    )
    sent = str(llm.requests)
    for secret in (PEXELS, PIXABAY):
        assert secret not in stored and secret not in sent
    assert MARK in stored and MARK in sent
    assert "Do not ask for it again" in str(llm.requests[0][-1]["content"])  # the context block says what happened
    first = dash.assistant.history(cid)["messages"][0]
    assert first["content"] == f"Here's my Pexels key: {MARK}"


def test_a_key_in_an_older_stored_message_is_blanked_on_the_way_out(tmp_home: Path):
    llm = ScriptedToolLlm([say("ok")])
    dash, _state, _ = make(tmp_home, llm)
    cid = dash.assistant_store.new_conversation()
    dash.assistant_store.add_message(cid, "user", f"old message with {PEXELS}")  # stored before this change
    dash.assistant_store.add_message(cid, "assistant", "noted")
    run_chat(dash, "and now?", conversation_id=cid)
    assert PEXELS not in str(llm.requests)
