"""B-flaky: tests/test_uv_resolver.py gives the same answer on a PC that already has a stage-1 uv.

The laptop has `%LOCALAPPDATA%/Nightshift/bootstrap/uv/uv.exe`, a runner has none. Two tests of that file
asked the real environment (`find_uv` without `env`, `ctx.uv()`), so they failed here and passed in CI. The
test runs that file in a child pytest on a PC that has the stage-1 uv, with the hand-over variable set too.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_resolver_tests_pass_on_a_pc_that_has_a_stage1_uv(tmp_path: Path) -> None:
    local = tmp_path / "local"
    planted = local / "Nightshift" / "bootstrap" / "uv" / "uv.exe"
    planted.parent.mkdir(parents=True)
    planted.write_bytes(b"MZ")
    env = dict(os.environ, LOCALAPPDATA=str(local), NIGHTSHIFT_BOOTSTRAP_UV=str(planted))
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_uv_resolver.py"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-1000:]
