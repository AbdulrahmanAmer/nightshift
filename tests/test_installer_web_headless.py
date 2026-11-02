"""C1: with an answers.json and no window, the server runs the same steps and the wizard stays untouched."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from installer.stage2.wizard import main as wizard_main
from ns_common.config import load_install_state
from ns_common.paths import Paths

ROOT = Path(__file__).resolve().parents[1]
WIZARD_STEPS = [
    "s00_welcome", "s01_questions", "s02_download_bins", "s03_download_models", "s04_venvs",
    "s05_opencode_prewarm", "s06_tune", "s07_configs", "s08_smoke", "s09_shortcuts_scheduler", "s10_summary",
]


def write_answers(folder: Path, home: Path) -> Path:
    data = json.loads((ROOT / "installer" / "answers.example.json").read_text(encoding="utf-8"))
    data["home_path"] = str(home)
    data["pexels_api_key"] = "headless-test-key"
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / "answers.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    return f


def wizard_steps(home: Path) -> dict[str, str]:
    steps = load_install_state(Paths(home)).steps
    return {i: steps[i].status for i in WIZARD_STEPS if i in steps}


@pytest.fixture(autouse=True)
def _no_real_home(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("NIGHTSHIFT_HOME", raising=False)


def test_the_server_headless_gives_the_same_exit_code_and_steps_as_the_old_path(tmp_path: Path):
    old_home = tmp_path / "old" / "home"
    old_answers = write_answers(tmp_path / "old", old_home)
    assert wizard_main([
        "--simulate-downloads", "--non-interactive", "--no-head-checks", "--home", str(old_home),
        "--answers", str(old_answers), "--app-path", str(ROOT), "--preflight", str(tmp_path / "none.json"),
    ]) == 0

    new_home = tmp_path / "new" / "home"
    new_answers = write_answers(tmp_path / "new", new_home)
    env = {k: v for k, v in os.environ.items() if k != "NIGHTSHIFT_HOME"}
    env.update(LOCALAPPDATA=str(tmp_path / "local"), PYTHONPATH=str(ROOT), PYTHONUTF8="1")
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "installer.stage2.web", "--headless", "--simulate",
         "--app-path", str(ROOT), "--home", str(new_home), "--answers", str(new_answers)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    new_steps = wizard_steps(new_home)
    assert new_steps == wizard_steps(old_home) and set(new_steps) == set(WIZARD_STEPS)
    assert set(new_steps.values()) <= {"done", "skipped"}


def test_the_server_headless_stops_with_exit_10_when_a_check_blocks(tmp_path: Path):
    answers = write_answers(tmp_path / "a", tmp_path / "a" / "home")
    pf = tmp_path / "preflight.json"
    from installer.stage2.web.fakes import sample_handover

    data = sample_handover(ROOT, home=str(tmp_path / "a" / "home"))
    for row in data["checks"]:
        if row["id"] == "gpu":
            row.update(status="fail", code="NS-E102", value="nvidia-smi found no GPU")
    pf.write_text(json.dumps(data), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "NIGHTSHIFT_HOME"}
    env.update(LOCALAPPDATA=str(tmp_path / "local"), PYTHONPATH=str(ROOT), PYTHONUTF8="1")
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "installer.stage2.web", "--headless", "--simulate",
         "--app-path", str(ROOT), "--preflight", str(pf), "--home", str(tmp_path / "a" / "home"),
         "--answers", str(answers)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 10, proc.stdout + proc.stderr
    assert "[FAIL]" in proc.stdout and "NS-E102" in proc.stdout
    assert "No NVIDIA graphics card found" in proc.stdout
    assert not (tmp_path / "a" / "home" / "config" / "install_state.json").exists()      # nothing was started


def test_the_wizard_without_the_bridge_flag_is_the_console_wizard_still():
    from installer.stage2.wizard import parse_args

    args = parse_args(["--simulate-downloads"])
    assert args.ui_bridge is None
    from installer.stage2.steps import bridge_steps, default_steps

    assert [s.id for s in default_steps()] == WIZARD_STEPS
    assert [s.id for s in bridge_steps()][:2] == ["s01_questions", "s02_download_bins"]
    bridge_ids = [s.id for s in bridge_steps()]
    assert "v01_samples" in bridge_ids and "s00_welcome" not in bridge_ids
