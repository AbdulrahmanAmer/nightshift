# ruff: noqa: E501
"""C1: `INSTALL.cmd --dev` in stage 1 (docs/DEV_MODE.md): the checks a PC with no NVIDIA card does not need.

Same method as tests/test_installer_stage1.py: the real stage1.ps1 in -DryRun mode against fake probes.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STAGE1 = ROOT / "installer" / "stage1.ps1"
PS = shutil.which("powershell") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(PS is None, reason="PowerShell not available")

LAPTOP = {"nvidia_smi": False, "gpu_name": "", "smi_text": "", "driver": "", "vram_mb": 0, "ram_gb": 8,
          "drives": [{"letter": "C", "free_gb": 30, "type": "SSD"}]}


def run(tmp_path: Path, probe: dict, *extra: str, env_dev: bool = False) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("NIGHTSHIFT_HOME", "NIGHTSHIFT_DEV_MODE")}
    env["NS_NONINTERACTIVE"] = "1"
    if env_dev:
        env["NIGHTSHIFT_DEV_MODE"] = "1"
    f = tmp_path / "probe.json"
    f.write_text(json.dumps(probe), encoding="utf-8")
    cmd = [str(PS), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(STAGE1), "-AppPath", str(ROOT),
           "-DryRun", "-FakeProbe", str(f), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=ROOT, env=env)


def summary(proc: subprocess.CompletedProcess) -> dict:
    for line in proc.stdout.splitlines():
        if line.startswith("[dry-run] preflight-json: "):
            return json.loads(line.split("preflight-json: ", 1)[1])
    raise AssertionError("no preflight-json line in:\n" + proc.stdout + proc.stderr)


def row(s: dict, row_id: str) -> dict:
    return next(r for r in s["checks"] if r["id"] == row_id)


def test_install_cmd_passes_its_arguments_on_so_dev_reaches_stage_1():
    text = (ROOT / "INSTALL.cmd").read_text(encoding="utf-8")
    assert "%*" in text            # `INSTALL.cmd --dev` is `stage1.ps1 ... --dev`


def test_a_laptop_with_no_card_and_8_gb_passes_every_row_in_dev_mode(tmp_path: Path):
    proc = run(tmp_path, LAPTOP, "-Dev")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    s = summary(proc)
    assert [r["status"] for r in s["checks"]] == ["ok"] * len(s["checks"]), s["checks"]
    assert "dev mode" in row(s, "gpu")["value"] and "dev mode" in row(s, "driver")["value"]
    assert s["profile"] == "cpu-dev" and s["dev_mode"] is True and s["llama_pair"] == ""
    assert "would block" not in proc.stdout and "NS-E10" not in proc.stdout
    assert "DEV MODE: fake engines, no models" in proc.stdout


def test_the_same_laptop_is_refused_without_dev(tmp_path: Path):
    proc = run(tmp_path, LAPTOP)
    s = summary(proc)
    assert (row(s, "gpu")["status"], row(s, "gpu")["code"]) == ("fail", "NS-E102")
    assert row(s, "disk")["status"] == "ok"          # 30 GB: stage 2 works out what the install needs
    assert row(s, "ram")["status"] == "warn"
    assert s["dev_mode"] is False


def test_dev_mode_with_the_environment_variable_equals_the_flag(tmp_path: Path):
    s = summary(run(tmp_path, LAPTOP, env_dev=True))
    assert s["dev_mode"] is True and s["profile"] == "cpu-dev"


def test_dev_mode_ignores_a_real_card_and_still_uses_the_cpu_profile(tmp_path: Path):
    s = summary(run(tmp_path, {"smi_text": "CUDA Version: 13.0"}, "-Dev"))
    assert s["profile"] == "cpu-dev" and s["llama_pair"] == "" and s["dev_mode"] is True
    assert row(s, "gpu")["status"] == "ok"


@pytest.mark.parametrize(
    ("free", "status"), [(9, "fail"), (10, "ok"), (12, "ok")]
)
def test_dev_mode_needs_10_gb_of_disk(tmp_path: Path, free: float, status: str):
    s = summary(run(tmp_path, {**LAPTOP, "drives": [{"letter": "C", "free_gb": free, "type": "SSD"}]}, "-Dev"))
    r = row(s, "disk")
    assert r["status"] == status and (r["code"] == "NS-E104") == (status == "fail")


def test_dev_mode_memory_row_warns_only_under_4_gb(tmp_path: Path):
    assert row(summary(run(tmp_path, {**LAPTOP, "ram_gb": 4}, "-Dev")), "ram")["status"] == "ok"
    assert row(summary(run(tmp_path, {**LAPTOP, "ram_gb": 3}, "-Dev")), "ram")["status"] == "warn"


def test_the_setup_server_is_started_with_dev(tmp_path: Path):
    out = run(tmp_path, LAPTOP, "-Dev").stdout
    line = next(ln for ln in out.splitlines() if ln.startswith("[dry-run] start setup server"))
    assert line.endswith("--dev") or " --dev" in line
    plain = next(ln for ln in run(tmp_path, LAPTOP).stdout.splitlines()
                 if ln.startswith("[dry-run] start setup server"))
    assert "--dev" not in plain


def test_other_rows_are_still_checked_in_dev_mode(tmp_path: Path):
    s = summary(run(tmp_path, {**LAPTOP, "unreachable": ["github.com"], "long_paths": False}, "-Dev"))
    assert (row(s, "network")["status"], row(s, "longpaths")["status"]) == ("fail", "warn")
