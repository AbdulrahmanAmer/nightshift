# ruff: noqa: E501
"""API <-> UI contract, part 2: the routes `test_api_ui_contract.py` left out, the POST/PUT answers and the chat stream.

Same method as part 1 (fake adapters, real services, `ui/src/lib/types.ts` parsed field for field): the GETs the
pages call with a filter or a query (`status=approved,uploaded`, `?q=`), the lists that had no declared type
(models, log components, conversations, history, uninstall, update check), the bodies the pages read from
POST and PUT calls (`Job`, validation, uploads, voices, reports, settings, series, confirm), and the event names
and payloads of the `/assistant/chat` stream against the `ChatEvent` union.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from ns_dashboard.assistant.llm import EngineOffline, ScriptedToolLlm, StaticProvider, call, say
from ns_dashboard.fakes import seed_channel, seed_output
from tests.test_api_ui_contract import (  # noqa: F401  (the `world` fixture is used by name)
    TYPES,
    Case,
    all_members,
    check_payload,
    check_value,
    split_top,
    world,
)
from tests.test_dashboard_api import make_env, sse_events

pytest.importorskip("fastapi")

HAS_MULTIPART = bool(importlib.util.find_spec("python_multipart") or importlib.util.find_spec("multipart"))
needs_multipart = pytest.mark.skipif(not HAS_MULTIPART, reason="python-multipart not installed")


def assert_clean(type_name: str, payload: Any, where: str, many: bool = False) -> None:
    problems = check_payload(type_name, payload, many, where)
    assert not problems, "API and UI disagree:\n  " + "\n  ".join(problems)


@pytest.fixture
def seeded(world: Any) -> Any:  # noqa: F811
    """`world` plus a conversation, a model download and a finished clip job that can be re-run."""
    client, state, ids = world
    dash = client.app.state.dashboard
    cid = dash.assistant_store.new_conversation("What is waiting?")
    dash.assistant_store.add_message(cid, "user", "What is waiting?")
    dash.assistant_store.add_message(cid, "assistant", "One video.")
    ids["cid"] = cid
    state.model_downloads["voicedesign"] = {"state": "done", "message": "voicedesign is ready"}
    clip = state.store.create("clip", "after-dark", {"source_id": "src-20261101-aaaa", "template": "auto", "max_clips": 3})
    state.store.mark_running(clip.job_id)
    state.store.finish(clip.job_id, "done", "Done", {"output_ids": [], "studio_job_id": "after-dark-20261101-cccc"})
    ids["clip_job"] = clip.job_id
    return client, state, ids


# ------------------------------------------------------------------ GETs that were skipped


GET_CASES = [
    Case("/api/outputs?status=approved,uploaded", "Output", many=True),
    Case("/api/outputs?status=rejected", "Output", many=True),
    Case("/api/models/state", "ModelsState"),
    Case("/api/logs/components", "string", many=True),
    Case("/api/assistant/conversations", "AssistantConversation", many=True),
    Case("/api/assistant/history?conversation_id={cid}", "AssistantHistory"),
    Case("/api/settings/uninstall", "UninstallInfo"),
    Case("/api/update/check", "UpdateCheck"),
    Case("/api/library/stories?q=Lift", "Story", many=True),
    Case("/api/library/footage?q=night", "Footage", many=True),
]


def test_the_new_types_exist_in_types_ts():
    ifaces, aliases = TYPES
    for case in GET_CASES:
        assert case.type == "string" or case.type in ifaces or case.type in aliases, case.type


@pytest.mark.parametrize("case", GET_CASES, ids=[c.path for c in GET_CASES])
def test_get_routes_answer_what_types_ts_declares(seeded: Any, case: Case):
    client, _state, ids = seeded
    r = client.get(case.path.format(**ids))
    assert r.status_code == 200, f"{case.path}: {r.status_code} {r.text[:200]}"
    assert_clean(case.type, r.json(), case.path.split("?")[0], case.many)


def test_the_status_filters_return_the_states_they_name(seeded: Any):
    client, _state, _ids = seeded
    both = client.get("/api/outputs", params={"status": "approved,uploaded"}).json()
    assert {o["status"] for o in both} == {"approved", "uploaded"}
    uploaded = next(o for o in both if o["status"] == "uploaded")
    assert uploaded.get("url") == "https://youtu.be/abc"
    rejected = client.get("/api/outputs", params={"status": "rejected"}).json()
    assert [o["status"] for o in rejected] == ["rejected"] and rejected[0].get("reason") == "bad audio"


def test_the_series_stand_in_has_the_shape_the_workshop_really_returns(tmp_home: Path):
    """`SeriesStory` was checked only against a hand-written stand-in: pin it against `Workshop.status()` too."""
    from ns_common.paths import Paths
    from ns_dashboard import studio_bridge

    _client, state = make_env(tmp_home)
    paths = Paths.from_env()
    seed_channel(state.db, paths, "after-dark")
    with studio_bridge.workshop_session(fake=studio_bridge.READ_ONLY) as ws:
        ws.start("after-dark")
    rows = studio_bridge.list_series()
    assert rows, "the started story is listed"
    ifaces, _ = TYPES
    declared, _index = all_members(ifaces, "SeriesStory")
    required = {n for n, m in declared.items() if not m.optional}
    assert required <= set(rows[0]), f"the workshop lacks {sorted(required - set(rows[0]))}"
    assert_clean("SeriesStory", rows[0], "Workshop.status()")


# ------------------------------------------------------------------ POST and PUT answers


def test_job_creation_answers_a_job(seeded: Any, tmp_home: Path):
    client, _state, ids = seeded
    run = client.post("/api/jobs/run", json={"channel": "after-dark", "format": "shorts_only"})
    assert run.status_code == 201
    assert_clean("Job", run.json(), "POST /jobs/run")
    video = tmp_home / "talk.mp4"
    video.write_bytes(b"x" * 64)
    clip = client.post("/api/jobs/clip", json={"channel": "after-dark", "path": str(video), "rights_confirmed": True})
    assert clip.status_code == 201
    assert_clean("Job", clip.json(), "POST /jobs/clip")
    rerun = client.post("/api/jobs/clip/rerun", json={"job_id": ids["clip_job"], "template": "auto"})
    assert rerun.status_code == 201, rerun.text
    assert_clean("Job", rerun.json(), "POST /jobs/clip/rerun")


def test_validate_answers_ok_errors_and_warnings(seeded: Any):
    client, _state, _ids = seeded
    saved = client.post("/api/channels/after-dark/validate", json={})
    assert saved.status_code == 200
    assert_clean("ChannelValidation", saved.json(), "POST /channels/{slug}/validate")
    bad = client.post("/api/channels/after-dark/validate", json={"slug": "after-dark", "display_name": ""})
    assert bad.status_code == 200 and bad.json()["ok"] is False and bad.json()["errors"]
    assert_clean("ChannelValidation", bad.json(), "POST /channels/{slug}/validate (draft)")


def test_the_chunked_upload_answers_one_shape_at_every_step(seeded: Any):
    client, _state, _ids = seeded
    started = client.post("/api/uploads", json={"filename": "stream.mp4", "size": 10})
    assert started.status_code == 201
    info = started.json()
    assert_clean("UploadState", info, "POST /uploads")
    assert info["chunk_size"] > 0 and info["status"] == "uploading"
    part = client.put(f"/api/uploads/{info['upload_id']}", params={"offset": 0}, content=b"0123456789")
    assert part.status_code == 200
    assert_clean("UploadState", part.json(), "PUT /uploads/{id}")
    done = client.post(f"/api/uploads/{info['upload_id']}/complete")
    assert done.status_code == 200 and done.json()["status"] == "complete" and done.json()["path"]
    assert_clean("UploadState", done.json(), "POST /uploads/{id}/complete")


def test_voice_design_answers_voice_made(seeded: Any):
    client, _state, _ids = seeded
    r = client.post("/api/voices/design", json={"name": "Narrator1", "description": "A warm, slow baritone", "test_line": "Once upon a midnight."})
    assert r.status_code == 200, r.text
    assert_clean("VoiceMade", r.json(), "POST /voices/design")


def test_report_exports_and_settings_answers(seeded: Any):
    client, _state, _ids = seeded
    for path in ("/api/diagnostics", "/api/settings/export"):
        r = client.post(path)
        assert r.status_code == 200, f"{path}: {r.text[:200]}"
        assert_clean("FileMade", r.json(), f"POST {path}")
    # an export that left the keys file out (a zip for a drive without permissions): `left_out` and `note`
    _state.adapters.backup.leave_out = ["Warning NS-W116: left out config/secrets.dpapi: no file permissions"]
    left = client.post("/api/settings/export")
    assert_clean("FileMade", left.json(), "POST /settings/export (keys left out)")
    assert left.json()["left_out"] and "NS-W116" in left.json()["note"]
    put = client.put("/api/settings", json={"schedule_time": "04:30", "notifications": True})
    assert put.status_code == 200
    assert_clean("Settings", put.json(), "PUT /settings")
    assert put.json()["schedule_result"]["applied"] is True


def test_series_produce_answers_the_queued_parts(seeded: Any):
    client, _state, _ids = seeded
    r = client.post("/api/library/series/the-lift/produce")
    assert r.status_code == 201, r.text
    assert_clean("SeriesQueued", r.json(), "POST /library/series/{slug}/produce")
    assert r.json()["queued"][0]["part"] == 1


def test_assistant_confirm_and_dismiss_answer_an_action(tmp_home: Path):
    llm = ScriptedToolLlm([call("pause_channel", {"slug": "after-dark"}), say("Please confirm."),
                           call("resume_channel", {"slug": "after-dark"}), say("Please confirm.")])
    client, state = make_env(tmp_home, llm=llm)
    seed_channel(state.db, state.paths)
    first = sse_events(client.post("/api/assistant/chat", json={"message": "pause after-dark"}))
    cid = first[0]["payload"]["conversation_id"]
    card = next(e["payload"] for e in first if e["kind"] == "confirm")
    done = client.post("/api/assistant/confirm", json={"action_id": card["action_id"]})
    assert done.status_code == 200 and done.json()["status"] == "done"
    assert_clean("ActionAnswer", done.json(), "POST /assistant/confirm")
    second = sse_events(client.post("/api/assistant/chat", json={"message": "resume it", "conversation_id": cid}))
    card2 = next(e["payload"] for e in second if e["kind"] == "confirm")
    no = client.post("/api/assistant/dismiss", json={"action_id": card2["action_id"]})
    assert no.status_code == 200 and no.json()["status"] == "dismissed"
    assert_clean("ActionAnswer", no.json(), "POST /assistant/dismiss")


@needs_multipart
def test_learn_and_settings_imports_answer_what_the_page_reads(seeded: Any):
    client, state, _ids = seeded
    for running in state.store.running_all():  # a restore is refused while a job runs
        state.store.finish(running.job_id, "done", "Done")
    learn = client.post("/api/learn/import", files={"file": ("export.csv", b"title,views\nA,10\nB,20\n", "text/csv")})
    assert learn.status_code == 200, learn.text
    assert_clean("LearnImported", learn.json(), "POST /learn/import")
    restore = client.post("/api/settings/import", files={"file": ("nightshift-backup.zip", b"PK\x05\x06" + b"\0" * 18, "application/zip")})
    assert restore.status_code == 200, restore.text
    assert_clean("SettingsImported", restore.json(), "POST /settings/import")


# ------------------------------------------------------------------ the chat stream


def declared_chat_kinds() -> dict[str, str]:
    """`kind` -> the member type text of that alternative of `ChatEvent` in types.ts."""
    _ifaces, aliases = TYPES
    out: dict[str, str] = {}
    for alt in split_top(aliases["ChatEvent"], "|"):
        if 'kind: "' not in alt:  # the alias starts with a `|`
            continue
        kind = alt.split('kind: "', 1)[1].split('"', 1)[0]
        out[kind] = alt
    return out


class OfflineProvider:
    def acquire(self) -> Any:
        raise EngineOffline()

    def release(self) -> None:
        return None


def test_the_chat_stream_uses_exactly_the_event_names_and_payloads_types_ts_declares(tmp_home: Path):
    declared = declared_chat_kinds()
    assert set(declared) == {"conversation", "message", "confirm", "tool_call", "tool_result", "busy", "error", "done"}
    llm = ScriptedToolLlm([call("list_queue", {"status": "pending"}), say("One video is waiting."),
                           call("pause_channel", {"slug": "after-dark"}), say("Please confirm.")])
    client, state = make_env(tmp_home, llm=llm)
    seed_channel(state.db, state.paths)
    seed_output(state.db, state.paths)
    dash = client.app.state.dashboard
    emitted: list[dict[str, Any]] = []
    first = sse_events(client.post("/api/assistant/chat", json={"message": "what is waiting?"}))
    emitted += first
    cid = first[0]["payload"]["conversation_id"]
    emitted += sse_events(client.post("/api/assistant/chat", json={"message": "pause it", "conversation_id": cid}))
    dash.assistant.provider = StaticProvider(ScriptedToolLlm([]), busy_owner="studio:after-dark-20261102-aaaa")
    emitted += sse_events(client.post("/api/assistant/chat", json={"message": "hello again", "conversation_id": cid}))
    dash.assistant.provider = OfflineProvider()
    emitted += sse_events(client.post("/api/assistant/chat", json={"message": "anyone there?", "conversation_id": cid}))
    kinds = {e["kind"] for e in emitted}
    assert kinds == set(declared), f"streamed {sorted(kinds)}, types.ts declares {sorted(declared)}"
    for event in emitted:
        assert set(event) == {"kind", "payload"}
        problems: list[str] = []
        check_value(declared[event["kind"]], event, f"chat {event['kind']}", problems)
        assert not problems, problems
    assert emitted[0]["kind"] == "conversation" and emitted[-1]["kind"] == "done"
