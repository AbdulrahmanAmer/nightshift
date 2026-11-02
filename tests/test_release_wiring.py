"""Release wiring: the 0.1.0 changelog heading, the release workflow's build order, the manifest pin rule."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_changelog_has_a_heading_for_the_version_file():
    version = read("VERSION").strip()
    text = read("CHANGELOG.md")
    m = re.search(rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}\s*$", text, re.MULTILINE)
    assert m, f"CHANGELOG.md needs '## [{version}] - <date>' (release.yml reads the release notes from it)"
    # the Unreleased heading stays above it and holds no entries once they moved under the version
    unreleased = re.search(r"^## \[Unreleased\]\s*$", text, re.MULTILINE)
    assert unreleased and unreleased.start() < m.start()
    assert text[unreleased.end() : m.start()].strip() == ""


def test_release_workflow_builds_the_uis_before_the_bundle():
    wf = read(".github/workflows/release.yml")
    dash = wf.index("npm run build")
    installer = wf.index("installer/ui")
    bundle = wf.index("./dev/make_bundle.ps1")
    assert dash < installer < bundle
    # the installer UI step is conditional on the folder existing
    assert "hashFiles('installer/ui/package.json')" in wf
    assert "git status --porcelain installer/ui/static" in wf


def test_bundle_requires_the_installer_ui_build_once_it_exists():
    bundle = read("dev/make_bundle.ps1")
    assert "installer/ui/static/index.html" in bundle and "installer/ui/static/app.js" in bundle


def test_the_qwentts_pin_is_still_open_or_a_real_release_asset():
    pin = json.loads(read("manifest.json"))["bins"].get("qwentts")
    if pin is not None:
        assert "/releases/download/v" in pin["url"] and pin["url"].endswith("qwentts-win-cuda-x64.zip")
        assert re.fullmatch(r"[0-9a-f]{64}", pin["sha256"])


def test_repo_settings_describe_the_tag_to_pin_loop():
    text = read("docs/REPO_SETTINGS.md")
    for needle in ("git tag v0.1.0", "build-qwentts", "bins.qwentts", "qwentts-win-cuda-x64.zip.sha256"):
        assert needle in text, needle
