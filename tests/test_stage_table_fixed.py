# ruff: noqa: E501
"""Card D-stages: a job's plan never rewrites the module-level stage table.

The Short combos and the policy gates go into `FORMAT_STAGES` once, when the stage modules load
(`jobs.load_stage_modules`); a job whose policy is off leaves the gates out of its own plan
(`orchestrator.plan_names`), and the choice is kept in `job.json` so a resume runs the same plan. These tests
cross the product's own entries: the `studio` CLI (`CliRunner` on `ns_studio.cli.app`), `jobs.make_job`, the
orchestrator, the owner records of `ownership` and the idea check.
"""

from __future__ import annotations

import copy
import inspect
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from ns_common.db import Db
from ns_common.paths import Paths
from ns_studio import jobs, ownership
from ns_studio.cli import app
from ns_studio.context import FAKEABLE
from ns_studio.orchestrator import FORMAT_STAGES, SHORTS_ONLY_STAGES, run_job
from ns_studio.policy import idea_check
from ns_studio.stages import REGISTRY

runner = CliRunner()
GATES = ("policy_text", "policy_final")
FMT = "shorts_only"


class Stub:
    """A stage that only writes its output (no model, no FFmpeg); `fail_once` raises on its first run."""

    tenant = None
    inputs: tuple[str, ...] = ()

    def __init__(self, name: str, fail_once: bool = False) -> None:
        self.name = name
        self.outputs = (f"{name}.json",)
        self.fail_once = fail_once

    def run(self, ctx: Any) -> None:
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError(f"{self.name}: killed here")
        ctx.write_json(f"{self.name}.json", {"ok": True})


def _stubs(monkeypatch: pytest.MonkeyPatch, *, fail_at: str | None = None) -> None:
    """Stand-ins for every stage of a Short, gates included (registered after the real ones were)."""
    for name in (*SHORTS_ONLY_STAGES, *GATES):
        monkeypatch.setitem(REGISTRY, name, Stub(name, fail_once=name == fail_at))


def _make(env: jobs.Env, policy: bool) -> Any:
    """A Short made the way `studio run` makes one: the plan prepared, then the job with its policy."""
    jobs.prepare_plan(FMT, policy)
    kw: dict[str, Any] = {"policy": policy} if "policy" in inspect.signature(jobs.make_job).parameters else {}
    return jobs.make_job(env, _demo(env), FMT, **kw)


def _demo(env: jobs.Env) -> Any:
    """The fake-services demo channel, made the first time."""
    from ns_studio.channels.crud import all_channels

    return next((c for c in all_channels(env.paths) if c.slug == "demo"), None) or jobs.ensure_demo_channel(env)


def _plan(output: str) -> list[str]:
    line = next(ln for ln in output.splitlines() if ln.startswith("plan: "))
    return line.removeprefix("plan: ").split(" -> ")


def _job_json(paths: Paths, job_id: str) -> dict[str, Any]:
    return json.loads((paths.job_dir(job_id) / "job.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- the table itself
def test_preparing_a_plan_with_the_gates_on_leaves_the_module_table_unchanged():
    jobs.load_stage_modules()
    before = copy.deepcopy(FORMAT_STAGES)
    for fmt in list(FORMAT_STAGES):
        jobs.prepare_plan(fmt, True)
    assert FORMAT_STAGES == before


def test_a_dry_run_without_policy_after_one_with_policy_lists_no_gate(tmp_home: Path):
    first = runner.invoke(app, ["run", "--dry-run", "--format", FMT, "--policy"])
    assert first.exit_code == 0, first.output
    table = copy.deepcopy(FORMAT_STAGES)
    second = runner.invoke(app, ["run", "--dry-run", "--format", FMT, "--no-policy"])
    assert second.exit_code == 0, second.output
    assert set(GATES) <= set(_plan(first.output))  # both ends: the gated plan has both gates
    assert not set(GATES) & set(_plan(second.output)), second.output
    third = runner.invoke(app, ["run", "--dry-run", "--format", FMT, "--policy"])  # and back again
    assert set(GATES) <= set(_plan(third.output))
    assert FORMAT_STAGES == table


# ---------------------------------------------------------------- every reader of a job's plan
def test_the_orchestrator_the_progress_card_and_the_idea_check_read_one_plan(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    jobs.load_stage_modules()
    env = jobs.open_env(FAKEABLE)
    db = Db(env.paths.db_path)
    jobs.prepare_plan(FMT, True)  # the old code wrote the gates into the table here
    _stubs(monkeypatch)
    on = _make(env, True)  # a job with the gates on, then one with them off, in one process
    ran_on = run_job(on)
    off = _make(env, False)
    ran_off = run_job(off)

    assert not set(GATES) & set(ran_off), ran_off
    assert not idea_check.enabled(off)
    assert ownership.job_finished(env.paths, db, off.job_id, FMT)  # a gate it never runs is not waited for
    assert ownership.reached_stage(env.paths, off.job_id, FMT) == "outbox"

    assert set(GATES) <= set(ran_on) and idea_check.enabled(on)
    assert ownership.job_finished(env.paths, db, on.job_id, FMT)
    state_file = on.job_dir / "stage_state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    del state["policy_final"]  # a gated job killed before its final gate is not finished
    state_file.write_text(json.dumps(state), encoding="utf-8")
    assert not ownership.job_finished(env.paths, db, on.job_id, FMT)
    assert ownership.reached_stage(env.paths, on.job_id, FMT) == "policy_final"


# ---------------------------------------------------------------- the second time
def test_a_resumed_job_keeps_the_policy_it_was_made_with(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    jobs.load_stage_modules()
    env = jobs.open_env(FAKEABLE)
    _stubs(monkeypatch, fail_at="voice")
    ctx = _make(env, False)
    with pytest.raises(RuntimeError, match="killed here"):  # failed (or killed) halfway, at the voice
        run_job(ctx)
    runner.invoke(app, ["run", "--dry-run", "--format", FMT, "--policy"])  # another job's choice in between

    res = runner.invoke(app, ["run", "--resume", ctx.job_id, "--dry-run", "--fake-services"])
    assert res.exit_code == 0, res.output
    assert not set(GATES) & set(_plan(res.output)), res.output
    assert _job_json(env.paths, ctx.job_id).get("policy") is False

    # an explicit flag wins for that run; a dry run records nothing
    flag = runner.invoke(app, ["run", "--resume", ctx.job_id, "--dry-run", "--policy", "--fake-services"])
    assert flag.exit_code == 0 and set(GATES) <= set(_plan(flag.output)), flag.output
    assert _job_json(env.paths, ctx.job_id).get("policy") is False


def test_an_explicit_flag_on_a_resume_is_kept_for_the_next_one(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    from ns_studio import cli
    from ns_studio.policy import gate

    jobs.load_stage_modules()
    env = jobs.open_env(FAKEABLE)
    _stubs(monkeypatch, fail_at="voice")
    ctx = _make(env, False)
    with pytest.raises(RuntimeError):
        run_job(ctx)
    # the CLI loads the real gate stages again; give them stand-in work (what they check is not this card's)
    for cls in (gate.PolicyTextStage, gate.PolicyFinalStage):
        monkeypatch.setattr(cls, "run", lambda self, c: c.write_json(f"{self.name}.json", {"passed": True}))
    monkeypatch.setattr(cli, "require_story_room", lambda *a, **k: None)  # the drive's room is not this card's
    res = runner.invoke(app, ["run", "--resume", ctx.job_id, "--policy", "--fake-services"])
    assert res.exit_code == 0, res.output
    assert _job_json(env.paths, ctx.job_id).get("policy") is True
    state = json.loads((ctx.job_dir / "stage_state.json").read_text(encoding="utf-8"))
    assert {"policy_text", "policy_final", "outbox"} <= {n for n, e in state.items() if e.get("status") == "done"}
    again = runner.invoke(app, ["run", "--resume", ctx.job_id, "--dry-run", "--fake-services"])
    assert set(GATES) <= set(_plan(again.output)), again.output


def test_a_job_folder_from_0_1_14_resumes_with_the_gates_when_they_are_available(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_studio.policy import gate

    jobs.load_stage_modules()
    env = jobs.open_env(FAKEABLE)
    ctx = _make(env, False)  # its choice is taken out below, as a 0.1.14 job.json never had one
    f = ctx.job_dir / "job.json"
    old = {k: v for k, v in _job_json(env.paths, ctx.job_id).items() if k != "policy"}  # written by 0.1.14
    f.write_text(json.dumps(old), encoding="utf-8")

    with monkeypatch.context() as m:  # no policy prompt on this install: no gates, as 0.1.14 did
        m.setattr(gate, "available", lambda: False)
        none = runner.invoke(app, ["run", "--resume", ctx.job_id, "--dry-run", "--fake-services"])
    assert none.exit_code == 0 and not set(GATES) & set(_plan(none.output)), none.output
    res = runner.invoke(app, ["run", "--resume", ctx.job_id, "--dry-run", "--fake-services"])
    assert res.exit_code == 0 and set(GATES) <= set(_plan(res.output)), res.output
    # one bad file among good ones: a value that is not a bool reads as no choice recorded
    f.write_text(json.dumps({**old, "policy": "no"}), encoding="utf-8")
    bad = runner.invoke(app, ["run", "--resume", ctx.job_id, "--dry-run", "--fake-services"])
    assert bad.exit_code == 0 and set(GATES) <= set(_plan(bad.output)), bad.output
    assert "policy" not in old and json.loads(f.read_text(encoding="utf-8"))["policy"] == "no"  # read, not written


def test_the_nightly_makes_its_jobs_with_the_flag_it_was_given(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    from ns_studio import cli

    made: list[Any] = []

    def night(env: jobs.Env, **_: Any) -> dict[str, Any]:
        ch = _demo(env)
        made.append(jobs.make_job(env, ch, FMT, origin="daily"))  # what `daily._tonight` calls
        return {"channels": [], "jobs": [], "failures": [], "backup": "-"}

    monkeypatch.setattr(cli, "run_daily", night)
    monkeypatch.setattr(cli, "_setup_voice", lambda env: None)
    res = runner.invoke(app, ["run", "--daily", "--no-policy", "--fake-services"])
    assert res.exit_code == 0, res.output
    assert made and getattr(made[0], "policy", None) is False and _job_json(made[0].paths, made[0].job_id).get("policy") is False
    res = runner.invoke(app, ["run", "--daily", "--fake-services"])  # no flag: on when the gates can run
    assert res.exit_code == 0, res.output
    assert getattr(made[1], "policy", None) is True and _job_json(made[1].paths, made[1].job_id).get("policy") is True


# ---------------------------------------------------------------- no test order changes a plan
def test_policy_p04_passes_after_the_disk_room_test_in_one_process(tmp_path: Path, repo_root: Path):
    first = "tests/test_job_disk_room.py::test_a_scheduled_or_batched_job_is_checked_again_when_it_starts"
    second = "tests/test_policy_p04.py::test_idea_is_not_classified_without_policy_gates"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", f"--basetemp={tmp_path / 'child'}", first, second],
        cwd=repo_root, capture_output=True, text=True, timeout=600, check=False,
    )
    tail = (proc.stdout + proc.stderr)[-3000:]
    assert proc.returncode == 0, tail
    assert re.search(r"\b2 passed\b", proc.stdout), tail
