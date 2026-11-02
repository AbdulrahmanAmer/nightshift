"""App host: the WebView2 NuGet pin (manifest entry or its open CR) and how the installer unpacks it."""

import hashlib
import json
import re
import zipfile
from pathlib import Path

import pytest

from installer.stage2 import downloader as dl
from ns_common.config import load_manifest
from ns_common.errors import NsError
from ns_common.models import BinPin

ROOT = Path(__file__).resolve().parents[1]
DLLS = ["Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.Wpf.dll", "WebView2Loader.dll"]
HEX64 = re.compile(r"^[0-9a-f]{64}$")
CR_TITLE = "## CR-C2-host: `bins.webview2`"


def manifest_entry() -> dict | None:
    data = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    return data["bins"].get("webview2")


def cr_entry() -> dict:
    """The JSON block of the change request that asks the contracts builder to add the manifest entry."""
    text = (ROOT / "contracts" / "CHANGE_REQUESTS.md").read_text(encoding="utf-8")
    section = text[text.index(CR_TITLE):]
    block = re.search(r'"webview2": (\{.*?\n  \})', section, re.S)
    assert block, "CR-C2-host lacks the JSON entry"
    return json.loads(block.group(1))


def assert_entry_shape(entry: dict) -> None:
    assert set(entry) == {"version", "url", "sha256"}
    assert re.fullmatch(r"1\.0\.\d+\.\d+", entry["version"]), "pin a stable version, not a -prerelease"
    assert entry["url"] == (
        "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/"
        f"{entry['version']}/microsoft.web.webview2.{entry['version']}.nupkg"
    )
    # A real SHA256, or the documented placeholder (the installer then skips it and the launcher falls back).
    assert HEX64.fullmatch(entry["sha256"]) or entry["sha256"] == "TBD-release"


def test_manifest_entry_shape_when_present() -> None:
    entry = manifest_entry()
    if entry is not None:
        assert_entry_shape(entry)
        pin = load_manifest().bins["webview2"]
        assert pin is not None and pin.version == entry["version"]


def test_change_request_carries_a_valid_entry() -> None:
    entry = cr_entry()
    assert_entry_shape(entry)
    assert HEX64.fullmatch(entry["sha256"])


def test_plan_includes_the_nupkg_only_when_pinned(tmp_path: Path) -> None:
    manifest = load_manifest()
    pin = manifest.bins.get("webview2")
    expected = pin is not None and dl.pinned_sha256(pin.sha256) is not None
    for profile, pair in (("gpu", "cuda13"), ("gpu", "cuda12"), ("cpu-dev", None)):
        by = {i.name: i for i in dl.plan_bins(manifest, profile, tmp_path, llama_pair=pair)}
        assert ("webview2" in by) is expected
        if expected:
            assert pin is not None
            item = by["webview2"]
            assert item.kind == "nupkg" and item.url == pin.url and item.sha256 == pin.sha256
            assert item.extract_to == tmp_path / "bin" / "webview2"
            assert item.expect == tuple(DLLS)


def with_pin(sha256: str):
    manifest = load_manifest()
    base = cr_entry()
    manifest.bins["webview2"] = BinPin(version=base["version"], url=base["url"], sha256=sha256)
    return manifest


def test_missing_or_tbd_pin_is_skipped(tmp_path: Path) -> None:
    manifest = with_pin("TBD-release")
    assert dl.webview2_item(manifest, tmp_path) is None
    assert "webview2" not in [i.name for i in dl.plan_bins(manifest, "gpu", tmp_path, llama_pair="cuda12")]
    manifest.bins["webview2"] = None
    assert dl.webview2_item(manifest, tmp_path) is None
    del manifest.bins["webview2"]
    assert dl.webview2_item(manifest, tmp_path) is None
    assert "webview2" not in [i.name for i in dl.plan_bins(manifest, "gpu", tmp_path, llama_pair="cuda12")]


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
    manifest = with_pin(digest)
    pin = manifest.bins["webview2"]
    assert pin is not None
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
