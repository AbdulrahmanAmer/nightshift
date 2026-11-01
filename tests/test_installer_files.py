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
    assert "Welcome to Nightshift" in actual and "~15 GB" in actual


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
    assert ans.llm_tier == "small" and ans.run_time == "03:00" and ans.masters_per_week == 1


def test_answers_example_matches_exported_schema():
    schema = json.loads((ROOT / "contracts" / "schemas" / "answers.schema.json").read_text("utf-8"))
    known = set(schema["properties"])
    example = json.loads((ROOT / "installer" / "answers.example.json").read_text("utf-8"))
    jsonschema.validate({k: v for k, v in example.items() if k in known}, schema)
    # Keys the published schema does not know yet are the ones filed in CR-C1-2.
    pending = set(example) - known
    assert pending <= {"home_path", "models_source", "llm_tier", "restore_from"}
