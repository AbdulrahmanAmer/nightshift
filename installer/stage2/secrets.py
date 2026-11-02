"""Secret storage for the installer (component C1): the Windows keyring, service ``Nightshift``."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from ns_common.paths import Paths
from ns_common.secret_file import dpapi_protect, dpapi_unprotect, load_secret, store_secret
from ns_common.secrets_names import KEY_NAMES, KEYRING_SERVICE, PEXELS_KEY, PIXABAY_KEY

# One definition for the studio, the installer and Settings: ``ns_common.secrets_names``.
SERVICE = KEYRING_SERVICE
__all__ = ["KEY_NAMES", "PEXELS_KEY", "PIXABAY_KEY", "SERVICE"]


class SecretStore(Protocol):
    """Where API keys live. Values are never logged."""

    def get(self, name: str) -> str | None:
        """Return the stored value or None."""
        ...

    def set(self, name: str, value: str) -> None:
        """Store a value."""
        ...


class KeyringStore:
    """Windows Credential Manager through the ``keyring`` package (studio extra, imported lazily)."""

    def get(self, name: str) -> str | None:
        """Read ``Nightshift/<name>``."""
        import keyring

        return keyring.get_password(SERVICE, name)

    def set(self, name: str, value: str) -> None:
        """Write ``Nightshift/<name>``."""
        import keyring

        keyring.set_password(SERVICE, name, value)


class MemoryStore:
    """In-memory store for tests and ``--simulate-downloads``."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        """Return the stored value or None."""
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        """Store a value."""
        self.values[name] = value


def restrict_to_user(path: Path) -> None:
    """Limit a file's ACL to the current user (and SYSTEM) with icacls. Best effort; never raises."""
    user = os.environ.get("USERNAME")
    if not user or os.name != "nt":
        return
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F", "/grant:r", "*S-1-5-18:F"],
            capture_output=True, check=False, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return


class ResilientStore:
    """Credential Manager first, plus a DPAPI file the nightly run can read when Credential Manager cannot.

    The nightly task may run in a session without access to the user's Credential Manager (S4U). ``set``
    writes both places and only fails when neither accepted the value; ``get`` asks the keyring and falls back
    to the file.
    """

    def __init__(
        self,
        primary: SecretStore,
        paths: Paths,
        *,
        protect: Callable[[bytes], bytes] = dpapi_protect,
        unprotect: Callable[[bytes], bytes] = dpapi_unprotect,
        lock_down: Callable[[Path], None] = restrict_to_user,
    ) -> None:
        self.primary = primary
        self.paths = paths
        self._protect = protect
        self._unprotect = unprotect
        self._lock_down = lock_down

    def get(self, name: str) -> str | None:
        """The keyring value, else the file's, else None."""
        try:
            value = self.primary.get(name)
        except Exception:  # noqa: BLE001 - a broken backend must not hide the file copy
            value = None
        return value or load_secret(self.paths, name, unprotect=self._unprotect)

    def set(self, name: str, value: str) -> None:
        """Write the keyring and the file; raise only when both fail."""
        failures: list[str] = []
        try:
            self.primary.set(name, value)
        except Exception as e:  # noqa: BLE001 - recorded, the file copy may still work
            failures.append(f"keyring: {type(e).__name__}")
        try:
            path = store_secret(self.paths, name, value, protect=self._protect)
            self._lock_down(path)
        except OSError as e:
            failures.append(f"file: {e}")
        if len(failures) == 2:
            raise OSError("could not store the key (" + "; ".join(failures) + ")")
