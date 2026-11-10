"""The dashboard completes the stage table before it recovers interrupted work (review of D-stages).

The dashboard process never loads the stage modules. Its recovery at start reads the stage table to say which
stage an interrupted job had reached, and without the Short combos and the policy gates in it the Queue card
named the stage after a gate. The check runs in a child interpreter, because any earlier test in this process
may already have completed the table.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHILD = r"""
import sys
from pathlib import Path
from ns_common.paths import Paths
from ns_dashboard.handlers import build_state
from ns_dashboard.supervisor_link import SupervisorLink
from ns_studio import orchestrator

before = list(orchestrator.FORMAT_STAGES["master_first"])
link = SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
state = build_state(Paths.from_env(), fake_services=True, link=link)
state.worker.recover_studio()
after = list(orchestrator.FORMAT_STAGES["master_first"])
print("BEFORE", "short_combos" in before)
print("AFTER", "short_combos" in after)
"""


def test_recovery_at_dashboard_start_sees_the_complete_stage_table(tmp_home: Path, tmp_path: Path) -> None:
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    # the child must import THIS checkout, whatever copy the interpreter has installed
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    done = subprocess.run(  # noqa: S603 - our own interpreter and script
        [sys.executable, str(script)], capture_output=True, text=True, timeout=120, cwd=ROOT, env=env
    )
    assert done.returncode == 0, done.stderr[-2000:]
    marks = ("BEFORE", "AFTER")
    lines = dict(line.split() for line in done.stdout.splitlines() if line.startswith(marks))
    assert lines["BEFORE"] == "False", "the dashboard completed the table before recovery was asked for"
    assert lines["AFTER"] == "True", "recovery ran on a stage table without the Short combos"
