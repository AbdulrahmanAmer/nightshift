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
        names = ("qwentts-win-cuda-x64.zip", "Nightshift-VoiceEngine-win-cuda-x64.zip")
        assert "/releases/download/v" in pin["url"] and pin["url"].endswith(names)
        assert re.fullmatch(r"[0-9a-f]{64}", pin["sha256"])


def test_repo_settings_describe_the_tag_to_pin_loop():
    text = read("docs/REPO_SETTINGS.md")
    for needle in (
        "git tag v0.1.0", "build-qwentts", "bins.qwentts", "qwentts-win-cuda-x64.zip.sha256",
        "Nightshift-VoiceEngine-win-cuda-x64.zip",
        "Nightshift-Setup-<ver>.zip",
        "Nightshift-Update-<ver>.zip",
    ):
        assert needle in text, needle


def test_the_test_job_has_a_forty_minute_cap_in_ci_and_release():
    for rel in (".github/workflows/ci.yml", ".github/workflows/release.yml"):
        wf = read(rel)
        job = wf[wf.index("\n  test:") :]
        head = job[: job.index("steps:")]
        assert "timeout-minutes: 40" in head, rel


def test_the_0_1_3_0_1_2_and_0_1_1_changelog_entries_are_in_place():
    version = read("VERSION").strip()
    assert version == "0.1.4"
    text = read("CHANGELOG.md")
    assert re.search(r"^## \[0\.1\.4\] - 2026-11-03\s*$", text, re.MULTILINE)
    assert re.search(r"^## \[0\.1\.3\] - 2026-11-03\s*$", text, re.MULTILINE)
    assert re.search(r"^## \[0\.1\.2\] - 2026-11-03\s*$", text, re.MULTILINE)
    assert re.search(r"^## \[0\.1\.1\] - 2026-11-03\s*$", text, re.MULTILINE)
    unreleased = text[text.index("## [Unreleased]") : text.index("## [0.1.4]")]
    assert unreleased.startswith("## [Unreleased]")  # entries may accumulate here between releases
    first_run = re.search(r"(?ms)^## \[0\.1\.3\][^\r\n]*\r?\n(?P<body>.*?)(?=^## \[|\Z)", text)
    assert first_run is not None
    for word in ("WebView2", "nvidia-smi", "Browse", "50 Mbit/s", "total_rate"):
        assert word in first_run.group("body"), word
    # release.yml builds the notes from this body, the same way: heading to the next heading
    hotfix = re.search(r"(?ms)^## \[0\.1\.2\][^\r\n]*\r?\n(?P<body>.*?)(?=^## \[|\Z)", text)
    assert hotfix is not None
    for word in ("PowerShell 5.1", "X-NS-Token", "Release asset names", "exit code 14"):
        assert word in hotfix.group("body"), word
    body = re.search(r"(?ms)^## \[0\.1\.1\][^\r\n]*\r?\n(?P<body>.*?)(?=^## \[|\Z)", text)
    assert body is not None
    for pr in ("#48", "#49", "#50", "#51"):
        assert pr in body.group("body"), pr
    for word in ("timeout", "Release", "signed in", "Create it now", "produced", "run"):
        assert word in body.group("body"), word


def test_the_handoff_says_nothing_is_in_flight_and_points_at_the_first_partner_run():
    text = read("docs/HANDOFF.md")
    assert "## In flight\n- None." in text
    assert "feat/release-0.1.0" not in text.split("## Next steps")[1]  # the old release-prep step is done
    steps = text.split("## Next steps for a fresh orchestrator")[1]
    for needle in ("git tag v0.1.4", "Verified on the partner PC", "Nightshift-Setup-0.1.4.zip"):
        assert needle in steps, needle
    merged = text.split("## Merged on main")[1].split("## UI direction")[0]
    for pr in ("#48", "#51", "feat/release-0.1.1-pretag"):
        assert pr in merged, pr


def test_release_assets_use_the_setup_update_voiceengine_names():
    wf = read(".github/workflows/release.yml")
    for name in ("Nightshift-Setup-$version.zip", "Nightshift-Setup-$version.zip.sha256",
                 "Nightshift-Update-$version.zip", "Nightshift-Update-$version.zip.sha256"):
        assert f'"dist/{name}"' in wf, name
    assert "nightshift-app-" not in wf
    bundle = read("dev/make_bundle.ps1")
    assert 'Nightshift-Setup-$Version.zip' in bundle and 'Nightshift-Update-$Version.zip' in bundle
    qwen = read(".github/workflows/build-qwentts.yml")
    assert "Nightshift-VoiceEngine-win-cuda-x64.zip" in qwen and "dist/qwentts-win-cuda-x64" not in qwen


def test_release_body_template_has_the_download_table_and_changelog_pointer():
    tpl = read("dev/release_notes.md")
    for needle in ("Which file do I download?", "Nightshift-Setup-<ver>.zip", "Nightshift-Update-<ver>.zip",
                   "Update from a file", "Source code", "CHANGELOG.md", "Windows"):
        assert needle in tpl, needle
    wf = read(".github/workflows/release.yml")
    assert "dev/release_notes.md" in wf and ".Replace('<ver>', $version)" in wf


def test_the_manifest_pin_keeps_the_existing_asset_name_until_a_new_release_exists():
    pin = json.loads(read("manifest.json"))["bins"]["qwentts"]
    assert "/download/v0.1.0/qwentts-win-cuda-x64.zip" in pin["url"]
