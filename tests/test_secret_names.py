"""The studio, the installer and Settings must use one set of keyring names (system gap 1)."""

import sys
import types

import pytest

from ns_common import secrets_names as names
from ns_dashboard import adapters
from ns_studio import jobs


def test_installer_dashboard_and_studio_share_the_names():
    from installer.stage2 import secrets as installer_secrets

    assert installer_secrets.SERVICE == adapters.KEYRING_SERVICE == names.KEYRING_SERVICE
    assert installer_secrets.PEXELS_KEY == names.PEXELS_KEY
    assert installer_secrets.PIXABAY_KEY == names.PIXABAY_KEY
    assert tuple(installer_secrets.KEY_NAMES) == names.KEY_NAMES == tuple(adapters.KEY_NAMES)


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


def test_the_dpapi_file_is_the_fallback_when_keyring_is_empty(monkeypatch: pytest.MonkeyPatch):
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


def test_keys_are_found_without_the_keyring_package(monkeypatch: pytest.MonkeyPatch):
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
