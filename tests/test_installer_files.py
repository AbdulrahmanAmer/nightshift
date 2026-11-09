"""C1: static installer files (launchers, welcome text, answers example, PowerShell hygiene)."""

import json
import re
from pathlib import Path

import jsonschema
import pytest

from installer.stage2.answers import InstallerAnswers, build_answers, load_raw

ROOT = Path(__file__).resolve().parents[1]
PS1_FILES = sorted((ROOT / "installer").rglob("*.ps1"))


def test_install_cmd_runs_stage1_then_pauses():
    text = (ROOT / "INSTALL.cmd").read_text(encoding="utf-8")
    assert (
        r'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\stage1.ps1" -AppPath "%~dp0."'
        in text
    )
    assert "pause" in text.lower()


def test_uninstall_cmd_runs_uninstall_ps1():
    text = (ROOT / "UNINSTALL.cmd").read_text(encoding="utf-8")
    assert "-ExecutionPolicy Bypass" in text
    assert r'"%~dp0installer\uninstall.ps1"' in text
    assert "pause" in text.lower()


def test_welcome_text_is_spec_a2_verbatim():
    spec = (ROOT / "BUILD_SPEC.md").read_text(encoding="utf-8").splitlines()
    start = spec.index("### A.2 Installer welcome screen (exact text shown by `INSTALL.cmd`)") + 1
    block: list[str] = []
    for line in spec[start:]:
        if line.startswith(">"):
            block.append(line[2:] if line.startswith("> ") else "")
        elif block:
            break
    expected = "\n".join(block).strip()
    actual = (ROOT / "installer" / "welcome.txt").read_text(encoding="utf-8").strip()
    assert actual == expected
    assert "Welcome to Nightshift" in actual and "~20 GB" in actual


@pytest.mark.parametrize("script", PS1_FILES, ids=lambda p: p.name)
def test_powershell_hygiene(script: Path):
    raw = script.read_bytes()
    raw.decode("ascii")  # ASCII only
    text = raw.decode("ascii")
    head = "\n".join(text.splitlines()[:60])
    assert "Set-StrictMode -Version Latest" in head
    assert "$ErrorActionPreference = 'Stop'" in head
    assert not re.search(r"invoke-expression|\biex\b", text, re.IGNORECASE)
    assert "-ExecutionPolicy" not in text


def test_expected_scripts_exist():
    names = {p.name for p in PS1_FILES}
    assert {"stage1.ps1", "uninstall.ps1"} <= names


def test_answers_example_is_valid_and_has_blank_secrets():
    path = ROOT / "installer" / "answers.example.json"
    ans = build_answers(load_raw(path))
    assert isinstance(ans, InstallerAnswers)
    assert not ans.pexels_api_key and not ans.pixabay_api_key
    assert ans.llm_tier == "balanced" and ans.run_time == "03:00" and ans.masters_per_week == 1


def test_answers_example_matches_exported_schema():
    schema = json.loads((ROOT / "contracts" / "schemas" / "answers.schema.json").read_text("utf-8"))
    known = set(schema["properties"])
    example = json.loads((ROOT / "installer" / "answers.example.json").read_text("utf-8"))
    jsonschema.validate({k: v for k, v in example.items() if k in known}, schema)
    # Keys the published schema does not know yet are the ones filed in CR-C1-2.
    pending = set(example) - known
    assert pending <= {"home_path", "models_source", "llm_tier", "restore_from", "include_voicedesign"}


def test_installer_ui_build_is_committed():
    static = ROOT / "installer" / "ui" / "static"
    for name in ("index.html", "app.js", "app.css"):
        assert (static / name).is_file(), name
    html = (static / "index.html").read_text(encoding="utf-8")
    assert 'data-theme="dark"' in html                      # dark is the installer's default theme
    assert re.search(r'<script[^>]*src="\./app\.js"', html)
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", html), "no inline scripts (CSP)"


def test_operator_copy_covers_every_code_the_installer_can_raise():
    copy = json.loads((ROOT / "installer" / "stage2" / "web" / "operator_copy.json").read_text("utf-8"))
    steps_dir = ROOT / "installer" / "stage2" / "steps"
    files = [ROOT / "installer" / "stage1.ps1", *sorted(steps_dir.glob("*.py")),
             ROOT / "installer" / "stage2" / "web" / "bootstrap.py"]
    used = set()
    for f in files:
        used |= set(re.findall(r"NS-[EW]\d{3}", f.read_text(encoding="utf-8")))
    assert {"NS-E113", "NS-W403"} <= used
    missing = used - set(copy) - {"NS-E703"}      # NS-E703 is s07's restore message, not a failure
    assert not missing, f"operator_copy.json lacks {sorted(missing)}"
    for code, entry in copy.items():
        assert entry["title"] and entry["whatToDo"], code
        assert not entry["whatToDo"].endswith("!") and "!" not in entry["title"], code


# ns_common modules with only standard-library imports (and each other): the bare-Python server may use them.
STDLIB_ONLY_NS_COMMON = {
    "ns_common.atomic", "ns_common.avblock", "ns_common.devmode", "ns_common.errors", "ns_common.paths",
    "ns_common.pathrules",
    "ns_common.uv", "ns_common.vcruntime", "ns_common.secret_file", "ns_common.secrets_names",
    "ns_common.singleton",
}
# installer.stage2.devmode (dev mode numbers, stubs, mode switch) is stdlib-only too and shared by the setup
# server and the wizard; the test below keeps it that way.
STDLIB_ONLY_INSTALLER = {"installer.stage2.devmode", "installer.stage2.sysinfo"}


def test_the_ns_common_modules_the_setup_server_uses_are_really_stdlib_only():
    import ast
    import sys

    for name in STDLIB_ONLY_NS_COMMON:
        tree = ast.parse((ROOT / (name.replace(".", "/") + ".py")).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module]
            for m in mods:
                ok = m.split(".")[0] in sys.stdlib_module_names or m in STDLIB_ONLY_NS_COMMON
                assert ok or m == "__future__", f"{name} imports {m}"


def test_the_dev_mode_module_shared_with_the_setup_server_is_stdlib_only():
    import ast
    import sys

    tree = ast.parse((ROOT / "installer" / "stage2" / "devmode.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods = [node.module]
        for m in mods:
            ok = m.split(".")[0] in sys.stdlib_module_names or m in STDLIB_ONLY_NS_COMMON
            assert ok or m == "__future__", f"installer/stage2/devmode.py imports {m}"


def test_web_package_imports_only_the_standard_library():
    import ast
    import sys

    allowed_first_party = "installer.stage2.web"
    offenders = []
    for path in sorted((ROOT / "installer" / "stage2" / "web").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if top in sys.stdlib_module_names or top == "__future__":
                    continue
                if name == allowed_first_party or name.startswith(allowed_first_party + "."):
                    continue
                if name in STDLIB_ONLY_NS_COMMON or name in STDLIB_ONLY_INSTALLER:
                    continue
                offenders.append(f"{path.name}: {name}")
    assert not offenders, "the setup server runs on the bare Python: " + ", ".join(offenders)


ALLOWED_WEB_IMPORTS = (
    "installer.stage2.web", "installer.stage2.web.samples", "installer.stage2.web.pathcheck",
)


def test_only_the_samples_helpers_are_shared_with_the_wizard():
    # P2 may import installer.stage2.web.samples and .pathcheck (stdlib only; pathcheck is the one rule for
    # what a folder holds, D-21); the rest of the web package is P1's.
    wizard_side = [ROOT / "installer" / "stage2" / "wizard.py", ROOT / "installer" / "stage2" / "bridge.py",
                   *sorted((ROOT / "installer" / "stage2" / "steps").glob("*.py"))]
    for f in wizard_side:
        for m in re.findall(r"installer\.stage2\.web(?:\.\w+)?", f.read_text(encoding="utf-8")):
            assert m in ALLOWED_WEB_IMPORTS, f"{f.name} imports {m}"


def test_operator_guide_covers_the_voice_fallback_the_design_model_and_the_keys_file():
    text = (ROOT / "OPERATOR.md").read_text(encoding="utf-8")
    sections = {line[3:].strip(): i for i, line in enumerate(text.splitlines()) if line.startswith("## ")}
    titles = (
        "If the voice engine fell back",
        "Get the voice design model",
        "If stock footage stops at night",
    )
    for title in titles:
        assert title in sections, title
    body = text.split("## If the voice engine fell back", 1)[1].split("## Start a video later", 1)[0]
    assert "NS-W301" in body and "NS-E314" in body and "NS-E311" in body
    assert "Settings, Models" in body and "ns models get voicedesign" in body
    assert "config\secrets.dpapi" in body and "Credential Manager" in body


def test_uninstall_cmd_leaves_the_program_folder_before_it_runs_and_pauses():
    # Explorer starts the console in <home>\app (where UNINSTALL.cmd lives); that folder is deleted by the
    # uninstall, and a console left at "pause" with it as working directory would keep it from going.
    lines = [ln.strip().lower() for ln in (ROOT / "UNINSTALL.cmd").read_text(encoding="utf-8").splitlines()]
    cd = next(i for i, ln in enumerate(lines) if ln.startswith("cd /d") and "%temp%" in ln)
    run = next(i for i, ln in enumerate(lines) if ln.startswith("powershell"))
    pause = next(i for i, ln in enumerate(lines) if ln == "pause")
    assert cd < run < pause
