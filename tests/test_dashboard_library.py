# ruff: noqa: E501
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from ns_common.paths import Paths
from ns_dashboard import formats, updater
from ns_dashboard.assistant.cli import run_ask
from ns_dashboard.assistant.llm import ScriptedToolLlm, StaticProvider, call, say
from ns_dashboard.errors import ApiError
from ns_dashboard.fakes import seed_channel, seed_output
from ns_dashboard.handlers import Dashboard, build_state
from ns_dashboard.supervisor_link import SupervisorLink


def make(tmp_home: Path, llm: ScriptedToolLlm | None = None):
    state = build_state(
        Paths.from_env(), fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
    )
    return Dashboard(state, llm_provider=StaticProvider(llm or ScriptedToolLlm([]))), state


def code_of(fn: Any, *args: Any, **kw: Any) -> str:
    with pytest.raises(ApiError) as ei:
        fn(*args, **kw)
    return ei.value.code


GOOD_FORMAT: dict[str, Any] = {
    "label": "Square 1x1",
    "kind": "square",
    "canvas": {"w": 1080, "h": 1080, "fps": 30},
    "duration": {"min_s": 15, "max_s": 60},
    "captions": {"styles_allowed": ["clean_white"], "anchor_y_pct": 70, "words_on_screen": 3},
    "layout": {"regions": [{"name": "main", "x": 0, "y": 0, "w": 1, "h": 1, "source": "full"}]},
    "safe_margins": {"top": 60, "bottom": 60},
    "shots": {"min_s": 1.5, "max_s": 3},
    "encoder": {"preset": "p4"},
    "sheet": {"hashtags_required": ["#Shorts"], "description_rules": "one line"},
}


# ------------------------------------------------------------------ ideas


def test_idea_bank_lifecycle(tmp_home: Path):
    dash, state = make(tmp_home)
    cols = {r["name"] for r in state.db.query("PRAGMA table_info(ideas)")}
    assert {"status", "setting", "protagonist", "keywords", "source", "updated_at"} <= cols
    seed_channel(state.db, state.paths, "after-dark")
    idea = dash.library.add_idea(
        {"channel": "after-dark", "premise": "A doorbell camera films tomorrow night.", "twist": "It is me.",
         "keywords": ["doorbell", "porch"], "setting": "suburb"}
    )
    assert idea["status"] == "new" and idea["source"] == "owner" and idea["keywords"] == ["doorbell", "porch"]
    approved = dash.library.add_idea({"channel": "after-dark", "premise": "A lighthouse keeper hears knocking.", "status": "approved"})
    assert approved["status"] == "approved" and approved["twist"] == ""

    assert [i["idea_id"] for i in dash.library.list_ideas("after-dark")] == [approved["idea_id"], idea["idea_id"]]
    assert [i["idea_id"] for i in dash.library.list_ideas(status="approved")] == [approved["idea_id"]]
    assert [i["idea_id"] for i in dash.library.list_ideas(q="doorbell")] == [idea["idea_id"]]
    assert dash.library.list_ideas(q="100%") == []

    used = dash.library.set_idea_status(idea["idea_id"], "used")
    assert used["status"] == "used"
    assert state.db.one("SELECT used FROM ideas WHERE idea_id = ?", (idea["idea_id"],))["used"] == 1
    assert [i["idea_id"] for i in dash.library.list_ideas(status="used")] == [idea["idea_id"]]
    assert dash.library.set_idea_status(idea["idea_id"], "dropped")["status"] == "dropped"
    assert state.db.one("SELECT used FROM ideas WHERE idea_id = ?", (idea["idea_id"],))["used"] == 0

    assert code_of(dash.library.set_idea_status, idea["idea_id"], "maybe") == "invalid_idea"
    assert code_of(dash.library.set_idea_status, 9999, "new") == "not_found"
    assert code_of(dash.library.add_idea, {"channel": "after-dark", "premise": "short"}) == "invalid_idea"
    assert code_of(dash.library.add_idea, {"channel": "ghost", "premise": "A long enough premise here."}) == "channel_not_found"
    assert code_of(dash.library.add_idea, {"channel": "after-dark", "premise": "A long enough premise here.", "status": "used"}) == "invalid_idea"
    assert code_of(dash.library.list_ideas, None, "weird") == "bad_filter"


def test_ideas_made_by_nightshift_show_their_used_flag(tmp_home: Path):
    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths, "after-dark")
    state.db.execute(
        "INSERT INTO ideas(channel, premise, twist, created_at, used) VALUES ('after-dark', 'Old idea premise text', 't', 'now', 1)"
    )
    only = dash.library.list_ideas()
    assert only[0]["status"] == "used" and only[0]["source"] == "nightshift"


def test_ensure_idea_columns_is_idempotent(tmp_home: Path):
    dash, state = make(tmp_home)
    from ns_dashboard.svc_library import ensure_idea_columns

    ensure_idea_columns(state.db)
    ensure_idea_columns(state.db)
    assert len([r for r in state.db.query("PRAGMA table_info(ideas)") if r["name"] == "status"]) == 1


# ------------------------------------------------------------------ stories and footage


def test_stories_with_outputs_and_search(tmp_home: Path):
    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths, "after-dark")
    seed_channel(state.db, state.paths, "night-owl")
    for job, channel, title, text in (
        ("after-dark-20261101-aaaa", "after-dark", "The Doorbell Camera", "At 3:12 the doorbell rings."),
        ("night-owl-20261101-bbbb", "night-owl", "Lighthouse", "The keeper heard knocking."),
    ):
        state.db.execute(
            "INSERT INTO stories(job_id, channel, format, working_title, text, word_count, opening_line, critic_avg, created_at)"
            " VALUES (?, ?, 'shorts_only', ?, ?, 5, ?, 8.5, ?)",
            (job, channel, title, text, text.split(".")[0], job[-4:]),
        )
    oid = seed_output(state.db, state.paths, channel="after-dark", job_id="after-dark-20261101-aaaa", status="uploaded")
    state.db.execute("INSERT INTO uploads(output_id, video_url, published_at) VALUES (?, 'https://youtu.be/x', 't')", (oid,))
    allr = dash.library.stories()
    assert [s["job_id"] for s in allr] == ["night-owl-20261101-bbbb", "after-dark-20261101-aaaa"]
    doorbell = dash.library.stories("doorbell")
    assert len(doorbell) == 1 and doorbell[0]["critic_avg"] == 8.5
    assert doorbell[0]["outputs"] == [{"output_id": oid, "kind": "short", "k": 1, "status": "uploaded", "video_url": "https://youtu.be/x"}]
    assert dash.library.stories(channel="night-owl")[0]["outputs"] == []
    assert dash.library.stories("zzzz") == []


def test_footage_search(tmp_home: Path):
    dash, state = make(tmp_home)
    for cid, kw in (("101", ["porch", "night"]), ("202", ["forest"])):
        state.db.execute(
            "INSERT INTO clips(source, clip_id, url, license, width, height, duration, local_path, fetched_at, keywords)"
            " VALUES ('pexels', ?, 'https://x/y', 'Pexels', 1080, 1920, 6.5, 'c.mp4', '2026-11-01', ?)",
            (cid, json.dumps(kw)),
        )
    state.db.execute("INSERT INTO clip_usage(source, clip_id, channel, job_id, item, used_at) VALUES ('pexels','101','c','j','s1','2026-11-02')")
    everything = dash.library.footage()
    assert {f["clip_id"] for f in everything} == {"101", "202"}
    porch = dash.library.footage("porch")
    assert [f["clip_id"] for f in porch] == ["101"] and porch[0]["last_used"] == "2026-11-02"
    assert porch[0]["keywords"] == ["porch", "night"] and porch[0]["license"] == "Pexels"
    assert dash.library.footage("nothing") == []


# ------------------------------------------------------------------ formats


def test_format_schema_accepts_the_spec_example_and_rejects_mistakes():
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(formats.FORMAT_SCHEMA)
    jsonschema.Draft202012Validator(formats.FORMAT_SCHEMA).validate(GOOD_FORMAT)  # the same shape passes real jsonschema too
    assert formats.validate_format(GOOD_FORMAT) == []
    bad = json.loads(json.dumps(GOOD_FORMAT))
    bad["kind"] = "poster"
    bad["canvas"]["fps"] = 0
    bad["layout"]["regions"][0]["source"] = "webcam"
    bad["duration"] = {"min_s": 90, "max_s": 30}
    problems = formats.validate_format(bad)
    assert len(problems) >= 1 and formats.validate_format({"kind": "short"}) != []
    for mutate, needle in (
        (lambda d: d.update(kind="poster"), "one of"),
        (lambda d: d["canvas"].update(fps=0), "at least"),
        (lambda d: d["layout"]["regions"][0].update(source="webcam"), "wrong format"),
        (lambda d: d["layout"].update(regions=[]), "at least one region"),
        (lambda d: d["duration"].update(min_s=90, max_s=30), "must not be smaller"),
        (lambda d: d["layout"]["regions"][0].pop("x"), "required"),
    ):
        d = json.loads(json.dumps(GOOD_FORMAT))
        mutate(d)
        assert any(needle in p for p in formats.validate_format(d)), needle
    d = json.loads(json.dumps(GOOD_FORMAT))
    d["layout"]["regions"][0]["source"] = "face:2"
    assert formats.validate_format(d) == []
    assert formats.validate_format("nope") == ["a format must be a mapping"]


def test_formats_create_duplicate_edit(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    shipped = tmp_home / "shipped"
    shipped.mkdir()
    (shipped / "short_9x16.yaml").write_text(
        yaml.safe_dump({**GOOD_FORMAT, "label": "Short 9x16", "kind": "short", "canvas": {"w": 1080, "h": 1920, "fps": 30}}),
        encoding="utf-8",
    )
    (shipped / "broken.yaml").write_text(yaml.safe_dump({"kind": "short"}), encoding="utf-8")
    monkeypatch.setattr(formats, "builtin_dir", lambda: shipped)
    dash, state = make(tmp_home)
    listed = {f["id"]: f for f in dash.library.list_formats()}
    assert set(listed) == {"short_9x16", "broken"}
    assert listed["short_9x16"]["source"] == "shipped" and listed["short_9x16"]["valid"] is True
    assert listed["broken"]["valid"] is False and listed["broken"]["errors"]

    dup = dash.library.create_format({"duplicate_of": "short_9x16", "id": "short_fast", "changes": {"duration": {"max_s": 40}}})
    assert dup["id"] == "short_fast" and dup["source"] == "yours" and dup["definition"]["duration"] == {"min_s": 15, "max_s": 40}
    assert dup["definition"]["label"] == "Short 9x16 (copy)"
    user_file = state.paths.config / "formats" / "short_fast.yaml"
    assert yaml.safe_load(user_file.read_text(encoding="utf-8"))["duration"]["max_s"] == 40
    assert dash.library.create_format({"duplicate_of": "short_9x16"})["id"] == "short_9x16_copy"
    assert dash.library.create_format({"duplicate_of": "short_9x16"})["id"] == "short_9x16_copy2"
    assert code_of(dash.library.create_format, {"duplicate_of": "short_9x16", "id": "short_fast"}) == "format_exists"
    assert code_of(dash.library.create_format, {"duplicate_of": "ghost"}) == "not_found"
    assert code_of(dash.library.create_format, {"id": "Bad Id", "definition": GOOD_FORMAT}) == "invalid_format"
    assert code_of(dash.library.create_format, {"id": "ok_id"}) == "invalid_format"
    made = dash.library.create_format({"id": "square_1x1", "definition": GOOD_FORMAT})
    assert made["kind"] == "square" and made["valid"]

    edited = dash.library.update_format("short_9x16", {"changes": {"captions": {"anchor_y_pct": 60}}})
    assert edited["source"] == "edited" and edited["definition"]["captions"]["anchor_y_pct"] == 60
    assert edited["definition"]["captions"]["words_on_screen"] == 3  # untouched fields survive the patch
    assert (state.paths.config / "formats" / "short_9x16.yaml").exists()
    assert "anchor_y_pct: 70" in (shipped / "short_9x16.yaml").read_text(encoding="utf-8")  # shipped file untouched
    replaced = dash.library.update_format("square_1x1", {"definition": {**GOOD_FORMAT, "label": "Squarer"}})
    assert replaced["label"] == "Squarer"
    assert code_of(dash.library.update_format, "square_1x1", {"changes": {"kind": "poster"}}) == "invalid_format"
    assert code_of(dash.library.update_format, "ghost", {"changes": {}}) == "not_found"
    assert code_of(dash.library.update_format, "square_1x1", {}) == "invalid_format"


# ------------------------------------------------------------------ update


def test_update_check_and_apply_use_the_adapter(tmp_home: Path):
    dash, state = make(tmp_home)
    chk = dash.library.update_check()
    assert chk["update_available"] is True and chk["latest"] == "0.2.0"
    out = dash.library.update_apply()
    assert out["ok"] is True and state.adapters.update.applied == 1  # type: ignore[attr-defined]
    state.draining = True
    assert code_of(dash.library.update_apply) == "shutting_down"


def test_update_output_parsing_and_runner(tmp_home: Path):
    p = updater.parse_update_output(0, 'checking\n{"update_available": true, "current": "0.1.0", "latest": "0.2.0", "extra": 1}\n', "")
    assert p["update_available"] is True and p["latest"] == "0.2.0" and "extra" not in p and p["ok"] is True
    q = updater.parse_update_output(0, "Nightshift 0.1.0 is up to date.\n", "")
    assert q["update_available"] is False and q["latest"] == "0.1.0"
    r = updater.parse_update_output(0, "New version available: 0.1.0 -> 0.3.1\n", "")
    assert r["update_available"] is True and (r["current"], r["latest"]) == ("0.1.0", "0.3.1")
    bad = updater.parse_update_output(1, "", "boom")
    assert bad["ok"] is False and bad["error"] == "boom" and bad["update_available"] is None

    seen: list[tuple[list[str], int]] = []

    def runner(cmd: list[str], timeout: int) -> tuple[int, str, str]:
        seen.append((cmd, timeout))
        return 0, "Nightshift 0.1.0 is up to date.", ""

    paths = Paths.from_env()
    a = updater.RealUpdateAdapter(paths, runner)
    assert a.check()["update_available"] is False and a.apply()["ok"] is True
    assert seen[0][0][-2:] == ["update", "--check"] and seen[1][0][-2:] == ["update", "--github"]
    assert seen[0][1] < seen[1][1]
    ns_exe = paths.venvs / "supervisor" / "Scripts" / "ns.exe"
    ns_exe.parent.mkdir(parents=True)
    ns_exe.write_text("", encoding="utf-8")
    a.check()
    assert seen[2][0][0] == str(ns_exe)
    assert updater.default_runner(["definitely-not-a-real-program-xyz"], 5)[0] == 127


# ------------------------------------------------------------------ command line


def test_ask_cli_runs_the_same_loop(tmp_home: Path):
    llm = ScriptedToolLlm([
        call("list_queue", {"status": "pending"}), say("One video is waiting."),
        call("pause_channel", {"slug": "after-dark"}), say("Asked."),
        call("pause_channel", {"slug": "after-dark"}), say("Asked again."),
    ])
    dash, state = make(tmp_home, llm)
    seed_channel(state.db, state.paths, "after-dark")
    seed_output(state.db, state.paths)
    lines: list[str] = []
    assert run_ask(dash, "what is waiting?", echo=lines.append) == 0
    assert lines[0].startswith("  [list_queue]") and lines[-1] == "One video is waiting."

    lines.clear()
    assert run_ask(dash, "pause after-dark", echo=lines.append, confirm_fn=lambda q: False) == 0
    assert any(line.startswith("  Needs your OK: Pause channel after-dark") for line in lines) and "  Okay, not doing it." in lines
    assert dash.channels.load("after-dark").status == "active"

    lines.clear()
    assert run_ask(dash, "pause after-dark", echo=lines.append, yes=True) == 0
    assert dash.channels.load("after-dark").status == "paused"
    assert any(line.strip().startswith("Done: Pause channel after-dark") for line in lines)


def test_ask_cli_reports_busy_engine_and_errors(tmp_home: Path):
    state = build_state(Paths.from_env(), fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2))
    busy = Dashboard(state, llm_provider=StaticProvider(ScriptedToolLlm([]), busy_owner="studio:x"))
    lines: list[str] = []
    assert run_ask(busy, "hello", echo=lines.append) == 0
    assert "busy" in lines[0]
    assert run_ask(busy, "  ", echo=lines.append) == 1  # empty question is an error
