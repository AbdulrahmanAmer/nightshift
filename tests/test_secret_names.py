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
