"""C1: installer/stage1.ps1 in -DryRun mode (real probes once, then fake probes row by row)."""

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

GOOD_SMI = "NVIDIA-SMI 572.16   Driver Version: 572.16   CUDA Version: 12.8"
DRIVES_OK = [{"letter": "C", "free_gb": 500, "type": "SSD"}]


def _run(
    *extra: str, app: Path = ROOT, cwd: Path = ROOT, dry: bool = True, window: bool = False
) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "NIGHTSHIFT_HOME"}
    if window:
        env.pop("NS_NONINTERACTIVE", None)      # no answers.json and no NS_NONINTERACTIVE: the window mode
    else:
        env["NS_NONINTERACTIVE"] = "1"
    cmd = [str(PS), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(STAGE1), "-AppPath", str(app)]
    if dry:
        cmd.append("-DryRun")
    cmd.extend(extra)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=cwd, env=env)


def _probe(tmp_path: Path, **over) -> Path:
    data = {"smi_text": GOOD_SMI, "drives": DRIVES_OK}
    data.update(over)
    f = tmp_path / "probe.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    return f


def _summary(proc: subprocess.CompletedProcess) -> dict:
    for line in proc.stdout.splitlines():
        if line.startswith("[dry-run] preflight-json: "):
            return json.loads(line.split("preflight-json: ", 1)[1])
    raise AssertionError("no preflight-json line in:\n" + proc.stdout + proc.stderr)


def _row(summary: dict, row_id: str) -> dict:
    return next(r for r in summary["checks"] if r["id"] == row_id)


def test_real_dry_run_exits_zero_and_lists_actions():
    proc = _run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    assert "[dry-run] use NIGHTSHIFT_HOME" in out
    assert "[dry-run] start setup server" in out and "installer.stage2.web" in out
    assert "--headless" in out                  # NS_NONINTERACTIVE=1 selects the unattended path
    assert "set user environment variable" not in out and " venv --python " not in out   # moved to the server
    assert "Dry run finished" in out
    assert _summary(proc)["schema"] == 1


def test_all_green_fake_probe(tmp_path: Path):
    proc = _run("-FakeProbe", str(_probe(tmp_path)))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    s = _summary(proc)
    assert [r["status"] for r in s["checks"]] == ["ok"] * len(s["checks"])
    assert {r["id"] for r in s["checks"]} >= {
        "os", "gpu", "driver", "ram", "disk", "disktype", "network", "winget", "longpaths", "waketimers",
    }
    assert s["profile"] == "gpu" and s["llama_pair"] == "cuda12" and s["cuda"] == "12.8"
    assert s["home"] == "C:\\Nightshift"
    assert "NS-E" not in proc.stdout


@pytest.mark.parametrize(
    ("over", "row", "status", "code"),
    [
        ({"os_arch": "ARM64"}, "os", "fail", "NS-E101"),
        ({"nvidia_smi": False, "gpu_name": "", "smi_text": ""}, "gpu", "fail", "NS-E102"),
        ({"vram_mb": 8192}, "gpu", "warn", ""),
        ({"smi_text": "CUDA Version: 12.2"}, "driver", "fail", "NS-E103"),
        ({"smi_text": "CUDA Version: 12.4"}, "driver", "warn", "NS-E103"),
        ({"smi_text": "CUDA Version: 13.0"}, "driver", "ok", ""),
        ({"smi_text": "no cuda line here"}, "driver", "fail", "NS-E103"),
        ({"ram_gb": 8}, "ram", "warn", ""),
        ({"drives": [{"letter": "C", "free_gb": 30, "type": "SSD"}]}, "disk", "fail", "NS-E104"),
        ({"drives": [{"letter": "C", "free_gb": 50, "type": "SSD"}]}, "disk", "warn", ""),
        ({"drives": [{"letter": "C", "free_gb": 500, "type": "HDD"}]}, "disktype", "warn", "NS-W106"),
        ({"unreachable": ["huggingface.co"]}, "network", "warn", "NS-E105"),
        ({"unreachable": ["github.com"]}, "network", "fail", "NS-E105"),
        ({"unreachable": ["huggingface.co", "pypi.org"]}, "network", "fail", "NS-E105"),
        ({"winget": False, "tools": {"git": False}}, "winget", "fail", "NS-E111"),
        ({"winget": False}, "winget", "ok", ""),
        ({"long_paths": False}, "longpaths", "warn", "NS-W112"),
        ({"wake_timers": False}, "waketimers", "warn", "NS-W107"),
    ],
)
def test_preflight_rows(tmp_path: Path, over: dict, row: str, status: str, code: str):
    proc = _run("-FakeProbe", str(_probe(tmp_path, **over)))
    assert proc.returncode == 0, proc.stdout + proc.stderr   # a dry run never blocks
    r = _row(_summary(proc), row)
    assert (r["status"], r["code"]) == (status, code), r
    if status == "fail":
        assert "would block" in proc.stdout
        assert code in proc.stdout
    if code:
        assert r["hint"], "every coded row carries a fix hint"


@pytest.mark.parametrize(
    ("smi", "pair", "cuda"),
    [("CUDA Version: 13.0", "cuda13", "13.0"), ("CUDA Version: 13.4", "cuda13", "13.4"),
     ("CUDA Version: 12.8", "cuda12", "12.8"), ("CUDA Version: 12.4", "cuda12", "12.4"),
     ("CUDA Version: 12.2", "", "12.2")],
)
def test_llama_pair_follows_cuda(tmp_path: Path, smi: str, pair: str, cuda: str):
    s = _summary(_run("-FakeProbe", str(_probe(tmp_path, smi_text=smi))))
    assert s["llama_pair"] == pair and s["cuda"] == cuda


def test_default_home_is_largest_free_ssd(tmp_path: Path):
    drives = [
        {"letter": "C", "free_gb": 100, "type": "SSD"},
        {"letter": "D", "free_gb": 900, "type": "HDD"},
        {"letter": "E", "free_gb": 300, "type": "SSD"},
    ]
    proc = _run("-FakeProbe", str(_probe(tmp_path, drives=drives)))
    assert _summary(proc)["home"] == "E:\\Nightshift"
    assert "[dry-run] use NIGHTSHIFT_HOME = E:\\Nightshift" in proc.stdout


def test_default_home_skips_ssd_that_is_too_small(tmp_path: Path):
    drives = [{"letter": "C", "free_gb": 30, "type": "SSD"}, {"letter": "D", "free_gb": 900, "type": "HDD"}]
    assert _summary(_run("-FakeProbe", str(_probe(tmp_path, drives=drives))))["home"] == "D:\\Nightshift"


def test_answers_home_path_wins_and_means_unattended(tmp_path: Path):
    drives = DRIVES_OK + [{"letter": "F", "free_gb": 200, "type": "SSD"}]
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"home_path": "F:\\Studio", "allow_cpu": False}), encoding="utf-8")
    proc = _run("-FakeProbe", str(_probe(tmp_path, drives=drives)), "-Answers", str(answers))
    assert "unattended mode" in proc.stdout
    assert _summary(proc)["home"] == "F:\\Studio"
    assert "--answers" in proc.stdout


def test_broken_answers_file_exits_11(tmp_path: Path):
    answers = tmp_path / "answers.json"
    answers.write_text("{ not json", encoding="utf-8")
    proc = _run("-FakeProbe", str(_probe(tmp_path)), "-Answers", str(answers))
    assert proc.returncode == 11


def test_allow_cpu_downgrades_missing_gpu_to_warning(tmp_path: Path):
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"allow_cpu": True}), encoding="utf-8")
    probe = _probe(tmp_path, nvidia_smi=False, gpu_name="", smi_text="")
    s = _summary(_run("-FakeProbe", str(probe), "-Answers", str(answers)))
    assert _row(s, "gpu")["status"] == "warn" and _row(s, "driver")["status"] == "warn"
    assert s["profile"] == "cpu-dev"


def test_dry_run_changes_nothing(tmp_path: Path):
    before = sorted(p.name for p in Path(os.environ.get("TEMP", str(tmp_path))).glob("nightshift-*"))
    _run("-FakeProbe", str(_probe(tmp_path)))
    after = sorted(p.name for p in Path(os.environ.get("TEMP", str(tmp_path))).glob("nightshift-*"))
    assert before == after


def test_fake_probe_requires_dry_run(tmp_path: Path):
    empty_app = tmp_path / "empty"
    empty_app.mkdir()
    proc = _run("-FakeProbe", str(_probe(tmp_path)), app=empty_app, dry=False)
    assert proc.returncode != 0
    assert "only be used together with -DryRun" in proc.stdout + proc.stderr


def test_missing_manifest_is_reported(tmp_path: Path):
    empty_app = tmp_path / "empty"
    empty_app.mkdir()
    proc = _run("-FakeProbe", str(_probe(tmp_path)), app=empty_app)
    assert proc.returncode == 11
    assert "manifest.json not found" in proc.stdout


def test_probe_only_writes_the_hand_over_json_and_nothing_else(tmp_path: Path):
    out = tmp_path / "sub" / "handover.json"
    proc = _run("-FakeProbe", str(_probe(tmp_path)), "-ProbeOnly", "-Out", str(out), "-HomePath", "D:\Studio",
                dry=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(out.read_text(encoding="utf-8-sig"))
    assert data["schema"] == 1 and data["home"] == "D:\Studio"
    assert {"is_admin", "os_caption", "os_version", "tools", "unreachable", "checks"} <= set(data)
    assert [r["id"] for r in data["checks"]][:3] == ["os", "gpu", "driver"]
    assert "Nightshift setup" not in proc.stdout                     # no splash


def test_probe_only_needs_out():
    proc = _run("-ProbeOnly", dry=False)
    assert proc.returncode != 0 and "-Out" in proc.stdout + proc.stderr


def test_answers_json_and_no_ui_select_headless(tmp_path: Path):
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"home_path": "C:\Studio"}), encoding="utf-8")
    probe = str(_probe(tmp_path))
    assert "--headless" in _run("-FakeProbe", probe, "-Answers", str(answers), window=True).stdout
    assert "--headless" in _run("-FakeProbe", probe, "-NoUi", window=True).stdout
    with_ui = _run("-FakeProbe", probe, "-Answers", str(answers), "-Ui", window=True).stdout
    assert "--headless" not in with_ui and "--answers" in with_ui     # -Ui: the window, answers prefilled


def test_window_mode_prints_the_splash_and_starts_the_server(tmp_path: Path):
    proc = _run("-FakeProbe", str(_probe(tmp_path)), window=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    assert "N I G H T S H I F T" in out and "setup 0." in out
    assert "[ ok ] This PC" in out
    assert "Windows 11 Pro (fake), NVIDIA GeForce RTX 5060 Ti (16 GB), 32 GB memory" in out
    assert "[dry-run] start setup server" in out and "--headless" not in out


def test_window_mode_stops_on_an_unsupported_windows(tmp_path: Path):
    proc = _run("-FakeProbe", str(_probe(tmp_path, os_arch="ARM64")), window=True)
    assert proc.returncode == 10
    assert "NS-E101" in proc.stdout and "[FAIL] This PC" in proc.stdout
