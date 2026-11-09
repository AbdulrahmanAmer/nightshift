# ruff: noqa: E501
"""A story the owner (or the Assistant) wrote becomes a video: `JobRunRequest.script`, `studio run --script`,
`POST /api/jobs/run` with a script and the Assistant's `start_script_job`. The text goes through the same
`script_import` stage as a series part; everything after it is unchanged."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from ns_common import ffmpeg
from ns_common.config import save_runtime
from ns_common.models import JobRunRequest, Runtime
from ns_common.paths import Paths
from ns_common.textstats import SCRIPT_LONG_MIN_WORDS, script_problem, word_count
from ns_dashboard.assistant import router
from ns_dashboard.assistant.llm import StaticProvider
from ns_dashboard.assistant.registry import ToolContext, render_confirm
from ns_dashboard.assistant.tools import build_registry
from ns_dashboard.errors import ApiError
from ns_dashboard.fakes import seed_channel
from ns_dashboard.handlers import Dashboard, build_state
from ns_dashboard.studio_runner import StudioRunner
from ns_dashboard.supervisor_link import SupervisorLink
from ns_studio import jobs
from ns_studio.cli import app
from ns_studio.context import FAKEABLE
from ns_studio.errors import StudioError
from ns_studio.orchestrator import FORMAT_STAGES, plan_format, run_job
from ns_studio.series.importer import script_markdown
from ns_studio.series.store import parse_script
from ns_supervisor import devfake
from tests.helpers import yaml_only_channel
from tests.test_dashboard_api import make_env

runner = CliRunner()


@pytest.fixture
def env_client(tmp_home: Path):
    return make_env(tmp_home)


SHORT = (
    "The porch light flickered twice before it died. I stood very still and counted the seconds, the way my "
    "grandmother taught me, because she always said the dark is only honest when it is quiet. Somewhere behind "
    "me the floorboards gave one long, patient creak. Then a voice I knew very well whispered my name from the "
    "empty kitchen."
)


def long_script(words: int = 1300) -> str:
    """Plain text of about `words` words in paragraphs of six sentences, with no chapter headings."""
    sentences = [
        f"On night {i} the keeper counted {i} lamps in the dark window and heard the water say his name again."
        for i in range(1, words // 19 + 2)
    ]
    return "\n\n".join(" ".join(sentences[i : i + 6]) for i in range(0, len(sentences), 6))


# ------------------------------------------------------------------ the request model


def test_job_run_request_without_a_script_is_unchanged():
    req = JobRunRequest(channel="after-dark", format="shorts_only", count=3)
    assert req.script is None and req.script_title is None and req.count == 3


def test_job_run_request_script_length_is_200_to_30000_characters():
    ok = JobRunRequest(channel="after-dark", script=SHORT)
    assert ok.script == SHORT
    assert JobRunRequest(channel="a", script="x" * 200).script == "x" * 200
    assert JobRunRequest(channel="a", script="  " + "x" * 200 + "\n").script == "x" * 200  # trimmed first
    assert JobRunRequest(channel="a", script="x" * 30000).script == "x" * 30000
    for bad in ("", "short", "x" * 199, "x" * 30001, " " * 300):
        with pytest.raises(ValidationError, match="200 to 30000 characters"):
            JobRunRequest(channel="a", script=bad)


def test_job_run_request_script_makes_exactly_one_video():
    assert JobRunRequest(channel="a", script=SHORT, count=1).count == 1
    assert JobRunRequest(channel="a", script=SHORT).count is None
    with pytest.raises(ValidationError, match="count must be 1"):
        JobRunRequest(channel="a", script=SHORT, count=2)
    with pytest.raises(ValidationError, match="only goes with a script"):
        JobRunRequest(channel="a", script_title="A title")
    assert JobRunRequest(channel="a", script=SHORT, script_title="  The   Porch ").script_title == "The Porch"
    with pytest.raises(ValidationError, match="120 characters"):
        JobRunRequest(channel="a", script=SHORT, script_title="t" * 121)


def test_a_long_video_needs_enough_words_for_nine_minutes():
    assert script_problem(SHORT, "shorts_only") is None
    assert script_problem(SHORT, "master_first") is not None and "at least 1100 words" in script_problem(SHORT, "master_first")  # type: ignore[operator]
    assert script_problem(long_script(1300), "master_first") is None
    assert word_count(long_script(1300)) >= SCRIPT_LONG_MIN_WORDS


# ------------------------------------------------------------------ plain text becomes a script.md


def test_plain_text_for_a_short_is_one_chapter_named_after_the_title():
    doc = parse_script(script_markdown(SHORT, "The Porch", chaptered=False))
    assert doc.title == "The Porch" and len(doc.chapters) == 1 and doc.chapters[0].title == "The Porch"
    assert doc.chapters[0].text.replace("\n\n", " ") == SHORT


def test_untitled_text_is_named_after_its_first_words_or_a_heading_line():
    assert parse_script(script_markdown(SHORT, None, chaptered=False)).title == "The porch light flickered twice before it died"
    headed = parse_script(script_markdown("# Wet Footprints\n\n" + SHORT, None, chaptered=False))
    assert headed.title == "Wet Footprints" and headed.chapters[0].text.startswith("The porch light")


def test_plain_text_for_a_long_video_is_cut_into_balanced_chapters():
    text = long_script(2100)
    doc = parse_script(script_markdown(text, "Lamps", chaptered=True))
    assert 2 <= len(doc.chapters) <= 7 and [c.n for c in doc.chapters] == list(range(1, len(doc.chapters) + 1))
    sizes = [word_count(c.text) for c in doc.chapters]
    assert min(sizes) * 2 >= max(sizes), sizes
    assert " ".join(c.text for c in doc.chapters).split() == text.split()  # nothing lost, nothing reordered


def test_text_with_chapter_headings_keeps_its_chapters():
    text = "## 1. The Door\n\nOne.\n\n## 2. The Hall\n\nTwo."
    doc = parse_script(script_markdown(text, "Mine", chaptered=True))
    assert doc.title == "Mine" and [c.title for c in doc.chapters] == ["The Door", "The Hall"]


# ------------------------------------------------------------------ the studio job


def _env(tmp_home: Path):
    env = jobs.open_env(FAKEABLE)
    return env, jobs.ensure_demo_channel(env)


def test_make_script_job_writes_the_script_where_script_import_reads_it(tmp_home: Path):
    env, ch = _env(tmp_home)
    ctx = jobs.make_script_job(env, ch, "shorts_only", SHORT, "The Porch")
    src = json.loads(ctx.file("script_source.json").read_text(encoding="utf-8"))
    assert src == {"kind": "script_import", "script": "script.md", "shorts": [], "title": "The Porch"}
    assert parse_script(ctx.file("script.md").read_text(encoding="utf-8")).title == "The Porch"
    assert plan_format("shorts_only", ctx.job_dir) == "shorts_from_script"
    assert plan_format("master_first", ctx.job_dir) == "master_from_script"
    assert plan_format("shorts_only", tmp_home / "elsewhere") == "shorts_only"


def test_both_formats_plan_script_import_instead_of_the_model_writing(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    for key in ("shorts_only", "master_first", "shorts_from_script", "master_from_script"):
        monkeypatch.setitem(FORMAT_STAGES, key, list(FORMAT_STAGES[key]))
    jobs.load_stage_modules()
    env, ch = _env(tmp_home)
    for fmt, script in (("shorts_only", SHORT), ("master_first", long_script(1300))):
        jobs.prepare_plan(fmt, True)
        ctx = jobs.make_script_job(env, ch, fmt, script)
        plan = run_job(ctx, dry_run=True)
        assert plan[0] == "script_import" and "policy_text" in plan and "policy_final" in plan, plan
        assert not {"ideas", "plan", "draft", "critic_revise", "long_plan", "chapters_draft", "continuity"} & set(plan)
        assert plan[-1] == "outbox" and "voice" in plan and "qc" in plan
    assert run_job(jobs.make_job(env, ch, "shorts_only"), dry_run=True)[0] == "ideas"  # an ordinary job is unchanged


def test_a_script_that_cannot_work_is_refused_before_any_job_folder_exists(tmp_home: Path):
    env, ch = _env(tmp_home)
    with pytest.raises(StudioError, match="at least 1100 words"):
        jobs.make_script_job(env, ch, "master_first", SHORT)
    with pytest.raises(StudioError, match="not 'series_compilation'"):
        jobs.make_script_job(env, ch, "series_compilation", SHORT)
    assert not list(env.paths.jobs_dir.iterdir())


@pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")
def test_studio_run_script_runs_through_to_an_output(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    jobs.load_stage_modules()
    for key in ("shorts_only", "shorts_from_script"):
        monkeypatch.setitem(FORMAT_STAGES, key, list(FORMAT_STAGES[key]))
    monkeypatch.setattr(jobs, "STAGE_MODULES", ())
    f = tmp_home / "story.txt"
    f.write_text(SHORT, encoding="utf-8")
    res = runner.invoke(app, ["run", "--fake-services", "--format", "shorts_only", "--script", str(f), "--title", "The Porch", "--policy"])
    assert res.exit_code == 0, res.output
    paths = Paths.from_env()
    job_dir = next(paths.jobs_dir.iterdir())
    assert re.search(rf"job {job_dir.name} done: (\d+) stages", res.output)
    story = json.loads((job_dir / "story.json").read_text(encoding="utf-8"))
    assert story["text"].replace("\n\n", " ") == SHORT and story["revisions"] == 0
    assert not (job_dir / "ideas_all.json").exists() and not (job_dir / "critic.json").exists()  # nothing was written by the model
    assert json.loads((job_dir / "policy_text.json").read_text(encoding="utf-8"))["passed"] is True
    state = json.loads((job_dir / "stage_state.json").read_text(encoding="utf-8"))
    assert "script_import" in state and "ideas" not in state and "render" in state
    assert list(paths.outbox.rglob("*.mp4"))


def test_studio_run_script_dry_run_prints_the_script_plan(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    for key in ("shorts_only", "shorts_from_script", "master_first", "master_from_script"):
        monkeypatch.setitem(FORMAT_STAGES, key, list(FORMAT_STAGES[key]))
    f = tmp_home / "story.txt"
    f.write_text(SHORT, encoding="utf-8")
    res = runner.invoke(app, ["run", "--dry-run", "--format", "shorts_only", "--script", str(f), "--fake-services"])
    assert res.exit_code == 0 and res.output.startswith("plan: script_import -> split -> hooks"), res.output
    assert "ideas" not in res.output


def test_studio_run_script_is_refused_for_the_wrong_use(tmp_home: Path):
    f = tmp_home / "story.txt"
    f.write_text(SHORT, encoding="utf-8")
    short = tmp_home / "short.txt"
    short.write_text("too short", encoding="utf-8")
    cases = [
        (["--script", str(f), "--count", "2"], "count must be 1"),
        (["--script", str(short)], "200 to 30000 characters"),
        (["--script", str(tmp_home / "missing.txt")], "cannot read"),
        (["--title", "Alone"], "--title goes with --script"),
        (["--script", str(f), "--resume", "x-20260101-abcd"], "cannot be combined"),
        (["--script", str(f), "--daily"], "cannot be combined"),
    ]
    for extra, needle in cases:
        res = runner.invoke(app, ["run", "--fake-services", "--format", "shorts_only", *extra])
        assert res.exit_code == 2, (extra, res.output)
        assert needle in re.sub(r"\s+", " ", re.sub(r"[│╭╰╮╯─]", " ", res.output)), (extra, res.output)
    assert not Paths.from_env().jobs_dir.exists() or not list(Paths.from_env().jobs_dir.iterdir())


def test_studio_run_script_for_a_long_video_needs_a_long_script(tmp_home: Path):
    f = tmp_home / "story.txt"
    f.write_text(SHORT, encoding="utf-8")
    res = runner.invoke(app, ["run", "--fake-services", "--format", "master_first", "--script", str(f)])
    assert res.exit_code == 1 and "at least 1100 words" in res.output + (res.stderr if res.stderr_bytes else "")


# ------------------------------------------------------------------ the dashboard


def test_post_jobs_run_with_a_script_stores_it_and_runs_the_script_stages(env_client):
    client, state = env_client
    seed_channel(state.db, state.paths)
    r = client.post("/api/jobs/run", json={"channel": "after-dark", "format": "shorts_only", "script": SHORT, "script_title": "The Porch"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["params"]["script"] == SHORT and body["params"]["script_title"] == "The Porch" and body["params"]["count"] == 1
    state.worker.run_next()
    job = client.get(f"/api/jobs/{body['job_id']}").json()
    assert job["status"] == "done" and len(job["result"]["output_ids"]) == 1
    assert state.adapters.run.scripts == [(SHORT, "The Porch")]  # type: ignore[attr-defined]
    assert state.adapters.run.last_stages[0] == "script_import"  # type: ignore[attr-defined]
    assert "ideas" not in state.adapters.run.last_stages  # type: ignore[attr-defined]
    assert state.adapters.run.calls == [("after-dark", "shorts_only", 1)]  # type: ignore[attr-defined]


def test_post_jobs_run_refuses_a_bad_script_in_plain_words(env_client):
    client, state = env_client
    seed_channel(state.db, state.paths)

    def post(**extra: Any):
        return client.post("/api/jobs/run", json={"channel": "after-dark", "format": "shorts_only", **extra})

    for body, needle in (
        ({"script": "too short"}, "200 to 30000 characters"),
        ({"script": SHORT, "count": 2}, "count must be 1"),
        ({"script": "x" * 30001}, "200 to 30000 characters"),
        ({"script": 12}, "valid string"),
    ):
        r = post(**body)
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_options", (body, r.text)
        assert needle in r.json()["error"]["message"], r.text
    long_short = client.post("/api/jobs/run", json={"channel": "after-dark", "format": "master_first", "script": SHORT})
    assert long_short.status_code == 422 and "at least 1100 words" in long_short.json()["error"]["message"]
    other = client.post("/api/jobs/run", json={"channel": "after-dark", "format": "series_compilation", "script": SHORT})
    assert other.status_code == 422
    assert state.store.counts() == {"queued": 0, "running": 0}


def test_post_jobs_run_without_a_script_is_unchanged(env_client):
    client, state = env_client
    seed_channel(state.db, state.paths)
    r = client.post("/api/jobs/run", json={"channel": "after-dark", "format": "shorts_only"})
    assert r.status_code == 201 and "script" not in r.json()["params"]


def test_a_long_script_for_a_long_video_is_accepted(env_client):
    client, state = env_client
    seed_channel(state.db, state.paths)
    r = client.post("/api/jobs/run", json={"channel": "after-dark", "format": "master_first", "script": long_script(1300)})
    assert r.status_code == 201 and r.json()["params"]["format"] == "master_first"


@pytest.mark.skipif(not ffmpeg.available(), reason="FFmpeg required for the fake pipeline")
def test_a_dev_install_turns_a_script_into_a_real_video(tmp_home: Path):
    """The dashboard's own child process (`studio_job run`) with the script in its arguments, on the fakes."""
    paths = Paths.from_env()
    paths.ensure()
    save_runtime(paths, Runtime.default("cpu-dev").model_copy(update={"dev_mode": True}))
    ch = yaml_only_channel(paths)
    seen: list[dict[str, Any]] = []
    done = StudioRunner(paths, poll_s=0.1, grace_s=2).run(
        "run", {"channel": ch.slug, "fmt": "shorts_only", "count": 1, "script": SHORT, "script_title": "The Porch"},
        on_event=seen.append,
    )
    assert done["output_ids"] and not any(e["ev"] == "error" for e in seen), seen
    names = [e["stage"] for e in seen if e["ev"] == "stage"]
    assert names[0] == "script_import" and "ideas" not in names and "outbox" in names, names
    job_dir = paths.job_dir(done["job_ids"][0])
    assert "The Porch" in (job_dir / "script.md").read_text(encoding="utf-8")
    assert list(paths.outbox.rglob("*.mp4"))


# ------------------------------------------------------------------ the Assistant


class DevFakeLlm:
    """The dev install's stand-in model (`devfake.chat_message`) as the Assistant's language model."""

    def __init__(self) -> None:
        self.tool_lists: list[list[dict[str, Any]]] = []

    def chat_tools(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, max_tokens: int | None = None) -> dict[str, Any]:
        self.tool_lists.append(tools)
        return devfake.chat_message({"messages": messages, "tools": tools})


def _tools(*names: str) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": n, "parameters": {}}} for n in names]


def _user(text: str) -> list[dict[str, Any]]:
    return [{"role": "system", "content": "x"}, {"role": "user", "content": text}]


def test_the_dev_fake_routes_script_phrases_to_start_script_job():
    tools = _tools("start_script_job", "start_story_job", "fetch_footage", "status")
    plan = devfake.plan_tool_call(_user(f"make a short for dev-night-tales from this script: {SHORT}"), tools)
    assert plan == {"name": "start_script_job", "args": {"channel": "dev-night-tales", "format": "shorts_only", "script": SHORT}}
    plan = devfake.plan_tool_call(_user(f"Use this script for after-dark, titled The Porch:\n{SHORT}"), tools)
    assert plan["name"] == "start_script_job" and plan["args"]["title"] == "The Porch" and plan["args"]["script"] == SHORT  # type: ignore[index]
    plan = devfake.plan_tool_call(_user(f"make a long video of this text for after-dark: {SHORT}"), tools)
    assert plan["args"]["format"] == "master_first"  # type: ignore[index]
    plan = devfake.plan_tool_call(_user(f"make a video of this text for after-dark: {SHORT}"), tools)
    assert "format" not in plan["args"] and plan["args"]["channel"] == "after-dark"  # type: ignore[index]
    # the story may hold words that other phrases look for ("get", "video"): the script cue wins
    tricky = "make a short from this script: I could get the video, but the clips of the hallway were gone."
    assert devfake.plan_tool_call(_user(tricky), tools)["name"] == "start_script_job"  # type: ignore[index]
    # no text after the cue, or the tool not offered: the old behaviour
    assert devfake.plan_tool_call(_user("make a short from this script"), tools) == {
        "name": "start_story_job", "args": {"format": "shorts_only"}}
    assert devfake.plan_tool_call(_user(f"make a short from this script: {SHORT}"), _tools("status")) is None


def test_the_router_offers_the_script_tool_when_the_owner_mentions_a_script():
    reg = build_registry()
    for q in ("make a short from this script: Once upon a night", "use this script for after-dark", "turn my script into a video"):
        topics = router.route(q)
        assert topics and topics[0] == "create", q
        assert "start_script_job" in router.select_tools(reg, topics, q)[:18], q
    assert "start_script_job" in router.PACKS["create"]


def test_start_script_job_tool_descriptor():
    td = build_registry().get("start_script_job")
    assert td is not None and td.spec.side_effect and td.spec.topic == "create"
    props = td.spec.args_schema["properties"]
    assert set(props) == {"channel", "format", "script", "title"} and td.spec.args_schema["required"] == ["channel", "script"]
    assert props["format"]["enum"] == ["shorts_only", "master_first"]
    text = render_confirm(td.spec, {"channel": "after-dark", "format": "shorts_only", "script": SHORT})
    assert text == f"Make a shorts_only for after-dark from the script you gave ({word_count(SHORT)} words). This uses the GPU for a while."
    assert "porch" not in text.lower()  # the card never repeats the story
    assert "Make a video for x from" in render_confirm(td.spec, {"channel": "x", "script": SHORT})


def test_the_assistant_checks_a_script_before_it_asks_for_a_yes():
    reg = build_registry()
    assert "at least 200 characters" in str(reg.validate("start_script_job", {"channel": "a", "script": "tiny"}))
    assert reg.validate("start_script_job", {"channel": "a", "script": SHORT}) is None
    assert "1100 words" in str(reg.validate("start_script_job", {"channel": "a", "format": "master_first", "script": SHORT}))
    assert reg.validate("start_script_job", {"channel": "a", "script": "x" * 30001}) is not None


def _assistant(tmp_home: Path):
    state = build_state(Paths.from_env(), fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2))
    llm = DevFakeLlm()
    return Dashboard(state, llm_provider=StaticProvider(llm)), state, llm  # type: ignore[arg-type]


def test_the_assistant_turns_a_pasted_script_into_a_job_after_a_yes(tmp_home: Path):
    dash, state, llm = _assistant(tmp_home)
    seed_channel(state.db, state.paths, "after-dark")
    events = list(dash.assistant.chat(f"make a short for after-dark from this script: {SHORT}"))
    cards = [e["payload"] for e in events if e["kind"] == "confirm"]
    assert len(cards) == 1 and cards[0]["tool"] == "start_script_job"
    assert cards[0]["text"] == f"Make a shorts_only for after-dark from the script you gave ({word_count(SHORT)} words). This uses the GPU for a while."
    assert "start_script_job" in [t["function"]["name"] for t in llm.tool_lists[0]]
    assert state.store.counts() == {"queued": 0, "running": 0}  # nothing started before the Yes
    out = dash.assistant.confirm(cards[0]["action_id"])
    assert out["status"] == "done"
    (job,) = state.store.list_jobs()
    assert job.kind == "run" and job.params["script"] == SHORT and job.params["format"] == "shorts_only" and job.params["count"] == 1


def test_the_assistant_tool_surfaces_the_dashboards_refusal(tmp_home: Path):
    dash, state, _ = _assistant(tmp_home)
    seed_channel(state.db, state.paths, "after-dark")
    td = dash.registry.get("start_script_job")
    assert td is not None
    with pytest.raises(ApiError, match="at least 1100 words"):
        td.call(ToolContext(dash=dash), {"channel": "after-dark", "format": "master_first", "script": SHORT})
    assert state.store.counts() == {"queued": 0, "running": 0}
