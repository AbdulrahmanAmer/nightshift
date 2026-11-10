# ruff: noqa: E501
"""The task-shaped read tools: diagnose, review_brief, what_worked, tonight_plan, video_stats, get_story, open_path."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ns_dashboard.assistant.registry import ToolContext
from ns_dashboard.errors import ApiError
from ns_supervisor.schedule_cfg import write_run_time
from tests.assistant_eval import fixture_home


@pytest.fixture
def home(tmp_home: Path) -> fixture_home.FixtureHome:
    return fixture_home.build(tmp_home)


def run(home: fixture_home.FixtureHome, name: str, args: dict[str, Any] | None = None, **context: Any) -> Any:
    td = home.dash.registry.get(name)
    assert td is not None, name
    problem = home.dash.registry.validate(name, args or {})
    assert problem is None, problem
    return td.call(ToolContext(home.dash, context), args or {})


def test_all_seven_are_registered_read_only_and_have_no_route(home: fixture_home.FixtureHome):
    for name in ("diagnose", "review_brief", "what_worked", "tonight_plan", "video_stats", "get_story", "open_path"):
        td = home.dash.registry.get(name)
        assert td is not None and td.route is None and td.spec.side_effect is False and td.spec.summary_fields


def test_diagnose_last_names_the_job_stage_code_and_log_lines(home: fixture_home.FixtureHome):
    out = run(home, "diagnose")
    assert out["job"]["job_id"] == home.ids["failed_job"] and out["job"]["failed_stage"] == "ideas"
    assert out["job"]["code"] == "NS-E316" and out["job"]["status"] == "failed"
    assert out["meaning"][0]["code"] == "NS-E316" and "embedding" in out["meaning"][0]["message"].lower()
    assert out["meaning"][0]["hint"]
    assert any("NS-E316" in ln["msg"] for ln in out["log_lines"])
    assert out["next"][0]["tool"] == "start_story_job" and out["next"][0]["args"] == {"channel": "after-dark"}


def test_diagnose_one_job_and_unknown_job(home: fixture_home.FixtureHome):
    first_failed = home.state.store.list_jobs("failed", None, 3)[-1]
    out = run(home, "diagnose", {"job_id": first_failed.job_id})
    assert out["job"]["code"] == "NS-E201"
    with pytest.raises(ApiError) as ei:
        run(home, "diagnose", {"job_id": "nope"})
    assert ei.value.status == 404


def test_diagnose_with_nothing_failed_says_so(tmp_home: Path):
    from tests.test_dashboard_assistant import make

    dash, _state, _ = make(tmp_home)
    out = dash.registry.get("diagnose").call(ToolContext(dash), {})  # type: ignore[union-attr]
    assert "job" not in out and "No failed job" in out["note"] and out["next"]


def test_review_brief_collects_policy_disclosure_and_repeat_rejections(home: fixture_home.FixtureHome):
    out = run(home, "review_brief", {"output_id": home.ids["review_id"]})
    assert out["title"] == "The Doorbell Camera" and out["channel"] == "after-dark" and out["file_exists"]
    assert out["policy_notes"][0].startswith("Problem: misleading claims")
    assert out["disclosure"]["ai_disclosure"] == "yes"
    assert out["options"]["hook"] == "cold_open"
    assert len(out["recent_rejections"]) == 3 and out["recent_rejections"][0]["reason"] == "hook too slow"
    assert any("cold_open" in r and "3 earlier" in r for r in out["repeat_rejections"])
    assert out["next"]["page"] == "Queue" and "Only you approve" in out["next"]["why"]
    with pytest.raises(ApiError):
        run(home, "review_brief", {"output_id": "ghost/s1"})


def test_what_worked_reports_n_and_says_when_it_is_too_few(home: fixture_home.FixtureHome):
    out = run(home, "what_worked", {"channel": "after-dark"})
    assert out["n"] == 10 and out["confidence"] == "high"
    hooks = out["best_options"]["hooks"]
    assert hooks[0]["option"] == "confession" and hooks[0]["n"] == 4 and hooks[0]["confidence"] == "low"
    assert hooks[0]["mean_score"] > hooks[1]["mean_score"] > hooks[2]["mean_score"]
    assert len(out["top"]) == 3 and out["top"][0]["hook"] == "confession" and out["bottom"][0]["hook"] == "cold_open"
    assert "note" not in out
    few = run(home, "what_worked", {"channel": "wholesome-twist"})
    assert few["n"] == 2 and few["confidence"] == "low" and "too few" in few["note"]
    assert few["next"]["tool"] == "import_analytics"
    recent = run(home, "what_worked", {"channel": "after-dark", "days": 30})
    assert recent["n"] == 10
    from_context = run(home, "what_worked", {}, channel="wholesome-twist")
    assert from_context["channel"] == "wholesome-twist"
    with pytest.raises(ApiError) as ei:
        run(home, "what_worked", {})  # two channels and no context: ask which
    assert ei.value.code == "which_channel"


def test_tonight_plan_lists_every_active_channel_once(home: fixture_home.FixtureHome):
    out = run(home, "tonight_plan")
    assert out["next_run"] is None and out["automatic_runs"]["on"] is False  # off: the plan is "if you start one"
    write_run_time(home.state.paths, "03:00")  # Automatic runs on: Windows has an enabled task
    out = run(home, "tonight_plan")
    assert out["automatic_runs"]["on"] is True and "03:00" in out["automatic_runs"]["state"]
    planned = [p["channel"] for p in out["plan"]]
    assert sorted(planned + out["active_but_nothing_due"]) == ["after-dark", "wholesome-twist"]
    assert out["next_run"] and out["paused"] == [] and out["queued_jobs"] == []
    for p in out["plan"]:
        assert p["count"] >= 1 and p["stage"] and isinstance(p["reasons"], list)
    home.dash.channels.pause("wholesome-twist", "strike")
    again = run(home, "tonight_plan")
    assert again["paused"] == ["wholesome-twist"] and "wholesome-twist" not in again["active_but_nothing_due"]


def test_video_stats_shows_numbers_and_rank(home: fixture_home.FixtureHome):
    row = home.state.db.one("SELECT output_id FROM outputs WHERE channel = 'after-dark' AND status = 'approved' ORDER BY output_id LIMIT 1")
    assert row is not None
    out = run(home, "video_stats", {"output_id": row["output_id"]})
    assert out["has_analytics"] and out["latest"]["views"] > 1000 and out["n"] == 10
    assert out["rank"].endswith("short videos on after-dark by views") and out["confidence"] == "high"
    none = run(home, "video_stats", {"output_id": home.ids["pending_id"]})
    assert none["has_analytics"] is False and none["next"]["tool"] == "import_analytics"
    with pytest.raises(ApiError):
        run(home, "video_stats", {"output_id": "ghost/s1"})


def test_get_story_by_job_or_output_id(home: fixture_home.FixtureHome):
    job = home.ids["story_job"]
    out = run(home, "get_story", {"job_id": job})
    assert out["title"] == "The Letters" and out["parts"][0]["hook"] == "A letter arrives."
    assert out["videos"] and out["text_excerpt"].startswith("The first letter") and out["idea"] is None
    by_output = run(home, "get_story", {"output_id": f"{job}/s1"})
    assert by_output["job_id"] == job
    part = run(home, "get_story", {"job_id": job, "part": 1})
    assert part["text_excerpt"] == "Part one text."
    with pytest.raises(ApiError):
        run(home, "get_story", {"job_id": job, "part": 9})
    with pytest.raises(ApiError):
        run(home, "get_story", {})


def test_open_path_opens_only_inside_the_home(home: fixture_home.FixtureHome):
    out = run(home, "open_path", {"thing": "after-dark outbox"})
    opened = home.state.adapters.folder.opened  # type: ignore[attr-defined]
    assert out["ok"] and opened and Path(opened[-1]) == Path(out["opened"])
    assert Path(out["opened"]).resolve() == (home.state.paths.outbox / "after-dark").resolve()
    out2 = run(home, "open_path", {"thing": "logs"})
    assert Path(out2["opened"]).resolve() == home.state.paths.logs.resolve()
    folder = run(home, "open_path", {"thing": home.ids["review_id"]})
    assert Path(folder["opened"]).is_dir()
    with pytest.raises(ApiError) as ei:
        run(home, "open_path", {"thing": "zzz"})
    assert ei.value.status == 404
