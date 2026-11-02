"""C1: the whole wizard in --simulate-downloads mode: full run, kill-and-resume, no-op rerun, subprocess."""

import json
import os
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from installer.stage2.wizard import main
from ns_common.config import load_channel, load_install_state, load_runtime
from ns_common.paths import Paths

ROOT = Path(__file__).resolve().parents[1]
SECRET = "e2e-secret-key-do-not-leak"
STEP_IDS = [
    "s00_welcome", "s01_questions", "s02_download_bins", "s03_download_models", "s04_venvs",
    "s05_opencode_prewarm", "s06_tune", "s07_configs", "s08_smoke", "s09_shortcuts_scheduler", "s10_summary",
]


def write_answers(tmp_path: Path, **over) -> Path:
    data = json.loads((ROOT / "installer" / "answers.example.json").read_text(encoding="utf-8"))
    data["home_path"] = str(tmp_path / "home")
    data["pexels_api_key"] = SECRET
    data.update(over)
    f = tmp_path / "answers.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    return f


def args(tmp_path: Path, answers: Path, *extra: str) -> list[str]:
    return [
        "--simulate-downloads", "--non-interactive", "--no-head-checks",
        "--home", str(tmp_path / "home"), "--answers", str(answers), "--app-path", str(ROOT),
        "--preflight", str(tmp_path / "no-preflight.json"), *extra,
    ]


def started_steps(home: Path) -> list[str]:
    out = []
    for f in sorted((home / "logs").glob("installer-*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec["event"] == "stage_start":
                out.append(rec["data"]["step"])
    return out


@pytest.fixture(autouse=True)
def _no_real_home(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("NIGHTSHIFT_HOME", raising=False)


def test_full_simulated_run(tmp_path: Path):
    answers = write_answers(tmp_path)
    assert main(args(tmp_path, answers)) == 0
    home = tmp_path / "home"

    state = load_install_state(Paths(home))
    assert [s for s in STEP_IDS if state.steps[s].status == "done"] == STEP_IDS
    assert state.completed_at and state.profile == "gpu" and state.app_path == str(ROOT)
    schema = json.loads((ROOT / "contracts" / "schemas" / "install_state.schema.json").read_text("utf-8"))
    jsonschema.validate(json.loads((home / "config" / "install_state.json").read_text("utf-8")), schema)

    assert (home / "bin" / "llama" / "llama-server.exe").exists()
    assert (home / "models" / "llm" / "Qwen3-14B-Q4_K_M.gguf").exists()
    assert (home / "venvs" / "studio" / "Scripts" / "python.exe").exists()
    assert not (home / "venvs" / "tts").exists()          # the pinned qwentts binary needs no PyTorch venv
    assert (home / "bin" / "qwentts" / ".ns-qwentts.done").exists()
    assert (home / "config" / "opencode.json").exists()
    assert list((home / "_simulated_desktop").glob("nightshift-diagnostics-*.zip"))


    p = Paths(home)
    rt = load_runtime(p)
    assert rt.tts.engine == "qwentts" and rt.llama.model_path.endswith("Qwen3-14B-Q4_K_M.gguf")
    assert load_channel(p, "after-dark-tales").niche == "horror fiction"


def test_secrets_never_reach_files_or_logs(tmp_path: Path):
    answers = write_answers(tmp_path)
    assert main(args(tmp_path, answers)) == 0
    home = tmp_path / "home"
    assert SECRET not in answers.read_text(encoding="utf-8")
    haystack = [p for p in home.rglob("*") if p.is_file() and p.suffix in (".json", ".jsonl", ".yaml")]
    assert haystack
    for f in haystack:
        assert SECRET not in f.read_text(encoding="utf-8", errors="replace"), f
    saved = json.loads((home / "config" / "answers.json").read_text(encoding="utf-8"))
    assert saved["pexels_api_key"] == "" and saved["pixabay_api_key"] == ""


def test_kill_after_step_three_then_resume_then_noop(tmp_path: Path):
    answers = write_answers(tmp_path)
    home = tmp_path / "home"
    assert main(args(tmp_path, answers, "--halt-after", "s03_download_models")) == 75

    st = load_install_state(Paths(home))
    assert sorted(st.steps) == STEP_IDS[:4] and all(s.status == "done" for s in st.steps.values())
    assert (home / "models" / "llm" / "Qwen3-14B-Q4_K_M.gguf").exists()
    assert not (home / "venvs" / "studio").exists()

    assert main(args(tmp_path, answers)) == 0
    started = started_steps(home)
    assert started.count("s03_download_models") == 1 and started.count("s02_download_bins") == 1
    assert started[-7:] == STEP_IDS[4:]
    assert load_install_state(Paths(home)).completed_at

    before = len(started)
    assert main(args(tmp_path, answers)) == 0
    again = started_steps(home)[before:]
    assert again == ["s10_summary"], "a finished install only shows the summary again"



def test_bad_answers_file_exits_2(tmp_path: Path):
    answers = tmp_path / "answers.json"
    answers.write_text("{ not json", encoding="utf-8")
    assert main(args(tmp_path, answers)) == 2


def test_wizard_runs_as_a_subprocess_the_way_stage1_starts_it(tmp_path: Path):
    answers = write_answers(tmp_path)
    env = {k: v for k, v in os.environ.items() if k != "NIGHTSHIFT_HOME"}
    env.update(PYTHONUTF8="1", PYTHONPATH=str(ROOT))
    cmd = [sys.executable, "-m", "installer.stage2.wizard", *args(tmp_path, answers)]
    proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=600,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Welcome to Nightshift" in proc.stdout
    st = json.loads((tmp_path / "home" / "config" / "install_state.json").read_text(encoding="utf-8"))
    assert [k for k, v in st["steps"].items() if v["status"] == "done"] == STEP_IDS
    assert SECRET not in proc.stdout + proc.stderr

    help_proc = subprocess.run([sys.executable, "-m", "installer.stage2", "--help"], cwd=ROOT, env=env,
                               capture_output=True, text=True, timeout=120, encoding="utf-8",
                               errors="replace")
    assert help_proc.returncode == 0 and "--simulate-downloads" in help_proc.stdout
