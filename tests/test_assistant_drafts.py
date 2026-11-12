# ruff: noqa: E501
"""WP7b (G-43 b, G-47): a story the Assistant writes is a draft file, filled one part per call, started by reference.

The owner planned a long video with the Assistant; the story was re-typed into one tool call, cut by the reply cap
and lost (it existed only inside `data/assistant.db`). Now the story is `<home>/drafts/<id>.md`, the Assistant adds
one part per call, the owner may edit the file by hand, the job is started from the file (read at the Yes), and a
cut reply leaves the draft intact and named.

WRITTEN, NOT RUN (the owner's build method of 2026-11-11: tests run once after the block is merged).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from ns_common.textstats import word_count
from ns_dashboard.assistant import drafts
from ns_dashboard.assistant.llm import ReplyCutInToolCall, ScriptedToolLlm, StaticProvider, call, say
from ns_dashboard.assistant.registry import render_confirm
from ns_dashboard.assistant.tools import build_registry
from ns_dashboard.errors import ApiError
from tests.assistant_eval import fixture_home
from tests.test_dashboard_api import make_env

#: One Short's story in three parts (the text `test_assistant_validate_before_card.SCRIPT` that passes the check).
PARTS = (
    "The porch light flickered twice before it died. I stood very still and counted the seconds, the way my "
    "grandmother taught me, because she always said the dark is only honest when it is quiet.",
    "Somewhere behind me the floorboards gave one long, patient creak.",
    "Then a voice I knew very well whispered my name from the empty kitchen.",
)
EDITED = "Somewhere behind me the old floorboards gave one long and very patient creak."


@pytest.fixture
def home(tmp_home: Path) -> fixture_home.FixtureHome:
    return fixture_home.build(tmp_home)


def turn(home: fixture_home.FixtureHome, *steps: Any, cid: str | None = None, text: str = "write the full story as a draft") -> list[dict[str, Any]]:
    """One chat turn on the scripted model: the given steps, then a closing sentence."""
    home.dash.assistant.provider = StaticProvider(ScriptedToolLlm([*steps, say("Saved.")]))
    return list(home.dash.assistant.chat(text, cid, {}))


def newest(home: fixture_home.FixtureHome) -> dict[str, Any]:
    listed = drafts.listing(home.state.paths, 1)
    assert listed, "no draft was written"
    return listed[0]


def create(fmt: str = "shorts_only", text: str = PARTS[0]) -> dict[str, Any]:
    return call("draft_create", {"title": "The Porch Light", "channel": "after-dark", "format": fmt, "text": text,
                                 "part_title": "The Light"})


def append(home: fixture_home.FixtureHome, text: str, part_title: str = "") -> Any:
    """A step that adds a part to the newest draft (the id is read when the step runs)."""
    args = {"text": text, **({"part_title": part_title} if part_title else {})}
    return lambda _messages: call("draft_append_part", {"draft": newest(home)["draft_id"], **args})


def results(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e["payload"] for e in events if e["kind"] == "tool_result"]


def conversation(events: list[dict[str, Any]]) -> str:
    return next(e["payload"]["conversation_id"] for e in events if e["kind"] == "conversation")


class CutLlm:
    """A model whose reply is cut inside a tool call: llama-server's 500 as the client turns it."""

    def chat_tools(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, max_tokens: int | None = None):
        raise ReplyCutInToolCall("HTTP 500: Failed to parse tool call arguments as JSON: missing closing quote")


# ------------------------------------------------------------------ a draft built over several parts


def test_a_story_is_built_as_a_draft_file_one_part_per_call(home: fixture_home.FixtureHome):
    events = turn(home, create(), append(home, PARTS[1], "The Creak"),
                  append(home, "Part 3: The Voice (Mara POV)\n\n" + PARTS[2]))
    out = results(events)
    assert [r["tool"] for r in out] == ["draft_create", "draft_append_part", "draft_append_part"]
    assert all(r["ok"] for r in out), out
    assert not [e for e in events if e["kind"] == "confirm"]  # writing a draft asks nothing; only the start does
    card = out[-1]["result"]["draft"]
    path = Path(card["path"])
    assert path.parent == home.state.paths.home / "drafts" and path.suffix == ".md" and path.is_file()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n") and "channel: after-dark" in text and "format: shorts_only" in text
    assert "status: writing" in text and "title: The Porch Light" in text
    heads = [line for line in text.splitlines() if line.startswith("## ")]
    assert heads == ["## Part 1: The Light", "## Part 2: The Creak", "## Part 3: The Voice (Mara POV)"]
    assert all(p in text for p in PARTS)
    assert len(card["parts"]) == 3 and card["words"] == sum(word_count(p) for p in PARTS)
    assert str(path) in out[-1]["result"]["note"]


def test_the_same_part_sent_twice_is_kept_once(home: fixture_home.FixtureHome):
    """A repeated call (the model lost the answer and sent the part again) does not double the story."""
    events = turn(home, create(), append(home, PARTS[1]), append(home, PARTS[1]))
    last = results(events)[-1]["result"]
    assert last["added"] is False and len(last["draft"]["parts"]) == 2
    assert Path(last["draft"]["path"]).read_text(encoding="utf-8").count(PARTS[1]) == 1


def test_a_part_over_the_limit_is_refused_and_the_draft_is_unchanged(home: fixture_home.FixtureHome):
    turn(home, create())
    path = Path(newest(home)["path"])
    before = path.read_bytes()
    huge = ("A long sentence about the house. " * 200).strip()
    with pytest.raises(ApiError, match="Split it into two parts"):
        drafts.append_part(home.state.paths, path.stem, huge)
    assert path.read_bytes() == before
    assert build_registry().validate("draft_append_part", {"draft": path.stem, "text": huge}) is not None  # the schema says it to the model too


# ------------------------------------------------------------------ a cut reply leaves the draft intact and named


def test_a_cut_reply_leaves_the_draft_intact_and_named(home: fixture_home.FixtureHome):
    events = turn(home, create(), append(home, PARTS[1]))
    cid = conversation(events)
    path = Path(newest(home)["path"])
    before = path.read_bytes()
    home.dash.assistant.provider = StaticProvider(CutLlm())
    events = list(home.dash.assistant.chat("go on, write the rest of the story", cid, {}))
    assert not [e for e in events if e["kind"] == "error"], events
    said = [e["payload"]["text"] for e in events if e["kind"] == "message"][-1]
    assert str(path) in said and "2 parts" in said and "too long for one reply" in said
    assert "Nothing was started" in said and "NS-E301" not in said
    assert path.read_bytes() == before  # the cut call never ran: not one byte changed
    logged = "".join(p.read_text(encoding="utf-8") for p in home.state.paths.logs.glob("dashboard*"))
    assert "assistant_reply_cut" in logged
    # another chat without a draft hears the plain note: no other chat's draft is named
    other = list(home.dash.assistant.chat("write the full story now", None, {}))
    plain = [e["payload"]["text"] for e in other if e["kind"] == "message"][-1]
    assert str(path) not in plain and "Create page" in plain


# ------------------------------------------------------------------ a job started from the draft


def test_a_job_is_started_from_the_draft_file_as_it_reads_at_the_yes(home: fixture_home.FixtureHome):
    turn(home, create(), append(home, PARTS[1]), append(home, "Part 3: The Voice (Mara POV)\n\n" + PARTS[2]))
    d = newest(home)
    path = Path(d["path"])
    jobs_before = len(home.state.store.list_jobs(limit=1000))
    events = turn(home, call("start_job_from_draft", {"draft": d["draft_id"], "channel": "after-dark", "format": "shorts_only"}),
                  text="start the video from the draft")
    (card,) = [e["payload"] for e in events if e["kind"] == "confirm"]
    assert card["text"] == (f"Make a Short for after-dark from the draft {d['draft_id']}, as the file reads when you "
                            "press Yes. This uses the GPU for a while.")
    assert "porch" not in card["text"].lower()  # the card never repeats the story
    assert len(home.state.store.list_jobs(limit=1000)) == jobs_before  # nothing started before the Yes
    # the owner edits the file between the card and his Yes (Notepad-style: CRLF line ends)
    edited = path.read_text(encoding="utf-8").replace(PARTS[1], EDITED)
    path.write_bytes(edited.replace("\n", "\r\n").encode("utf-8"))
    out = home.dash.assistant.confirm(card["action_id"])
    assert out["status"] == "done", out
    job = home.state.store.get(out["result"]["job_id"])
    assert job is not None and job.kind == "run" and job.params["format"] == "shorts_only"
    script = job.params["script"]
    assert EDITED in script and PARTS[1] not in script  # his edit is what is narrated
    assert PARTS[0] in script and PARTS[2] in script
    assert "##" not in script and "Part 3" not in script and "POV" not in script  # headings are never narrated
    assert out["result"]["left_out"] and str(path) in out["message"]
    after = drafts.load(home.state.paths, d["draft_id"])
    assert after.meta["status"] == "started" and after.meta["job_id"] == job.job_id
    assert EDITED in after.body  # the status write kept his text


def test_a_draft_the_check_refuses_is_named_in_the_refusal(home: fixture_home.FixtureHome):
    """G-47: when anything fails the answer names the file, never only an error; no card is shown."""
    turn(home, create(fmt="master_first", text=f"{PARTS[0]} {PARTS[1]}"))  # 200 characters or more: only the length is wrong
    d = newest(home)
    events = turn(home, call("start_job_from_draft", {"draft": d["draft_id"], "channel": "after-dark"}), text="start it")
    assert not [e for e in events if e["kind"] == "confirm"]
    (res,) = results(events)
    assert res["ok"] is False and "9:00" in res["error"] and d["path"] in res["error"]


def test_start_script_job_refuses_a_long_video_and_keeps_the_text_as_a_draft(home: fixture_home.FixtureHome):
    """G-43 b: the old tool stays for a Short; a long video is refused with "use a draft" and the text is kept."""
    story = ("Part 1: The Arrival (Mara POV)\n\n" + PARTS[0] + "\n\nPart 2: The Kitchen (Mara POV)\n\n" + PARTS[1]
             + " " + PARTS[2])
    args = {"channel": "after-dark", "format": "master_first", "script": story, "title": "The Porch"}
    events = turn(home, call("start_script_job", args), text="make a long video from this")
    assert not [e for e in events if e["kind"] == "confirm"]
    (res,) = results(events)
    d = newest(home)
    assert res["ok"] is False and "draft_create" in res["error"] and d["path"] in res["error"]
    text = Path(d["path"]).read_text(encoding="utf-8")
    assert "## Part 1: The Arrival (Mara POV)" in text and "## Part 2: The Kitchen (Mara POV)" in text
    assert "status: refused" in text and PARTS[0] in text and PARTS[2] in text
    assert "Part 1: The Arrival (Mara POV)" not in drafts.load(home.state.paths, d["draft_id"]).narration()[0]
    turn(home, call("start_script_job", args), text="make a long video from this")  # the same text again
    assert len(list((home.state.paths.home / "drafts").glob("*.md"))) == 1


def test_the_draft_tools_and_the_card_text():
    reg = build_registry()
    for name in ("draft_create", "draft_append_part", "draft_read", "list_drafts"):
        td = reg.get(name)
        assert td is not None and not td.spec.side_effect and td.spec.topic == "drafts", name
    start = reg.get("start_job_from_draft")
    assert start is not None and start.spec.side_effect and start.dry_run is not None
    assert "story" not in start.spec.args_schema["properties"] and "script" not in start.spec.args_schema["properties"]
    assert render_confirm(start.spec, {"draft": "x", "channel": "after-dark"}).startswith("Make a video for after-dark from the draft x")
    assert "draft_create" in reg.get("start_script_job").spec.description  # type: ignore[union-attr]


# ------------------------------------------------------------------ a hand-edited draft


def test_the_owners_hand_edits_between_parts_are_kept(home: fixture_home.FixtureHome):
    turn(home, create())
    path = Path(newest(home)["path"])
    mine = path.read_text(encoding="utf-8").replace("The porch light", "The old porch light")
    path.write_bytes(("﻿" + mine).replace("\n", "\r\n").encode("utf-8"))  # Notepad: BOM and CRLF
    turn(home, append(home, PARTS[1]))
    after = path.read_text(encoding="utf-8")
    assert "The old porch light" in after and PARTS[1] in after
    assert [line for line in after.splitlines() if line.startswith("## Part")] == ["## Part 1: The Light", "## Part 2"]
    # he breaks the block at the top: it is written back word for word and the story still grows
    path.write_text(after.replace("status: writing", "status: [writing"), encoding="utf-8")
    turn(home, append(home, PARTS[2]))
    final = path.read_text(encoding="utf-8")
    assert "status: [writing" in final and PARTS[2] in final and "The old porch light" in final
    d = drafts.load(home.state.paths, path.stem)
    assert not d.meta_ok and "warning" in drafts.card(d)
    # he removes the heading of part 2 and adds a line of his own: the narration is exactly his text
    no_head = final.replace("## Part 2\n", "").replace(PARTS[2], PARTS[2] + "\n\nI never answered.")
    path.write_text(no_head, encoding="utf-8")
    narration, _ = drafts.load(home.state.paths, path.stem).narration()
    assert narration.endswith("I never answered.") and "##" not in narration


def test_heading_lines_are_left_out_and_story_lines_are_kept():
    body = ("## Part 1: The Arrival\n\nPart 2: The Descent (Mara POV)\n**Chapter 3**\n# Title\n\n"
            "Part of me wanted to run.\nChapter one of my life ended that night.\n\n---\n\n"
            "Act civil, she said.\n<!-- a note to myself -->\nThe end.")
    text, left_out = drafts.script_text(body)
    assert left_out == ["## Part 1: The Arrival", "Part 2: The Descent (Mara POV)", "**Chapter 3**", "# Title"]
    for kept in ("Part of me wanted to run.", "Chapter one of my life ended that night.", "Act civil, she said.", "The end."):
        assert kept in text, kept
    assert "---" not in text and "note to myself" not in text and "\n\n\n" not in text


# ------------------------------------------------------------------ the Open route opens only files inside drafts


def test_open_opens_a_draft_and_refuses_every_path_outside_drafts(home: fixture_home.FixtureHome, tmp_path: Path):
    turn(home, create())
    d = newest(home)
    opener = home.state.adapters.folder
    out = drafts.open_draft(home.state.paths, opener, d["draft_id"])
    assert Path(out["opened"]) == Path(d["path"]) and opener.opened[-1] == Path(d["path"])  # type: ignore[attr-defined]
    outside = tmp_path / "outside.md"
    outside.write_text("not a draft", encoding="utf-8")
    secret = home.state.paths.config / "runtime.json"
    Path(d["path"]).with_suffix(".txt").write_text("x", encoding="utf-8")
    refs = (str(outside), str(secret), "..\\config\\runtime", "../config/runtime", "..", ".", "nul", "CON",
            f"{d['draft_id']}.txt", str(Path(d["path"]).with_suffix(".txt")), "", "a/../b", "no-such-draft")
    for ref in refs:
        n = len(opener.opened)  # type: ignore[attr-defined]
        with pytest.raises(ApiError) as e:
            drafts.open_draft(home.state.paths, opener, ref)
        assert e.value.status in (403, 404, 422), (ref, e.value.status)
        assert len(opener.opened) == n, ref  # type: ignore[attr-defined]


def test_open_refuses_a_link_inside_drafts_that_points_outside(home: fixture_home.FixtureHome, tmp_path: Path):
    target = tmp_path / "elsewhere.md"
    target.write_text("---\ntitle: x\n---\nnot a draft\n", encoding="utf-8")
    root = home.state.paths.home / "drafts"
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, root / "sneaky.md")
    except (OSError, NotImplementedError):
        pytest.skip("this machine does not allow symbolic links")
    with pytest.raises(ApiError) as e:
        drafts.open_draft(home.state.paths, home.state.adapters.folder, "sneaky")
    assert e.value.status == 403


def test_open_falls_back_to_notepad_when_no_program_opens_md(home: fixture_home.FixtureHome):
    turn(home, create())
    d = newest(home)

    class NoProgram:
        def open(self, path: Path) -> None:
            raise OSError("[WinError 1155] No application is associated with the specified file")

    seen: list[Path] = []
    if os.name != "nt":
        with pytest.raises(ApiError, match="The draft is at"):
            drafts.open_draft(home.state.paths, NoProgram(), d["draft_id"], notepad=seen.append)
        return
    out = drafts.open_draft(home.state.paths, NoProgram(), d["draft_id"], notepad=seen.append)
    assert seen == [Path(d["path"])] and out["opened"] == d["path"]


def test_the_http_routes_read_a_draft_and_open_only_drafts(tmp_home: Path):
    client, state = make_env(tmp_home)
    d = drafts.create(state.paths, title="The Porch Light", channel="after-dark", fmt="shorts_only",
                      parts=[("The Light", PARTS[0])])
    r = client.get(f"/api/drafts/{d.draft_id}")
    assert r.status_code == 200 and r.json()["path"] == str(d.path) and r.json()["words"] == word_count(PARTS[0])
    r = client.post(f"/api/drafts/{d.draft_id}/open")
    assert r.status_code == 200 and state.adapters.folder.opened == [d.path]  # type: ignore[attr-defined]
    for bad in ("..%5Cconfig%5Cruntime", "nul", "no-such-draft", "x.txt"):
        r = client.post(f"/api/drafts/{bad}/open")
        assert r.status_code in (403, 404, 422), (bad, r.status_code, r.text)
    assert state.adapters.folder.opened == [d.path]  # type: ignore[attr-defined]
