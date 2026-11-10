"""The studio, the installer and Settings must use one set of keyring names (system gap 1)."""

import sys
import types
from pathlib import Path

import pytest

from ns_common import secrets_names as names
from ns_dashboard import adapters
from ns_studio import jobs


def test_installer_dashboard_and_studio_share_the_names():
    from installer.stage2 import secrets as installer_secrets

    assert installer_secrets.SERVICE == adapters.KEYRING_SERVICE == names.KEYRING_SERVICE
    assert installer_secrets.PEXELS_KEY == names.PEXELS_KEY
    assert installer_secrets.PIXABAY_KEY == names.PIXABAY_KEY
    assert installer_secrets.KEY_NAMES is names.KEY_NAMES


def test_the_keyring_names_are_spelled_out_in_one_module_only(repo_root: Path):
    """The studio, the installer's secret store and Settings must import the names, not retype them."""
    literals = (names.PEXELS_KEY, names.PIXABAY_KEY)
    # `Answers` fields and wizard plumbing use the same words as JSON keys; the keyring side must not.
    scanned = [
        *sorted((repo_root / "ns_studio").rglob("*.py")),
        *sorted((repo_root / "ns_supervisor").rglob("*.py")),
        *sorted((repo_root / "ns_dashboard").glob("*.py")),
        repo_root / "installer" / "stage2" / "secrets.py",
        repo_root / "installer" / "stage2" / "answers.py",
    ]
    offenders = [
        f"{p.relative_to(repo_root)}: {lit}"
        for p in scanned
        for lit in literals
        if lit in p.read_text(encoding="utf-8")
    ]
    assert not offenders, "import ns_common.secrets_names, do not retype the names:\n" + "\n".join(offenders)


def test_the_studio_reads_the_keys_setup_and_settings_store(monkeypatch: pytest.MonkeyPatch):
    """Before: the studio asked for `pexels` and `pixabay`, so both clip sources got an empty key."""
    stored = {("Nightshift", "pexels_api_key"): "PK", ("Nightshift", "pixabay_api_key"): "XK"}
    kr = types.ModuleType("keyring")
    errors = types.ModuleType("keyring.errors")
    errors.KeyringError = type("KeyringError", (Exception,), {})  # type: ignore[attr-defined]
    kr.get_password = lambda service, name: stored.get((service, name))  # type: ignore[attr-defined]
    kr.errors = errors  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", kr)
    monkeypatch.setitem(sys.modules, "keyring.errors", errors)
    assert jobs.load_keys() == {"pexels": "PK", "pixabay": "XK"}


def test_the_dpapi_file_is_the_fallback_when_keyring_is_empty(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """The nightly task (S4U) may not reach Credential Manager; config/secrets.dpapi is the fallback."""
    kr = types.ModuleType("keyring")
    errors = types.ModuleType("keyring.errors")
    errors.KeyringError = type("KeyringError", (Exception,), {})  # type: ignore[attr-defined]
    kr.get_password = lambda service, name: None  # type: ignore[attr-defined]
    kr.errors = errors  # type: ignore[attr-defined]
    sf = types.ModuleType("ns_common.secret_file")
    sf.load_secret = lambda paths, name: {"pexels_api_key": "FILEKEY"}.get(name)  # type: ignore[attr-defined]
    for mod in (kr, errors, sf):
        monkeypatch.setitem(sys.modules, mod.__name__, mod)
    assert jobs.load_keys() == {"pexels": "FILEKEY"}


def test_keys_are_found_without_the_keyring_package(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "keyring", None)  # import raises ImportError
    sf = types.ModuleType("ns_common.secret_file")
    sf.load_secret = lambda p, name: "K" if name == "pixabay_api_key" else None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ns_common.secret_file", sf)
    assert jobs.load_keys() == {"pixabay": "K"}


def test_the_nightly_toast_falls_back_to_a_morning_note(monkeypatch: pytest.MonkeyPatch):
    from ns_studio import notifications

    mod = types.ModuleType("ns_supervisor.notify")
    mod.toast = lambda title, msg: True  # type: ignore[attr-defined]
    mod.toast_or_note = lambda title, msg: False  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ns_supervisor.notify", mod)
    assert notifications.default_notifier() is mod.toast_or_note  # type: ignore[attr-defined]
    del mod.toast_or_note  # an older supervisor
    assert notifications.default_notifier() is mod.toast  # type: ignore[attr-defined]


def test_a_keyring_that_raises_any_exception_falls_back_to_the_file(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """Nightly review row 1: in the S4U session CredRead error 1312 is no KeyringError; open_env crashed."""

    class CredReadError(OSError):  # pywin32-style: neither a KeyringError nor an ImportError
        pass

    kr = types.ModuleType("keyring")
    errors = types.ModuleType("keyring.errors")
    errors.KeyringError = type("KeyringError", (Exception,), {})  # type: ignore[attr-defined]

    def boom(service: str, name: str) -> str:
        raise CredReadError(1312, "A specified logon session does not exist.")

    kr.get_password = boom  # type: ignore[attr-defined]
    kr.errors = errors  # type: ignore[attr-defined]
    sf = types.ModuleType("ns_common.secret_file")
    sf.load_secret = lambda paths, name: {"pexels_api_key": "FILEKEY"}.get(name)  # type: ignore[attr-defined]
    for mod in (kr, errors, sf):
        monkeypatch.setitem(sys.modules, mod.__name__, mod)
    assert jobs.load_keys() == {"pexels": "FILEKEY"}


def test_no_key_anywhere_is_empty_not_an_error_and_the_source_raises_e501_later(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from ns_common.errors import NsError
    from ns_studio.media.clip_sources import PexelsSource

    kr = types.ModuleType("keyring")
    def no_session(service: str, name: str) -> str:
        raise RuntimeError("no logon session")

    def no_dpapi(paths: object, name: str) -> str:
        raise RuntimeError("dpapi unavailable")

    kr.get_password = no_session  # type: ignore[attr-defined]
    sf = types.ModuleType("ns_common.secret_file")
    sf.load_secret = no_dpapi  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", kr)
    monkeypatch.setitem(sys.modules, "ns_common.secret_file", sf)
    keys = jobs.load_keys()  # must not raise at start
    assert keys == {}
    with pytest.raises(NsError) as ei:
        PexelsSource(keys.get("pexels", ""), tmp_path).search("forest", orientation="landscape")
    assert ei.value.code == "NS-E501"
