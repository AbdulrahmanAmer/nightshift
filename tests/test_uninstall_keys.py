# ruff: noqa: E501
"""FN-4, T4: the uninstaller says what happened to the stock-footage keys, in every state.

A key is kept in two places: Windows Credential Manager (one per Windows account, shared by every Nightshift
folder of that account) and `config\\secrets.dpapi` (one per Nightshift folder). The uninstaller printed
"forgot the stock-footage keys in Credential Manager" whatever happened, and the window's closing screen
listed the same keys under what stays. Now one Python function knows both places and returns the lines;
`uninstall.ps1` prints them as they are. Credential Manager is a fake here (`FakeVault`): no test reads or
changes the real one.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import venv
from pathlib import Path
from typing import Any

import pytest

from installer.stage2 import secrets
from installer.stage2.web import uninstall as window
from ns_common.paths import Paths
from ns_common.secret_file import FILE_NAME, secret_path
from ns_common.secrets_names import PEXELS_KEY, PIXABAY_KEY
from ns_supervisor import pwsh

ROOT = Path(__file__).resolve().parents[1]
UNINSTALL = ROOT / "installer" / "uninstall.ps1"
needs_powershell = pytest.mark.skipif(pwsh.find_powershell() is None, reason="needs PowerShell")
BOTH = (PEXELS_KEY, PIXABAY_KEY)
UNSEEN = (
    "  removed the Pexels key from Credential Manager if it was there (it could not be read first: "
    "RuntimeError: the vault is locked)"
)
OTHER = "D:\\Other Nightshift"
PANEL = (
    "open Control Panel > Credential Manager > Windows Credentials and remove every entry whose name ends "
    "in Nightshift"
)


class FakeVault:
    """Credential Manager as `installer.stage2.secrets.KeyringStore` gives it: get and delete by name.

    `refuse`: deleting these names fails. `unreadable`: reading these names fails. `missing`: the keyring
    package is not installed (every call raises `ImportError`, as `import keyring` does)."""

    def __init__(self, *names: str, refuse: tuple[str, ...] = (), unreadable: tuple[str, ...] = (), missing: bool = False) -> None:
        self.values = {name: f"value-of-{name}" for name in names}
        self.refuse, self.unreadable, self.missing = refuse, unreadable, missing
        self.calls: list[str] = []

    def get(self, name: str) -> str | None:
        self.calls.append(f"get {name}")
        if self.missing:
            raise ModuleNotFoundError("No module named 'keyring'")
        if name in self.unreadable:
            raise RuntimeError("the vault is locked")
        return self.values.get(name)

    def delete(self, name: str) -> None:
        self.calls.append(f"delete {name}")
        if self.missing:
            raise ModuleNotFoundError("No module named 'keyring'")
        if name in self.refuse:
            raise PermissionError(5, "Access is denied")
        self.values.pop(name, None)


def home_with_file(tmp_path: Path, *names: str, text: str | None = None) -> Paths:
    """A Nightshift folder; with `names` (or `text`) its `config\\secrets.dpapi` exists."""
    paths = Paths(tmp_path / "ns")
    paths.config.mkdir(parents=True)
    if names or text is not None:
        body = text if text is not None else json.dumps({name: "YmxvYg==" for name in names})
        secret_path(paths).write_text(body, encoding="utf-8")
    return paths


def said(paths: Paths, vault: FakeVault, **kw: Any) -> tuple[list[str], bool | None]:
    """The key lines of the uninstaller for this state, and whether Credential Manager was asked.

    On a base without `forget_stock_keys` the line is the script's own fixed text, whatever happened."""
    forget = getattr(secrets, "forget_stock_keys", None)
    if forget is None:
        fixed = re.search(r"Write-Host '(  forgot the stock-footage keys[^']*)'", UNINSTALL.read_text(encoding="utf-8"))
        return ([fixed.group(1)] if fixed else []), None
    out = forget(paths, vault, **kw)
    return list(out.lines), out.vault_asked


# what each state is: (Credential Manager holds, refuses, cannot read), the file's key names, then the lines
STATES: dict[str, tuple[dict[str, Any], tuple[str, ...], list[str]]] = {
    "both keys in both places": (
        {"names": BOTH}, BOTH,
        ["  forgot the Pexels and Pixabay keys in Credential Manager",
         "  removed {file} (the copy of the Pexels and Pixabay keys for nightly runs)"],
    ),
    "nothing was stored": (
        {"names": ()}, (),
        ["  no stock-footage keys were stored (Credential Manager and config\\secrets.dpapi held none)"],
    ),
    "one key, in Credential Manager only": (
        {"names": (PIXABAY_KEY,)}, (),
        ["  forgot the Pixabay key in Credential Manager"],
    ),
    "one key, in the file only": (
        {"names": ()}, (PIXABAY_KEY,),
        ["  removed {file} (the copy of the Pixabay key for nightly runs)"],
    ),
    "Credential Manager refuses one key": (
        {"names": BOTH, "refuse": (PEXELS_KEY,)}, BOTH,
        ["  forgot the Pixabay key in Credential Manager",
         "  could not remove the Pexels key from Credential Manager (PermissionError: Access is denied): " + PANEL,
         "  removed {file} (the copy of the Pexels and Pixabay keys for nightly runs)"],
    ),
    "Credential Manager cannot be read, the removal goes through": (
        {"names": BOTH, "unreadable": (PEXELS_KEY,)}, (),
        ["  forgot the Pixabay key in Credential Manager", UNSEEN],
    ),
}


@pytest.mark.parametrize("state", STATES)
def test_each_key_line_says_what_happened(state: str, tmp_path: Path) -> None:
    held, in_file, lines = STATES[state]
    paths = home_with_file(tmp_path, *in_file)
    vault = FakeVault(*held["names"], refuse=held.get("refuse", ()), unreadable=held.get("unreadable", ()))
    got, asked = said(paths, vault)
    assert got == [ln.format(file=secret_path(paths)) for ln in lines]
    assert asked is True
    # the effect, not only the words: what a line calls forgotten is gone, what it calls refused is there
    assert sorted(vault.values) == sorted(held.get("refuse", ()))
    assert not secret_path(paths).exists()
    for line in got:
        assert window.is_key_line(line), line   # the window finds every one of these lines
    # empty / nothing to do, and the second run (a run killed halfway is run again): each says only what is true now
    again, _ = said(paths, vault)
    if held.get("refuse"):
        assert again == ["  could not remove the Pexels key from Credential Manager (PermissionError: Access is denied): " + PANEL]
    elif held.get("unreadable"):
        assert again == [UNSEEN]    # still unreadable: it never claims a key it did not see
    else:
        assert again == ["  no stock-footage keys were stored (Credential Manager and config\\secrets.dpapi held none)"]


def test_a_keys_file_that_cannot_be_removed_is_named_with_what_to_do(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = home_with_file(tmp_path, *BOTH)
    vault = FakeVault(*BOTH)

    def refuse(path: Path) -> None:
        raise PermissionError(13, "The process cannot access the file because it is being used by another process")

    assert getattr(secrets, "forget_stock_keys", None) is not None, "the key lines are still fixed PowerShell text"
    monkeypatch.setattr(secrets, "_remove_file", refuse)
    got, _ = said(paths, vault)
    assert got == [
        "  forgot the Pexels and Pixabay keys in Credential Manager",
        f"  could not remove {secret_path(paths)} (PermissionError: The process cannot access the file because it is "
        "being used by another process): delete it by hand, it still holds the Pexels and Pixabay keys",
    ]
    assert secret_path(paths).exists() and vault.values == {}


def test_one_bad_file_among_good_keys_is_removed_and_said(tmp_path: Path) -> None:
    """A torn `secrets.dpapi` names no key that can be read: it still goes, and the line does not claim keys."""
    paths = home_with_file(tmp_path, text='{"pexels_api_key": "YmxvY')
    got, _ = said(paths, FakeVault(PIXABAY_KEY))
    assert got == [
        "  forgot the Pixabay key in Credential Manager",
        f"  removed {secret_path(paths)} (no key could be read in it)",
    ]
    assert not secret_path(paths).exists()


def test_another_install_keeps_credential_manager_and_this_one_loses_only_its_file(tmp_path: Path) -> None:
    """The takeover's rule: Credential Manager is one per Windows account, so its keys stay while another
    Nightshift folder exists; `config\\secrets.dpapi` is this folder's own and goes with it."""
    paths = home_with_file(tmp_path, PIXABAY_KEY)
    vault = FakeVault(*BOTH)
    got, asked = said(paths, vault, used_by=OTHER)
    assert got == [
        f"Left the stock-footage keys in Credential Manager: the Nightshift at {OTHER} uses them.",
        f"  removed {secret_path(paths)} (the copy of the Pixabay key for nightly runs)",
    ]
    assert asked is True and sorted(vault.values) == sorted(BOTH) and not any(c.startswith("delete") for c in vault.calls)
    assert got[0].startswith(window.LEFT) and all(window.is_key_line(ln) for ln in got)
    # Credential Manager holds none: nothing is "left", and nothing is claimed
    got, _ = said(home_with_file(tmp_path / "second"), FakeVault(), used_by=OTHER)
    assert got == ["  no stock-footage keys were stored (Credential Manager and config\\secrets.dpapi held none)"]
    # it cannot be read: left untouched, and the line says that it was not looked into
    got, _ = said(home_with_file(tmp_path / "third"), FakeVault(*BOTH, unreadable=BOTH), used_by=OTHER)
    assert got == [
        "Left Credential Manager as it is (it could not be read: RuntimeError: the vault is locked): any "
        f"stock-footage keys in it are used by the Nightshift at {OTHER}."
    ]


def test_without_the_keyring_package_the_file_still_goes_and_the_caller_is_told(tmp_path: Path) -> None:
    paths = home_with_file(tmp_path, *BOTH)
    got, asked = said(paths, FakeVault(*BOTH, missing=True))
    assert got == [f"  removed {secret_path(paths)} (the copy of the Pexels and Pixabay keys for nightly runs)"]
    assert asked is False   # nothing is said about Credential Manager: the script looks with cmdkey instead
    assert not secret_path(paths).exists()


def test_a_dry_run_says_where_each_key_is_and_changes_nothing(tmp_path: Path) -> None:
    paths = home_with_file(tmp_path, PIXABAY_KEY)
    vault = FakeVault(*BOTH)
    got, asked = said(paths, vault, dry_run=True)
    assert got == [
        "[dry-run] forget the Pexels key (Credential Manager)",
        "[dry-run] forget the Pixabay key (Credential Manager and config\\secrets.dpapi)",
    ]
    assert asked is True and sorted(vault.values) == sorted(BOTH) and secret_path(paths).exists()
    assert not any(c.startswith("delete") for c in vault.calls)
    got, _ = said(home_with_file(tmp_path / "empty"), FakeVault(), dry_run=True)
    assert got == ["[dry-run] no stock-footage keys are stored (Credential Manager and config\\secrets.dpapi hold none)"]
    got, _ = said(paths, vault, dry_run=True, used_by=OTHER)
    assert got == [
        f"Left the stock-footage keys in Credential Manager: the Nightshift at {OTHER} uses them.",
        "[dry-run] forget the Pixabay key (config\\secrets.dpapi)",
    ]


# ------------------------------------------------------------------------------ the real script, dry
def dry(home: Path, tmp_path: Path, machine: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    """`uninstall.ps1 -DryRun` against a described PC (`-StateFile`): nothing of this PC is read or changed."""
    exe = pwsh.find_powershell()
    assert exe is not None
    state = tmp_path / "machine.json"
    state.write_text(json.dumps(machine), encoding="utf-8")
    (tmp_path / "desktop").mkdir(exist_ok=True)
    argv = [
        exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(UNINSTALL), "-DryRun", "-HomeDir", str(home),
        "-DesktopDir", str(tmp_path / "desktop"), "-StateFile", str(state),
    ]
    env = {k: v for k, v in os.environ.items() if k != "NIGHTSHIFT_HOME"}
    env["NS_NONINTERACTIVE"] = "1"
    return subprocess.run(argv, capture_output=True, text=True, timeout=300, env=env)


def install_with_python(tmp_path: Path) -> Paths:
    """A home whose `venvs\\supervisor` is a real, empty environment that finds this checkout (a `.pth`), as an
    install's does. It has no `keyring` package, so Credential Manager cannot be reached from it."""
    paths = home_with_file(tmp_path, PIXABAY_KEY)
    env_dir = paths.home / "venvs" / "supervisor"
    venv.create(env_dir, with_pip=False, system_site_packages=False)
    site = env_dir / "Lib" / "site-packages" if sys.platform == "win32" else next((env_dir / "lib").glob("python*/site-packages"))
    (site / "checkout.pth").write_text(str(ROOT) + "\n", encoding="utf-8")
    return paths


@needs_powershell
def test_the_uninstaller_prints_what_python_reported(tmp_path: Path) -> None:
    source = UNINSTALL.read_text(encoding="utf-8")
    assert "forgot the stock-footage keys in Credential Manager" not in source   # no fixed line: Python says it
    assert "forget the Pexels and Pixabay keys in Credential Manager" not in source
    # no Python in the install: the script looks with cmdkey (here: at the described PC) and says so
    bare = home_with_file(tmp_path / "bare", PIXABAY_KEY)
    machine = {"task": None, "userHome": str(bare.home), "toastKey": False, "credentials": ["Nightshift", "pexels_api_key@Nightshift"]}
    out = dry(bare.home, tmp_path / "bare", machine).stdout
    assert "[dry-run] remove from Credential Manager with cmdkey: Nightshift, pexels_api_key@Nightshift (this install has no Python environment to look with)" in out
    assert f"[dry-run] delete {secret_path(bare)}" in out and secret_path(bare).exists()
    out = dry(bare.home, tmp_path / "bare", {**machine, "credentials": []}).stdout
    assert "[dry-run] no stock-footage keys found in Credential Manager (this install has no Python environment to look with)" in out
    # another install uses them: left, and the line says that Credential Manager was not looked into
    out = dry(bare.home, tmp_path / "bare", {**machine, "userHome": str(tmp_path)}).stdout
    assert f"Left Credential Manager as it is (not looked into: this install has no Python environment to look with): any stock-footage keys in it are used by the Nightshift at {tmp_path}." in out
    # the install's own Python: its lines are printed as they are; it has no keyring package, so the script
    # looks at Credential Manager itself and Python's line covers the file
    paths = install_with_python(tmp_path / "full")
    proc = dry(paths.home, tmp_path / "full", {**machine, "userHome": str(paths.home)})
    out = proc.stdout
    assert proc.returncode == 0, out + proc.stderr
    assert "[dry-run] forget the Pixabay key (config\\secrets.dpapi)" in out
    assert "[dry-run] remove from Credential Manager with cmdkey: Nightshift, pexels_api_key@Nightshift (this install's Python has no keyring package)" in out
    assert f"[dry-run] delete {secret_path(paths)}" not in out   # said once, by Python
    assert secret_path(paths).exists()


def test_the_command_the_script_runs_prints_the_lines_and_tells_a_missing_keyring_apart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """`python -m installer.stage2.secrets forget --home <home>`: exit 0 when Credential Manager was asked,
    4 when the keyring package is missing (the script then looks with cmdkey)."""
    main = getattr(secrets, "main", None)
    assert main is not None, "installer.stage2.secrets has no command yet: the script cannot ask it"
    paths = home_with_file(tmp_path, PIXABAY_KEY)
    monkeypatch.setattr(secrets, "KeyringStore", lambda: FakeVault(PEXELS_KEY))
    assert main(["forget", "--home", str(paths.home), "--dry-run"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "[dry-run] forget the Pexels key (Credential Manager)",
        "[dry-run] forget the Pixabay key (config\\secrets.dpapi)",
    ]
    monkeypatch.setattr(secrets, "KeyringStore", lambda: FakeVault(missing=True))
    assert main(["forget", "--home", str(paths.home), "--used-by", OTHER]) == secrets.NO_VAULT == 4
    assert capsys.readouterr().out.splitlines() == [f"  removed {secret_path(paths)} (the copy of the Pixabay key for nightly runs)"]
    assert FILE_NAME == "secrets.dpapi"
