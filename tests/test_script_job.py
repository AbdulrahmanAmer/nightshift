# ruff: noqa: E501
"""A story the owner (or the Assistant) wrote becomes a video: `JobRunRequest.script`, `studio run --script`,
`POST /api/jobs/run` with a script and the Assistant's `start_script_job`. The text goes through a variant of
the `script_import` stage; the owner's words are never cut, extended or replaced, and everything the pipeline
would reject after the voice or the render (length, banned phrases, long sentences, chapters) is refused
before a job exists."""

from __future__ import annotations

import json
import os
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
from ns_common.textstats import split_sentences, word_count
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
from ns_studio.series.plain import ScriptRefused, plan_script, script_markdown, script_problem, spoken_wpm
from ns_studio.series.store import parse_script
from ns_studio.stages import REGISTRY, run_stage
from ns_supervisor import devfake
from tests.helpers import yaml_only_channel
from tests.test_dashboard_api import make_env

runner = CliRunner()

SHORT = (
    "The porch light flickered twice before it died. I stood very still and counted the seconds, the way my "
    "grandmother taught me, because she always said the dark is only honest when it is quiet. Somewhere behind "
    "me the floorboards gave one long, patient creak. Then a voice I knew very well whispered my name from the "
    "empty kitchen."
)
PACINGS = ["standard", "measured"]


def long_script(words: int = 1300) -> str:
    """Plain text of about `words` words in paragraphs of six sentences, with no chapter headings."""
    sentences = [
        f"On night {i} the keeper counted {i} lamps in the dark window and heard the water say his name again."
        for i in range(1, words // 19 + 2)
    ]
    return "\n\n".join(" ".join(sentences[i : i + 6]) for i in range(0, len(sentences), 6))


_FILL = "the keeper counted lamps and heard cold water say his name again while rain fell on grey stone".split()
# (sentences before the moment, sentences of the moment, words per moment sentence) for each of 5 chapters
_MOMENTS = [(15, 5, 13), (14, 8, 13), (11, 10, 17), (7, 7, 15), (11, 9, 12)]


def _sentence(words: int, k: int) -> str:
    ws = [_FILL[(k + i) % len(_FILL)] for i in range(words)]
    ws[0] = ws[0].capitalize()
    return " ".join(ws) + "."


def fitted_script() -> str:
    """About 1380 words in 5 chapters whose sentences are sized for the moments the canned fake model picks.

    The fake model's `shorts_select` answer names sentence spans of its own sample story; a script whose
    spans hold the right number of words lets the whole master pipeline run on the fakes. The story text
    is still entirely the owner's: nothing here depends on what the fake model writes.
    """
    chapters = []
    for n, (before, span, words) in enumerate(_MOMENTS, start=1):
        sentences = [_sentence(14, n * 100 + i) for i in range(before)]
        sentences += [_sentence(words, n * 100 + 50 + i) for i in range(span)]
        chapters.append(f"## {n}. Chapter {n}" + chr(10) * 2 + " ".join(sentences))
    return (chr(10) * 2).join(chapters)


@pytest.fixture(autouse=True)
def _keep_plans(monkeypatch: pytest.MonkeyPatch):
    """`studio run` adds policy gates and Short combos to the plans in place: give each test its own copies."""
    jobs.load_stage_modules()
    for key, names in list(FORMAT_STAGES.items()):
        monkeypatch.setitem(FORMAT_STAGES, key, list(names))


@pytest.fixture
def env_client(tmp_home: Path):
    return make_env(tmp_home)


# ------------------------------------------------------------------ the request model


def test_job_run_request_without_a_script_is_unchanged():
    req = JobRunRequest(channel="after-dark", format="shorts_only", count=3)
    assert req.script is None and req.script_title is None and req.count == 3


def test_job_run_request_script_length_is_200_to_30000_characters():
    assert JobRunRequest(channel="after-dark", script=SHORT).script == SHORT
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


# ------------------------------------------------------------------ plain text becomes a script.md


def test_plain_text_for_a_short_is_one_chapter_named_after_the_title():
    doc = parse_script(script_markdown(SHORT, "The Porch"))
    assert doc.title == "The Porch" and len(doc.chapters) == 1 and doc.chapters[0].title == "The Porch"
    assert doc.chapters[0].text.replace("\n\n", " ") == SHORT


def test_untitled_text_is_named_after_its_first_words_or_a_heading_line():
    assert parse_script(script_markdown(SHORT, None)).title == "The porch light flickered twice before it died"
    headed = parse_script(script_markdown("# Wet Footprints\n\n" + SHORT, None))
    assert headed.title == "Wet Footprints" and headed.chapters[0].text.startswith("The porch light")


def test_plain_text_for_a_long_video_is_cut_into_balanced_chapters():
    text = long_script(2100)
    doc = parse_script(script_markdown(text, "Lamps", chapters=6))
    assert [c.n for c in doc.chapters] == [1, 2, 3, 4, 5, 6]
    sizes = [word_count(c.text) for c in doc.chapters]
    assert min(sizes) * 2 >= max(sizes), sizes
    assert " ".join(c.text for c in doc.chapters).split() == text.split()  # nothing lost, nothing reordered


def test_text_with_chapter_headings_keeps_its_chapters():
    text = "## 1. The Door\n\nOne.\n\n## 2. The Hall\n\nTwo."
    doc = parse_script(script_markdown(text, "Mine", chapters=5))
    assert doc.title == "Mine" and [c.title for c in doc.chapters] == ["The Door", "The Hall"]


# ------------------------------------------------------------------ what the pipeline would reject later


def test_a_short_gets_the_length_bucket_of_its_own_estimate():
    plan = plan_script(SHORT, "shorts_only", PACINGS)
    assert plan.pacing == "standard" and plan.length_bucket == round(plan.seconds) and 10 < plan.seconds < 40
    mid = plan_script(long_script(300), "shorts_only", PACINGS)
    assert mid.length_bucket == round(mid.seconds) and 100 < mid.seconds < 165


def test_a_short_over_two_minutes_forty_five_or_under_ten_seconds_is_refused_in_plain_words():
    with pytest.raises(ScriptRefused, match=r"about 3:\d\d as a Short. A Short can be about 2:45 at most"):
        plan_script(long_script(500), "shorts_only", PACINGS)
    with pytest.raises(ScriptRefused, match=r"only about \d+ seconds"):
        plan_script("Quiet night. Nothing moved. The door stayed shut. We slept.", "shorts_only", PACINGS)


def test_a_long_video_must_fit_nine_minutes_to_fourteen_thirty_at_the_channels_pace():
    for words in (960, 1160):
        with pytest.raises(ScriptRefused, match=r"needs at least 9:00. At this channel's pace that is about \d+ words"):
            plan_script(long_script(words), "master_first", ["standard"])
    plan = plan_script(long_script(1300), "master_first", PACINGS)
    assert 556 <= plan.seconds <= 870 and len(plan.doc.chapters) == 5 and plan.length_bucket is None
    with pytest.raises(ScriptRefused, match=r"about 1\d:\d\d as a long video; a long video can be about 14:30 at most"):
        plan_script(long_script(2200), "master_first", PACINGS)
    assert script_problem(long_script(1300), "master_first", PACINGS) is None
    assert "at least 9:00" in str(script_problem(SHORT, "master_first", PACINGS))


def test_the_pace_is_picked_from_the_channels_pool_so_the_text_fits():
    text = long_script(1950)  # fits at the standard pace (about 14:00), runs long at the measured one
    assert plan_script(text, "master_first", ["measured", "standard"]).pacing == "standard"
    assert plan_script(long_script(1300), "master_first", ["measured", "standard"]).pacing == "measured"
    with pytest.raises(ScriptRefused):
        plan_script(text, "master_first", ["measured"])


def test_a_long_video_is_always_five_to_seven_chapters():
    chapters = {len(plan_script(long_script(w), "master_first", PACINGS).doc.chapters) for w in (1300, 1750, 1950)}
    assert chapters <= {5, 6, 7}
    two = "## 1. One\n\n" + long_script(700) + "\n\n## 2. Two\n\n" + long_script(700)
    with pytest.raises(ScriptRefused, match="5 to 7 chapters, but the script has 2"):
        plan_script(two, "master_first", PACINGS)
    stray = "## 1. One\n\nHello there.\n\n## 3. Three\n\nMore words follow."
    with pytest.raises(ScriptRefused, match="chapter headings in the script do not work"):
        plan_script(stray, "shorts_only", PACINGS)


def test_banned_phrases_and_very_long_sentences_are_refused():
    banned = next(ln.strip() for ln in (Path(__file__).parents[1] / "contracts" / "policy" / "banned_phrases.txt").read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#"))
    with pytest.raises(ScriptRefused, match="does not allow"):
        plan_script(SHORT + f" And then I said {banned} out loud to nobody at all.", "shorts_only", PACINGS)
    run_on = " ".join(["word"] * 40) + "."
    with pytest.raises(ScriptRefused, match="sentence in the script is too long to narrate well.*35 words or fewer"):
        plan_script(SHORT + " " + run_on, "shorts_only", PACINGS)


# ------------------------------------------------------------------ the studio job


def _env(tmp_home: Path):
    env = jobs.open_env(FAKEABLE)
    return env, jobs.ensure_demo_channel(env)


def test_make_script_job_sets_pace_and_bucket_from_the_text(tmp_home: Path):
    env, ch = _env(tmp_home)
    ctx = jobs.make_script_job(env, ch, "shorts_only", SHORT, "The Porch")
    plan = plan_script(SHORT, "shorts_only", list(ch.pools.pacings), "The Porch")
    assert ctx.combo.length_bucket == plan.length_bucket and ctx.combo.pacing == plan.pacing
    src = json.loads(ctx.file("script_source.json").read_text(encoding="utf-8"))
    assert src == {"kind": "script_import", "owner_script": True, "script": "script.md", "shorts": [], "title": "The Porch"}
    assert parse_script(ctx.file("script.md").read_text(encoding="utf-8")).title == "The Porch"
    assert plan_format("shorts_only", ctx.job_dir) == "shorts_from_owner_script"
    assert plan_format("master_first", ctx.job_dir) == "master_from_owner_script"
    assert plan_format("shorts_only", tmp_home / "elsewhere") == "shorts_only"
    long_ctx = jobs.make_script_job(env, ch, "master_first", long_script(1300))
    assert long_ctx.combo.pacing in ch.pools.pacings and long_ctx.combo.length_bucket in ch.length_buckets


def test_a_series_part_keeps_its_own_plan(tmp_home: Path):
    env, ch = _env(tmp_home)
    ctx = jobs.make_job(env, ch, "master_first")
    ctx.write_json("script_source.json", {"kind": "script_import", "script": "x"})
    assert plan_format("master_first", ctx.job_dir) == "master_from_script"
    assert plan_format("shorts_only", ctx.job_dir) == "shorts_only"


def test_the_owner_script_plans_have_no_stage_that_writes_or_rewrites_the_story(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    for key in ("shorts_only", "master_first", "shorts_from_owner_script", "master_from_owner_script"):
        monkeypatch.setitem(FORMAT_STAGES, key, list(FORMAT_STAGES[key]))
    jobs.load_stage_modules()
    env, ch = _env(tmp_home)
    jobs.prepare_plan("shorts_only", True)
    jobs.prepare_plan("master_first", True)
    short = run_job(jobs.make_script_job(env, ch, "shorts_only", SHORT), dry_run=True)
    assert short[0] == "script_import_short" and short[-1] == "outbox"
    assert not {"ideas", "plan", "draft", "critic_revise", "split", "hooks"} & set(short), short
    assert short.index("policy_text") > short.index("visual_plan") and "policy_final" in short
    long = run_job(jobs.make_script_job(env, ch, "master_first", long_script(1300)), dry_run=True)
    assert long[0] == "script_import_long" and long[-1] == "outbox"
    assert not {"ideas", "long_plan", "chapters_draft", "chapters_critic", "continuity"} & set(long), long
    assert long.index("short_combos") == long.index("shorts_select") + 1  # the master gets Shorts as usual
    assert long.index("policy_text") == long.index("seo_shorts") + 1 and "duration_check" in long
    assert run_job(jobs.make_job(env, ch, "shorts_only"), dry_run=True)[0] == "ideas"  # an ordinary job is unchanged


def test_a_script_that_cannot_work_is_refused_before_any_job_folder_exists(tmp_home: Path):
    env, ch = _env(tmp_home)
    with pytest.raises(StudioError, match="needs at least 9:00"):
        jobs.make_script_job(env, ch, "master_first", SHORT)
    with pytest.raises(StudioError, match="not 'series_compilation'"):
        jobs.make_script_job(env, ch, "series_compilation", SHORT)
    with pytest.raises(StudioError, match="as a Short"):
        jobs.make_script_job(env, ch, "shorts_only", long_script(600))
    assert not list(env.paths.jobs_dir.iterdir())


def test_the_short_import_writes_one_part_with_the_text_exactly_as_given(tmp_home: Path):
    from ns_studio.media.voice import narration_items

    env, ch = _env(tmp_home)
    ctx = jobs.make_script_job(env, ch, "shorts_only", SHORT)
    jobs.load_stage_modules()
    stage = REGISTRY["script_import_short"]
    assert run_stage(ctx, stage) is True and run_stage(ctx, stage) is False  # resume does not run it again
    (part,) = ctx.read_json("parts.json")
    assert part["text"].replace("\n\n", " ") == SHORT and part["hook"] == split_sentences(SHORT)[0]
    assert abs(part["est_seconds"] - plan_script(SHORT, "shorts_only", list(ch.pools.pacings)).seconds) < 0.01
    assert ctx.read_json("story.json")["text"] == part["text"]
    items = narration_items([part], "Ryan", 1.0)
    assert [i.id for i in items] == ["p1"]  # the first sentence is the hook: no extra line is spoken before it
    assert not ctx.file("hooks_all.json").exists()


def test_the_long_import_writes_the_owners_chapters_and_no_shorts_plan(tmp_home: Path):
    env, ch = _env(tmp_home)
    text = long_script(1300)
    ctx = jobs.make_script_job(env, ch, "master_first", text)
    jobs.load_stage_modules()
    stage = REGISTRY["script_import_long"]
    assert run_stage(ctx, stage) is True and run_stage(ctx, stage) is False
    story = ctx.read_json("story.json")
    assert 5 <= len(story["chapters"]) <= 7
    assert " ".join(c["text"] for c in story["chapters"]).split() == text.split()
    from ns_common.models import Plan

    assert len(Plan.model_validate(ctx.read_json("plan.json")).chapters) == len(story["chapters"])  # expand_chapter can read it
    assert not ctx.file("shorts_plan.json").exists()


def test_the_duration_check_never_extends_an_owner_script(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    from ns_common.errors import NsError
    from ns_studio.media import master as M
    from tests.helpers import make_ctx
    from tests.test_master import write_story

    pytest.importorskip("numpy")
    if not ffmpeg.available():
        pytest.skip("ffmpeg required")
    monkeypatch.setattr(M, "FLOOR_S", 1000.0)
    ctx = make_ctx(tmp_home, fmt="master_first")
    write_story(ctx)
    ctx.write_json("script_source.json", {"kind": "script_import", "owner_script": True, "script": "script.md"})
    M.run_voice_master(ctx)
    called: list[int] = []
    monkeypatch.setattr(M, "load_expand_chapter", lambda: lambda *a, **k: called.append(1))
    with pytest.raises(NsError) as ei:
        M.DurationCheckStage().run(ctx)
    assert ei.value.code == "NS-E305" and called == [] and "script too short" in ei.value.detail


# ------------------------------------------------------------------ the whole studio run on the fakes


def _fake_run(monkeypatch: pytest.MonkeyPatch, tmp_home: Path, fmt: str, script: str, *extra: str):
    jobs.load_stage_modules()
    for key in ("shorts_only", "master_first", "shorts_from_owner_script", "master_from_owner_script"):
        monkeypatch.setitem(FORMAT_STAGES, key, list(FORMAT_STAGES[key]))
    monkeypatch.setattr(jobs, "STAGE_MODULES", ())
    f = tmp_home / "story.txt"
    f.write_text(script, encoding="utf-8")
    res = runner.invoke(app, ["run", "--fake-services", "--format", fmt, "--script", str(f), "--policy", *extra])
    assert res.exit_code == 0, res.output
    return next(Paths.from_env().jobs_dir.iterdir())


@pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")
def test_studio_run_script_short_renders_the_owners_words_inside_the_qc_band(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    """A 249-word script (the size the review found failing QC after the render) now fits its own bucket."""
    text = long_script(240)
    job_dir = _fake_run(monkeypatch, tmp_home, "shorts_only", text, "--title", "The Porch")
    (part,) = json.loads((job_dir / "parts.json").read_text(encoding="utf-8"))
    assert part["text"].replace("\n\n", " ").split() == text.split()  # verbatim: nothing cut, nothing added
    state = json.loads((job_dir / "stage_state.json").read_text(encoding="utf-8"))
    assert {"script_import_short", "seo", "voice", "render", "qc", "outbox"} <= set(state)
    assert not {"ideas", "split", "hooks", "critic_revise"} & set(state)
    assert not (job_dir / "ideas_all.json").exists() and not (job_dir / "critic.json").exists()
    assert json.loads((job_dir / "policy_text.json").read_text(encoding="utf-8"))["passed"] is True
    qc = json.loads((job_dir / "qc" / "report.json").read_text(encoding="utf-8"))
    assert all(v["passed"] for v in qc.values()), qc
    combo = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))["combo"]
    video = next(Paths.from_env().outbox.rglob("*.mp4"))
    assert abs(ffmpeg.duration_s(video) - combo["length_bucket"]) <= 0.15 * combo["length_bucket"]


@pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")
def test_long_script_voice_reaches_the_nine_minute_floor_without_a_word_added(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    """The fast half of the long-video check: import, Shorts, voice and `duration_check` on the fake voice."""
    jobs.load_stage_modules()
    names = ["script_import_long", "seo_long", "shorts_select", "visual_plan_long", "seo_shorts", "voice", "duration_check"]
    monkeypatch.setitem(FORMAT_STAGES, "master_from_owner_script", names)
    env, ch = _env(tmp_home)
    text = fitted_script()
    ctx = jobs.make_script_job(env, ch, "master_first", text)
    assert run_job(ctx) == names
    check = ctx.read_json("duration_check.json")
    assert check["expansions"] == [] and 540 <= check["total_s"] <= 870, check
    story = ctx.read_json("story.json")
    spoken = " ".join(ctx.file(f"audio/c{c['n']}.txt").read_text(encoding="utf-8") for c in story["chapters"])
    assert spoken.split() == re.sub(r"^## .*$", "", text, flags=re.M).split()  # every spoken word is the owner's
    assert len(ctx.read_json("shorts_plan.json")) >= 4  # the master still gets its Shorts


@pytest.mark.skipif(not ffmpeg.available() or not os.environ.get("NS_SLOW_TESTS"), reason="set NS_SLOW_TESTS=1: renders a 9 minute video")
def test_studio_run_script_long_renders_the_owners_chapters_and_cuts_shorts(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    text = fitted_script()
    assert 1300 <= word_count(text) <= 1450
    job_dir = _fake_run(monkeypatch, tmp_home, "master_first", text)
    story = json.loads((job_dir / "story.json").read_text(encoding="utf-8"))
    body = " ".join(c["text"] for c in story["chapters"])
    assert body.split() == re.sub(r"^## .*$", "", text, flags=re.M).split()  # the voice read exactly these words
    spoken = " ".join(p.read_text(encoding="utf-8") for p in sorted((job_dir / "audio").glob("c[0-9].txt")))
    assert spoken.split() == body.split()
    state = json.loads((job_dir / "stage_state.json").read_text(encoding="utf-8"))
    assert {"script_import_long", "shorts_select", "duration_check", "render_master", "qc", "outbox"} <= set(state)
    assert not {"ideas", "long_plan", "chapters_draft", "continuity"} & set(state)
    assert json.loads((job_dir / "duration_check.json").read_text(encoding="utf-8"))["expansions"] == []
    qc = json.loads((job_dir / "qc" / "report.json").read_text(encoding="utf-8"))
    assert qc["long"]["passed"] is True, qc
    assert list(Paths.from_env().outbox.rglob("*.mp4"))


@pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")
def test_a_slow_voice_gets_a_longer_bucket_and_still_lands_inside_the_qc_band(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    """The review's case: 260 words with the channel voice at speed 0.85 ran 123 s against a 105 s estimate."""
    from ns_common.models import Voice

    text = long_script(240)
    paths = Paths.from_env()
    paths.ensure()
    ch = yaml_only_channel(paths, voice=Voice(speed=0.85))
    normal = plan_script(text, "shorts_only", list(ch.pools.pacings))
    slow = plan_script(text, "shorts_only", list(ch.pools.pacings), voice_speed=0.85)
    assert slow.pacing == normal.pacing and slow.seconds > normal.seconds * 1.1
    assert slow.length_bucket is not None and normal.length_bucket is not None and slow.length_bucket > normal.length_bucket
    job_dir = _fake_run(monkeypatch, tmp_home, "shorts_only", text, "--channel", ch.slug)
    combo = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))["combo"]
    assert combo["length_bucket"] == slow.length_bucket
    (part,) = json.loads((job_dir / "parts.json").read_text(encoding="utf-8"))
    assert abs(part["est_seconds"] - slow.seconds) < 0.01  # the stage estimates at the speed the voice speaks at
    qc = json.loads((job_dir / "qc" / "report.json").read_text(encoding="utf-8"))
    assert all(v["passed"] for v in qc.values()), qc
    video = next(paths.outbox.rglob("*.mp4"))
    assert abs(ffmpeg.duration_s(video) - combo["length_bucket"]) <= 0.15 * combo["length_bucket"]


def test_the_long_window_moves_with_the_voice_speed():
    fast_only = long_script(1100)  # about 8:30 at speed 1.0: too short, long enough at 1.15
    assert script_problem(fast_only, "master_first", ["standard"]) is not None
    assert script_problem(fast_only, "master_first", ["standard"], voice_speed=0.85) is None  # slower voice: longer
    near_top = long_script(1950)  # about 14:00 at speed 1.0
    assert script_problem(near_top, "master_first", ["standard"]) is None
    with pytest.raises(ScriptRefused, match="can be about 14:30 at most"):
        plan_script(near_top, "master_first", ["standard"], voice_speed=0.85)
    short = plan_script(SHORT, "shorts_only", ["standard"])
    assert plan_script(SHORT, "shorts_only", ["standard"], voice_speed=0.85).seconds > short.seconds * 1.1
    assert spoken_wpm("standard", 1.0) == 155 and round(spoken_wpm("standard", 0.85)) == 132


def test_a_script_job_fails_at_once_when_only_its_own_import_would_be_redone(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    from ns_common.errors import NsError
    from ns_studio.policy import regen

    env, ch = _env(tmp_home)
    ctx = jobs.make_script_job(env, ch, "shorts_only", SHORT)
    calls: list[int] = []

    def fails(c, resume=True, dry_run=False):
        calls.append(1)
        raise NsError("NS-E601", "unsafe topic")

    monkeypatch.setattr(regen, "run_job", fails)
    with pytest.raises(NsError) as ei:
        regen.run_job_guarded(ctx)
    assert ei.value.code == "NS-E601" and calls == [1]  # no second pass over the same text
    assert not (ctx.job_dir / "rejected").exists() and len(ctx.read_json("policy_attempts.json")["history"]) == 1
    # a metadata failure can still be fixed by writing the metadata again
    ctx2 = jobs.make_script_job(env, ch, "shorts_only", SHORT)
    seen: list[int] = []

    def once(c, resume=True, dry_run=False):
        seen.append(1)
        if len(seen) == 1:
            raise NsError("NS-E603", "misleading claim")
        return ["seo"]

    monkeypatch.setattr(regen, "run_job", once)
    assert regen.run_job_guarded(ctx2) == ["seo"] and seen == [1, 1]


def test_studio_run_script_dry_run_prints_the_owner_script_plan(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    for key in ("shorts_only", "shorts_from_owner_script", "master_first", "master_from_owner_script"):
        monkeypatch.setitem(FORMAT_STAGES, key, list(FORMAT_STAGES[key]))
    f = tmp_home / "story.txt"
    f.write_text(SHORT, encoding="utf-8")
    res = runner.invoke(app, ["run", "--dry-run", "--format", "shorts_only", "--script", str(f), "--fake-services"])
    assert res.exit_code == 0 and res.output.startswith("plan: script_import_short -> seo -> visual_plan"), res.output
    assert "ideas" not in res.output and "split" not in res.output


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
    res = runner.invoke(app, ["run", "--dry-run", "--format", "series_compilation", "--script", str(f)])
    assert res.exit_code == 2 and "needs --format shorts_only or master_first" in re.sub(r"\s+", " ", res.output)
    assert not Paths.from_env().jobs_dir.exists() or not list(Paths.from_env().jobs_dir.iterdir())


def test_studio_run_script_for_a_long_video_needs_a_long_script(tmp_home: Path):
    f = tmp_home / "story.txt"
    f.write_text(SHORT, encoding="utf-8")
    res = runner.invoke(app, ["run", "--fake-services", "--format", "master_first", "--script", str(f)])
    assert res.exit_code == 1 and "needs at least 9:00" in res.output + (res.stderr if res.stderr_bytes else "")


# ------------------------------------------------------------------ the dashboard


def test_post_jobs_run_with_a_script_stores_it_and_runs_the_script_stages(env_client):
    client, state = env_client
    seed_channel(state.db, state.paths)
    r = client.post("/api/jobs/run", json={"channel": "after-dark", "format": "shorts_only", "script": SHORT, "script_title": "The Porch"})
    assert r.status_code == 201, r.text
    body = r.json()
    # the API shows the size of the script, not the text (a list of jobs is polled every few seconds)
    assert "script" not in body["params"] and body["params"]["script_words"] == word_count(SHORT)
    assert body["params"]["script_title"] == "The Porch" and body["params"]["count"] == 1
    assert state.store.get(body["job_id"]).params["script"] == SHORT  # type: ignore[union-attr]  # the child gets the text
    state.worker.run_next()
    job = client.get(f"/api/jobs/{body['job_id']}").json()
    assert job["status"] == "done" and len(job["result"]["output_ids"]) == 1 and "script" not in job["params"]
    assert all("script" not in j["params"] for j in client.get("/api/jobs").json())
    assert state.adapters.run.scripts == [(SHORT, "The Porch")]  # type: ignore[attr-defined]
    assert state.adapters.run.last_stages[0] == "script_import"  # type: ignore[attr-defined]
    assert "ideas" not in state.adapters.run.last_stages  # type: ignore[attr-defined]
    assert state.adapters.run.calls == [("after-dark", "shorts_only", 1)]  # type: ignore[attr-defined]


def test_post_jobs_run_refuses_a_bad_script_in_plain_words(env_client):
    client, state = env_client
    seed_channel(state.db, state.paths)

    def post(fmt: str = "shorts_only", **extra: Any):
        return client.post("/api/jobs/run", json={"channel": "after-dark", "format": fmt, **extra})

    for body, needle in (
        ({"script": "too short"}, "200 to 30000 characters"),
        ({"script": SHORT, "count": 2}, "count must be 1"),
        ({"script": "x" * 30001}, "200 to 30000 characters"),
        ({"script": 12}, "valid string"),
        ({"script": long_script(600)}, "A Short can be about 2:45 at most"),
        ({"script": SHORT + " " + " ".join(["word"] * 40) + "."}, "too long to narrate well"),
        ({"script": "## 1. One\n\nHello there friend.\n\n## 3. Three\n\n" + SHORT}, "chapter headings in the script do not work"),
    ):
        r = post(**dict[str, Any](body))
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_options", (body, r.text)
        assert needle in r.json()["error"]["message"], r.text
    short_for_long = post("master_first", script=SHORT)
    assert short_for_long.status_code == 422 and "needs at least 9:00" in short_for_long.json()["error"]["message"]
    assert post("series_compilation", script=SHORT).status_code == 422
    assert state.store.counts() == {"queued": 0, "running": 0}


def test_post_jobs_run_without_a_script_is_unchanged(env_client):
    client, state = env_client
    seed_channel(state.db, state.paths)
    r = client.post("/api/jobs/run", json={"channel": "after-dark", "format": "shorts_only"})
    assert r.status_code == 201 and "script" not in r.json()["params"] and "script_words" not in r.json()["params"]


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
    assert names[0] == "script_import_short" and "ideas" not in names and "outbox" in names, names
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
    tools = _tools("start_script_job", "start_story_job", "fetch_footage", "status", "add_idea")
    plan = devfake.plan_tool_call(_user(f"make a short for dev-night-tales from this script: {SHORT}"), tools)
    assert plan == {"name": "start_script_job", "args": {"channel": "dev-night-tales", "format": "shorts_only", "script": SHORT}}
    plan = devfake.plan_tool_call(_user(f"Use this script for after-dark, titled The Porch:\n{SHORT}"), tools)
    assert plan["name"] == "start_script_job" and plan["args"]["title"] == "The Porch" and plan["args"]["script"] == SHORT  # type: ignore[index]
    plan = devfake.plan_tool_call(_user(f"make a long video of this text for after-dark: {SHORT}"), tools)
    assert plan["args"]["format"] == "master_first"  # type: ignore[index]
    plan = devfake.plan_tool_call(_user(f"make a video of this text for after-dark: {SHORT}"), tools)
    assert "format" not in plan["args"] and plan["args"]["channel"] == "after-dark"  # type: ignore[index]
    # the story may hold words that other phrases look for ("get", "video"): the script cue wins
    tricky = "make a short from this script: " + SHORT + " I could get the video, but the clips of the hallway were gone."
    assert devfake.plan_tool_call(_user(tricky), tools)["name"] == "start_script_job"  # type: ignore[index]
    # no text after the cue, the tool not offered: the old behaviour
    assert devfake.plan_tool_call(_user("make a short from this script"), tools) == {
        "name": "start_story_job", "args": {"format": "shorts_only"}}
    assert devfake.plan_tool_call(_user(f"make a short from this script: {SHORT}"), _tools("status")) is None


def test_the_dev_fake_does_not_take_ideas_notes_or_short_bodies_for_a_script():
    tools = _tools("start_script_job", "start_story_job", "add_idea", "list_ideas")
    for ask in (
        f"make a short of this story idea: {SHORT}",
        f"add an idea from my story: {SHORT}",
        "make a short from this script: the lighthouse keeper hears his name",
        "use this story: too short to be a script",
    ):
        plan = devfake.plan_tool_call(_user(ask), tools)
        assert plan is None or plan["name"] != "start_script_job", ask


def test_the_router_offers_the_script_tool_when_the_owner_mentions_a_script():
    reg = build_registry()
    for q in ("make a short from this script: Once upon a night", "use this script for after-dark", "turn my script into a video"):
        topics = router.route(q)
        assert topics and topics[0] == "create", q
        assert "start_script_job" in router.select_tools(reg, topics, q)[:18], q
    assert "start_script_job" in router.PACKS["create"]


def test_start_script_job_tool_descriptor():
    td = build_registry().get("start_script_job")
    assert td is not None and td.spec.side_effect and td.spec.topic == "create" and td.dry_run is not None
    props = td.spec.args_schema["properties"]
    assert set(props) == {"channel", "format", "script", "title"} and td.spec.args_schema["required"] == ["channel", "script"]
    assert props["format"]["enum"] == ["shorts_only", "master_first"]
    assert "narrated as written" in td.spec.description and "nothing is invented" not in td.spec.description
    text = render_confirm(td.spec, {"channel": "after-dark", "format": "shorts_only", "script": SHORT})
    assert text == f"Make a Short for after-dark from the script you gave ({word_count(SHORT)} words). This uses the GPU for a while."
    assert "porch" not in text.lower()  # the card never repeats the story
    assert render_confirm(td.spec, {"channel": "x", "format": "master_first", "script": SHORT}).startswith("Make a long video for x from")
    assert "Make a video for x from" in render_confirm(td.spec, {"channel": "x", "script": SHORT})


def test_the_assistant_checks_a_script_before_it_asks_for_a_yes():
    reg = build_registry()
    assert "at least 200 characters" in str(reg.validate("start_script_job", {"channel": "a", "script": "tiny"}))
    assert reg.validate("start_script_job", {"channel": "a", "script": SHORT}) is None
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
    assert cards[0]["text"] == f"Make a Short for after-dark from the script you gave ({word_count(SHORT)} words). This uses the GPU for a while."
    assert "start_script_job" in [t["function"]["name"] for t in llm.tool_lists[0]]
    assert state.store.counts() == {"queued": 0, "running": 0}  # nothing started before the Yes
    out = dash.assistant.confirm(cards[0]["action_id"])
    assert out["status"] == "done"
    (job,) = state.store.list_jobs()
    assert job.kind == "run" and job.params["script"] == SHORT and job.params["format"] == "shorts_only" and job.params["count"] == 1


def test_the_assistant_refuses_a_bad_script_before_the_confirm_card(tmp_home: Path):
    """A script that is too long for a Short never reaches a Yes: the refusal is in plain words, no card."""
    dash, state, _ = _assistant(tmp_home)
    seed_channel(state.db, state.paths, "after-dark")
    events = list(dash.assistant.chat(f"make a short for after-dark from this script: {long_script(600)}"))
    assert not [e for e in events if e["kind"] == "confirm"]
    results = [e["payload"] for e in events if e["kind"] == "tool_result"]
    assert results and results[0]["ok"] is False and "A Short can be about 2:45 at most" in results[0]["error"]
    assert state.store.counts() == {"queued": 0, "running": 0}
    td = dash.registry.get("start_script_job")
    assert td is not None
    with pytest.raises(ApiError, match="needs at least 9:00"):
        td.call(ToolContext(dash=dash), {"channel": "after-dark", "format": "master_first", "script": SHORT})
