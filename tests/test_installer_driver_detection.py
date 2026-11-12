"""Driver detection in stage 1 and step s02: where nvidia-smi is looked for, and the other CUDA sources.

The partner's first run reported "NVIDIA driver too old" on a current driver: DCH drivers keep nvidia-smi.exe
in the DriverStore FileRepository (nv* folders) only, so stage 1 found no nvidia-smi, read no CUDA version and
treated "not reported" as "too old". Now: more places to look, the driver number as a second source (and
Windows' own display adapter record as a third), "not reported" is the warning NS-W115, and only a
version that was read and is below 12.4 is the failure NS-E103.
The fake-probe rows are in test_installer_stage1.py.
"""

import json
import shutil
from pathlib import Path

import pytest

from installer.stage2 import sysinfo
from installer.stage2.steps.s02_download_bins import S02DownloadBins, choose_pair
from installer.stage2.testing import example_answers, make_test_context
from ns_common.errors import NsError
from ns_supervisor import pwsh

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(pwsh.find_powershell() is None, reason="PowerShell not available")

LOAD = r"""
$errs = $null; $tokens = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:NS_STAGE1, [ref]$tokens, [ref]$errs)
$kind = [System.Management.Automation.Language.FunctionDefinitionAst]
$wanted = @('Get-CudaVersion', 'Get-CudaFromDriver', 'Resolve-CudaVersion', 'ConvertTo-NvidiaDriverVersion',
            'Find-NvidiaSmi', 'Get-ProbeHosts')
$defs = @($ast.FindAll({ param($n) $n -is $kind -and $n.Name -in $wanted }, $true))
if ($defs.Count -ne $wanted.Count) { Write-Output 'MISSING FUNCTIONS'; exit 9 }
foreach ($d in $defs) { . ([scriptblock]::Create($d.Extent.Text)) }
$assign = [System.Management.Automation.Language.AssignmentStatementAst]
$name = '$script:DriverCudaTable'
$table = $ast.FindAll({ param($n) $n -is $assign -and $n.Left.Extent.Text -eq $name }, $true)
if (@($table).Count -ne 1) { Write-Output 'MISSING TABLE'; exit 9 }
Invoke-Expression $table[0].Extent.Text
"""


def ps(body: str, **env: str) -> list[str]:
    extra = {"NS_STAGE1": str(ROOT / "installer" / "stage1.ps1"), **env}
    out = pwsh.run_powershell(LOAD + body, env_extra=extra, timeout_s=120)
    assert "MISSING" not in out.stdout, out.stdout + out.stderr
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


# ---- the driver number as a CUDA source ---------------------------------------------------------------
@pytest.mark.parametrize(
    ("driver", "cuda"),
    [("621.01", "13.0"),                    # newer than the table's newest row: that row answers
     ("581.57", "13.0"), ("580.65", "13.0"), ("572.16", "12.8"), ("570.00", "12.8"), ("566.14", "12.6"),
     ("560.94", "12.6"), ("551.23", "12.4"), ("550.00", "12.4"), ("546.01", "12.3"), ("545.84", "12.3"),
     ("536.99", "12.2"), ("531.18", "12.1"), ("527.56", "12.0"), ("472.12", "11.8"), ("", ""), ("n/a", "")],
)
def test_the_driver_table_maps_to_cuda(driver: str, cuda: str) -> None:
    out = ps("Write-Output ('V=' + (Get-CudaFromDriver -Driver $env:NS_D))", NS_D=driver)
    assert out == [f"V={cuda}"]


def test_the_table_is_one_list_newest_first() -> None:
    mins = ps("Write-Output ('M=' + (($script:DriverCudaTable | ForEach-Object { $_.Min }) -join ','))")
    values = [int(x) for x in mins[0].removeprefix("M=").split(",")]
    assert values == sorted(values, reverse=True) and values[:5] == [580, 570, 560, 550, 545]
    assert values[-1] == 0
    text = (ROOT / "installer" / "stage1.ps1").read_text(encoding="ascii")
    assert text.count("DriverCudaTable") == 2          # the definition and its one user
    assert "docs.nvidia.com/cuda" in text              # the source of the numbers is cited next to the table


@pytest.mark.parametrize(
    ("windows", "nvidia"),
    [("32.0.15.8157", "581.57"), ("31.0.15.5123", "551.23"), ("27.21.14.6109", "461.09"),
     ("572.16", "572.16"), ("1.2", ""), ("", "")],
)
def test_windows_driver_versions_become_nvidia_numbers(windows: str, nvidia: str) -> None:
    out = ps("Write-Output ('V=' + (ConvertTo-NvidiaDriverVersion -WindowsVersion $env:NS_W))", NS_W=windows)
    assert out == [f"V={nvidia}"]


@pytest.mark.parametrize(
    ("smi", "driver", "source", "cuda"),
    [("NVIDIA-SMI 572.16   Driver Version: 572.16   CUDA Version: 12.8", "572.16", "nvidia-smi", "12.8"),
     ("CUDA  Version : 12.4", "", "nvidia-smi", "12.4"),
     # the banner of driver 621.01, word for word from the first real install (2026-11-12): rc5 read nothing
     # from it and answered "CUDA 13.0 (from driver 621.01)" out of the table
     ("NVIDIA-SMI 621.01   KMD Version: 621.01   CUDA UMD Version: 13.5", "621.01", "nvidia-smi", "13.5"),
     ("NVIDIA-SMI 621.01   KMD Version: 621.01   CUDA UMD Version: 13.5", "", "nvidia-smi", "13.5"),
     ("CUDA UMD KMD Version: 13.5", "621.01", "driver", "13.0"),                # two words: not this banner
     ("", "572.16", "driver", "12.8"),                                          # banner missing
     ("NVIDIA-SMI 572.16  Treiberversion: 572.16  CUDA-Version: 12.8", "572.16", "driver", "12.8"),
     ("garbage", "531.18", "driver", "12.1"),
     ("", "", "none", ""),
     ("", "not a number", "none", "")],
)
def test_the_banner_comes_first_then_the_driver_number(smi: str, driver: str, source: str, cuda: str) -> None:
    body = r"""
$r = Resolve-CudaVersion -Probe ([pscustomobject]@{ smi_text = $env:NS_SMI; driver = $env:NS_D })
Write-Output ('S=' + $r.source); Write-Output ('V=' + $r.version)
"""
    assert ps(body, NS_SMI=smi, NS_D=driver) == [f"S={source}", f"V={cuda}"]


# ---- which sites the PC check asks --------------------------------------------------------------------
def test_a_pc_that_has_the_visual_c_runtime_is_not_asked_about_its_download_site() -> None:
    """First real install, 2026-11-12: the row warned "unreachable: download.visualstudio.microsoft.com" (with
    NS-E105) on a PC whose Visual C++ runtime was installed, about a site setup then took nothing from."""
    body = r"""
$vc = 'https://download.visualstudio.microsoft.com'
$all = @('https://github.com', $vc, 'https://pypi.org')
Write-Output ('HAS=' + (@(Get-ProbeHosts -Hosts $all -VcRuntimePresent $true) -join ','))
Write-Output ('LACKS=' + (@(Get-ProbeHosts -Hosts $all -VcRuntimePresent $false) -join ','))
Write-Output ('ONE=' + (@(Get-ProbeHosts -Hosts @($vc) -VcRuntimePresent $true)).Count)
"""
    assert ps(body) == [
        "HAS=https://github.com,https://pypi.org",
        "LACKS=https://github.com,https://download.visualstudio.microsoft.com,https://pypi.org",
        "ONE=0",
    ]
    text = (ROOT / "installer" / "stage1.ps1").read_text(encoding="ascii")
    # the real probe asks the list through it, and the site is still on the list for a PC without the runtime
    assert "Get-ProbeHosts -Hosts $script:NetworkHosts -VcRuntimePresent (Test-VcRuntime)" in text
    assert "'https://download.visualstudio.microsoft.com'" in text


# ---- where nvidia-smi is looked for -------------------------------------------------------------------
FIND = r"""
$p = Find-NvidiaSmi -SystemRoot $env:NS_ROOT -ProgramFilesDir (Join-Path $env:NS_ROOT 'pf') -SkipPath
if ($null -eq $p) { Write-Output 'FOUND=none' } else { Write-Output ('FOUND=' + $p) }
"""


def exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ")
    return path


def found(root: Path) -> str:
    return ps(FIND, NS_ROOT=str(root))[0].removeprefix("FOUND=")


def test_a_dch_driver_with_nvidia_smi_only_in_the_driver_store_is_found(tmp_path: Path) -> None:
    store = tmp_path / "System32" / "DriverStore" / "FileRepository"
    smi = exe(store / "nv_dispi.inf_amd64_0123456789abcdef" / "nvidia-smi.exe")
    exe(store / "other.inf_amd64_x" / "something.exe")                   # not nvidia-smi
    assert Path(found(tmp_path)) == smi


def test_the_newest_driver_store_copy_wins(tmp_path: Path) -> None:
    import os

    store = tmp_path / "System32" / "DriverStore" / "FileRepository"
    old = exe(store / "nv_dispi.inf_amd64_old" / "nvidia-smi.exe")
    new = exe(store / "nv_dispig.inf_amd64_new" / "nvidia-smi.exe")
    os.utime(old, (1_600_000_000, 1_600_000_000))
    os.utime(new, (1_700_000_000, 1_700_000_000))
    assert Path(found(tmp_path)) == new


def test_system32_and_the_old_nvsmi_folder_still_come_first(tmp_path: Path) -> None:
    exe(tmp_path / "System32" / "DriverStore" / "FileRepository" / "nv_dispi.inf_amd64_a" / "nvidia-smi.exe")
    nvsmi = exe(tmp_path / "pf" / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe")
    assert Path(found(tmp_path)) == nvsmi
    sys32 = exe(tmp_path / "System32" / "nvidia-smi.exe")
    assert Path(found(tmp_path)) == sys32


def test_no_nvidia_smi_anywhere_is_none(tmp_path: Path) -> None:
    (tmp_path / "System32" / "DriverStore" / "FileRepository" / "nv_dispi.inf_amd64_a").mkdir(parents=True)
    assert found(tmp_path) == "none"
    shutil.rmtree(tmp_path / "System32")
    assert found(tmp_path) == "none"


# ---- step s02: a version that could not be read is a warning, not a refusal ---------------------------
ADA = "NVIDIA GeForce RTX 4070"          # a card the CUDA 12.4 build runs on
BLACKWELL = "NVIDIA GeForce RTX 5060 Ti"  # the partner's card: CUDA 13 build only


def s02_ctx(tmp_path: Path, cuda: str, gpu: str = ADA):
    pf = sysinfo.sample_preflight(tmp_path / "home", ROOT, cuda=cuda)
    pf["gpu"]["name"] = gpu
    return make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, preflight=pf)


def test_s02_unreadable_cuda_uses_the_12_4_build_and_warns_w115(tmp_path: Path) -> None:
    ctx = s02_ctx(tmp_path, "")
    assert choose_pair(ctx) == "cuda12" and ctx.warnings == []              # planning alone stays quiet
    assert choose_pair(ctx, warn=True) == "cuda12"
    assert [c for c, _ in ctx.warnings] == ["NS-W115"]


def test_s02_unreadable_cuda_on_an_rtx_50_card_takes_the_cuda13_build(tmp_path: Path) -> None:
    ctx = s02_ctx(tmp_path, "", gpu=BLACKWELL)
    assert choose_pair(ctx, warn=True) == "cuda13"
    assert [c for c, _ in ctx.warnings] == ["NS-W115"]
    names = {i.name for i in S02DownloadBins()._items(ctx)}
    assert {"llama_cuda13", "cudart13"} <= names and "llama_cuda" not in names


def test_s02_a_version_that_was_read_and_is_too_old_is_still_e103(tmp_path: Path) -> None:
    with pytest.raises(NsError) as ei:
        choose_pair(s02_ctx(tmp_path, "12.1"), warn=True)
    assert ei.value.code == "NS-E103" and "12.1" in ei.value.detail


@pytest.mark.parametrize(("cuda", "pair"), [("13.0", "cuda13"), ("12.8", "cuda12"), ("12.4", "cuda12")])
def test_s02_readable_versions_are_unchanged(tmp_path: Path, cuda: str, pair: str) -> None:
    ctx = s02_ctx(tmp_path, cuda)
    assert choose_pair(ctx, warn=True) == pair and ctx.warnings == []


@pytest.mark.parametrize("cuda", ["12.8", "12.4"])
def test_s02_an_rtx_50_card_on_a_cuda_12_driver_stops_with_e103_before_any_download(tmp_path: Path,
                                                                                    cuda: str) -> None:
    with pytest.raises(NsError) as ei:
        choose_pair(s02_ctx(tmp_path, cuda, gpu=BLACKWELL), warn=True)
    assert ei.value.code == "NS-E103" and "580" in ei.value.detail and cuda in ei.value.detail


def test_s02_an_rtx_50_card_on_a_cuda_13_driver_is_fine(tmp_path: Path) -> None:
    assert choose_pair(s02_ctx(tmp_path, "13.0", gpu=BLACKWELL), warn=True) == "cuda13"


def test_s02_plans_the_cuda12_downloads_when_the_version_is_unreadable(tmp_path: Path) -> None:
    ctx = s02_ctx(tmp_path, "")
    names = {i.name for i in S02DownloadBins()._items(ctx)}
    assert {"llama_cuda", "cudart"} <= names and "llama_cuda13" not in names


# ---- the words ----------------------------------------------------------------------------------------
def test_w115_has_a_code_a_plain_text_and_a_ui_string() -> None:
    codes = json.loads((ROOT / "contracts" / "errors.json").read_text(encoding="utf-8"))
    assert "NS-W115" in codes and "NS-E103" in codes and "12.4" in codes["NS-E103"]["message"]
    copy_file = ROOT / "installer" / "stage2" / "web" / "operator_copy.json"
    copy = json.loads(copy_file.read_text(encoding="utf-8"))
    assert copy["NS-W115"]["title"] and "CUDA 12.4" in copy["NS-W115"]["whatToDo"]
    ts = (ROOT / "installer" / "ui" / "src" / "copy.ts").read_text(encoding="utf-8")
    assert "operator_copy.json" in ts and "NS-W115" in (ROOT / "OPERATOR.md").read_text(encoding="utf-8")
