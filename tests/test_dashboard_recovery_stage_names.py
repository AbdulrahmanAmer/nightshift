"""A job's stage is named from its own complete plan in every process (block B install proof, defect 2).

`studio status` said "stopped at stage voice" for a job that was in `policy_text`: that process loads no stage
module, so nothing had put the policy gates and the Short combos into the stage table it read. The dashboard's
recovery had the same fault and was given a set-up call of its own; that treated one caller. The rule now sits
at the one reader: `orchestrator.plan_names` completes a table entry that is still bare before it answers
(`complete_plan`), so `ownership.reached_stage` and `job_finished` see the job's plan (its format, its
recorded policy) whoever asks, with no set-up call.

Every route runs in a child interpreter: any earlier test in this process may already have completed the
table, and the fault only shows where nothing has. Each child first proves that its table is still bare.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# What every child starts with: a home, its database, and a way to leave a job whose process is gone (a record
# that says `running`, no lock held) with the stages in `done` finished.
PREAMBLE = r"""
import json, sys
from datetime import UTC, datetime
from ns_common.db import Db
from ns_common.paths import Paths
from ns_studio import orchestrator, ownership

paths = Paths.from_env()
paths.ensure()
db = Db(paths.db_path)
db.migrate()
STAMP = datetime.now(UTC).isoformat(timespec="seconds")
TEXT_STAGES = ["ideas", "plan", "draft", "critic_revise", "split", "hooks", "seo", "visual_plan"]


def bare() -> bool:
    # the story plans as the orchestrator module holds them before anything completed them (the Clipper's
    # plan is set by importing the Clipper, which `ns_studio.cli` does: the `function` route checks it)
    table = orchestrator.FORMAT_STAGES
    return "policy_text" not in table["shorts_only"] and "short_combos" not in table["master_first"]


def leave_job(job_id, fmt, done, *, policy=True, origin="manual", announced=True, state="running", files=()):
    folder = paths.job_dir(job_id)
    folder.mkdir(parents=True)
    job = {"channel": "demo", "fmt": fmt, "policy": policy, "created_at": STAMP, "combo": {"x": "y"}}
    (folder / "job.json").write_text(json.dumps(job), encoding="utf-8")
    state_text = json.dumps({name: {"status": "done"} for name in done})
    (folder / "stage_state.json").write_text(state_text, encoding="utf-8")
    for name in files:
        (folder / name).write_text("{}", encoding="utf-8")
    fields = {"kind": "job", "state": state, "origin": origin, "channel": "demo", "fmt": fmt}
    if announced:
        fields["announced_at"] = "2026-01-01T00:00:00+00:00"
    ownership.write_record(paths, job_id, **fields)


print("BARE", bare())
"""

ROUTES = {
    # the function itself, for each kind of plan: a Short with and without the policy, a long video (its
    # Short combos), a long video from the owner's script, a series part, a clip
    "function": r"""
leave_job("s-policy", "shorts_only", TEXT_STAGES)
leave_job("s-nopolicy", "shorts_only", TEXT_STAGES, policy=False)
leave_job("m-combos", "master_first", ["ideas", "long_plan", "chapters_draft", "chapters_critic",
                                        "continuity", "seo_long", "shorts_select"])
leave_job("m-part", "master_first", ["script_import"], files=("script_source.json",))
leave_job("c-clip", "clip", [])
print("CLIPBARE", orchestrator.FORMAT_STAGES["clip"] == [])
for job_id, fmt in (("s-policy", "shorts_only"), ("s-nopolicy", "shorts_only"), ("m-combos", "master_first"),
                    ("m-part", "master_first"), ("c-clip", "clip")):
    print("STAGE", job_id, ownership.reached_stage(paths, job_id, fmt))
done = [n for n in orchestrator.stage_names("shorts_only", paths.job_dir("s-policy")) if n != "policy_final"]
leave_job("s-nearly", "shorts_only", done)
print("FINISHED", ownership.job_finished(paths, db, "s-nearly", "shorts_only"))
""",
    # `studio status`, the command the proof ran: a job whose process is gone, not closed yet
    "status": r"""
from typer.testing import CliRunner
from ns_studio import jobs
from ns_studio.cli import app

jobs.ensure_demo_channel(jobs.open_env(light=True))
leave_job("s-policy", "shorts_only", TEXT_STAGES)
print("BARE", bare())
res = CliRunner().invoke(app, ["status"])
print("EXIT", res.exit_code)
for line in res.output.splitlines():
    if "s-policy" in line:
        print("LINE", line.strip())
""",
    # `studio status` while the job is being made (this process holds its lock)
    "working": r"""
leave_job("s-policy", "shorts_only", TEXT_STAGES)
with ownership.hold_job(paths, "s-policy", channel="demo", fmt="shorts_only"):
    for line in ownership.status_lines(paths, db, "demo"):
        print("LINE", line.strip())
""",
    # the dashboard at engine start: its recovery writes the Queue card
    "dashboard": r"""
from ns_dashboard.handlers import build_state
from ns_dashboard.supervisor_link import SupervisorLink

leave_job("s-policy", "shorts_only", TEXT_STAGES)
link = SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
state = build_state(paths, fake_services=True, link=link)
print("BARE", bare())
state.worker.recover_studio()
for row in db.query("select message, params_json from dashboard_jobs"):
    print("LINE", row["message"])
    print("CARDSTAGE", json.loads(row["params_json"])["result"]["error"]["stage"])
print("RECORD", ownership.read_record(paths, "s-policy")["interrupted_at_stage"])
""",
    # the nightly: it closes dead work without a note, then leaves the note itself (daily.py)
    "nightly": r"""
leave_job("s-policy", "shorts_only", TEXT_STAGES, origin="daily")
out = ownership.recover(paths, db, note=False)
ownership.leave_recovery_note(paths, None, out)
from ns_supervisor.notify import read_notes

print("LINE", read_notes(paths)[-1]["message"])
""",
    # the dashboard's status views of a run: an interrupted job that has since finished reads as resumed
    "views": r"""
leave_job("s-policy", "shorts_only", TEXT_STAGES, state="interrupted")
items = [{"job_id": "s-policy", "channel": "demo", "stage": "policy_text", "state": "open"}]
print("STATE", ownership.current_interrupted(paths, db, items)[0]["state"])
names = orchestrator.stage_names("shorts_only", paths.job_dir("s-policy"))
state_text = json.dumps({name: {"status": "done"} for name in names if name != "policy_final"})
(paths.job_dir("s-policy") / "stage_state.json").write_text(state_text, encoding="utf-8")
print("STATE", ownership.current_interrupted(paths, db, items)[0]["state"])
""",
}


def run_route(route: str, tmp_home: Path, tmp_path: Path) -> dict[str, list[str]]:
    """Run one route in a fresh interpreter; its printed lines by their first word."""
    script = tmp_path / f"child_{route}.py"
    script.write_text(PREAMBLE + ROUTES[route], encoding="utf-8")
    # the child must import THIS checkout, whatever copy the interpreter has installed
    env = {**os.environ, "PYTHONPATH": str(ROOT), "NIGHTSHIFT_HOME": str(tmp_home)}
    done = subprocess.run(  # noqa: S603 - our own interpreter and script
        [sys.executable, str(script)], capture_output=True, text=True, timeout=180, cwd=ROOT, env=env
    )
    assert done.returncode == 0, done.stderr[-3000:]
    out: dict[str, list[str]] = {}
    for line in done.stdout.splitlines():
        mark, _, rest = line.partition(" ")
        out.setdefault(mark, []).append(rest)
    assert out["BARE"] and set(out["BARE"]) == {"True"}, "the child completed the stage table too early"
    return out


def test_the_stage_of_a_job_comes_from_its_own_plan_in_a_process_that_loaded_nothing(
    tmp_home: Path, tmp_path: Path
) -> None:
    out = run_route("function", tmp_home, tmp_path)
    stages = dict(line.split() for line in out["STAGE"])
    assert out["CLIPBARE"] == ["True"]                  # nothing had imported the Clipper either
    assert stages["s-policy"] == "policy_text"          # the proof's job: not "voice"
    assert stages["s-nopolicy"] == "voice"              # made without the policy: its plan has no gate
    assert stages["m-combos"] == "short_combos"         # a long video: not "visual_plan_long"
    assert stages["m-part"] == "short_combos"           # a series part: its own plan, after `script_import`
    assert stages["c-clip"] == "clip_import"              # a clip: the Clipper's plan, not "no stage"
    # every stage but the final gate is done: the job is NOT finished (a bare table said it was)
    assert out["FINISHED"] == ["False"]


def test_studio_status_names_the_policy_stage_of_a_job_whose_process_is_gone(
    tmp_home: Path, tmp_path: Path
) -> None:
    out = run_route("status", tmp_home, tmp_path)
    assert out["EXIT"] == ["0"]
    [line] = out["LINE"]
    assert line.startswith("interrupted: job s-policy stopped at stage policy_text;"), line


def test_studio_status_names_the_policy_stage_of_a_job_that_is_being_made(
    tmp_home: Path, tmp_path: Path
) -> None:
    out = run_route("working", tmp_home, tmp_path)
    [line] = [ln for ln in out["LINE"] if ln.startswith("working:")]
    assert "job s-policy, at stage policy_text" in line, line


def test_recovery_at_dashboard_start_names_the_policy_stage_on_the_card_and_in_the_record(
    tmp_home: Path, tmp_path: Path
) -> None:
    """The dashboard makes no set-up call any more: the reader completes the plan."""
    out = run_route("dashboard", tmp_home, tmp_path)
    [message] = out["LINE"]
    assert message.startswith("Interrupted at policy_text:"), message
    assert out["CARDSTAGE"] == ["policy_text"] and out["RECORD"] == ["policy_text"]


def test_the_nightlys_morning_note_names_the_policy_stage(tmp_home: Path, tmp_path: Path) -> None:
    out = run_route("nightly", tmp_home, tmp_path)
    [note] = out["LINE"]
    assert "demo stopped at policy_text." in note, note


def test_a_status_view_does_not_call_a_job_finished_before_its_final_gate(
    tmp_home: Path, tmp_path: Path
) -> None:
    out = run_route("views", tmp_home, tmp_path)
    assert out["STATE"] == ["open", "open"]              # still open with only `policy_final` to go


def test_a_plan_a_caller_put_in_the_table_is_never_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only an entry that is still this module's bare plan is completed: a stub plan stays as it was set."""
    from ns_studio import orchestrator

    monkeypatch.setitem(orchestrator.FORMAT_STAGES, "shorts_only", ["a", "b"])
    monkeypatch.setitem(orchestrator.FORMAT_STAGES, "clip", ["c"])
    assert orchestrator.plan_names("shorts_only", True) == ["a", "b"]
    assert orchestrator.plan_names("clip", True) == ["c"]
    assert orchestrator.FORMAT_STAGES["shorts_only"] == ["a", "b"]


def test_completing_a_bare_plan_twice_changes_nothing_and_no_reader_goes_around_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ns_studio import orchestrator

    bare = list(orchestrator.SHORTS_ONLY_STAGES)
    monkeypatch.setitem(orchestrator.FORMAT_STAGES, "shorts_only", list(bare))
    first = orchestrator.plan_names("shorts_only", True)
    assert first != bare and "policy_text" in first and "policy_final" in first
    assert orchestrator.plan_names("shorts_only", True) == first
    assert orchestrator.plan_names("shorts_only", False) == bare      # the policy off: the gates left out
    # every reader of a story plan outside the orchestrator and the modules that own a stage reads it
    # through `plan_names` / `stage_names` / `plan_job`: nothing else indexes the table by a story format
    owners = {"orchestrator.py", "gate.py", "stages.py", "stages_clip.py"}
    for folder in ("ns_studio", "ns_dashboard", "ns_supervisor"):
        for source in (ROOT / folder).rglob("*.py"):
            if source.name in owners:
                continue
            text = source.read_text(encoding="utf-8")
            for line in text.splitlines():
                code = line.split("#")[0]
                if "FORMAT_STAGES[" not in code and "FORMAT_STAGES.get(" not in code:
                    continue
                if code.lstrip().startswith(('"""', "`", "(`")) or "`FORMAT_STAGES" in code:
                    continue                                # a sentence of a docstring, not code
                allowed = ('FORMAT_STAGES["clip"]', 'FORMAT_STAGES.get("clip")', "FORMAT_STAGES.get(f)")
                assert any(a in code for a in allowed), f"{source.name}: {line.strip()}"


def test_the_route_scripts_are_plain_text() -> None:
    assert json.dumps(sorted(ROUTES)) == '["dashboard", "function", "nightly", "status", "views", "working"]'
