"""A channel that exists only as YAML (what the installer writes) must be runnable (review row 1)."""

from pathlib import Path

import pytest

from ns_common.models import Combo
from ns_common.paths import Paths
from ns_studio import jobs
from ns_studio.context import FAKEABLE
from ns_studio.orchestrator import FORMAT_STAGES, run_job
from ns_studio.variation.draw import record_combo
from tests.helpers import make_combo, yaml_only_channel


def _rows(env: jobs.Env) -> list[str]:
    return [r["slug"] for r in env.db.query("select slug from channels")]


def test_make_job_creates_the_channel_row_and_ideas_insert_works(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    jobs.load_stage_modules()
    monkeypatch.setitem(FORMAT_STAGES, "ideas_only", ["ideas"])
    env = jobs.open_env(frozenset(FAKEABLE))
    ch = yaml_only_channel(Paths.from_env())
    assert _rows(env) == []  # the installer leaves no row behind
    ctx = jobs.make_job(env, ch, "ideas_only")
    assert _rows(env) == [ch.slug]
    # Before the fix this raised sqlite3.IntegrityError: FOREIGN KEY constraint failed.
    assert run_job(ctx) == ["ideas"]
    assert env.db.query("select channel from ideas")[0]["channel"] == ch.slug


def test_load_job_restores_a_missing_row(tmp_home: Path):
    env = jobs.open_env(frozenset(FAKEABLE))
    ch = yaml_only_channel(Paths.from_env())
    ctx = jobs.make_job(env, ch, "shorts_only")
    env.db.execute("delete from combos")
    env.db.execute("delete from channels")
    jobs.load_job(env, ctx.job_id)
    assert _rows(env) == [ch.slug]


def test_record_combo_survives_a_missing_channel_row(tmp_home: Path):
    env = jobs.open_env(frozenset(FAKEABLE))
    combo: Combo = make_combo()
    record_combo(env.db, "yaml-only", "job-1", "long", combo)  # dashboard series path: no make_job
    assert "yaml-only" in _rows(env)
