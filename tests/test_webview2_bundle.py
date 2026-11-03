"""The release bundle carries the three WebView2 DLLs so the FIRST setup window is the frameless host.

Before: the DLLs came from the nupkg in step s02, after the window was already open, so the first run
showed an Edge app window (default title bar, Edge icon). Now ``dev/make_bundle.ps1 -WebView2Dir`` packs
them into
``bin/webview2/`` of the zip, ``launch.py`` looks there, and s02 adopts them instead of downloading the nupkg.
"""

import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from installer.stage2 import downloader as dl
from installer.stage2.steps.s02_download_bins import S02DownloadBins
from installer.stage2.web import launch
from ns_common.config import load_manifest
from ns_common.paths import Paths
from ns_supervisor import pwsh

ROOT = Path(__file__).resolve().parents[1]
DLLS = ["Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.Wpf.dll", "WebView2Loader.dll"]
needs_powershell = pytest.mark.skipif(pwsh.find_powershell() is None, reason="powershell not on PATH")
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ---- the shipped description of the DLLs ----------------------------------------------------------
def test_webview2_files_json_matches_the_manifest_pin_and_the_nupkg_members() -> None:
    spec = json.loads(read("installer/webview2_files.json"))
    pin = load_manifest().bins["webview2"]
    assert pin is not None and spec["version"] == pin.version
    assert sorted(spec["files"]) == sorted(DLLS)
    for name, candidates in dl.WEBVIEW2_MEMBERS:
        assert spec["files"][name]["member"] in candidates
        assert len(spec["files"][name]["sha256"]) == 64


def test_the_release_workflow_verifies_the_nupkg_and_packs_the_dlls_before_the_bundle() -> None:
    wf = read(".github/workflows/release.yml")
    fetch = wf.index("Fetch the WebView2 window files")
    build = wf.index("./dev/make_bundle.ps1")
    assert fetch < build < wf.index("Publish GitHub Release")
    step = wf[fetch:build]
    assert "installer/webview2_files.json" in step and "manifest.json" in step
    assert "Get-FileHash" in step and "does not match" in step          # nupkg and every DLL are checked
    assert "-WebView2Dir $env:WEBVIEW2_DIR" in wf[build:build + 120]


# ---- make_bundle.ps1 ------------------------------------------------------------------------------
def test_bundle_script_allows_exactly_three_files_under_bin() -> None:
    text = read("dev/make_bundle.ps1")
    assert "[string]$WebView2Dir" in text and "$AllowedBinEntries" in text
    for name in DLLS:
        assert name in text
    assert "'bin/*'" in text  # anything else under bin/ is a problem
    assert "'*.exe'" in text                                             # binaries stay forbidden


ZIP_PROBE = r"""
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$errs = $null; $tokens = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:NS_SCRIPT, [ref]$tokens, [ref]$errs)
$kind = [System.Management.Automation.Language.FunctionDefinitionAst]
foreach ($n in @('Get-ZipEntries', 'Test-BundleZip')) {
    $d = $ast.FindAll({ param($x) $x -is $kind -and $x.Name -eq $n }, $true)
    if (@($d).Count -ne 1) { Write-Output "MISSING $n"; exit 9 }
    . ([scriptblock]::Create($d[0].Extent.Text))
}
$ForbiddenNames = @('*.gguf', '*.exe'); $ForbiddenDirs = @('.venv', '.git'); $RequiredEntries = @('VERSION')
$MaxZipBytes = 25MB
$WebView2Files = @('Microsoft.Web.WebView2.Core.dll', 'Microsoft.Web.WebView2.Wpf.dll', 'WebView2Loader.dll')
$AllowedBinEntries = @($WebView2Files | ForEach-Object { 'bin/webview2/' + $_ })
$WebView2Dir = $env:NS_WV2
foreach ($p in @(Test-BundleZip $env:NS_ZIP)) { Write-Output ('PROBLEM ' + $p) }
Write-Output 'DONE'
"""


def probe_zip(tmp_path: Path, entries: list[str], *, with_dir: bool) -> list[str]:
    archive = tmp_path / "b.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("VERSION", "0.0.0\n")
        for e in entries:
            zf.writestr(e, "x")
    env = {"NS_SCRIPT": str(ROOT / "dev" / "make_bundle.ps1"), "NS_ZIP": str(archive),
           "NS_WV2": str(tmp_path) if with_dir else ""}
    out = pwsh.run_powershell(ZIP_PROBE, env_extra=env, timeout_s=120)
    assert "DONE" in out.stdout and "MISSING" not in out.stdout, out.stdout + out.stderr
    return [ln[len("PROBLEM "):] for ln in out.stdout.splitlines() if ln.startswith("PROBLEM ")]


@needs_powershell
def test_the_zip_check_accepts_the_three_dlls_and_nothing_else_under_bin(tmp_path: Path) -> None:
    good = [f"bin/webview2/{n}" for n in DLLS]
    assert probe_zip(tmp_path, good, with_dir=True) == []
    assert probe_zip(tmp_path, [], with_dir=False) == []                 # a bundle without the window files
    stray = probe_zip(tmp_path, [*good, "bin/webview2/evil.dll", "bin/llama/llama-server.dll"], with_dir=True)
    assert len(stray) == 2 and all("not allowed under bin/" in p for p in stray)
    assert any("bin/webview2/evil.dll" in p for p in stray)
    wrong_place = probe_zip(tmp_path, [f"bin/{n}" for n in DLLS], with_dir=False)
    assert len(wrong_place) == 3


@needs_powershell
def test_the_zip_check_requires_all_three_dlls_when_the_dir_was_given(tmp_path: Path) -> None:
    two = [f"bin/webview2/{n}" for n in DLLS[:2]]
    problems = probe_zip(tmp_path, two, with_dir=True)
    assert problems == ["required entry missing: bin/webview2/WebView2Loader.dll"]


def dry_run(tmp_path: Path, folder: Path) -> subprocess.CompletedProcess[str]:
    exe = pwsh.find_powershell()
    assert exe is not None
    cmd = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
           str(ROOT / "dev" / "make_bundle.ps1"), "-DryRun", "-WebView2Dir", str(folder)]
    # a PSModulePath from PowerShell 7 (the CI runner shell) hides the 5.1 modules: no Get-FileHash
    env = {k: v for k, v in os.environ.items() if k.lower() != "psmodulepath"}
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=ROOT, env=env, check=False)


@needs_powershell
@needs_git
def test_a_dry_run_with_the_dir_lists_the_three_dlls_in_the_zip(tmp_path: Path) -> None:
    folder = tmp_path / "wv2"
    folder.mkdir()
    for name in DLLS:
        (folder / name).write_bytes(b"MZ" + name.encode())
    proc = dry_run(tmp_path, folder)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for name in DLLS:
        assert f"bin/webview2/{name}" in proc.stdout


@needs_powershell
@needs_git
def test_a_dry_run_refuses_a_dir_that_is_not_exactly_the_three_dlls(tmp_path: Path) -> None:
    folder = tmp_path / "wv2"
    folder.mkdir()
    for name in [*DLLS, "extra.dll"]:
        (folder / name).write_bytes(b"MZ")
    proc = dry_run(tmp_path, folder)
    assert proc.returncode != 0 and "exactly" in proc.stdout + proc.stderr


# ---- launch.py: where the host looks --------------------------------------------------------------
APP = Path("D:/ns-app")
HOME_ENV = {"NIGHTSHIFT_HOME": "E:/ns-home"}


class Popen:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        return SimpleNamespace(poll=lambda: None)


def open_with(present: set[Path]) -> tuple[launch.WindowHandle, Popen]:
    popen = Popen()
    handle = launch.open_window(
        "http://127.0.0.1:1/?t=x", app_path=APP, env=HOME_ENV, popen=popen, exists=lambda p: p in present,
        startfile=lambda _u: None, sleep=lambda _s: None, wait_s=0.0, ready=lambda: True,
    )
    return handle, popen


def test_the_lookup_covers_the_unzipped_bundle_and_the_install_folder() -> None:
    dirs = launch.webview2_dirs(APP, HOME_ENV)
    assert Path("E:/ns-home/bin/webview2") in dirs and APP / "bin" / "webview2" in dirs


def test_the_first_run_uses_the_host_when_only_the_bundle_has_the_dlls() -> None:
    host = APP / "launchers" / "nightshift-host.ps1"
    bundled = APP / "bin" / "webview2" / launch.WEBVIEW2_CORE_DLL
    handle, popen = open_with({host, bundled})
    assert handle.host == "webview2" and len(popen.calls) == 1 and str(host) in popen.calls[0]


def test_the_host_is_also_used_when_only_the_install_folder_has_the_dlls() -> None:
    host = APP / "launchers" / "nightshift-host.ps1"
    installed = Path("E:/ns-home/bin/webview2") / launch.WEBVIEW2_CORE_DLL
    assert open_with({host, installed})[0].host == "webview2"


def test_without_the_dlls_anywhere_the_host_is_not_started() -> None:
    host = APP / "launchers" / "nightshift-host.ps1"
    handle, popen = open_with({host})
    assert handle.host != "webview2" and "webview2" not in handle.tried and not popen.calls


# ---- s02: adopt the bundle DLLs instead of downloading the nupkg -----------------------------------
def make_bundle_dir(app: Path, *, tamper: str | None = None, skip: str | None = None) -> Path:
    """<app>/bin/webview2 with fake DLLs, and a files json that describes them for the real pin's version."""
    folder = app / "bin" / "webview2"
    folder.mkdir(parents=True)
    spec = {"version": load_manifest().bins["webview2"].version, "files": {}}  # type: ignore[union-attr]
    for name in DLLS:
        data = b"MZ-" + name.encode()
        spec["files"][name] = {"member": "x", "sha256": hashlib.sha256(data).hexdigest()}
        if name != skip:
            (folder / name).write_bytes(b"tampered" if name == tamper else data)
    jf = app / "files.json"
    jf.write_text(json.dumps(spec), encoding="utf-8")
    return jf


def test_matching_bundle_dlls_are_adopted_and_the_item_counts_as_done(tmp_path: Path) -> None:
    manifest, home = load_manifest(), tmp_path / "home"
    jf = make_bundle_dir(tmp_path / "app")
    expected = tmp_path / "app" / "bin" / "webview2"
    assert dl.bundled_webview2(manifest, tmp_path / "app", files_json=jf) == expected
    assert dl.adopt_bundled_webview2(manifest, tmp_path / "app", home, files_json=jf) is True
    item = dl.webview2_item(manifest, home)
    assert item is not None and dl.is_done(item)
    assert sorted(p.name for p in (home / "bin" / "webview2").glob("*.dll")) == sorted(DLLS)

    def no_fetch(*_a, **_k):
        raise AssertionError("the nupkg must not be downloaded")

    result = dl.run_plan([item], fetch=no_fetch)
    assert result.skipped == ["webview2"] and not result.downloaded


@pytest.mark.parametrize("case", ["tamper", "missing", "version", "unpinned"])
def test_anything_else_falls_back_to_the_download(tmp_path: Path, case: str) -> None:
    manifest, home, app = load_manifest(), tmp_path / "home", tmp_path / "app"
    jf = make_bundle_dir(app, tamper=DLLS[0] if case == "tamper" else None,
                         skip=DLLS[2] if case == "missing" else None)
    if case == "version":
        spec = json.loads(jf.read_text(encoding="utf-8"))
        spec["version"] = "0.0.1"
        jf.write_text(json.dumps(spec), encoding="utf-8")
    if case == "unpinned":
        manifest.bins["webview2"] = manifest.bins["webview2"].model_copy(update={"sha256": "TBD-release"})
    assert dl.adopt_bundled_webview2(manifest, app, home, files_json=jf) is False
    assert not (home / "bin" / "webview2").exists()
    assert dl.bundled_webview2(manifest, app, files_json=jf) is None


def test_no_bundle_folder_is_a_quiet_no_op(tmp_path: Path) -> None:
    assert dl.adopt_bundled_webview2(load_manifest(), tmp_path / "app", tmp_path / "home") is False


def test_step_s02_adopts_the_bundle_before_it_plans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest, app, home = load_manifest(), tmp_path / "app", tmp_path / "home"
    jf = make_bundle_dir(app)
    monkeypatch.setattr(dl, "WEBVIEW2_FILES_JSON", jf)
    ctx = SimpleNamespace(
        manifest=manifest, app_path=app, paths=Paths(home), profile="gpu", preflight={"cuda": "12.8"},
        ensure_answers=lambda: SimpleNamespace(install_code=False),
    )
    items = S02DownloadBins()._items(ctx)  # type: ignore[arg-type]
    web = next(i for i in items if i.name == "webview2")
    assert dl.is_done(web)
    assert not any(dl.is_done(i) for i in items if i.name != "webview2")      # the rest still downloads
