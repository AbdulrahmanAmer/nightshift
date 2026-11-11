"""Secret storage for the installer (component C1): the Windows keyring, service ``Nightshift``.

Run as a program it is the uninstaller's one source of what happened to the stock-footage keys
(``python -m installer.stage2.secrets forget --home <home> [--dry-run] [--used-by <folder>]``, see
`forget_stock_keys`): ``installer\\uninstall.ps1`` prints its lines as they are.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ns_common.paths import Paths
from ns_common.secret_file import (  # restrict_to_user lives with the file it protects; re-exported here
    FILE_NAME,
    delete_secret,
    dpapi_protect,
    dpapi_unprotect,
    load_secret,
    restrict_to_user,
    secret_path,
    store_secret,
    stored_names,
)
from ns_common.secrets_names import KEY_NAMES, KEYRING_SERVICE, PEXELS_KEY, PIXABAY_KEY
from ns_common.testguard import refuse_real_keyring

# One definition for the studio, the installer and Settings: ``ns_common.secrets_names``.
SERVICE = KEYRING_SERVICE
__all__ = ["KEY_NAMES", "PEXELS_KEY", "PIXABAY_KEY", "SERVICE", "any_key_stored"]


def any_key_stored(store: Any) -> bool:
    """One stock-footage key is enough (Pixabay alone works; Pexels stopped handing out new keys in 2026)."""
    return any(store.get(k) for k in KEY_NAMES)


class SecretStore(Protocol):
    """Where API keys live. Values are never logged."""

    def get(self, name: str) -> str | None:
        """Return the stored value or None."""
        ...

    def set(self, name: str, value: str) -> None:
        """Store a value."""
        ...

    def delete(self, name: str) -> None:
        """Forget a value (no error when it is not there)."""
        ...


class KeyringStore:
    """Windows Credential Manager through the ``keyring`` package (studio extra, imported lazily)."""

    def get(self, name: str) -> str | None:
        """Read ``Nightshift/<name>``."""
        import keyring

        refuse_real_keyring(keyring)
        return keyring.get_password(SERVICE, name)

    def set(self, name: str, value: str) -> None:
        """Write ``Nightshift/<name>``."""
        import keyring

        refuse_real_keyring(keyring)
        keyring.set_password(SERVICE, name, value)

    def delete(self, name: str) -> None:
        """Remove ``Nightshift/<name>`` (quietly when absent)."""
        import keyring

        refuse_real_keyring(keyring)
        try:
            keyring.delete_password(SERVICE, name)
        except keyring.errors.PasswordDeleteError:
            return


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

    def delete(self, name: str) -> None:
        """Forget a value."""
        self.values.pop(name, None)


class ResilientStore:
    """Credential Manager first, plus a DPAPI file the nightly run can read when Credential Manager cannot.

    The nightly task may run in a session without access to the user's Credential Manager (S4U). ``set``
    writes both places and only fails when neither accepted the value; ``get`` asks the keyring and falls back
    to the file. When only the file refused (its lock failed, ``NS-E120``, or the disk did), the key is in
    Credential Manager and ``warn`` says so (setup shows, logs and keeps the line as a notice). The file keeps
    what it had unless only the check after the swap failed: ``NS-E120`` then says "the new file is in place",
    and the new key IS in the file. On a drive without permissions (FAT, exFAT) the file is saved without a
    lock and ``warn`` gets the one-time ``NS-W116`` line (setup's notice, its summary and install report).
    """

    def __init__(
        self,
        primary: SecretStore,
        paths: Paths,
        *,
        protect: Callable[[bytes], bytes] = dpapi_protect,
        unprotect: Callable[[bytes], bytes] = dpapi_unprotect,
        lock_down: Callable[[Path], None] = restrict_to_user,
        warn: Callable[[str], None] | None = None,
    ) -> None:
        self.primary = primary
        self.paths = paths
        self._protect = protect
        self._unprotect = unprotect
        self._lock_down = lock_down
        self.warn: Callable[[str], None] = warn or (lambda line: None)

    def get(self, name: str) -> str | None:
        """The keyring value, else the file's, else None."""
        try:
            value = self.primary.get(name)
        except Exception:  # noqa: BLE001 - a broken backend must not hide the file copy
            value = None
        return value or load_secret(self.paths, name, unprotect=self._unprotect)

    def delete(self, name: str) -> None:
        """Forget the key in both places (a stored key the service rejected must not survive setup)."""
        keyring_said = "was removed from Credential Manager"
        try:
            self.primary.delete(name)
        except Exception as e:  # noqa: BLE001 - best effort, the file copy is still removed
            keyring_said = f"could not be removed from Credential Manager ({type(e).__name__})"
        try:
            delete_secret(self.paths, name, lock_down=self._lock_down, notice=self._notice)
        except OSError as e:  # the file may still hold the key; the nightly fallback may still find it
            self.warn(f"The {name} key {keyring_said}, and it was not removed from {FILE_NAME}: {e}")

    def set(self, name: str, value: str) -> None:
        """Write the keyring and the file; raise only when both fail (a file-only failure is warned)."""
        failures: list[str] = []
        keyring_ok = True
        try:
            self.primary.set(name, value)
        except Exception as e:  # noqa: BLE001 - recorded, the file copy may still work
            keyring_ok = False
            failures.append(f"keyring: {type(e).__name__}")
        try:
            store_secret(
                self.paths, name, value, protect=self._protect, lock_down=self._lock_down, notice=self._notice
            )
        except OSError as e:
            failures.append(f"file: {e}")
            if keyring_ok:
                self.warn(
                    f"The {name} key is saved in Credential Manager, but saving its copy for nightly runs "
                    f"that cannot reach Credential Manager ({FILE_NAME}) failed: {e}"
                )
        if len(failures) == 2:
            raise OSError("could not store the key (" + "; ".join(failures) + ")")

    def _notice(self, code: str, line: str) -> None:
        """The one-time warning of a keys file saved on a drive without permissions: the same channel as a
        failure (``warn`` finds the code in the line), because the owner should hear it in the same place."""
        self.warn(line if line.startswith(code) else f"{code}: {line}")


# ---------------------------------------------------------------------------------------------------------
# the uninstaller: what happened to the stock-footage keys, in true lines
# ---------------------------------------------------------------------------------------------------------
#: A key is kept in two places (`ResilientStore`): Windows Credential Manager, one per Windows account and so
#: shared by every Nightshift folder of that account, and `config\secrets.dpapi`, this folder's own.
LABELS = {PEXELS_KEY: "Pexels", PIXABAY_KEY: "Pixabay"}
FILE_SHOWN = "config\\" + FILE_NAME
#: The way out when Credential Manager kept a key: the entries `keyring` makes are named `Nightshift` and
#: `<key name>@Nightshift`.
BY_HAND = (
    "open Control Panel > Credential Manager > Windows Credentials and remove every entry whose name ends "
    "in Nightshift"
)
#: `main`'s exit code when Credential Manager could not be asked (no `keyring` package): the lines cover the
#: file only, and `uninstall.ps1` looks at Credential Manager with cmdkey.
NO_VAULT = 4


@dataclass(frozen=True)
class Forgotten:
    """The uninstaller's lines about the keys, and whether Credential Manager could be asked at all."""

    lines: tuple[str, ...]
    vault_asked: bool


def _keys(names: Sequence[str]) -> str:
    words = [LABELS.get(n, n) for n in names]
    return " and ".join(words) + (" keys" if len(words) > 1 else " key")


def _why(e: BaseException) -> str:
    return f"{type(e).__name__}: {getattr(e, 'strerror', None) or e}"


def _remove_file(path: Path) -> None:
    """Delete `config\\secrets.dpapi` (a seam for the tests: a file another program holds)."""
    path.unlink()


def forget_stock_keys(
    paths: Paths, vault: Any, *, dry_run: bool = False, used_by: str | None = None
) -> Forgotten:
    """Forget the stock-footage keys of the Nightshift at `paths` and say what happened, line by line.

    `vault` is Credential Manager (`KeyringStore`: `get` and `delete` by key name; `ImportError` when the
    `keyring` package is missing). `used_by`: another Nightshift folder on this PC still uses Credential
    Manager (the takeover's rule): its keys are left and said to be left; this folder's file goes all the
    same. `dry_run`: nothing is removed, each line says where each key is now.

    Every line is about what was found and done in this call: a key it did not see is never called
    forgotten, a refusal is named with its error and the way to finish by hand, and when Credential Manager
    cannot be asked nothing is said about it (`vault_asked` False: the caller looks another way).
    """
    held: list[str] = []
    unreadable: dict[str, str] = {}
    asked = True
    for name in KEY_NAMES:
        try:
            if vault.get(name):
                held.append(name)
        except ImportError:
            asked = False
            break
        except Exception as e:  # noqa: BLE001 - a vault that cannot be read is said, never guessed
            unreadable[name] = _why(e)
    if not asked:
        held, unreadable = [], {}
    file = secret_path(paths)
    has_file = file.is_file()
    in_file = [n for n in KEY_NAMES if n in stored_names(paths)] if has_file else []
    lines: list[str] = []
    if asked and used_by:
        if unreadable:
            why = next(iter(unreadable.values()))
            lines.append(
                f"Left Credential Manager as it is (it could not be read: {why}): any stock-footage keys in "
                f"it are used by the Nightshift at {used_by}."
            )
        elif held:
            lines.append(
                f"Left the stock-footage keys in Credential Manager: the Nightshift at {used_by} uses them."
            )
        held, unreadable = [], {}
    if dry_run:
        for name in KEY_NAMES:
            places = ["Credential Manager"] if name in held else []
            places += [FILE_SHOWN] if name in in_file else []
            if places:
                lines.append(f"[dry-run] forget the {_keys([name])} ({' and '.join(places)})")
            elif name in unreadable:
                lines.append(
                    f"[dry-run] remove the {_keys([name])} from Credential Manager if it is there (it cannot "
                    f"be read now: {unreadable[name]})"
                )
        if has_file and not in_file:
            lines.append(f"[dry-run] delete {file} (no key could be read in it)")
        if asked and not lines:
            lines.append(
                f"[dry-run] no stock-footage keys are stored (Credential Manager and {FILE_SHOWN} hold none)"
            )
        return Forgotten(tuple(lines), asked)
    forgot: list[str] = []
    after: list[str] = []
    for name in KEY_NAMES:
        if name not in held and name not in unreadable:
            continue
        try:
            vault.delete(name)
        except Exception as e:  # noqa: BLE001 - the key is still there: said, with the way to remove it
            after.append(
                f"  could not remove the {_keys([name])} from Credential Manager ({_why(e)}): {BY_HAND}"
            )
            continue
        if name in held:
            forgot.append(name)
        else:
            after.append(
                f"  removed the {_keys([name])} from Credential Manager if it was there (it could not be "
                f"read first: {unreadable[name]})"
            )
    if forgot:
        lines.append(f"  forgot the {_keys(forgot)} in Credential Manager")
    lines += after
    if has_file:
        what = "no key could be read in it"
        if in_file:
            what = f"the copy of the {_keys(in_file)} for nightly runs"
        try:
            _remove_file(file)
        except FileNotFoundError:
            pass  # gone between the look and the removal: nothing of it is left to say
        except OSError as e:
            still = f", it still holds the {_keys(in_file)}" if in_file else ""
            lines.append(f"  could not remove {file} ({_why(e)}): delete it by hand{still}")
        else:
            lines.append(f"  removed {file} ({what})")
    if asked and not lines:
        lines.append(f"  no stock-footage keys were stored (Credential Manager and {FILE_SHOWN} held none)")
    return Forgotten(tuple(lines), asked)


def main(argv: Sequence[str] | None = None) -> int:
    """``forget --home <home> [--dry-run] [--used-by <folder>]``: print `forget_stock_keys`' lines.

    Exit 0 when Credential Manager was asked, `NO_VAULT` when it could not be (the lines then cover only
    `config\\secrets.dpapi`), 2 for a wrong call.
    """
    parser = argparse.ArgumentParser(prog="python -m installer.stage2.secrets")
    sub = parser.add_subparsers(dest="command", required=True)
    forget = sub.add_parser("forget", help="forget the stock-footage keys of one Nightshift folder")
    forget.add_argument("--home", required=True)
    forget.add_argument("--dry-run", action="store_true")
    forget.add_argument("--used-by", default=None)
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    out = forget_stock_keys(
        Paths(Path(args.home)), KeyringStore(), dry_run=args.dry_run, used_by=args.used_by or None
    )
    for line in out.lines:
        sys.stdout.write(line + "\n")
    sys.stdout.flush()
    return 0 if out.vault_asked else NO_VAULT


if __name__ == "__main__":
    raise SystemExit(main())
