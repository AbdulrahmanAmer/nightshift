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

GOOD_SMI = "NVIDIA-SMI 581.57   Driver Version: 581.57   CUDA Version: 13.0"
ADA = "NVIDIA GeForce RTX 4070"   # runs the CUDA 12.4 build; the fake default card is an RTX 5060 Ti
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
        "os", "gpu", "driver", "ram", "disk", "disktype", "network", "winget", "vcruntime", "longpaths",
        "waketimers",
    }
    assert s["profile"] == "gpu" and s["llama_pair"] == "cuda13" and s["cuda"] == "13.0"
    assert s["home"] == "C:\\Nightshift"
    assert "NS-E" not in proc.stdout


@pytest.mark.parametrize(
    ("over", "row", "status", "code"),
    [
        ({"os_arch": "ARM64"}, "os", "fail", "NS-E101"),
        ({"nvidia_smi": False, "gpu_name": "", "smi_text": ""}, "gpu", "fail", "NS-E102"),
        ({"vram_mb": 8192}, "gpu", "warn", ""),
        ({"smi_text": "CUDA Version: 12.2"}, "driver", "fail", "NS-E103"),
        ({"smi_text": "CUDA Version: 12.4"}, "driver", "fail", "NS-E103"),   # the fake card is an RTX 5060 Ti
        ({"smi_text": "CUDA Version: 12.8"}, "driver", "fail", "NS-E103"),   # the 12.4 build cannot run on it
        ({"smi_text": "CUDA Version: 12.4", "gpu_name": ADA}, "driver", "ok", ""),
        ({"smi_text": "CUDA Version: 13.0"}, "driver", "ok", ""),
        # second source: the driver version, mapped to CUDA (banner missing or unreadable)
        ({"smi_text": "no cuda line here", "gpu_name": ADA}, "driver", "ok", ""),  # 572.16 = 12.8
        ({"smi_text": "", "driver": "531.18"}, "driver", "fail", "NS-E103"),          # CUDA 12.1
        ({"smi_text": "", "driver": "566.14"}, "driver", "fail", "NS-E103"),   # CUDA 12.6 on an RTX 50 card
        ({"smi_text": "", "driver": "566.14", "gpu_name": ADA}, "driver", "ok", ""),
        ({"smi_text": "", "driver": "581.57"}, "driver", "ok", ""),
        # nothing readable: a warning, never a failure
        ({"smi_text": "", "driver": ""}, "driver", "warn", "NS-W115"),
        ({"nvidia_smi": False, "smi_text": "", "driver": "581.57"}, "driver", "ok", ""),
        ({"nvidia_smi": False, "smi_text": "", "driver": ""}, "driver", "warn", "NS-W115"),
        ({"vram_mb": 0}, "gpu", "warn", ""),                                          # memory size not read
        ({"ram_gb": 8}, "ram", "warn", ""),
        ({"drives": [{"letter": "C", "free_gb": 30, "type": "SSD"}]}, "disk", "fail", "NS-E104"),
        ({"drives": [{"letter": "C", "free_gb": 50, "type": "SSD"}]}, "disk", "warn", ""),
        ({"drives": [{"letter": "C", "free_gb": 500, "type": "HDD"}]}, "disktype", "warn", "NS-W106"),
        ({"unreachable": ["huggingface.co"]}, "network", "warn", "NS-E105"),
        ({"unreachable": ["github.com"]}, "network", "fail", "NS-E105"),
        ({"unreachable": ["huggingface.co", "pypi.org"]}, "network", "fail", "NS-E105"),
        # winget is a first choice only: FFmpeg and the Visual C++ runtime have a pinned download behind it
        ({"winget": False, "tools": {"ffmpeg": False}}, "winget", "warn", ""),
        ({"winget": False, "vc_runtime": False}, "winget", "warn", ""),
        ({"winget": False}, "winget", "ok", ""),
        ({"vc_runtime": False}, "vcruntime", "warn", ""),
        ({"vc_runtime": True}, "vcruntime", "ok", ""),
        ({"long_paths": False}, "longpaths", "warn", "NS-W112"),
        ({"wake_timers": False}, "waketimers", "warn", "NS-W107"),
        # Allow wake timers: 0 = Disable, 1 = Enable, 2 = Important wake timers only (not enough for the task)
        ({"wake_timers": 0}, "waketimers", "warn", "NS-W107"),
        ({"wake_timers": 1}, "waketimers", "ok", ""),
        ({"wake_timers": 2}, "waketimers", "warn", "NS-W107"),
        ({"wake_timers": True}, "waketimers", "ok", ""),
        ({"wake_timers": None}, "waketimers", "ok", ""),
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
    ("smi", "gpu", "pair", "cuda"),
    [("CUDA Version: 13.0", "NVIDIA GeForce RTX 5060 Ti", "cuda13", "13.0"),
     ("CUDA Version: 13.4", "NVIDIA GeForce RTX 5060 Ti", "cuda13", "13.4"),
     ("CUDA Version: 12.8", "NVIDIA GeForce RTX 4070", "cuda12", "12.8"),
     ("CUDA Version: 12.4", "NVIDIA GeForce RTX 4070", "cuda12", "12.4"),
     ("CUDA Version: 12.8", "NVIDIA GeForce RTX 5060 Ti", "", "12.8"),     # Blackwell: 12.x stops setup
     ("CUDA Version: 12.2", "NVIDIA GeForce RTX 4070", "", "12.2")],
)
def test_llama_pair_follows_cuda(tmp_path: Path, smi: str, gpu: str, pair: str, cuda: str):
    s = _summary(_run("-FakeProbe", str(_probe(tmp_path, smi_text=smi, gpu_name=gpu))))
    assert s["llama_pair"] == pair and s["cuda"] == cuda


@pytest.mark.parametrize(
    ("over", "pair", "cuda", "source"),
    [
        ({"smi_text": "CUDA Version: 12.8", "gpu_name": ADA}, "cuda12", "12.8", "nvidia-smi"),
        ({"smi_text": "", "driver": "572.16", "gpu_name": ADA}, "cuda12", "12.8", "driver"),
        ({"smi_text": "NVIDIA-SMI 581.57  Treiberversion  CUDA-Version: 13.0", "driver": "581.57"},
         "cuda13", "13.0", "driver"),
        ({"smi_text": "", "driver": "551.23", "gpu_name": ADA}, "cuda12", "12.4", "driver"),
        ({"smi_text": "", "driver": "531.18"}, "", "12.1", "driver"),
        ({"smi_text": "", "driver": "", "gpu_name": ADA}, "cuda12", "", "none"),  # unreadable: 12.4 build
        ({"smi_text": "", "driver": ""}, "cuda13", "", "none"),  # unreadable, RTX 50: CUDA 13
    ],
)
def test_the_hand_over_names_the_cuda_source_and_the_pair(tmp_path: Path, over: dict, pair: str, cuda: str,
                                                          source: str):
    s = _summary(_run("-FakeProbe", str(_probe(tmp_path, **over))))
    assert (s["llama_pair"], s["cuda"], s["cuda_source"]) == (pair, cuda, source)
    assert s["profile"] == "gpu"


def test_the_full_nvidia_smi_text_of_an_rtx_5060_ti_picks_the_cuda13_pair(tmp_path: Path):
    """The whole banner nvidia-smi prints (driver 580.88, CUDA 13.0, 16311 MiB), not just its first line."""
    fixture = ROOT / "tests" / "fixtures" / "supervisor" / "nvidia-smi-rtx-5060-ti.txt"
    smi = fixture.read_text(encoding="utf-8")
    s = _summary(_run("-FakeProbe", str(_probe(tmp_path, smi_text=smi, driver="580.88", vram_mb=16311))))
    got = (s["profile"], s["llama_pair"], s["cuda"], s["cuda_source"])
    assert got == ("gpu", "cuda13", "13.0", "nvidia-smi")
    assert [r["status"] for r in s["checks"] if r["id"] in ("gpu", "driver")] == ["ok", "ok"]


def test_an_unreadable_driver_names_the_build_that_will_really_be_used(tmp_path: Path):
    blackwell = _summary(_run("-FakeProbe", str(_probe(tmp_path, smi_text="", driver=""))))
    assert blackwell["llama_pair"] == "cuda13"
    assert "CUDA 13 build" in _row(blackwell, "driver")["value"]
    ada = _summary(_run("-FakeProbe", str(_probe(tmp_path, smi_text="", driver="", gpu_name=ADA))))
    assert ada["llama_pair"] == "cuda12"
    assert "CUDA 12.4 build" in _row(ada, "driver")["value"]


def test_a_pc_without_any_nvidia_card_fails_the_gpu_row_only(tmp_path: Path):
    probe = _probe(tmp_path, nvidia_smi=False, gpu_name="", smi_text="", driver="")
    proc = _run("-FakeProbe", str(probe))
    s = _summary(proc)
    assert (_row(s, "gpu")["status"], _row(s, "gpu")["code"]) == ("fail", "NS-E102")
    assert (_row(s, "driver")["status"], _row(s, "driver")["code"]) == ("warn", "")    # not a second red row
    assert "NS-E103" not in proc.stdout and s["profile"] == "cpu-dev"


def test_the_driver_row_has_one_label_for_every_state(tmp_path: Path):
    labels = {_row(_summary(_run("-FakeProbe", str(_probe(tmp_path, **over)))), "driver")["label"]
              for over in ({}, {"smi_text": "CUDA Version: 12.2"}, {"smi_text": "", "driver": ""})}
    assert labels == {"NVIDIA driver (CUDA 12.4 or newer; 13.0+ for RTX 50-series)"}


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
    # G: because a checkout under D:\NightShift has the extracted program in D:\Nightshift (the same folder)
    drives = [{"letter": "C", "free_gb": 30, "type": "SSD"}, {"letter": "G", "free_gb": 900, "type": "HDD"}]
    assert _summary(_run("-FakeProbe", str(_probe(tmp_path, drives=drives))))["home"] == "G:\\Nightshift"


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
    proc = _run("-FakeProbe", str(_probe(tmp_path)), "-ProbeOnly", "-Out", str(out), "-HomePath",
                "D:\\Studio", dry=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(out.read_text(encoding="utf-8-sig"))
    assert data["schema"] == 1 and data["home"] == "D:\\Studio"
    assert {"is_admin", "os_caption", "os_version", "tools", "unreachable", "checks"} <= set(data)
    assert [r["id"] for r in data["checks"]][:3] == ["os", "gpu", "driver"]
    assert "Nightshift setup" not in proc.stdout                     # no splash


def test_probe_only_needs_out():
    proc = _run("-ProbeOnly", dry=False)
    assert proc.returncode != 0 and "-Out" in proc.stdout + proc.stderr


def test_answers_json_and_no_ui_select_headless(tmp_path: Path):
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"home_path": "C:\\Studio"}), encoding="utf-8")
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


def test_stage1_hands_the_uv_path_to_the_setup_server():
    proc = _run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "NIGHTSHIFT_BOOTSTRAP_UV=" in proc.stdout


PIN = "0.13.6"


def _install_uv_harness(
    tmp_path: Path,
    winget: str,
    path_uv: str | None = None,
    pin: str | None = PIN,
    shadow_uv: str | None = None,
) -> subprocess.CompletedProcess:
    """Run the real Install-Uv from stage1.ps1 with a dead download URL and a chosen 'winget'.

    ``path_uv`` is the version the uv on PATH reports after winget ran (None: there is none); ``pin`` is
    ``bins.uv.version`` of the manifest (None: the manifest has no ``version`` property at all);
    ``shadow_uv`` is a uv of another version that sits earlier on PATH. The winget stub writes its arguments
    to ``winget-args.txt`` next to the harness. The harness runs under the same StrictMode as stage 1.
    """
    args_file = tmp_path / "winget-args.txt"
    stubs = {
        "none": "function Test-Tool { param($Name) return $false }",
        "broken": "function Test-Tool { param($Name) return $true }\n"
                  "function winget.exe { throw 'winget exploded' }",
        "ok": "function Test-Tool { param($Name) return $true }\n"
              f"function winget.exe {{ Set-Content -LiteralPath '{args_file}' -Value ($args -join ' ') }}",
    }[winget]
    entries = []
    if shadow_uv is not None:
        entries.append("[pscustomobject]@{ Source = 'C:/fake/Shadow/uv.exe' }")
    if path_uv is not None:
        entries.append("[pscustomobject]@{ Source = 'C:/fake/Links/uv.exe' }")
    on_path = "@(" + ", ".join(entries) + ")"
    versions = {"C:/fake/Shadow/uv.exe": shadow_uv or "?", "C:/fake/Links/uv.exe": path_uv or "?"}
    version_map = "@{ " + "; ".join(f"'{k}' = '{v}'" for k, v in versions.items()) + " }"
    version = "" if pin is None else f"; version = '{pin}'"
    script = f"""
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{STAGE1}', [ref]$null, [ref]$null)
$fnType = [System.Management.Automation.Language.FunctionDefinitionAst]
foreach ($name in 'Get-Prop', 'Install-Uv') {{
    $fn = $ast.FindAll({{ param($n) $n -is $fnType -and $n.Name -eq $name }}, $true)[0]
    . ([scriptblock]::Create($fn.Extent.Text))
}}
{stubs}
function Get-Command {{ [CmdletBinding()] param([string]$Name, [switch]$All) return {on_path} }}
$uvVersions = {version_map}
function Get-UvVersion {{ param([string]$Uv) return $uvVersions[$Uv] }}
function Get-DataDir {{ return '{tmp_path}' }}
function Join-Str {{ param($a, $b) return (Join-Path $a $b) }}
function Update-SessionPath {{ }}
function Set-Status {{ param($Index, $Tag, $Text) }}
function Stop-Setup {{ param($Code, $Detail, $ExitCode) Write-Output "STOP:$Code|$Detail"; exit 13 }}
$script:IsDry = $false
$uv = [pscustomobject]@{{ url = 'http://127.0.0.1:9/uv.zip'; sha256 = '0'{version} }}
$manifest = [pscustomobject]@{{ bins = [pscustomobject]@{{ uv = $uv }} }}
Write-Output ('RESULT:' + (Install-Uv -Manifest $manifest))
"""
    f = tmp_path / "harness.ps1"
    f.write_text(script, encoding="utf-8")
    return subprocess.run([str(PS), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(f)],
                          capture_output=True, text=True, timeout=120)


def test_uv_download_failure_without_winget_is_a_clear_error_not_a_raw_powershell_exception(tmp_path: Path):
    proc = _install_uv_harness(tmp_path, "none")
    assert proc.returncode == 13, proc.stdout + proc.stderr
    assert "STOP:NS-E105" in proc.stdout and "winget is not available" in proc.stdout
    assert "CommandNotFoundException" not in proc.stderr


def test_uv_download_failure_with_a_failing_winget_is_ns_e110(tmp_path: Path):
    proc = _install_uv_harness(tmp_path, "broken")
    assert proc.returncode == 13, proc.stdout + proc.stderr
    assert "STOP:NS-E110" in proc.stdout


def test_uv_fallback_asks_winget_for_the_pinned_version(tmp_path: Path):
    _install_uv_harness(tmp_path, "ok", path_uv=PIN)
    args = (tmp_path / "winget-args.txt").read_text(encoding="utf-8")
    assert "--id astral-sh.uv" in args and f"--version {PIN}" in args


def test_uv_fallback_refuses_a_uv_of_another_version_that_stage_2_would_refuse(tmp_path: Path):
    """D-19: stage 1 and stage 2 agree. winget's latest (or a PATH uv) of another version stops setup here
    with the pin named, not late in b02 after Python and the VC++ prompt."""
    proc = _install_uv_harness(tmp_path, "ok", path_uv="0.13.7")
    assert proc.returncode == 13, proc.stdout + proc.stderr
    assert "STOP:NS-E105" in proc.stdout and "RESULT:" not in proc.stdout
    assert "0.13.7" in proc.stdout and PIN in proc.stdout and "http://127.0.0.1:9/uv.zip" in proc.stdout


def test_uv_fallback_accepts_a_uv_of_the_pinned_version(tmp_path: Path):
    proc = _install_uv_harness(tmp_path, "ok", path_uv=PIN)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "RESULT:C:/fake/Links/uv.exe" in proc.stdout


def test_uv_fallback_takes_the_pinned_uv_that_an_older_uv_earlier_on_path_hides(tmp_path: Path):
    """winget installed the pin, but a cargo or pip uv comes first on PATH: the pin is the one returned."""
    proc = _install_uv_harness(tmp_path, "ok", path_uv=PIN, shadow_uv="0.9.0")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "RESULT:C:/fake/Links/uv.exe" in proc.stdout


def test_uv_fallback_without_a_manifest_version_takes_the_uv_that_runs(tmp_path: Path):
    proc = _install_uv_harness(tmp_path, "ok", path_uv="0.13.7", pin=None)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "RESULT:C:/fake/Links/uv.exe" in proc.stdout
    assert "--version" not in (tmp_path / "winget-args.txt").read_text(encoding="utf-8")


def test_select_default_home_avoids_the_extracted_folder_and_treats_a_root_specially(tmp_path: Path):
    ps = tmp_path / "pick.ps1"
    lines = [
        "$ErrorActionPreference = 'Stop'",
        f"$text = Get-Content -LiteralPath '{STAGE1}' -Raw",
        "$start = $text.IndexOf('function Test-PathInside')",
        "$end = $text.IndexOf('# Machine probes')",
        "$script:MinFreeGbRecommended = 80",
        "Invoke-Expression $text.Substring($start, $end - $start)",
        "$drives = @([pscustomobject]@{ letter = 'D'; free_gb = 900; type = 'SSD' },",
        "            [pscustomobject]@{ letter = 'E'; free_gb = 400; type = 'SSD' })",
        r"foreach ($app in @('D:', 'D:\', 'D:\Nightshift', 'D:\Nightshift-home', 'E:\Nightshift')) {",
        "    '{0} => {1}' -f $app, (Select-DefaultHome -Drives $drives -AppFolder $app -IsTaken { $false })",
        "}",
    ]
    ps.write_text("\n".join(lines) + "\n", encoding="utf-8")
    proc = subprocess.run([str(PS), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    got = dict(line.split(" => ", 1) for line in proc.stdout.splitlines() if " => " in line)
    assert got["D:"] == r"D:\Nightshift" and got["D:" + chr(92)] == r"D:\Nightshift"   # a root: only itself
    assert got[r"D:\Nightshift"] == r"D:\Nightshift-home"          # the default clashes: the next name
    assert got[r"D:\Nightshift-home"] == r"D:\Nightshift"
    assert got[r"E:\Nightshift"] == r"D:\Nightshift"


# ---------------------------------------------------------------------------------- Visual C++ runtime
def test_a_missing_runtime_is_installed_by_stage_1_with_the_pinned_download(tmp_path: Path):
    proc = _run("-FakeProbe", str(_probe(tmp_path, vc_runtime=False)))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    pin = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))["bins"]["vcredist"]
    line = next(x for x in proc.stdout.splitlines() if "install the Visual C++ runtime" in x)
    assert pin["url"] in line and pin["sha256"] in line
    assert "-Verb RunAs" in line and "/install /quiet /norestart" in line
    # it comes before the setup server starts, so the permission prompt appears in front of the console
    assert proc.stdout.index("install the Visual C++ runtime") < proc.stdout.index("start setup server")


def test_an_installed_runtime_is_left_alone(tmp_path: Path):
    proc = _run("-FakeProbe", str(_probe(tmp_path, vc_runtime=True)))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Visual C++ runtime:" not in proc.stdout and "install the Visual C++ runtime" not in proc.stdout


def test_the_runtime_installer_is_ps51_compatible_and_says_what_will_happen():
    text = STAGE1.read_text(encoding="utf-8")
    body = text[text.index("function Install-VcRuntime"):text.index("function Get-UvVersion")]
    assert "-UseBasicParsing" in body and "Get-FileHash -Algorithm SHA256" in body
    assert "Start-Process" in body and "-Verb RunAs" in body and "-Wait" in body
    assert "'/install', '/quiet', '/norestart'" in body
    assert "Windows will ask for permission to install the Microsoft Visual C++ runtime; press Yes" in body
    assert "https://aka.ms/vs/17/release/vc_redist.x64.exe" in body        # the fix when it is declined
    assert "?." not in body and "??" not in body and "&&" not in body        # no PowerShell 7 syntax
    assert body.index("1223") < body.rindex("Fix:")


def test_the_runtime_row_value_matches_what_stage_1_does(tmp_path: Path):
    proc = _run("-FakeProbe", str(_probe(tmp_path, vc_runtime=False)))
    row = _row(_summary(proc), "vcruntime")
    assert "stage 1 installs it" in row["value"]


# ----------------------------------------------------------------------- bootstrap uv, Python, Unblock-File
def _functions(names: list[str]) -> str:
    """PowerShell that defines the named functions of stage1.ps1 (and nothing else of it)."""
    listed = ", ".join(repr(x) for x in names)
    return f"""
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{STAGE1}', [ref]$null, [ref]$null)
$fnType = [System.Management.Automation.Language.FunctionDefinitionAst]
foreach ($n in @({listed})) {{
    $found = $ast.FindAll({{ param($x) $x -is $fnType -and $x.Name -eq $n }}, $true)[0]
    . ([scriptblock]::Create($found.Extent.Text))
}}
function Join-Str {{ param($a, $b) return (Join-Path $a $b) }}
"""


def _ps(tmp_path: Path, script: str) -> subprocess.CompletedProcess:
    tmp_path.mkdir(parents=True, exist_ok=True)
    f = tmp_path / "harness.ps1"
    f.write_text(script, encoding="utf-8")
    return subprocess.run([str(PS), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(f)],
                          capture_output=True, text=True, timeout=120)


def _find_uv(tmp_path: Path, *, boot: str | None, home: str | None, on_path: str | None, version: str) -> str:
    """What Find-Uv answers; each copy of uv reports the version given for it (a path with 'old' is 0.9.0)."""
    data, nsh = tmp_path / "data", tmp_path / "nshome"
    for found, folder in ((boot, data / "bootstrap" / "uv"), (home, nsh / "bin" / "uv")):
        if found:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "uv.exe").write_text(found, encoding="utf-8")       # the text is the version it reports
    path_exe = ""
    if on_path:
        (tmp_path / "pathdir").mkdir(exist_ok=True)
        (tmp_path / "pathdir" / "uv.exe").write_text(on_path, encoding="utf-8")
        path_exe = str(tmp_path / "pathdir" / "uv.exe")
    script = _functions(["Find-Uv"]) + f"""
function Get-DataDir {{ return '{data}' }}
function Get-UvVersion {{ param([string]$Uv) return (Get-Content -Raw -LiteralPath $Uv).Trim() }}
function Get-Command {{ param($Name, $ErrorAction)
  if ('{path_exe}') {{ return [pscustomobject]@{{ Source = '{path_exe}' }} }} }}
$r = Find-Uv -NsHome '{nsh}' -Version '{version}'
if ($null -eq $r) {{ Write-Output 'NONE' }} else {{ Write-Output $r }}
"""
    proc = _ps(tmp_path, script)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout.strip()


def test_stage_1_prefers_the_pinned_uv_over_an_older_one_on_the_path(tmp_path: Path):
    pin = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))["bins"]["uv"]["version"]
    got = _find_uv(tmp_path, boot=pin, home=None, on_path="0.9.0", version=pin)
    assert got == str(tmp_path / "data" / "bootstrap" / "uv" / "uv.exe")


def test_an_older_uv_on_the_path_is_not_used_so_the_pin_is_downloaded(tmp_path: Path):
    assert _find_uv(tmp_path, boot=None, home="0.9.0", on_path="0.9.0", version="0.13.6") == "NONE"


def test_the_pinned_uv_on_the_path_or_in_the_install_folder_is_accepted(tmp_path: Path):
    got = _find_uv(tmp_path, boot=None, home=None, on_path="0.13.6", version="0.13.6")
    assert got == str(tmp_path / "pathdir" / "uv.exe")
    got = _find_uv(tmp_path / "b", boot=None, home="0.13.6", on_path=None, version="0.13.6")
    assert got.endswith("uv.exe") and "nshome" in got


def test_without_a_pin_in_the_manifest_any_uv_is_taken_path_first(tmp_path: Path):
    assert _find_uv(tmp_path, boot="0.1.0", home=None, on_path="0.9.0", version="") == str(
        tmp_path / "pathdir" / "uv.exe")


def test_stage_1_asks_for_the_managed_python_only_and_the_pin_is_looked_up_from_the_manifest():
    text = STAGE1.read_text(encoding="utf-8")
    assert "$env:UV_PYTHON_PREFERENCE = 'only-managed'" in text
    assert text.index("UV_PYTHON_PREFERENCE") < text.index("$py = Find-PythonExe -Uv $uv")
    assert "Find-Uv -NsHome $NsHome -Version" in text


def _find_python(tmp_path: Path, answer: str) -> str:
    """Find-PythonExe against a fake uv (a .cmd) that prints ``answer``."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    uv = tmp_path / "uv.cmd"
    uv.write_text(f"@echo off\r\necho {answer}\r\n", encoding="ascii")
    script = _functions(["Find-PythonExe", "Test-StorePythonAlias"]) + f"""
$r = Find-PythonExe -Uv '{uv}' -Version '3.12'
if ($null -eq $r) {{ Write-Output 'NONE' }} else {{ Write-Output $r }}
"""
    proc = _ps(tmp_path, script)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout.strip()


def test_a_microsoft_store_python_is_never_chosen(tmp_path: Path):
    alias = tmp_path / "AppData" / "Local" / "Microsoft" / "WindowsApps" / "python.exe"
    alias.parent.mkdir(parents=True)
    alias.write_bytes(b"")
    assert _find_python(tmp_path, str(alias)) == "NONE"
    store = tmp_path / "Program Files" / "WindowsApps" / "PythonSoftwareFoundation.Python.3.12" / "python.exe"
    store.parent.mkdir(parents=True)
    store.write_bytes(b"")
    assert _find_python(tmp_path / "second", str(store)) == "NONE"


def test_a_uv_managed_python_is_chosen(tmp_path: Path):
    managed = tmp_path / "uv" / "python" / "cpython-3.12.9-windows-x86_64-none" / "python.exe"
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"")
    assert _find_python(tmp_path, str(managed)) == str(managed)


def test_unblock_removes_the_mark_of_the_web_from_programs_and_leaves_other_files(tmp_path: Path):
    folder = tmp_path / "Nightshift-extracted"
    (folder / "bin" / "webview2").mkdir(parents=True)
    dll = folder / "bin" / "webview2" / "Microsoft.Web.WebView2.Core.dll"
    note = folder / "notes.txt"
    dll.write_bytes(b"MZ")
    note.write_text("x", encoding="utf-8")
    mark = "[ZoneTransfer]`r`nZoneId=3"
    script = _functions(["Unblock-ExtractedFiles"]) + f"""
foreach ($f in @('{dll}', '{note}')) {{ Set-Content -LiteralPath $f -Stream Zone.Identifier -Value "{mark}" }}
Unblock-ExtractedFiles -Folder '{folder}'
foreach ($f in @('{dll}', '{note}')) {{
    $has = $null -ne (Get-Item -LiteralPath $f -Stream Zone.Identifier -ErrorAction SilentlyContinue)
    Write-Output ((Split-Path -Leaf $f) + '=' + $has)
}}
"""
    proc = _ps(tmp_path, script)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Microsoft.Web.WebView2.Core.dll=False" in proc.stdout       # unblocked: the helpers can load
    assert "notes.txt=True" in proc.stdout                                # only programs are touched


def test_stage_1_unblocks_the_extracted_files_before_anything_loads_them():
    proc = _run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "unblock the extracted program files" in proc.stdout
    assert proc.stdout.index("unblock the extracted") < proc.stdout.index("start setup server")
    text = STAGE1.read_text(encoding="utf-8")
    assert text.index("Unblock-ExtractedFiles -Folder $App") < text.index("Show-Splash -Version $Version")


def test_the_setup_server_gets_60_seconds_and_a_progress_line():
    text = STAGE1.read_text(encoding="utf-8")
    wait = text[text.index("$serverStart = Get-Date"):text.index("-Text 'the setup program did not start'")]
    assert "AddSeconds(60)" in wait and "AddSeconds(20)" not in wait
    assert "still waiting" in wait and "TotalSeconds -ge 15" in wait
