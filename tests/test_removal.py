# ruff: noqa: E501
"""WP6: the owner removes a voice, an idea, a story or a footage clip (moved aside with a record), Reject leaves
no empty folder and no stats behind, the voice list does not depend on the voice service, and the Assistant
names the real Outbox layout (G-36, G-38, G-39, G-40, G-42)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ns_common.paths import Paths
from ns_dashboard.errors import ApiError
from ns_dashboard.fakes import FakeTtsAdapter, seed_channel, seed_output
from ns_studio import ledger, ownership
from tests.test_dashboard_api import make_env
from tests.test_dashboard_handlers import code_of, make


def record_of(out: dict[str, Any]) -> dict[str, Any]:
    data = json.loads(Path(out["record"]).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def make_voice(paths: Paths, name: str, mode: str = "design") -> Path:
    folder = paths.models_tts_voices / name
    folder.mkdir(parents=True)
    (folder / "voice.json").write_text(json.dumps({"name": name, "mode": mode, "files": ["speaker.bin"]}), encoding="utf-8")
    (folder / "speaker.bin").write_bytes(b"embedding")
    return folder


# ------------------------------------------------------------------ G-38: remove a voice
def test_voice_in_use_is_refused_naming_the_channel_then_removed_with_a_record(tmp_home: Path):
    dash, state = make(tmp_home)
    paths = state.paths
    folder = make_voice(paths, "test-voice")
    seed_channel(state.db, paths, voice={"mode": "design", "name": "test-voice", "description": "warm"})
    with pytest.raises(ApiError) as ei:
        dash.voices.remove("test-voice")
    assert ei.value.status == 409 and ei.value.code == "remove_in_use" and "After Dark" in ei.value.message
    assert folder.is_dir()  # nothing moved
    from ns_common.config import load_channel, save_channel

    ch = load_channel(paths, "after-dark")
    save_channel(paths, ch.model_copy(update={"voice": ch.voice.model_copy(update={"mode": "preset", "name": None})}))
    out = dash.voices.remove("test-voice")
    assert not folder.exists()
    rec = record_of(out)
    assert rec["kind"] == "voices" and rec["state"] == "removed" and rec["voice"]["mode"] == "design"
    assert (Path(rec["moved"][0]["to"]) / "speaker.bin").read_bytes() == b"embedding"  # moved, not deleted
    assert not any(v["name"] == "test-voice" for v in dash.settings.voices())


def test_a_preset_a_bad_name_and_an_unknown_voice_are_refused(tmp_home: Path):
    dash, _ = make(tmp_home)
    assert code_of(dash.voices.remove, "Ryan") == "remove_invalid"
    assert code_of(dash.voices.remove, "../models") == "remove_invalid"
    assert code_of(dash.voices.remove, "no-such-voice") == "remove_not_found"


def test_the_delete_routes_answer_with_their_statuses(tmp_home: Path):
    client, state = make_env(tmp_home)
    make_voice(state.paths, "route-voice")
    assert client.delete("/api/voices/Ryan").status_code == 422
    assert client.delete("/api/voices/nobody").status_code == 404
    assert client.delete("/api/voices/route-voice").status_code == 200
    assert client.delete("/api/library/ideas/abc").status_code == 404
    assert client.delete("/api/library/ideas/99").status_code == 404
    assert client.delete("/api/library/stories/none-20261101-aaaa").status_code == 404


# ------------------------------------------------------------------ G-39: the voice list does not move
def test_the_voice_list_is_the_same_with_the_service_running_or_stopped(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    from ns_common.tts_client import TtsClient
    from ns_dashboard.adapters import RealTtsAdapter
    from ns_dashboard.supervisor_link import SupervisorLink
    from ns_tts.voices import PRESETS

    paths = Paths.from_env()
    make_voice(paths, "stored-one", "clone")

    def no_list(self: Any) -> list[dict[str, Any]]:
        raise AssertionError("the list must not come from the service")

    monkeypatch.setattr(TtsClient, "voices", no_list)
    adapter = RealTtsAdapter(paths, SupervisorLink("http://127.0.0.1:9", timeout_s=0.2))
    monkeypatch.setattr(TtsClient, "health", lambda self: False)
    stopped, live_a = adapter.voices()
    monkeypatch.setattr(TtsClient, "health", lambda self: True)
    running, live_b = adapter.voices()
    assert (live_a, live_b) == (False, True)
    assert stopped == running
    names = [v["name"] for v in stopped]
    assert names == [*PRESETS, "stored-one"] and len(PRESETS) == 9
    assert FakeTtsAdapter(paths, live=True).voices()[0] == FakeTtsAdapter(paths, live=False).voices()[0]


# ------------------------------------------------------------------ G-36: remove an idea, a story, footage
def test_an_idea_is_removed_with_its_row_in_the_record(tmp_home: Path):
    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths)
    idea = dash.library.add_idea({"channel": "after-dark", "premise": "A doorbell camera that records tomorrow."})
    # the embed service is not running here, so add_idea stored no vector: give the row the blob the record
    # must leave out (float32 little-endian, contracts/schema.sql)
    state.db.execute("UPDATE ideas SET embedding = ? WHERE idea_id = ?", (b"\x00\x00\x80\x3f" * 4, idea["idea_id"]))
    out = dash.library.remove_idea(idea["idea_id"])
    assert state.db.one("SELECT 1 FROM ideas WHERE idea_id = ?", (idea["idea_id"],)) is None
    rec = record_of(out)
    assert rec["rows"]["ideas"][0]["premise"].startswith("A doorbell") and "embedding" not in rec["rows"]["ideas"][0]
    assert code_of(dash.library.remove_idea, idea["idea_id"]) == "remove_not_found"


def seed_story(dash: Any, state: Any, paths: Paths, job_id: str) -> tuple[list[str], Path]:
    """A story with two Shorts in the Outbox, a job folder, an owner record, a combo, clip use and ledger lines."""
    state.db.execute(
        "INSERT INTO stories(job_id, channel, format, working_title, text, word_count, opening_line, created_at)"
        " VALUES (?, 'after-dark', 'shorts_only', 'The Door', 'text', 1, 'It rang.', ?)", (job_id, datetime.now(UTC).isoformat()),
    )
    state.db.execute("INSERT INTO parts(job_id, n, text, word_count, est_seconds) VALUES (?, 1, 'p', 1, 1.0)", (job_id,))
    state.db.execute(
        "INSERT INTO combos(combo_id, channel, job_id, item, created_at) VALUES (?, 'after-dark', ?, 's1', 'now')",
        (f"combo-{job_id}", job_id),
    )
    state.db.execute(
        "INSERT INTO clips(source, clip_id, url, license, fetched_at) VALUES ('fake', ?, 'u', 'l', 'now')", (f"c-{job_id}",)
    )
    state.db.execute(
        "INSERT INTO clip_usage(source, clip_id, channel, job_id, item, used_at) VALUES ('fake', ?, 'after-dark', ?, 's1', 'now')",
        (f"c-{job_id}", job_id),
    )
    ids = [seed_output(state.db, paths, kind="short", k=k, title="The Door", job_id=job_id) for k in (1, 2)]
    state.db.execute("INSERT INTO uploads(output_id, video_url, published_at) VALUES (?, 'https://youtu.be/x', 'now')", (ids[0],))
    job_dir = paths.job_dir(job_id)
    (job_dir / "qc").mkdir(parents=True)
    (job_dir / "story.json").write_text("{}", encoding="utf-8")
    ownership.write_record(paths, job_id, kind="job", state="done")
    now = datetime.now(UTC)
    ledger.write(paths, {"kind": "stage", "job_id": job_id, "stage": "story", "at": now.isoformat()}, when=now)
    ledger.write(paths, {"kind": "stage", "job_id": "other-20261101-bbbb", "stage": "story", "at": now.isoformat()}, when=now)
    return ids, Path(state.db.one("SELECT path FROM outputs WHERE output_id = ?", (ids[0],))["path"])


def test_a_story_is_removed_whole_and_every_row_and_file_is_in_the_record(tmp_home: Path):
    dash, state = make(tmp_home)
    paths = state.paths
    seed_channel(state.db, paths)
    job_id = "after-dark-20261101-aaaa"
    ids, video = seed_story(dash, state, paths, job_id)
    story_folder = video.parent.parent
    assert story_folder.is_dir() and (story_folder / ".nightshift-job").is_file()

    out = dash.library.remove_story(job_id)

    assert out["videos"] == 2
    for table, col in (("stories", "job_id"), ("parts", "job_id"), ("outputs", "job_id"), ("combos", "job_id"), ("clip_usage", "job_id")):
        assert state.db.one(f"SELECT COUNT(*) AS n FROM {table} WHERE {col} = ?", (job_id,))["n"] == 0, table  # noqa: S608
    assert state.db.one("SELECT COUNT(*) AS n FROM uploads")["n"] == 0
    assert not story_folder.exists() and not paths.job_dir(job_id).exists()
    assert not ownership.record_path(paths, job_id).exists()
    rec = record_of(out)
    assert rec["state"] == "removed" and {o["output_id"] for o in rec["rows"]["outputs"]} == set(ids)
    assert rec["rows"]["uploads"][0]["video_url"] == "https://youtu.be/x"
    moved_to = [Path(m["to"]) for m in rec["moved"]]
    assert all(p.exists() for p in moved_to)  # moved, not deleted
    assert any((p / "shorts").is_dir() for p in moved_to)
    left = [x for x in ledger.read(paths, months=None) if x.get("job_id") == job_id]
    assert left == [] and any(x.get("job_id") == "other-20261101-bbbb" for x in ledger.read(paths, months=None))
    assert len(rec["ledger_lines"]) == 1
    assert code_of(dash.library.remove_story, job_id) == "remove_not_found"


def test_a_story_that_is_being_made_is_refused_and_nothing_moves(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths)
    job_id = "after-dark-20261101-cccc"
    _ids, video = seed_story(dash, state, state.paths, job_id)
    monkeypatch.setattr(ownership, "liveness", lambda paths, owner_id, rec=None: "live")
    with pytest.raises(ApiError) as ei:
        dash.library.remove_story(job_id)
    assert ei.value.status == 409 and ei.value.code == "remove_in_use"
    assert video.is_file() and state.db.one("SELECT 1 FROM stories WHERE job_id = ?", (job_id,)) is not None
    assert not (state.paths.home / "removed").exists()


def test_a_failing_database_step_puts_the_files_back(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths)
    job_id = "after-dark-20261101-dddd"
    _ids, video = seed_story(dash, state, state.paths, job_id)
    # the transaction fails at its third statement, after the files were moved
    state.db.execute("CREATE TRIGGER boom BEFORE DELETE ON parts BEGIN SELECT RAISE(ABORT, 'boom'); END")
    with pytest.raises(ApiError) as ei:
        dash.library.remove_story(job_id)
    assert ei.value.code == "remove_failed"
    assert video.is_file() and state.paths.job_dir(job_id).is_dir() and ownership.record_path(state.paths, job_id).is_file()
    assert state.db.one("SELECT COUNT(*) AS n FROM outputs WHERE job_id = ?", (job_id,))["n"] == 2
    assert list((state.paths.home / "removed" / "stories").glob("*")) == []  # the record folder is gone too


def test_footage_goes_aside_with_its_file_and_rows(tmp_home: Path):
    dash, state = make(tmp_home)
    clip = state.paths.home / "cache" / "clips" / "pexels" / "1.mp4"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"x")
    state.db.execute("INSERT INTO clips(source, clip_id, url, license, local_path, fetched_at) VALUES ('pexels', '1', 'u', 'l', ?, 'now')", (str(clip),))
    state.db.execute("INSERT INTO clip_usage(source, clip_id, channel, job_id, item, used_at) VALUES ('pexels', '1', 'c', 'j', 'long', 'now')")
    out = dash.library.delete_footage("pexels", "1")
    assert out["file_removed"] is True and not clip.exists()
    rec = record_of(out)
    assert rec["rows"]["clip_usage"][0]["job_id"] == "j" and Path(rec["moved"][0]["to"]).read_bytes() == b"x"
    assert state.db.one("SELECT COUNT(*) AS n FROM clips")["n"] == 0


def test_the_commands_remove_a_voice_an_idea_and_report_a_refusal(tmp_home: Path, capsys: pytest.CaptureFixture[str]):
    import typer

    from ns_studio import cli

    paths = Paths.from_env()
    paths.ensure()
    folder = make_voice(paths, "cli-voice")
    cli.voice_remove("cli-voice")
    assert not folder.exists() and "removed" in capsys.readouterr().out
    with pytest.raises(typer.Exit):
        cli.voice_remove("Ryan")
    assert "built-in" in capsys.readouterr().err


# ------------------------------------------------------------------ G-42: Reject leaves nothing behind
def test_reject_removes_the_emptied_story_folder_and_its_marker(tmp_home: Path):
    dash, state = make(tmp_home)
    paths = state.paths
    seed_channel(state.db, paths)
    job_id = "after-dark-20261101-eeee"
    a = seed_output(state.db, paths, kind="short", k=1, job_id=job_id)
    b = seed_output(state.db, paths, kind="short", k=2, job_id=job_id)
    video = Path(state.db.one("SELECT path FROM outputs WHERE output_id = ?", (a,))["path"])
    story = video.parent.parent
    dash.outputs.reject(a, "bad ending")
    assert story.is_dir() and (story / ".nightshift-job").is_file()  # the other Short is still there
    dash.outputs.reject(b, "bad ending too")
    assert not story.exists()  # no empty folder, no marker
    rejected = [Path(r["path"]) for r in state.db.query("SELECT path FROM outputs WHERE status = 'rejected'")]
    assert len(rejected) == 2 and all(p.is_file() for p in rejected)
    dash.outputs.approve(a)  # back to the Outbox: the folder and its marker come back
    back = Path(state.db.one("SELECT path FROM outputs WHERE output_id = ?", (a,))["path"])
    assert back == video and (story / ".nightshift-job").read_text(encoding="utf-8") == job_id


def test_a_rejected_video_does_not_count_in_the_channel_stats(tmp_home: Path):
    dash, state = make(tmp_home)
    paths = state.paths
    seed_channel(state.db, paths)
    job_id = "after-dark-20261101-ffff"
    a = seed_output(state.db, paths, kind="short", k=1, job_id=job_id)
    b = seed_output(state.db, paths, kind="short", k=2, job_id=job_id)
    old = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    state.db.execute("UPDATE outputs SET created_at = ? WHERE output_id = ?", (old, a))
    job = paths.job_dir(job_id)
    (job / "qc").mkdir(parents=True)
    (job / "job.json").write_text(json.dumps({"job_id": job_id, "channel": "after-dark", "created_at": datetime.now(UTC).isoformat()}), encoding="utf-8")
    for item, passed in (("s1", True), ("s2", False)):
        (job / "qc" / f"{item}.json").write_text(json.dumps({"item": item, "passed": passed}), encoding="utf-8")

    def card() -> dict[str, Any]:
        return next(c for c in dash.channels.cards() if c["slug"] == "after-dark")

    before = card()
    assert before["stats"]["qc_pass_rate"] == 0.5 and before["stats"]["week"] == 1
    dash.outputs.reject(b, "failed QC")  # the failing one
    after = card()
    assert after["stats"]["qc_pass_rate"] == 1.0  # only s1 is counted now
    assert after["last_output_at"] == old  # the rejected Short was the newer one
    dash.outputs.reject(a, "also bad")
    assert card()["stats"]["qc_pass_rate"] is None and card()["last_output_at"] is None and card()["stats"]["week"] == 0


# ------------------------------------------------------------------ G-40: the Assistant names the real layout
def test_the_assistant_folder_text_is_the_layout_the_outbox_writes(tmp_home: Path):
    from ns_dashboard.assistant import orientation
    from ns_studio.media.outbox import OUTBOX_LAYOUT

    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths)
    oid = seed_output(state.db, state.paths, kind="short", k=1)
    rel = Path(state.db.one("SELECT path FROM outputs WHERE output_id = ?", (oid,))["path"]).relative_to(state.paths.outbox)
    assert rel.parts[0] == "after-dark" and rel.parts[1] == "stories" and rel.parts[3] == "shorts"
    assert orientation.FOLDERS["outbox"] == OUTBOX_LAYOUT
    assert "<channel>/stories/<story title>/full/" in OUTBOX_LAYOUT and "shorts/" in OUTBOX_LAYOUT
    shown = orientation.static_map()
    assert "<date>" not in shown and "<job_id>" not in orientation.FOLDERS["outbox"] and "removed:" in shown
