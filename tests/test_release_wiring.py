"""Release wiring: the 0.1.0 changelog heading, the release workflow's build order, the manifest pin rule."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from ns_supervisor import pwsh

ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_changelog_has_a_heading_for_the_version_file():
    # a release candidate (VERSION 0.1.15-rc5) uses the heading of the final version it previews (H-12)
    version = read("VERSION").strip().split("-rc", 1)[0]
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


def test_the_test_shards_have_a_cap_well_under_the_old_forty_minutes_in_ci_and_release():
    # the single test job (40 minutes, about 28 used) became four shards of about 8 minutes of tests (H-3)
    for rel in (".github/workflows/ci.yml", ".github/workflows/release.yml"):
        jobs = yaml.safe_load(read(rel))["jobs"]
        assert 15 <= jobs["test-shard"]["timeout-minutes"] < 40, rel
        assert jobs["test"]["timeout-minutes"] <= 20, rel


def test_the_0_1_5_to_0_1_1_changelog_entries_are_in_place():
    # a release or a release candidate; the three version files agree; the changelog has a dated heading for
    # its base version. No literal number here, so the next bump needs no edit of this test
    version = read("VERSION").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+(-rc[1-9]\d*)?", version), version
    assert f'version = "{version}"' in read("pyproject.toml").splitlines()
    assert f'__version__ = "{version}"' in read("ns_common/__init__.py")
    base = version.split("-rc", 1)[0]
    text = read("CHANGELOG.md")
    assert re.search(rf"^## \[{re.escape(base)}\] - \d{{4}}-\d{{2}}-\d{{2}}\s*$", text, re.MULTILINE), base
    assert re.search(r"^## \[0\.1\.14\] - 2026-11-09\s*$", text, re.MULTILINE)
    fourteen = re.search(r"(?ms)^## \[0\.1\.14\][^\r\n]*\r?\n(?P<body>.*?)(?=^## \[|\Z)", text)
    assert fourteen is not None
    words14 = ('NS-E613', 'NS-W405', 'NS-E326', 'NS-E503', 'NS-E104', 'wizard_flags.json',
               'NS_DASHBOARD_NO_HOST')
    for word in words14:
        assert word in fourteen.group("body"), word
    assert re.search(r"^## \[0\.1\.13\] - 2026-11-09\s*$", text, re.MULTILINE)
    thirteen = re.search(r"(?ms)^## \[0\.1\.13\][^\r\n]*\r?\n(?P<body>.*?)(?=^## \[|\Z)", text)
    assert thirteen is not None
    words = ("start_script_job", "fake_footage", "NS-W115", "opencode.json", "NS-E327", "schedule_mode",
             "studio dashboard")
    for word in words:
        assert word in thirteen.group("body"), word
    assert re.search(r"^## \[0\.1\.12\] - 2026-11-09\s*$", text, re.MULTILINE)
    ten = re.search(r"(?ms)^## \[0\.1\.10\][^\r\n]*\r?\n(?P<body>.*?)(?=^## \[|\Z)", text)
    assert ten is not None
    for word in ("tool calls", "accepted the key", "Hide", "full video", "NS-W406", "NS-W405"):
        assert word in ten.group("body"), word
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
    unreleased = text[text.index("## [Unreleased]") : text.index("## [0.1.14]")]
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
    for needle in ("git tag v0.1.14", "Verified on the partner PC", "Nightshift-Setup-0.1.14.zip"):
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


# ---- card gpu-kit: the one-command kit is in the Setup zip --------------------------------------------
def test_the_setup_zip_carries_the_gpu_kit():
    entries = re.search(r"(?m)^\$RequiredEntries = @\((.*)\)\s*$", read("dev/make_bundle.ps1"))
    assert entries, "make_bundle.ps1 has no $RequiredEntries line"
    required = re.findall(r"'([^']+)'", entries.group(1))
    assert {"GPU-KIT.cmd", "dev/kit/gpukit.ps1", "dev/kit/gpukit.py"} <= set(required)
    raw = (ROOT / "GPU-KIT.cmd").read_bytes()
    raw.decode("ascii")
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")  # CRLF only
    text = raw.decode("ascii")
    assert r'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev\kit\gpukit.ps1" %*' in text
    assert "pause" in text.lower()
    from installer.stage2.web import appcopy

    assert "gpu-kit.cmd" in appcopy.ROOT_ENTRIES  # setup copies it into <home>\app with dev\kit
    exe = shutil.which("powershell.exe")  # Windows PowerShell 5.1, as GPU-KIT.cmd runs it
    if exe is None:
        pytest.skip("Windows PowerShell 5.1 not available")
    check = ("$e = $null; $t = $null; "
             "[void][System.Management.Automation.Language.Parser]::ParseFile($env:NS_PS1, "
             "[ref]$t, [ref]$e); if ($e.Count) { $e | ForEach-Object { $_.Message }; exit 1 }; "
             "$PSVersionTable.PSVersion.Major")  # fmt: skip
    for rel in ("dev/kit/gpukit.ps1", "dev/kit/tools/nsup.ps1", "dev/kit/tools/nsproc.ps1"):
        env = {**os.environ, "NS_PS1": str(ROOT / rel)}
        done = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", check], capture_output=True,
                              text=True, timeout=120, env=env, check=False)  # fmt: skip
        assert done.returncode == 0 and done.stdout.strip() == "5", (rel, done.stdout, done.stderr)


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
    # P29a: the zip holds the dependency lock setup and ns update install from (an entry of the file list)
    assert re.search(r"(?m)^\s+\d+\s+uv\.lock\s*$", proc.stdout), "uv.lock is not in the bundle"


def test_a_rerun_refreshes_the_venvs_and_the_smoke_test_when_the_manifest_version_changes(tmp_path):
    from installer.stage2.steps.s04_venvs import S04Venvs
    from installer.stage2.steps.s08_smoke import S08Smoke
    from installer.stage2.testing import example_answers, make_test_context

    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True)
    S04Venvs().run(ctx)                       # packages in both venvs, not just an interpreter
    for name, value in (("smoke", "passed"), ("smoke_version", ctx.manifest.version)):
        ctx.set_flag(name, value)
    assert S04Venvs().check(ctx) and S08Smoke().check(ctx)
    newer = ctx.manifest.model_copy(update={"version": ctx.manifest.version + ".1"})
    ctx.manifest = newer                                   # setup from a newer bundle, same install folder
    assert not S04Venvs().check(ctx) and not S08Smoke().check(ctx)


def test_the_venvs_are_refreshed_when_the_program_folder_moved(tmp_path):
    from installer.stage2.steps.s04_venvs import S04Venvs
    from installer.stage2.testing import example_answers, make_test_context

    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True)
    S04Venvs().run(ctx)
    ctx.set_flag("venvs_app", str(tmp_path / "somewhere else"))
    assert not S04Venvs().check(ctx)                      # editable installs still point at the old folder
    ctx.set_flag("venvs_app", str(ctx.app_path))
    assert S04Venvs().check(ctx)


# ---- H-12: a tag like v0.1.15-rc1 is a prerelease candidate, never "latest" ----------------------------
#
# The decision (recorded here because the workflow is YAML and cannot say it in a test name):
#   * a candidate tag is `vX.Y.Z-rcN` (N >= 1); VERSION holds `X.Y.Z-rcN`, the tag without the `v`;
#   * its changelog check demands the heading of the FINAL version, `## [X.Y.Z]` (the entries a candidate
#     previews are the ones the final will carry), and also accepts an exact `## [X.Y.Z-rcN]` heading;
#   * a plain `vX.Y.Z` tag demands `## [X.Y.Z]` exactly as before;
#   * any other suffix (`-beta1`, `-rc`, `rc1`, `-rc0`, a fourth number) fails the check.
RELEASE_YML = ".github/workflows/release.yml"
CHANGELOG_SAMPLE = """# Changelog

## [Unreleased]

## [0.1.15] - 2026-11-10

### Fixed
- the entry the release body must carry

## [0.1.14] - 2026-11-09

- older
"""


def _release_step(needle: str) -> str:
    wf = yaml.safe_load(read(RELEASE_YML))
    found = [s for s in wf["jobs"]["release"]["steps"] if needle in s.get("name", "")]
    assert len(found) == 1, needle
    assert found[0]["shell"] == "pwsh"
    return found[0]["run"]


def _run_pwsh(script: str, cwd: Path, env_extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
    exe = pwsh.find_powershell()
    if exe is None:
        pytest.skip("powershell not available")
    file = cwd / "step.ps1"
    file.write_text(script, encoding="utf-8-sig")  # a BOM keeps Windows PowerShell 5.1 reading it as UTF-8
    env = {k: v for k, v in os.environ.items() if k.lower() != "psmodulepath"}
    env.update(env_extra)
    return subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(file)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=cwd,
        env=env,
        check=False,
    )


def _check_tag(tmp_path: Path, tag: str, version_file: str, changelog: str = CHANGELOG_SAMPLE):
    (tmp_path / "dev").mkdir(exist_ok=True)
    (tmp_path / "release-notes.md").unlink(missing_ok=True)
    (tmp_path / "VERSION").write_text(version_file + "\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    template = tmp_path / "dev" / "release_notes.md"
    template.write_text("Download Nightshift-Setup-<ver>.zip\n", encoding="utf-8")
    return _run_pwsh(_release_step("Tag, VERSION and CHANGELOG agree"), tmp_path, {"TAG": tag})


@pytest.mark.parametrize("tag", ["v0.1.15", "v0.1.15-rc1", "v0.1.15-rc2", "v0.1.15-rc12"])
def test_the_release_check_accepts_a_final_tag_and_a_candidate_tag(tmp_path: Path, tag: str):
    proc = _check_tag(tmp_path, tag, tag[1:])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    notes = (tmp_path / "release-notes.md").read_text(encoding="utf-8")
    assert f"Nightshift-Setup-{tag[1:]}.zip" in notes  # the download table names the candidate's own files
    # a candidate carries the changelog entries of the final version it previews
    assert "the entry the release body must carry" in notes


@pytest.mark.parametrize(
    "tag",
    [
        "v0.1.15rc1",
        "v0.1.15-rc",
        "v0.1.15-rc0",
        "v0.1.15-beta1",
        "v0.1.15-rc1-x",
        "v0.1.15.1",
        "v0.1",
        "vfoo",
    ],
)
def test_the_release_check_rejects_bad_tag_forms(tmp_path: Path, tag: str):
    proc = _check_tag(tmp_path, tag, tag[1:])  # VERSION agrees with the tag, so only the form can fail it
    assert proc.returncode != 0, proc.stdout
    assert "not a release tag" in proc.stdout + proc.stderr
    assert not (tmp_path / "release-notes.md").exists()


def test_the_release_check_still_requires_the_tag_to_equal_version(tmp_path: Path):
    for tag, version in (("v0.1.15-rc2", "0.1.15-rc1"), ("v0.1.15", "0.1.15-rc1"), ("v0.1.15-rc1", "0.1.15")):
        proc = _check_tag(tmp_path, tag, version)
        assert proc.returncode != 0, (tag, version)
        assert "does not match VERSION" in proc.stdout + proc.stderr


def test_a_final_tag_needs_its_own_changelog_heading_and_a_candidate_may_use_the_final_one(tmp_path: Path):
    only_rc_heading = CHANGELOG_SAMPLE.replace("## [0.1.15] - 2026-11-10", "## [0.1.15-rc1] - 2026-11-10")
    # the final release must have the final heading: an rc heading is not enough
    proc = _check_tag(tmp_path, "v0.1.15", "0.1.15", only_rc_heading)
    assert proc.returncode != 0 and "no '## [0.1.15]' heading" in proc.stdout + proc.stderr
    # a candidate accepts its own exact heading ...
    proc = _check_tag(tmp_path, "v0.1.15-rc1", "0.1.15-rc1", only_rc_heading)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # ... and with no heading for the version at all it fails, naming the final heading to add
    proc = _check_tag(tmp_path, "v0.1.16-rc1", "0.1.16-rc1")
    assert proc.returncode != 0 and "no '## [0.1.16]' heading" in proc.stdout + proc.stderr


FAKE_GH = """
$global:LASTEXITCODE = 0
function gh {
  Add-Content -LiteralPath $env:GH_LOG -Value ($args -join ' ')
  $code = 0
  if ($args[0] -eq 'release' -and $args[1] -eq 'view') { $code = [int]$env:GH_VIEW_EXIT }
  $global:LASTEXITCODE = $code
}
"""


def _publish(tmp_path: Path, tag: str, release_exists: bool) -> list[str]:
    log = tmp_path / "gh.log"
    log.write_text("", encoding="utf-8")
    env = {"TAG": tag, "GH_LOG": str(log), "GH_VIEW_EXIT": "0" if release_exists else "1"}
    proc = _run_pwsh(FAKE_GH + _release_step("Publish GitHub Release"), tmp_path, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_a_candidate_tag_is_published_as_a_prerelease_and_never_as_latest(tmp_path: Path):
    calls = _publish(tmp_path, "v0.1.15-rc1", release_exists=False)
    create = [c for c in calls if c.startswith("release create")]
    assert len(create) == 1 and "--prerelease" in create[0].split() and "--latest=false" in create[0].split()
    # build-qwentts.yml may have made the release first, as a final: the flags are forced on whichever made it
    calls = _publish(tmp_path, "v0.1.15-rc1", release_exists=True)
    assert not [c for c in calls if c.startswith("release create")]
    edit = [c for c in calls if c.startswith("release edit")]
    assert edit == ["release edit v0.1.15-rc1 --prerelease --latest=false"]
    # the flags are in place before the assets arrive
    assert calls.index(edit[0]) < next(i for i, c in enumerate(calls) if c.startswith("release upload"))


def test_a_final_tag_is_published_as_today_without_the_prerelease_flags(tmp_path: Path):
    for exists in (False, True):
        calls = _publish(tmp_path, "v0.1.15", release_exists=exists)
        flagged = [c for c in calls if "--prerelease" in c or "--latest" in c or c.startswith("release edit")]
        assert not flagged
        assert any(c.startswith("release upload v0.1.15 ") for c in calls)
    assert any(c.startswith("release create v0.1.15 --title") for c in _publish(tmp_path, "v0.1.15", False))


def test_build_qwentts_makes_a_candidate_release_a_prerelease_too():
    # whichever of release.yml and build-qwentts.yml creates the release first, a -rcN tag is a prerelease
    wf = read(".github/workflows/build-qwentts.yml")
    assert "--prerelease" in wf and "--latest=false" in wf
    assert re.search(r"case \"\$TAG\" in\s+v\*-\*\)", wf), "the flags depend on a prerelease part"
    create = wf[wf.index("gh release create") :].split("\n", 1)[0]
    assert "flags[@]" in create


# ---- the updater never offers a prerelease -------------------------------------------------------------
def _github_api(requests: list[str]):
    """A GitHub with the final v0.1.14 and a newer candidate; /releases/latest never returns a prerelease."""
    import httpx

    final = {"tag_name": "v0.1.14", "html_url": "https://github.com/x/y/releases/tag/v0.1.14", "assets": []}
    candidate = {**final, "tag_name": "v0.1.15-rc1", "prerelease": True}

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req.url.path)
        if req.url.path.endswith("/releases/latest"):
            return httpx.Response(200, json=final)
        if req.url.path.endswith("/releases"):  # the list: newest first, the candidate on top
            return httpx.Response(200, json=[candidate, final])
        return httpx.Response(404, json={"message": "Not Found"})

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_a_prerelease_is_not_offered_as_the_update(tmp_home: Path):  # the check reads the install's home
    from ns_supervisor import releases

    requests: list[str] = []
    res = releases.check("0.1.14", _github_api(requests))
    assert res["update_available"] is False and res["latest"] == "0.1.14"
    # the updater reads only GitHub's "latest release" endpoint, which skips prereleases and drafts; it never
    # reads the list of releases, where the candidate would be the newest entry
    assert requests == ["/repos/AbdulrahmanAmer/nightshift/releases/latest"]


def test_a_prerelease_or_draft_answered_as_the_latest_release_is_refused(tmp_home: Path):
    # the guard is in the code, not in the endpoint: if /releases/latest ever returned a candidate (or a
    # draft), `ns update --check`, `--github`, the stable git channel and the dashboard would offer it.
    import httpx
    import pytest

    from ns_common.errors import NsError
    from ns_supervisor import releases

    for flag in ("prerelease", "draft"):
        body = {"tag_name": "v0.1.15-rc1", "html_url": "https://github.com/x/y", "assets": [], flag: True}
        client = httpx.Client(transport=httpx.MockTransport(lambda req, b=body: httpx.Response(200, json=b)))
        with pytest.raises(NsError) as e:
            releases.check("0.1.14", client)
        assert e.value.code == "NS-E701", flag
        with pytest.raises(NsError):
            releases.fetch_latest(client)
        with pytest.raises(NsError):
            releases.parse_release(body)
    # a normal final release (both flags false) is still read
    ok = {"tag_name": "v0.1.15", "assets": [], "prerelease": False, "draft": False}
    assert releases.parse_release(ok).version == "0.1.15"


def test_candidates_sort_below_their_final_and_in_number_order():
    from ns_supervisor import releases

    assert releases.is_newer("0.1.15", "0.1.15-rc1") and not releases.is_newer("0.1.15-rc1", "0.1.15")
    assert releases.is_newer("0.1.15-rc2", "0.1.15-rc1") and not releases.is_newer("0.1.15-rc1", "0.1.15-rc2")
    assert releases.is_newer("0.1.15-rc10", "0.1.15-rc2")  # rc10 is after rc2, not before it as text
    assert releases.is_newer("0.1.15-rc1", "0.1.14") and not releases.is_newer("0.1.14", "0.1.15-rc1")
    # an installation on a candidate is offered the final, and never an older final
    assert releases.is_newer("0.1.15", "0.1.15-rc3") and not releases.is_newer("0.1.14", "0.1.15-rc3")
