# ruff: noqa: E501
"""A small Nightshift home for the offline Assistant evaluation: channels, outputs with analytics, failed jobs.

Everything runs on the fake services (no model, no GPU). `build(tmp_home)` returns the Dashboard, its state and
the ids the cases file refers to as `<name>`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ns_dashboard.assistant.registry import ToolContext
from ns_dashboard.fakes import seed_channel, seed_output
from ns_dashboard.handlers import Dashboard
from ns_dashboard.state import AppState
from tests.test_dashboard_assistant import make

#: A Pexels-shaped key (56 letters and digits), built in pieces so no secret scanner sees a literal.
SECRET = "5634" + "92ad6f91" * 6 + "7000"

#: (hook, viewed %, average % viewed) of the ten After Dark videos that have analytics.
AFTER_DARK = [("confession", 80, 70)] * 4 + [("cold_open", 50, 50)] * 4 + [("question", 60, 60)] * 2


@dataclass
class FixtureHome:
    """The built home."""

    dash: Dashboard
    state: AppState
    ids: dict[str, str]


def _combo(state: AppState, channel: str, job: str, item: str, hook: str) -> str:
    combo_id = f"{job}:{item}"
    state.db.execute(
        "INSERT INTO combos(combo_id, channel, job_id, item, hook, structure, pov_tone, pacing, caption_style,"
        " visual_treatment, title_formula, length_bucket, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (combo_id, channel, job, item, hook, "twist_end", "first_person", "standard", "bold", "dark", "question", 45,
         datetime.now(UTC).isoformat()),
    )
    return combo_id


def _output(state: AppState, channel: str, n: int, status: str, hook: str, *, files: bool = False,
            title: str | None = None, analytics: tuple[int, int] | None = None) -> str:
    job = f"{channel}-20261101-{n:04x}"
    oid = seed_output(state.db, state.paths, channel=channel, kind="short", k=1, status=status, job_id=job,
                      title=title or f"{channel} short {n}", with_files=files)
    combo = _combo(state, channel, job, "s1", hook)
    state.db.execute("UPDATE outputs SET combo_id = ? WHERE output_id = ?", (combo, oid))
    if status == "rejected":
        state.db.execute("UPDATE outputs SET review_note = 'hook too slow' WHERE output_id = ?", (oid,))
    if analytics is not None:
        state.db.execute(
            "INSERT INTO analytics(output_id, date, views, viewed_pct, avg_pct_viewed, avg_view_duration_s, likes,"
            " comments, shares, subs_gained) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (oid, "2026-11-03", 1000 + n, analytics[0], analytics[1], 20.0, 5, 1, 0, 1),
        )
    return oid


def build(tmp_home: Path) -> FixtureHome:
    """Seed the home under `tmp_home` (the `NIGHTSHIFT_HOME` of the test)."""
    dash, state, _provider = make(tmp_home)
    seed_channel(state.db, state.paths, "after-dark")
    seed_channel(state.db, state.paths, "wholesome-twist")
    n = 0
    for hook, viewed, avg in AFTER_DARK:
        n += 1
        _output(state, "after-dark", n, "approved", hook, analytics=(viewed, avg))
    for i in range(2):
        n += 1
        _output(state, "wholesome-twist", n, "uploaded", "twist_end", analytics=(70 + i, 60))
    for _ in range(3):
        n += 1
        _output(state, "after-dark", n, "rejected", "cold_open")
    pending = [_output(state, "after-dark", n + i + 1, "pending", "confession") for i in range(3)]
    n += 3
    n += 1
    review = _output(state, "after-dark", n, "pending", "cold_open", files=True, title="The Doorbell Camera")
    job_dir = state.paths.job_dir(review.split("/", 1)[0])
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "policy_final.json").write_text(
        json.dumps({
            "checks": {
                "real_people": {"passed": True, "detail": ""},
                "misleading_claims": {"passed": False, "detail": "the title says true story"},
            },
            "advice": {"ai_disclosure": "yes", "made_for_kids": False, "age_restrict_suggested": False},
        }),
        encoding="utf-8",
    )
    failed = ""
    for code, stage, studio_job in (
        ("NS-E201", "voice", "after-dark-20261031-0e01"),
        ("NS-E502", "clips", "after-dark-20261031-0e02"),
        ("NS-E316", "ideas", "after-dark-20261031-0e03"),
    ):
        rec = state.store.create("run", "after-dark", {"channel": "after-dark"})
        state.store.finish(rec.job_id, "failed", f"{code} in {stage}", {
            "error": {"code": code, "hint": "see the hint", "stage": stage}, "studio_job_id": studio_job,
        })
        state.log.error("stage_failed", f"{code} in the {stage} stage", job_id=studio_job, code=code)
        failed = rec.job_id
    dash.library.add_idea({"channel": "after-dark", "premise": "A lighthouse keeper receives letters from the future.",
                           "twist": "She wrote them."})
    story_job = "after-dark-20261101-0001"
    state.db.execute(
        "INSERT INTO stories(job_id, channel, format, working_title, text, word_count, opening_line, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (story_job, "after-dark", "shorts_only", "The Letters", "The first letter came on a Tuesday. " * 20, 140,
         "The first letter came on a Tuesday.", datetime.now(UTC).isoformat()),
    )
    state.db.execute(
        "INSERT INTO parts(job_id, n, text, hook, word_count, est_seconds) VALUES (?,?,?,?,?,?)",
        (story_job, 1, "Part one text.", "A letter arrives.", 3, 2.0),
    )
    series = dash.registry.get("workshop_start")
    assert series is not None
    slug = series.call(ToolContext(dash), {"channel": "after-dark"})["slug"]
    ids = {"review_id": review, "pending_id": pending[0], "failed_job": failed, "series_slug": slug, "story_job": story_job}
    return FixtureHome(dash, state, ids)
