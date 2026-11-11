"""Card update-signing: `dev/release_keys.ps1 -Create`, the owner's one step, run under Windows PowerShell 5.1.

Every outward effect is on a stand-in (`-DryRun -GhExe -KeysFile`): `gh` is a recorder, the keys file is a copy
with no key in it, the backup folder is a temporary one, and Python is called through a wrapper that writes
down its arguments. Without `-DryRun` a test run is refused before anything, so no test reaches the real
GitHub secret or this checkout. The keys made here are thrown away with the folders.
"""
# ruff: noqa: E501

from __future__ import annotations

import base64
import json
import os
import re
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ns_supervisor import pwsh
from tests.test_update_signing import ROOT, key_of, sigcheck

needs_powershell = pytest.mark.skipif(pwsh.find_powershell() is None, reason="powershell not on PATH")
SCRIPT = ROOT / "dev" / "release_keys.ps1"
BACKUP_NAME = "nightshift-release-backup-key.txt"
SECRET_CALL = ["secret", "set", "NS_RELEASE_SIGNING_KEY", "--repo", "AbdulrahmanAmer/nightshift"]
GH = """import json, os, sys
argv = sys.argv[1:]
given = sys.stdin.read() if argv[:2] == ["secret", "set"] else ""
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "gh-calls.jsonl"), "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": argv, "stdin": given}) + "\\n")
sys.exit(1 if os.environ.get("GH_FAIL") == argv[0] else 0)
"""


class Rig:
    """A backup folder, a keys file with no key, and the stand-ins for `gh` and Python."""

    def __init__(self, folder: Path) -> None:
        self.tools, self.backup, self.keys = folder / "tools", folder / "usb", folder / "checkout" / "release_keys.py"
        for made in (self.tools, self.backup, self.keys.parent):
            made.mkdir(parents=True)
        (self.tools / "gh.py").write_text(GH, encoding="utf-8")
        self.gh, self.py = self.tools / "gh.cmd", self.tools / "py.cmd"
        self.gh.write_text(f'@"{sys.executable}" "{self.tools / "gh.py"}" %*\r\n', encoding="ascii", newline="")
        wrapper = f'@echo off\r\necho %*>>"{self.tools / "py-args.txt"}"\r\n"{sys.executable}" %*\r\n'
        self.py.write_text(wrapper, encoding="ascii", newline="")
        text = (ROOT / "ns_common" / "release_keys.py").read_text(encoding="utf-8") if sigcheck else ""
        empty = re.sub(r"(?s)(# BEGIN TRUSTED[^\n]*\n).*?(# END TRUSTED)", r"\1TRUSTED: dict[str, str] = {}\n\2", text)
        self.keys.write_text(empty, encoding="utf-8", newline="\n")
        self.backup_file = self.backup / BACKUP_NAME

    def run(self, *, dry: bool = True, backup: Path | None = None, **env: str) -> subprocess.CompletedProcess[str]:
        argv = [str(pwsh.find_powershell()), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                str(SCRIPT), "-Create", "-BackupFolder", str(backup or self.backup)]  # fmt: skip
        if dry:
            argv += ["-DryRun", "-GhExe", str(self.gh), "-KeysFile", str(self.keys), "-Python", str(self.py)]
        return subprocess.run(argv, capture_output=True, text=True, timeout=180, env={**os.environ, **env}, check=False)

    def calls(self, what: str | None = None) -> list[dict[str, Any]]:
        log = self.tools / "gh-calls.jsonl"
        made = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
        return [c for c in made if what is None or c["argv"][0] == what]

    def trusted(self) -> dict[str, str]:
        return runpy.run_path(str(self.keys))["TRUSTED"]


def key_in(path: Path) -> bytes:
    lines = [ln for ln in path.read_text(encoding="ascii").splitlines() if ln and not ln.startswith("#")]
    assert len(lines) == 1, lines
    return base64.b64decode(lines[0], validate=True)


@needs_powershell
def test_create_hands_the_seed_to_gh_on_stdin_only(tmp_path: Path) -> None:
    assert SCRIPT.is_file() and sigcheck is not None, "dev/release_keys.ps1 is missing"
    rig = Rig(tmp_path)
    done = rig.run()
    said = done.stdout + done.stderr
    assert done.returncode == 0, said
    assert [c["argv"] for c in rig.calls("secret")] == [SECRET_CALL]  # the name and the repository, no value
    primary = base64.b64decode(rig.calls("secret")[0]["stdin"].strip(), validate=True)
    backup = key_in(rig.backup_file)
    assert len(primary) == 32 == len(backup) and primary != backup
    assert [p.name for p in rig.backup.iterdir()] == [BACKUP_NAME]  # written to the folder given, and only there
    assert list(rig.trusted().items()) == [key_of(primary), key_of(backup)]  # the public halves, primary first
    # nothing secret anywhere else: not printed, not an argument of gh or Python, not in the keys file
    secret = [base64.b64encode(k).decode("ascii") for k in (primary, backup)] + [primary.hex(), backup.hex()]
    elsewhere = said + (rig.tools / "py-args.txt").read_text(encoding="ascii", errors="replace")
    elsewhere += rig.keys.read_text(encoding="utf-8") + json.dumps([c["argv"] for c in rig.calls()])
    assert not [s for s in secret if s in elsewhere]
    assert key_of(primary)[0] in said and key_of(backup)[0] in said
    assert "Keep the backup file safe and off this PC; it is the only way to sign a release if the GitHub secret is lost." in said


@needs_powershell
def test_create_refuses_a_second_run_a_kept_backup_file_and_a_real_run_under_a_test(tmp_path: Path) -> None:
    assert SCRIPT.is_file(), "dev/release_keys.ps1 is missing"
    rig = Rig(tmp_path / "a")
    real = rig.run(dry=False)  # no -DryRun: a test run stops before the real gh and this checkout
    assert real.returncode == 1 and "-DryRun" in real.stdout and rig.calls() == [] and not rig.backup_file.exists()
    assert rig.run().returncode == 0
    state = (rig.keys.read_bytes(), rig.backup_file.read_bytes(), len(rig.calls()))
    again = rig.run()  # run twice: the keys exist
    assert again.returncode == 1 and "already exist" in again.stdout and '"Lost key"' in again.stdout
    assert (rig.keys.read_bytes(), rig.backup_file.read_bytes(), len(rig.calls())) == state
    kept = Rig(tmp_path / "b")
    kept.backup_file.write_text("mine\n", encoding="ascii")  # a file of that name is never overwritten or deleted
    done = kept.run()
    assert done.returncode == 1 and str(kept.backup_file) in done.stdout and "Nothing was changed" in done.stdout
    assert kept.backup_file.read_text(encoding="ascii") == "mine\n" and kept.trusted() == {} and not kept.calls("secret")
    for n, bad in enumerate((tmp_path / "not there", ROOT / "dev")):  # no such folder; one inside the checkout
        try:
            done = Rig(tmp_path / f"c{n}").run(backup=bad)
            assert done.returncode == 1 and "Nothing was changed" in done.stdout and not (bad / BACKUP_NAME).exists()
        finally:
            (bad / BACKUP_NAME).unlink(missing_ok=True)


@needs_powershell
def test_a_failed_secret_step_leaves_no_key_behind(tmp_path: Path) -> None:
    assert SCRIPT.is_file(), "dev/release_keys.ps1 is missing"
    rig = Rig(tmp_path / "a")
    failed = rig.run(GH_FAIL="secret")  # gh cannot set the secret (no rights, no network)
    assert failed.returncode == 1 and "Nothing was changed" in failed.stdout and len(rig.calls("secret")) == 1
    assert not rig.backup_file.exists() and rig.trusted() == {}
    signed_out = rig.run(GH_FAIL="auth")  # gh is not signed in: stopped before a key is made
    assert signed_out.returncode == 1 and "gh auth login" in signed_out.stdout and len(rig.calls("secret")) == 1
    assert not rig.backup_file.exists() and rig.trusted() == {}
    assert rig.run().returncode == 0 and len(rig.trusted()) == 2  # failed, then retried: the keys are made
