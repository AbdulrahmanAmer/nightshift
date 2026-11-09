"""F-9: `config/secrets.dpapi` keeps its user-only ACL however it is written, deleted from or restored over.

The blob is machine-scope DPAPI with a fixed entropy: any program of any user on the PC can decrypt it, so the
file's ACL (the current user and SYSTEM, nothing inherited) is its only protection. Every writer puts the new
content in a temporary file and swaps it in, and a new file takes the folder's inherited ACL.
`ResilientStore.set` re-applied the restriction by hand; `delete_secret`, a restore from a backup and the
dashboard's Settings > Keys (`RealKeyringAdapter.set`) did not. The tests read the ACL back with `icacls`,
which is what the owner's tools show too.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from ns_common.paths import Paths
from ns_common.secret_file import delete_secret, load_secret, restrict_to_user, secret_path, store_secret

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not os.environ.get("USERNAME"),
    reason="file ACLs are read with icacls, which exists only on Windows (and the lock needs %USERNAME%)",
)

ACE = re.compile(r"^(?P<who>.+?):(?P<rights>(?:\([^)]*\))+)\s*$")


def xor(data: bytes) -> bytes:  # stands in for DPAPI, which needs no key here
    return bytes(b ^ 0x5A for b in data)


def acl(path: Path) -> list[tuple[str, str]]:
    """(who, rights) of every entry in the file's ACL as `icacls` prints it; rights keep the `(I)` marks."""
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True, timeout=30).stdout
    entries: list[tuple[str, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if line.lower().startswith(str(path).lower()):
            line = line[len(str(path)) :].strip()
        m = ACE.match(line)
        if m:
            entries.append((m["who"], m["rights"]))
    return entries


def assert_user_only(path: Path) -> None:
    entries = acl(path)
    who = {w.split("\\")[-1].lower() for w, _ in entries}
    me = os.environ["USERNAME"].lower()
    assert who == {me, "system"}, f"{path.name} is open to {sorted(who)}: {entries}"
    assert not any("(I)" in rights for _, rights in entries), f"{path.name} inherits its ACL: {entries}"


def paths_for(tmp_path: Path) -> Paths:
    p = Paths(tmp_path / "home")
    p.config.mkdir(parents=True)
    return p


def inheriting_folder(tmp_path: Path) -> Path:
    """A folder whose ACL passes the Users group down to new files, as a home folder under a user profile does
    (a CI runner's temp folder passes nothing down, so the premise is made here and not found)."""
    folder = tmp_path / "inheriting"
    folder.mkdir()
    subprocess.run(
        ["icacls", str(folder), "/grant", "*S-1-5-32-545:(OI)(CI)R"],
        capture_output=True,
        check=True,
        timeout=30,
    )
    return folder


def test_the_acl_check_tells_a_locked_file_from_an_ordinary_one(tmp_path: Path) -> None:
    plain = inheriting_folder(tmp_path) / "plain.txt"
    plain.write_text("x", encoding="utf-8")
    assert any("(I)" in rights for _, rights in acl(plain)), "a new file should inherit the folder's ACL"
    with pytest.raises(AssertionError):
        assert_user_only(plain)
    restrict_to_user(plain)
    assert_user_only(plain)


def test_a_file_in_a_folder_that_passes_nothing_down_is_user_only_too(tmp_path: Path) -> None:
    """Such a file takes its creator's default ACL (the user, SYSTEM, Administrators, OWNER RIGHTS), which
    `/inheritance:r` alone leaves in place: the CI runner's temp folder is one."""
    paths = paths_for(tmp_path)
    subprocess.run(
        ["icacls", str(paths.config), "/inheritance:r", "/grant:r", f"{os.environ['USERNAME']}:F"],
        capture_output=True,
        check=True,
        timeout=30,
    )
    store_secret(paths, "pexels_api_key", "K1", protect=xor)
    assert_user_only(secret_path(paths))
    assert load_secret(paths, "pexels_api_key", unprotect=xor) == "K1"


def test_storing_a_key_leaves_the_file_user_only_and_readable(tmp_path: Path) -> None:
    paths = paths_for(tmp_path)
    store_secret(paths, "pexels_api_key", "K1", protect=xor)
    store_secret(paths, "pixabay_api_key", "K2", protect=xor)  # a second write is a new file again
    assert_user_only(secret_path(paths))
    assert load_secret(paths, "pexels_api_key", unprotect=xor) == "K1"  # the lock did not shut us out


def test_deleting_a_key_leaves_the_file_user_only(tmp_path: Path) -> None:
    paths = paths_for(tmp_path)
    store_secret(paths, "pexels_api_key", "K1", protect=xor)
    store_secret(paths, "pixabay_api_key", "K2", protect=xor)
    assert delete_secret(paths, "pexels_api_key") is True
    assert_user_only(secret_path(paths))
    assert load_secret(paths, "pixabay_api_key", unprotect=xor) == "K2"
    assert load_secret(paths, "pexels_api_key", unprotect=xor) is None


def test_deleting_the_last_key_leaves_the_file_user_only(tmp_path: Path) -> None:
    paths = paths_for(tmp_path)
    store_secret(paths, "pexels_api_key", "K1", protect=xor)
    assert delete_secret(paths, "pexels_api_key") is True
    assert_user_only(secret_path(paths))


def test_the_installers_store_leaves_the_file_user_only_after_set_and_delete(tmp_path: Path) -> None:
    from installer.stage2.secrets import MemoryStore, ResilientStore

    paths = paths_for(tmp_path)
    store = ResilientStore(MemoryStore(), paths, protect=xor, unprotect=xor)  # the real lock-down
    store.set("pexels_api_key", "K1")
    store.set("pixabay_api_key", "K2")
    assert_user_only(secret_path(paths))
    store.delete("pexels_api_key")
    assert_user_only(secret_path(paths))
    assert store.get("pixabay_api_key") == "K2"


def test_settings_keys_leave_the_file_user_only_after_save_and_clear(tmp_path: Path) -> None:
    """The dashboard's `RealKeyringAdapter` never locked the file down (it called `store_secret` alone)."""
    from ns_dashboard.adapters import RealKeyringAdapter

    class Keyring:
        def __init__(self) -> None:
            self.items: dict[str, str] = {}

        def get_password(self, service: str, name: str) -> str | None:
            return self.items.get(name)

        def set_password(self, service: str, name: str, value: str) -> None:
            self.items[name] = value

        def delete_password(self, service: str, name: str) -> None:
            self.items.pop(name, None)

    paths = paths_for(tmp_path)
    adapter = RealKeyringAdapter(paths, keyring_module=Keyring(), protect=xor, unprotect=xor)
    adapter.set("pexels_api_key", "K1")
    adapter.set("pixabay_api_key", "K2")
    assert_user_only(secret_path(paths))
    adapter.delete("pexels_api_key")
    assert_user_only(secret_path(paths))
    assert load_secret(paths, "pixabay_api_key", unprotect=xor) == "K2"


def backup_with_secrets(tmp_path: Path, blob: bytes) -> Path:
    from ns_supervisor import backup

    z = tmp_path / "b.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr(backup.MANIFEST, "{}")
        zf.writestr("config/secrets.dpapi", blob)
        zf.writestr("config/ui_prefs.json", "{}")
    return z


def test_restoring_a_backup_over_the_secrets_file_leaves_it_user_only(tmp_path: Path) -> None:
    from ns_supervisor import backup

    paths = paths_for(tmp_path)
    store_secret(paths, "pexels_api_key", "OLD", protect=xor)
    assert_user_only(secret_path(paths))
    blob = b'{"pexels_api_key": "bmV3"}\n'
    lines = backup.restore_backup(paths, backup_with_secrets(tmp_path, blob))
    assert "restored config/secrets.dpapi" in lines
    assert secret_path(paths).read_bytes() == blob  # the restore did happen
    assert_user_only(secret_path(paths))


def test_restoring_a_backup_when_there_was_no_secrets_file_leaves_it_user_only(tmp_path: Path) -> None:
    from ns_supervisor import backup

    paths = paths_for(tmp_path)
    backup.restore_backup(paths, backup_with_secrets(tmp_path, b"{}\n"))
    assert secret_path(paths).is_file()
    assert_user_only(secret_path(paths))


def test_the_copy_a_restore_keeps_of_the_old_secrets_file_is_user_only_too(tmp_path: Path) -> None:
    from ns_supervisor import backup

    paths = paths_for(tmp_path)
    store_secret(paths, "pexels_api_key", "OLD", protect=xor)
    backup.restore_backup(paths, backup_with_secrets(tmp_path, b"{}\n"))
    kept = list(paths.backup_dir.rglob("secrets.dpapi"))
    assert len(kept) == 1, kept
    assert load_secret(paths, "pexels_api_key", unprotect=xor) is None  # restored over
    assert_user_only(kept[0])
