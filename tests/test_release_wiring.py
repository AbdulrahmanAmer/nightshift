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
    # the Unreleased heading stays above it; it holds the fixes made since that release (a release moves them
    # under the new version heading in the same commit that bumps VERSION, and this test follows both)
    unreleased = re.search(r"^## \[Unreleased\]\s*$", text, re.MULTILINE)
    assert unreleased and unreleased.start() < m.start()


def test_release_workflow_builds_the_uis_before_the_bundle():
    wf = read(".github/workflows/release.yml")
    dash = wf.index("npm run build")
    bundle = wf.index("./dev/make_bundle.ps1")
    assert dash < bundle
    # one `npm run build` in ns_dashboard/ui writes both ns_dashboard/static and installer/ui/static
    assert "git status --porcelain ns_dashboard/static" in wf
    assert "git status --porcelain installer/ui/static" in wf
    assert wf.index("git status --porcelain installer/ui/static") < bundle


def test_ci_ui_build_checks_both_committed_builds():
    ci = read(".github/workflows/ci.yml")
    assert "git status --porcelain ns_dashboard/static" in ci
    assert "git status --porcelain installer/ui/static" in ci


def test_bundle_requires_the_installer_ui_build():
    bundle = read("dev/make_bundle.ps1")
    assert "installer/ui/static/index.html" in bundle and "installer/ui/static/app.js" in bundle


def test_changelog_lists_the_installer_wizard_as_shipped():
    text = read("CHANGELOG.md")
    assert "Installer web wizard" in text and "in progress" not in text


def test_the_qwentts_pin_is_still_open_or_a_real_release_asset():
    pin = json.loads(read("manifest.json"))["bins"].get("qwentts")
    if pin is not None:
        assert "/releases/download/v" in pin["url"] and pin["url"].endswith("qwentts-win-cuda-x64.zip")
        assert re.fullmatch(r"[0-9a-f]{64}", pin["sha256"])


def test_repo_settings_describe_the_tag_to_pin_loop():
    text = read("docs/REPO_SETTINGS.md")
    for needle in ("git tag v0.1.0", "build-qwentts", "bins.qwentts", "qwentts-win-cuda-x64.zip.sha256"):
        assert needle in text, needle


def test_the_test_job_has_a_forty_minute_cap_in_ci_and_release():
    for rel in (".github/workflows/ci.yml", ".github/workflows/release.yml"):
        wf = read(rel)
        job = wf[wf.index("\n  test:") :]
        head = job[: job.index("steps:")]
        assert "timeout-minutes: 40" in head, rel


def test_the_0_1_1_changelog_covers_the_re_review_prs_and_unreleased_is_empty():
    version = read("VERSION").strip()
    assert version == "0.1.1"
    text = read("CHANGELOG.md")
    assert re.search(r"^## \[0\.1\.1\] - 2026-11-03\s*$", text, re.MULTILINE)
    unreleased = text[text.index("## [Unreleased]") : text.index("## [0.1.1]")]
    assert unreleased.strip() == "## [Unreleased]"  # everything moved under the version
    # release.yml builds the notes from this body, the same way: heading to the next heading
    body = re.search(r"(?ms)^## \[0\.1\.1\][^\r\n]*\r?\n(?P<body>.*?)(?=^## \[|\Z)", text)
    assert body is not None
    for pr in ("#48", "#49", "#50", "#51"):
        assert pr in body.group("body"), pr
    for word in ("timeout", "Release", "signed in", "Create it now", "produced", "run"):
        assert word in body.group("body"), word
