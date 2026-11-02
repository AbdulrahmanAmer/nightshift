"""App host: the pinned WebView2 NuGet package, its manifest entry and how the installer unpacks it."""

import hashlib
import json
import re
import zipfile
from pathlib import Path

import pytest

from installer.stage2 import downloader as dl
from ns_common.config import load_manifest
from ns_common.errors import NsError

ROOT = Path(__file__).resolve().parents[1]
DLLS = ["Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.Wpf.dll", "WebView2Loader.dll"]
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def raw_entry() -> dict:
    data = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    return data["bins"]["webview2"]


def test_manifest_has_the_webview2_entry_shape() -> None:
    entry = raw_entry()
    assert set(entry) == {"version", "url", "sha256"}
    assert re.fullmatch(r"1\.0\.\d+\.\d+", entry["version"]), "pin a stable version, not a -prerelease"
    assert entry["url"] == (
        "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/"
        f"{entry['version']}/microsoft.web.webview2.{entry['version']}.nupkg"
    )
    # A real SHA256, or the documented placeholder (the installer then skips it and the launcher falls back).
    assert HEX64.fullmatch(entry["sha256"]) or entry["sha256"] == "TBD-release"


def test_manifest_model_accepts_the_entry() -> None:
    pin = load_manifest().bins["webview2"]
    assert pin is not None and pin.version == raw_entry()["version"]


def test_plan_includes_the_nupkg_for_every_profile(tmp_path: Path) -> None:
    manifest = load_manifest()
    pin = manifest.bins["webview2"]
    assert pin is not None
    expected = dl.pinned_sha256(pin.sha256) is not None
    for profile, pair in (("gpu", "cuda13"), ("gpu", "cuda12"), ("cpu-dev", None)):
        by = {i.name: i for i in dl.plan_bins(manifest, profile, tmp_path, llama_pair=pair)}
        assert ("webview2" in by) is expected
        if expected:
            item = by["webview2"]
            assert item.kind == "nupkg" and item.url == pin.url and item.sha256 == pin.sha256
            assert item.extract_to == tmp_path / "bin" / "webview2"
            assert item.expect == tuple(DLLS)


def test_tbd_hash_is_skipped(tmp_path: Path) -> None:
    manifest = load_manifest()
    pin = manifest.bins["webview2"]
    assert pin is not None
    manifest.bins["webview2"] = pin.model_copy(update={"sha256": "TBD-release"})
    assert dl.webview2_item(manifest, tmp_path) is None
    assert "webview2" not in [i.name for i in dl.plan_bins(manifest, "gpu", tmp_path, llama_pair="cuda12")]
    manifest.bins["webview2"] = None
    assert dl.webview2_item(manifest, tmp_path) is None


def make_nupkg(path: Path, *, folder: str = "lib/net462") -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Microsoft.Web.WebView2.nuspec", "<package/>")
        zf.writestr(f"{folder}/Microsoft.Web.WebView2.Core.dll", "core")
        zf.writestr(f"{folder}/Microsoft.Web.WebView2.Wpf.dll", "wpf")
        zf.writestr(f"{folder}/Microsoft.Web.WebView2.WinForms.dll", "winforms")
        zf.writestr("runtimes/win-x64/native/WebView2Loader.dll", "x64")
        zf.writestr("runtimes/win-x86/native/WebView2Loader.dll", "x86")
        zf.writestr("tools/wv2winrt/codegen_util.dll", "tool")


@pytest.mark.parametrize("folder", ["lib/net462", "lib/net45"])
def test_only_the_three_dlls_are_extracted(tmp_path: Path, folder: str) -> None:
    pkg = tmp_path / "pkg.nupkg"
    make_nupkg(pkg, folder=folder)
    out = tmp_path / "bin" / "webview2"
    assert dl.extract_members(pkg, out, dl.WEBVIEW2_MEMBERS) == DLLS
    assert sorted(p.name for p in out.iterdir()) == sorted(DLLS)
    assert (out / "WebView2Loader.dll").read_text() == "x64"


def test_missing_member_is_e109(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg.nupkg"
    with zipfile.ZipFile(pkg, "w") as zf:
        zf.writestr("Microsoft.Web.WebView2.nuspec", "<package/>")
    with pytest.raises(NsError) as ei:
        dl.extract_members(pkg, tmp_path / "out", dl.WEBVIEW2_MEMBERS)
    assert ei.value.code == "NS-E109"


def test_run_plan_unpacks_a_pinned_nupkg(tmp_path: Path) -> None:
    pkg = tmp_path / "src.nupkg"
    make_nupkg(pkg)
    digest = hashlib.sha256(pkg.read_bytes()).hexdigest()
    manifest = load_manifest()
    pin = manifest.bins["webview2"]
    assert pin is not None
    manifest.bins["webview2"] = pin.model_copy(update={"sha256": digest})
    home = tmp_path / "home"
    item = dl.webview2_item(manifest, home)
    assert item is not None

    def fetch(url, dest, sha256, **_):
        assert url == pin.url and sha256 == digest
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(pkg.read_bytes())
        return dest

    result = dl.run_plan([item], fetch=fetch)
    assert result.downloaded == ["webview2"]
    assert dl.is_done(item)
    assert sorted(p.name for p in (home / "bin" / "webview2").glob("*.dll")) == sorted(DLLS)
    assert not item.dest.exists()                       # the package is removed once unpacked
    assert dl.run_plan([item], fetch=fetch).skipped == ["webview2"]
