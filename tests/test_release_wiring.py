"""Release wiring: the 0.1.0 changelog heading, the release workflow's build order, the manifest pin rule."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ns_supervisor import pwsh

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


def test_the_0_1_5_to_0_1_1_changelog_entries_are_in_place():
    version = read("VERSION").strip()
    assert version == "0.1.9"
    text = read("CHANGELOG.md")
    assert re.search(r"^## \[0\.1\.9\] - 2026-11-04\s*$", text, re.MULTILINE)
    nine = re.search(r"(?ms)^## \[0\.1\.9\][^\r\n]*\r?\n(?P<body>.*?)(?=^## \[|\Z)", text)
    assert nine is not None
    for word in ("rejects", "Settings clears", "uninstall forgets"):
        assert word in nine.group("body"), word
    assert re.search(r"^## \[0\.1\.8\] - 2026-11-04\s*$", text, re.MULTILINE)
    eight = re.search(r"(?ms)^## \[0\.1\.8\][^\r\n]*\r?\n(?P<body>.*?)(?=^## \[|\Z)", text)
    assert eight is not None
    words = ("dev-mode", "EOFError", "/api/health", "maxrate", "fetch_footage", "keep-awake",
             "Smart App Control")
    for word in words:
        assert word in eight.group("body"), word
    assert re.search(r"^## \[0\.1\.7\] - 2026-11-04\s*$", text, re.MULTILINE)
    seven = re.search(r"(?ms)^## \[0\.1\.7\][^\r\n]*\r?\n(?P<body>.*?)(?=^## \[|\Z)", text)
    assert seven is not None
    for word in ("faster-whisper", "SetThreadExecutionState", "Visual C++", "ASCII", "4551", "revision",
                 "fetch_footage", "Continue"):
        assert word in seven.group("body"), word
    assert re.search(r"^## \[0\.1\.6\] - 2026-11-04\s*$", text, re.MULTILINE)
    hotfix6 = re.search(r"(?ms)^## \[0\.1\.6\][^\r\n]*\r?\n(?P<body>.*?)(?=^## \[|\Z)", text)
    assert hotfix6 is not None and "User-Agent" in hotfix6.group("body") and "403" in hotfix6.group("body")
    assert re.search(r"^## \[0\.1\.5\] - 2026-11-03\s*$", text, re.MULTILINE)
    changes = re.search(r"(?ms)^## \[0\.1\.5\][^\r\n]*\r?\n(?P<body>.*?)(?=^## \[|\Z)", text)
    assert changes is not None
    for word in ("shortcuts_app", ".ns-app.done", "NS-W404", "cuda13", "drive root", "UNINSTALL.cmd"):
        assert word in changes.group("body"), word
    assert re.search(r"^## \[0\.1\.4\] - 2026-11-03\s*$", text, re.MULTILINE)
    assert re.search(r"^## \[0\.1\.3\] - 2026-11-03\s*$", text, re.MULTILINE)
    assert re.search(r"^## \[0\.1\.2\] - 2026-11-03\s*$", text, re.MULTILINE)
    assert re.search(r"^## \[0\.1\.1\] - 2026-11-03\s*$", text, re.MULTILINE)
    unreleased = text[text.index("## [Unreleased]") : text.index("## [0.1.9]")]
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
    for needle in ("git tag v0.1.9", "Verified on the partner PC", "Nightshift-Setup-0.1.9.zip"):
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


def test_the_manifest_pins_the_v0_1_4_voice_engine_asset_under_its_new_name():
    pin = json.loads(read("manifest.json"))["bins"]["qwentts"]
    assert pin["url"].endswith("/download/v0.1.4/Nightshift-VoiceEngine-win-cuda-x64.zip")
    assert pin["version"] == "0.1.4"
    # the digest GitHub shows for that release asset (gh api repos/.../releases/tags/v0.1.4)
    assert pin["sha256"] == "dac3dbdd1cc4291aebb18a127eff71aab2d5595437f35213ef6f64176ae4e6f5"


# ---- manifest.version follows VERSION at release build (install audit #8) -------------------------
def test_the_bundle_script_rewrites_the_manifest_version_from_the_version_file():
    script = read("dev/make_bundle.ps1")
    assert "manifest.json version:" in script and "'manifest.json'" in script
    assert "expected '$Version'" in script                                         # the zip is verified too
    # the pattern it uses must match the top-level line of the committed manifest and nothing nested
    text = read("manifest.json")
    matches = re.findall(r'(?m)^(  "version"\s*:\s*")[^"]*(")', text)
    assert len(matches) == 1


def test_a_bundle_dry_run_puts_the_version_file_number_in_the_zip_manifest():
    exe = pwsh.find_powershell()
    if exe is None or shutil.which("git") is None:
        pytest.skip("powershell or git not available")
    env = {k: v for k, v in os.environ.items() if k.lower() != "psmodulepath"}
    cmd = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
           str(ROOT / "dev" / "make_bundle.ps1"), "-DryRun"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=ROOT, env=env, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    version = read("VERSION").strip()
    assert f"manifest.json version: {version}" in proc.stdout
    assert f"verified: manifest.json version {version} = VERSION" in proc.stdout


def test_a_rerun_refreshes_the_venvs_and_the_smoke_test_when_the_manifest_version_changes(tmp_path):
    from installer.stage2.steps.s04_venvs import S04Venvs
    from installer.stage2.steps.s08_smoke import S08Smoke
    from installer.stage2.testing import example_answers, make_test_context

    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True)
    for name in ("studio", "supervisor"):
        py = ctx.venv_python(name)
        py.parent.mkdir(parents=True)
        py.write_bytes(b"MZ")
    for name, value in (("venvs_installed", True), ("venvs_version", ctx.manifest.version),
                        ("venvs_app", str(ctx.app_path)), ("smoke", "passed"),
                        ("smoke_version", ctx.manifest.version)):
        ctx.set_flag(name, value)
    assert S04Venvs().check(ctx) and S08Smoke().check(ctx)
    newer = ctx.manifest.model_copy(update={"version": ctx.manifest.version + ".1"})
    ctx.manifest = newer                                   # setup from a newer bundle, same install folder
    assert not S04Venvs().check(ctx) and not S08Smoke().check(ctx)


def test_the_venvs_are_refreshed_when_the_program_folder_moved(tmp_path):
    from installer.stage2.steps.s04_venvs import S04Venvs
    from installer.stage2.testing import example_answers, make_test_context

    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True)
    for name in ("studio", "supervisor"):
        py = ctx.venv_python(name)
        py.parent.mkdir(parents=True)
        py.write_bytes(b"MZ")
    ctx.set_flag("venvs_installed", True)
    ctx.set_flag("venvs_version", ctx.manifest.version)
    ctx.set_flag("venvs_app", str(tmp_path / "somewhere else"))
    assert not S04Venvs().check(ctx)                      # editable installs still point at the old folder
    ctx.set_flag("venvs_app", str(ctx.app_path))
    assert S04Venvs().check(ctx)
