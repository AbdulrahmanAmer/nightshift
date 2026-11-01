"""Secret storage for the installer (component C1): the Windows keyring, service ``Nightshift``."""

from __future__ import annotations

from typing import Protocol

SERVICE = "Nightshift"
PEXELS_KEY = "pexels_api_key"
PIXABAY_KEY = "pixabay_api_key"
KEY_NAMES = (PEXELS_KEY, PIXABAY_KEY)


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
