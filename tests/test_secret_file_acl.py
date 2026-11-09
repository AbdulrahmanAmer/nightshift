"""F-9: `config/secrets.dpapi` keeps its user-only ACL however it is written, deleted from or restored over.

The blob is machine-scope DPAPI with a fixed entropy: any program of any user on the PC can decrypt it, so the
file's ACL (the current user and SYSTEM, nothing inherited) is its only protection. Every writer puts the new
content in a temporary file and swaps it in, and a new file takes the folder's inherited ACL.
`ResilientStore.set` re-applied the restriction by hand; `delete_secret`, a restore from a backup and the
dashboard's Settings > Keys (`RealKeyringAdapter.set`) did not. The tests read the ACL back with
`icacls /save` (the ACL as SDDL, every entry by its SID) and compare it with the account `whoami /user`
names: the lock grants the account by its SID, so an account name that does not map (an Entra ID sign-in, a
process without `%USERNAME%`) can no longer leave the file open (review of PR 175: icacls exit 1332, nothing
changed, no error).
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_common.secret_file import delete_secret, load_secret, restrict_to_user, secret_path, store_secret

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="file ACLs are read with icacls, which exists only on Windows"
)

SYSTEM = "S-1-5-18"
ALIASES = {"SY": SYSTEM, "BA": "S-1-5-32-544", "BU": "S-1-5-32-545", "AU": "S-1-5-11", "OW": "S-1-3-4"}


def xor(data: bytes) -> bytes:  # stands in for DPAPI, which needs no key here
    return bytes(b ^ 0x5A for b in data)


def me() -> str:
    """The SID of the account the tests run as, from `whoami` (not from the code under test)."""
    out = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True, timeout=30
    ).stdout
    return out.strip().rsplit(",", 1)[-1].strip('"').upper()


def acl(path: Path) -> tuple[str, list[tuple[str, str]]]:
    """(DACL flags, [(SID, ACE flags)]) of the file, from the SDDL `icacls /save` writes."""
    with tempfile.TemporaryDirectory(prefix="ns-acltest-") as tmp:
        saved = Path(tmp) / "acl.txt"
        subprocess.run(
            ["icacls", str(path), "/save", str(saved)], capture_output=True, check=True, timeout=30
        )
        text = saved.read_bytes().decode("utf-16-le").lstrip("﻿")
    dacl = next(line.strip() for line in text.splitlines() if line.strip().startswith("D:"))
    entries = []
    for ace in re.findall(r"\(([^()]*)\)", dacl):
        parts = ace.split(";")
        entries.append((ALIASES.get(parts[5].upper(), parts[5].upper()), parts[1].upper()))
    return dacl[2:].split("(", 1)[0], entries


def inherits(path: Path) -> bool:
    return any("ID" in flags for _, flags in acl(path)[1])


def assert_user_only(path: Path) -> None:
    flags, entries = acl(path)
    who = {sid for sid, _ in entries}
    assert who == {me(), SYSTEM}, f"{path.name} is open to {sorted(who)}: {entries}"
    assert "P" in flags and not inherits(path), f"{path.name} inherits its ACL: {flags} {entries}"


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
    assert inherits(plain), "a new file should inherit the folder's ACL"
    with pytest.raises(AssertionError):
        assert_user_only(plain)
    restrict_to_user(plain)
    assert_user_only(plain)


@pytest.mark.parametrize("name", ["NoSuchUserXyz", None], ids=["unmapped-name", "no-USERNAME"])
def test_the_lock_holds_whatever_the_user_name_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str | None
) -> None:
    """The account is granted by its SID: a `%USERNAME%` icacls cannot map (exit 1332, nothing changed) or no
    `%USERNAME%` at all left the file open to Users and Authenticated Users, and nobody was told."""
    if name is None:
        monkeypatch.delenv("USERNAME", raising=False)
    else:
        monkeypatch.setenv("USERNAME", name)
    f = inheriting_folder(tmp_path) / "secrets.dpapi"
    f.write_text("{}", encoding="utf-8")
    restrict_to_user(f)
    assert_user_only(f)


def test_a_lock_icacls_refuses_is_ns_e120_and_not_silence(tmp_path: Path) -> None:
    with pytest.raises(NsError) as ei:
        restrict_to_user(tmp_path / "not-there.dpapi")  # icacls: "The system cannot find the file specified"
    assert ei.value.code == "NS-E120" and isinstance(ei.value, PermissionError)
    assert "not-there.dpapi" in ei.value.detail


def test_a_file_in_a_folder_that_passes_nothing_down_is_user_only_too(tmp_path: Path) -> None:
    """Such a file takes its creator's default ACL (the user, SYSTEM, Administrators, OWNER RIGHTS), which
    `/inheritance:r` alone leaves in place: the CI runner's temp folder is one."""
    paths = paths_for(tmp_path)
    subprocess.run(
        ["icacls", str(paths.config), "/inheritance:r", "/grant:r", f"*{me()}:F"],
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
