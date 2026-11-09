"""F-9 fix round: the secrets file's lock fails loudly, names the account by its SID, is on before the keys.

`config/secrets.dpapi` is machine-scope DPAPI with a fixed entropy: any account on the PC can decrypt it, so
the file's ACL is its only protection. Review of PR 175 found `restrict_to_user` failing silently: it granted
`%USERNAME%` by name, icacls answered exit 1332 ("No mapping between account names and security IDs was
done") and changed nothing, and every caller returned normally with the keys readable by Users and
Authenticated Users.
The temp file was also written under the folder's ACL before the swap, so a crash there left every key blob in
`config\\~ns*.tmp`, open to every account, and nothing swept that folder.

These tests drive the lock with a fake icacls (its exit code, and the ACL its `/save` writes back) and a fake
account SID; `test_secret_file_acl.py` does the same with the real icacls.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ns_common import atomic, secret_file
from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_common.secret_file import delete_secret, load_secret, restrict_to_user, secret_path, store_secret

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the lock is a Windows ACL set with icacls")

SID = "S-1-5-21-111-222-333-1001"
GOOD = f"secrets.dpapi\r\nD:PAI(A;;FA;;;SY)(A;;FA;;;{SID})\r\n"
NO_MAPPING = "No mapping between account names and security IDs was done."


def xor(data: bytes) -> bytes:  # stands in for DPAPI
    return bytes(b ^ 0x5A for b in data)


class FakeIcacls:
    """Stands in for icacls: `grant_exit` answers the permission change, `saved` is the ACL `/save` writes."""

    def __init__(self) -> None:
        self.grant_exit = 0
        self.saved = GOOD
        self.after_remove: str | None = None  # the ACL once `/remove` took the leftover entries out
        self.raises: BaseException | None = None
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(list(args))
        if self.raises is not None:
            raise self.raises
        if "/save" in args:
            Path(args[args.index("/save") + 1]).write_bytes(self.saved.encode("utf-16-le"))
            return 0, "Successfully processed 1 files; Failed processing 0 files"
        if self.grant_exit:
            return self.grant_exit, NO_MAPPING
        if "/remove" in args and self.after_remove is not None:
            self.saved = self.after_remove
        return 0, f"processed file: {args[0]}"

    @property
    def grants(self) -> list[list[str]]:
        return [c for c in self.calls if "/inheritance:r" in c]

    @property
    def removals(self) -> list[list[str]]:
        return [c for c in self.calls if "/remove" in c]


@pytest.fixture
def icacls(monkeypatch: pytest.MonkeyPatch) -> FakeIcacls:
    """The product's default lock (`restrict_to_user` as every caller uses it), on a fake icacls and SID."""
    fake = FakeIcacls()
    monkeypatch.setattr(secret_file, "_run_icacls", fake)
    monkeypatch.setattr(secret_file, "current_user_sid", lambda: SID)
    return fake


def paths_for(tmp_path: Path) -> Paths:
    p = Paths(tmp_path / "home")
    p.config.mkdir(parents=True)
    return p


def config_names(paths: Paths) -> list[str]:
    return sorted(p.name for p in paths.config.iterdir())


def refuse(path: Path) -> None:
    raise secret_file.SecretFileLockError(path, f"icacls exit 1332: {NO_MAPPING}")


# ------------------------------------------------------------------------------------- the lock itself
def test_the_grant_names_the_account_by_its_sid_never_by_its_user_name(
    tmp_path: Path, icacls: FakeIcacls, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USERNAME", "NoSuchUserXyz")  # an Entra ID sign-in's name does not map on its own
    f = tmp_path / "f.txt"
    f.write_text("x", encoding="utf-8")
    restrict_to_user(f)
    [grant] = icacls.grants
    assert grant[:2] == [str(f), "/inheritance:r"]
    assert f"*{SID}:F" in grant and "*S-1-5-18:F" in grant
    assert grant[grant.index("/remove:g") + 1 :] == ["*S-1-5-32-544", "*S-1-3-4"]
    assert not any("NoSuchUserXyz" in a for a in grant), grant
    assert icacls.calls[-1][:2] == [str(f), "/save"], "the ACL is read back after the grant"
    assert icacls.removals == []


def test_an_entry_the_grant_left_is_removed_by_its_sid_and_read_back_again(
    tmp_path: Path, icacls: FakeIcacls
) -> None:
    """A file in a folder that passes nothing down takes the token's default ACL, which on a signed-in desktop
    also names the logon session (S-1-5-5-0-...): `/inheritance:r` and the grants leave such an entry."""
    f = tmp_path / "f.txt"
    f.write_text("x", encoding="utf-8")
    icacls.saved = f"f.txt\r\nD:PAI(A;;FA;;;SY)(A;;FA;;;{SID})(A;;0x1200a9;;;S-1-5-5-0-474716)\r\n"
    icacls.after_remove = GOOD
    restrict_to_user(f)
    assert icacls.removals == [[str(f), "/remove", "*S-1-5-5-0-474716"]]
    assert [c[1] for c in icacls.calls] == ["/inheritance:r", "/save", "/remove", "/save"]


def test_icacls_exit_1332_is_ns_e120_and_the_stored_key_stays_as_it_was(
    tmp_path: Path, icacls: FakeIcacls
) -> None:
    paths = paths_for(tmp_path)
    store_secret(paths, "pexels_api_key", "OLD", protect=xor)
    before = secret_path(paths).read_bytes()
    icacls.grant_exit = 1332
    with pytest.raises(NsError) as ei:
        store_secret(paths, "pexels_api_key", "NEW", protect=xor)
    assert ei.value.code == "NS-E120" and isinstance(ei.value, PermissionError)
    assert "1332" in ei.value.detail and NO_MAPPING in ei.value.detail, ei.value.detail
    assert secret_path(paths).read_bytes() == before
    assert load_secret(paths, "pexels_api_key", unprotect=xor) == "OLD"
    assert config_names(paths) == ["secrets.dpapi"], "the temp file is gone"


def test_deleting_a_key_when_the_lock_fails_is_ns_e120_and_keeps_the_file(
    tmp_path: Path, icacls: FakeIcacls
) -> None:
    paths = paths_for(tmp_path)
    store_secret(paths, "pexels_api_key", "K1", protect=xor)
    before = secret_path(paths).read_bytes()
    icacls.grant_exit = 1332
    with pytest.raises(NsError) as ei:
        delete_secret(paths, "pexels_api_key")
    assert ei.value.code == "NS-E120"
    assert secret_path(paths).read_bytes() == before
    assert config_names(paths) == ["secrets.dpapi"]


@pytest.mark.parametrize("sid", [None, "", "ka", "S-1-5"])
def test_no_account_sid_is_ns_e120_and_nothing_is_written(
    tmp_path: Path, icacls: FakeIcacls, monkeypatch: pytest.MonkeyPatch, sid: str | None
) -> None:
    monkeypatch.setattr(secret_file, "current_user_sid", lambda: sid)
    paths = paths_for(tmp_path)
    with pytest.raises(NsError) as ei:
        store_secret(paths, "pexels_api_key", "K1", protect=xor)
    assert ei.value.code == "NS-E120" and "SID" in ei.value.detail
    assert icacls.calls == [], "nothing is granted to an account the lock cannot name"
    assert config_names(paths) == [], "no secrets file, no temp file"


@pytest.mark.parametrize(
    "error",
    [FileNotFoundError(2, "The system cannot find the file"), subprocess.TimeoutExpired("icacls", 30)],
    ids=["missing", "timeout"],
)
def test_icacls_that_cannot_run_or_hangs_is_ns_e120(
    tmp_path: Path, icacls: FakeIcacls, error: BaseException
) -> None:
    paths = paths_for(tmp_path)
    icacls.raises = error
    with pytest.raises(NsError) as ei:
        store_secret(paths, "pexels_api_key", "K1", protect=xor)
    assert ei.value.code == "NS-E120" and type(error).__name__ in ei.value.detail
    assert config_names(paths) == []


BAD_READ_BACKS = {
    "users": f"D:PAI(A;;FA;;;SY)(A;;FA;;;{SID})(A;;0x1200a9;;;BU)",
    "authenticated-users": f"D:PAI(A;;FA;;;SY)(A;;FA;;;{SID})(A;;0x1301bf;;;AU)",
    "administrators": f"D:PAI(A;;FA;;;BA)(A;;FA;;;SY)(A;;FA;;;{SID})",
    "owner-rights": f"D:PAI(A;;FA;;;SY)(A;;FA;;;{SID})(A;;FA;;;OW)",
    "another-user": f"D:PAI(A;;FA;;;SY)(A;;FA;;;{SID})(A;;FA;;;S-1-5-21-111-222-333-1002)",
    "still-inherits": f"D:AI(A;;FA;;;SY)(A;;FA;;;{SID})",
    "inherited-entry": f"D:PAI(A;ID;FA;;;SY)(A;;FA;;;{SID})",
    "no-acl-at-all": "D:NO_ACCESS_CONTROL",
    "account-missing": "D:PAI(A;;FA;;;SY)",
    "nothing-saved": "",
}


@pytest.mark.parametrize("saved", list(BAD_READ_BACKS.values()), ids=list(BAD_READ_BACKS))
def test_a_read_back_with_anything_but_the_account_and_system_is_ns_e120(
    tmp_path: Path, icacls: FakeIcacls, saved: str
) -> None:
    paths = paths_for(tmp_path)
    store_secret(paths, "pexels_api_key", "OLD", protect=xor)
    before = secret_path(paths).read_bytes()
    icacls.saved = f"secrets.dpapi\r\n{saved}\r\n" if saved else ""
    with pytest.raises(NsError) as ei:
        store_secret(paths, "pexels_api_key", "NEW", protect=xor)
    assert ei.value.code == "NS-E120"
    assert secret_path(paths).read_bytes() == before
    assert config_names(paths) == ["secrets.dpapi"]


def test_the_read_back_accepts_the_account_and_system_however_icacls_spells_them() -> None:
    assert secret_file.acl_problem(GOOD, SID) is None
    assert secret_file.acl_problem(f"x\nD:P(A;;FA;;;{SID.lower()})(A;;FA;;;S-1-5-18)\n", SID) is None
    # Windows writes a well-known account as its SDDL alias: BA is Administrators, never the account
    assert secret_file._sid_of("BA") == "S-1-5-32-544"  # noqa: SLF001
    assert secret_file.acl_problem(f"D:P(A;;FA;;;SY)(A;;FA;;;{SID})(A;;FA;;;BA)", SID) is not None
    # ... and the built-in Administrator ACCOUNT (RID 500) as LA: signed in as it, the lock still passes
    local_admin = secret_file._sid_of("LA")  # noqa: SLF001
    assert local_admin.startswith("S-1-5-21-") and local_admin.endswith("-500")
    assert secret_file.acl_problem("D:PAI(A;;FA;;;SY)(A;;FA;;;LA)", local_admin) is None


def test_the_account_sid_comes_from_the_process_token_and_whoami_is_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True, timeout=30
    ).stdout
    expected = out.strip().rsplit(",", 1)[-1].strip('"').upper()
    assert expected.startswith("S-1-5-")
    assert secret_file._token_user_sid() == expected  # noqa: SLF001 - the token is read, not only whoami
    assert secret_file._whoami_sid() == expected  # noqa: SLF001
    assert secret_file.current_user_sid() == expected
    monkeypatch.setattr(secret_file, "_token_user_sid", lambda: None)
    assert secret_file.current_user_sid() == expected
    monkeypatch.setattr(secret_file, "_whoami_sid", lambda: None)
    assert secret_file.current_user_sid() is None


# ------------------------------------------------------------- the temp file is locked before the keys go in
def test_the_temp_file_is_locked_while_empty_and_the_swapped_in_file_is_checked_again(tmp_path: Path) -> None:
    paths = paths_for(tmp_path)
    seen: list[tuple[str, int]] = []

    def lock(p: Path) -> None:
        assert p.parent == paths.config
        seen.append((p.name, p.stat().st_size))

    store_secret(paths, "pexels_api_key", "K1", protect=xor, lock_down=lock)
    store_secret(paths, "pixabay_api_key", "K2", protect=xor, lock_down=lock)
    assert delete_secret(paths, "pexels_api_key", lock_down=lock) is True
    assert len(seen) == 6, seen
    for (tmp_name, tmp_size), (final_name, final_size) in zip(seen[::2], seen[1::2], strict=True):
        assert atomic.is_tmp_name(tmp_name) and tmp_size == 0, "locked before a byte of the keys was in it"
        assert final_name == "secrets.dpapi" and final_size > 0
    assert load_secret(paths, "pixabay_api_key", unprotect=xor) == "K2"


def test_a_failed_check_under_the_final_name_says_the_new_file_is_in_place(tmp_path: Path) -> None:
    paths = paths_for(tmp_path)

    def lock(p: Path) -> None:  # the temp file locks; the check after the swap fails
        if not atomic.is_tmp_name(p.name):
            refuse(p)

    with pytest.raises(NsError) as ei:
        store_secret(paths, "pexels_api_key", "K1", protect=xor, lock_down=lock)
    assert ei.value.code == "NS-E120" and "the new file is in place" in ei.value.detail, ei.value.detail
    assert load_secret(paths, "pexels_api_key", unprotect=xor) == "K1"


def _text(path: Path, prepare: Callable[[Path], None]) -> Path:
    return atomic.write_text_atomic(path, "new", prepare=prepare)


def _bytes(path: Path, prepare: Callable[[Path], None]) -> Path:
    return atomic.write_bytes_atomic(path, b"new", prepare=prepare)


def _stream(path: Path, prepare: Callable[[Path], None]) -> Path:
    return atomic.write_stream_atomic(path, io.BytesIO(b"new"), prepare=prepare)


@pytest.mark.parametrize("write", [_text, _bytes, _stream], ids=["text", "bytes", "stream"])
def test_prepare_sees_the_empty_temp_file_and_a_refusal_leaves_the_old_file(
    tmp_path: Path, write: Callable[[Path, Callable[[Path], None]], Path]
) -> None:
    target = tmp_path / "secrets.dpapi"
    target.write_bytes(b"old")
    seen: list[tuple[str, int]] = []
    write(target, lambda p: seen.append((p.name, p.stat().st_size)))
    assert target.read_bytes() == b"new"
    [(name, size)] = seen
    assert atomic.is_tmp_name(name) and size == 0
    target.write_bytes(b"old")
    with pytest.raises(NsError):
        write(target, refuse)
    assert target.read_bytes() == b"old" and [p.name for p in tmp_path.iterdir()] == ["secrets.dpapi"]


# --------------------------------------------------------------------- stale temp files in config/ are swept
def test_storing_and_deleting_a_key_sweep_the_temp_files_a_crash_left_in_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = paths_for(tmp_path)
    monkeypatch.setattr(atomic, "STALE_TMP_S", -5.0)  # "an hour ago" is in the future of every file
    left = atomic._tmp_for(secret_path(paths))  # noqa: SLF001
    left.write_bytes(b"every key blob, under the folder's ACL")
    store_secret(paths, "pexels_api_key", "K1", protect=xor, lock_down=lambda p: None)
    assert config_names(paths) == ["secrets.dpapi"]
    left = atomic._tmp_for(secret_path(paths))  # noqa: SLF001
    left.write_bytes(b"another")
    assert delete_secret(paths, "pexels_api_key", lock_down=lambda p: None) is True
    assert config_names(paths) == ["secrets.dpapi"]


def test_the_engine_sweeps_the_temp_files_a_crash_left_in_config_when_it_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ns_supervisor import app

    paths = paths_for(tmp_path)
    left = atomic._tmp_for(secret_path(paths))  # noqa: SLF001
    left.write_bytes(b"every key blob")
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(paths.home))
    monkeypatch.setattr(atomic, "STALE_TMP_S", -5.0)
    served: list[Path] = []
    monkeypatch.setattr(app, "_serve", lambda args, p: served.append(p.home) or 0)
    assert app.main(["--headless"]) == 0
    assert served == [paths.home] and not left.exists()


# ------------------------------------------------------------------------------------ backup and restore
def backup_zip(tmp_path: Path, members: list[tuple[str, bytes]]) -> Path:
    from ns_supervisor import backup

    z = tmp_path / "b.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr(backup.MANIFEST, "{}")
        for name, data in members:
            zf.writestr(name, data)
    return z


def test_a_restore_that_cannot_lock_the_secrets_file_skips_it_and_restores_the_rest(
    tmp_path: Path, icacls: FakeIcacls
) -> None:
    """F-9 failed the whole restore here (NS-E703, everything put back); F-9b skips that one file."""
    from ns_supervisor import backup

    paths = paths_for(tmp_path)
    prefs = paths.config / "ui_prefs.json"
    prefs.write_text('{"theme": "old"}', encoding="utf-8")
    store_secret(paths, "pexels_api_key", "OLD", protect=xor)
    before = secret_path(paths).read_bytes()
    members = [("config/ui_prefs.json", b'{"theme": "new"}'), ("config/secrets.dpapi", b"{}\n")]
    z = backup_zip(tmp_path, members)
    icacls.grant_exit = 1332
    lines = backup.restore_backup(paths, z)
    assert "restored config/ui_prefs.json" in lines
    assert any(ln.startswith("skipped config/secrets.dpapi (NS-E120") for ln in lines), lines
    assert secret_path(paths).read_bytes() == before
    assert prefs.read_text(encoding="utf-8") == '{"theme": "new"}'
    assert config_names(paths) == ["secrets.dpapi", "ui_prefs.json"]
    # the copy a restore keeps of the old file was never written under the folder's ACL
    assert not [p for p in paths.backup_dir.rglob("*") if p.is_file() and p.name != "ui_prefs.json"]


def test_a_restore_locks_both_secrets_copies_before_the_blob_goes_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ns_supervisor import backup

    paths = paths_for(tmp_path)
    store_secret(paths, "pexels_api_key", "OLD", protect=xor, lock_down=lambda p: None)
    seen: list[tuple[Path, int]] = []
    monkeypatch.setattr(backup, "restrict_to_user", lambda p: seen.append((p, p.stat().st_size)))
    backup.restore_backup(paths, backup_zip(tmp_path, [("config/secrets.dpapi", b'{"x": "eQ=="}\n')]))
    [keep] = list(paths.backup_dir.rglob("secrets.dpapi"))
    names = [(p.parent, p.name if not atomic.is_tmp_name(p.name) else "tmp", size) for p, size in seen]
    assert names[0][1:] == ("tmp", 0) and names[0][0] == keep.parent
    assert names[1][1] == "secrets.dpapi" and names[1][0] == keep.parent and names[1][2] > 0
    assert names[2][1:] == ("tmp", 0) and names[2][0] == paths.config
    assert names[3][1] == "secrets.dpapi" and names[3][0] == paths.config and names[3][2] > 0
    assert len(names) == 4, names
    assert secret_path(paths).read_bytes() == b'{"x": "eQ=="}\n'


def test_a_restore_that_fails_later_puts_the_secrets_file_back_under_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ns_supervisor import backup

    paths = paths_for(tmp_path)
    store_secret(paths, "pexels_api_key", "OLD", protect=xor, lock_down=lambda p: None)
    before = secret_path(paths).read_bytes()
    (paths.config / "zz.json").mkdir()  # a folder where the backup has a file: its copy fails
    locked: list[Path] = []
    monkeypatch.setattr(backup, "restrict_to_user", lambda p: locked.append(p))
    z = backup_zip(tmp_path, [("config/secrets.dpapi", b"{}\n"), ("config/zz.json", b"{}")])
    with pytest.raises(NsError) as ei:
        backup.restore_backup(paths, z)
    assert ei.value.code == "NS-E703" and "1 file(s) put back" in ei.value.detail, ei.value.detail
    assert secret_path(paths).read_bytes() == before
    rolled = [p for p in locked if p.parent == paths.config]
    assert [atomic.is_tmp_name(p.name) for p in rolled][-2:] == [True, False], "the put-back is locked too"
    assert not [p for p in paths.config.iterdir() if atomic.is_tmp_name(p.name)]


# ---------------------------------------------------------- the key stores keep the keyring copy and say so
def test_the_installers_store_keeps_the_keyring_copy_and_says_the_file_was_not_locked(tmp_path: Path) -> None:
    from installer.stage2.secrets import MemoryStore, ResilientStore

    paths = paths_for(tmp_path)
    store_secret(paths, "pexels_api_key", "OLD", protect=xor, lock_down=lambda p: None)
    before = secret_path(paths).read_bytes()
    said: list[str] = []
    store = ResilientStore(
        MemoryStore(), paths, protect=xor, unprotect=xor, lock_down=refuse, warn=said.append
    )
    store.set("pexels_api_key", "K1")  # Credential Manager took it: setup goes on
    assert store.primary.get("pexels_api_key") == "K1"
    assert secret_path(paths).read_bytes() == before
    [line] = said
    assert "NS-E120" in line and "pexels_api_key" in line and "Credential Manager" in line
    store.delete("pexels_api_key")
    assert store.primary.get("pexels_api_key") is None and secret_path(paths).read_bytes() == before
    assert len(said) == 2 and "NS-E120" in said[1]


def test_the_installers_store_raises_the_lock_error_when_the_keyring_failed_too(tmp_path: Path) -> None:
    from installer.stage2.secrets import ResilientStore

    class Broken:
        def get(self, name: str) -> str | None:
            return None

        def set(self, name: str, value: str) -> None:
            raise RuntimeError("no logon session")

        def delete(self, name: str) -> None:
            return None

    paths = paths_for(tmp_path)
    store = ResilientStore(Broken(), paths, protect=xor, unprotect=xor, lock_down=refuse, warn=lambda m: None)
    with pytest.raises(OSError, match="NS-E120"):
        store.set("pexels_api_key", "K1")


def test_setup_shows_and_logs_a_key_file_it_could_not_lock(tmp_path: Path, repo_root: Path) -> None:
    from installer.stage2.secrets import ResilientStore
    from installer.stage2.testing import FakeUi
    from installer.stage2.wizard import build_context, parse_args

    home = tmp_path / "h"
    args = parse_args(
        [
            "--home",
            str(home),
            "--app-path",
            str(repo_root),
            "--non-interactive",
            "--preflight",
            str(tmp_path / "none.json"),
        ]
    )
    ui = FakeUi(interactive=False)
    ctx = build_context(args, ui=ui)
    assert isinstance(ctx.store, ResilientStore)
    ctx.store.warn("NS-E120: the test line")
    assert "NS-E120: the test line" in ui.text
    logged = "".join(f.read_text(encoding="utf-8") for f in ctx.paths.logs.glob("installer-*.jsonl"))
    assert "NS-E120: the test line" in logged and '"WARN"' in logged


def test_settings_keys_keep_the_keyring_copy_and_log_a_file_they_could_not_lock(
    tmp_path: Path, icacls: FakeIcacls
) -> None:
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
    kr = Keyring()
    adapter = RealKeyringAdapter(paths, keyring_module=kr, protect=xor, unprotect=xor)
    adapter.set("pexels_api_key", "OLD")
    before = secret_path(paths).read_bytes()
    icacls.grant_exit = 1332
    adapter.set("pexels_api_key", "NEW")  # Credential Manager took it: Settings says saved
    assert kr.items["pexels_api_key"] == "NEW"
    assert secret_path(paths).read_bytes() == before
    adapter.delete("pexels_api_key")
    assert "pexels_api_key" not in kr.items and secret_path(paths).read_bytes() == before
    records: list[dict[str, Any]] = []
    for f in paths.logs.glob("dashboard-*.jsonl"):
        records += [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()]
    warned = [r for r in records if r["level"] == "WARN" and "NS-E120" in r["msg"]]
    assert len(warned) == 2, records


# ---------------------------------------- the failure is said where the person looks, and stays said
class Keyring:
    """A keyring module that works (Credential Manager took the key)."""

    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def get_password(self, service: str, name: str) -> str | None:
        return self.items.get(name)

    def set_password(self, service: str, name: str, value: str) -> None:
        self.items[name] = value

    def delete_password(self, service: str, name: str) -> None:
        self.items.pop(name, None)


def test_a_key_file_setup_could_not_lock_stays_in_the_window_as_a_notice(
    tmp_path: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The warning goes out of the wizard as a bridge event and is read by the setup window's controller. A
    `say` line there is only the running step's detail (cut to 160 characters, replaced by the next line), so
    the line must also be a notice: the window keeps it, with the code's message and hint."""
    from installer.stage2.bridge import BridgeUi
    from installer.stage2.wizard import build_context, parse_args
    from tests.test_installer_web_controller import Setup, step_event

    out = io.StringIO()
    args = parse_args(
        [
            "--home",
            str(tmp_path / "h"),
            "--app-path",
            str(repo_root),
            "--non-interactive",
            "--preflight",
            str(tmp_path / "none.json"),
            "--ui-bridge",
            "stdio",
        ]
    )
    ctx = build_context(args, ui=BridgeUi(out=out, inp=io.StringIO("")))
    line = (
        "The pexels_api_key key is saved in Credential Manager, but saving its copy for nightly runs that "
        "cannot reach Credential Manager (secrets.dpapi) failed: NS-E120: The file that holds your API keys "
        "could not be limited to your Windows account. (icacls exit 1332)"
    )
    ctx.store.warn(line)  # type: ignore[attr-defined]
    window = Setup(tmp_path, monkeypatch)
    for raw in out.getvalue().splitlines():
        window.ctl._on_event(json.loads(raw))
    # the setup goes on: the next line and the next step replace the running step's detail, not the notice
    window.ctl._on_event(step_event("s06_tune", "running"))
    window.ctl._on_event(
        {"type": "say", "text": "Trying the story model at different settings", "style": None}
    )
    state = window.snap()
    [notice] = [n for n in state["notices"] if n["code"] == "NS-E120"]
    assert "API keys" in notice["title"] and "Try again" in notice["whatToDo"], notice
    assert any("NS-E120" in entry["text"] for entry in state["log"]), "the cause is in the window's log"
    assert ctx.warnings == [("NS-E120", line)], "the summary lists it too"


def test_settings_keys_say_on_the_page_that_the_nightly_copy_was_not_updated(
    tmp_home: Path, icacls: FakeIcacls
) -> None:
    """PUT used to answer `{set: true}` with nothing else: the owner replaced a revoked key and the nightly
    run (which reads secrets.dpapi when Credential Manager is out of reach) kept the old key unnoticed."""
    from ns_dashboard.adapters import RealKeyringAdapter
    from tests.test_dashboard_api import make_env

    client, state = make_env(tmp_home)
    paths = Paths.from_env()
    state.adapters.keyring = RealKeyringAdapter(  # type: ignore[assignment]
        paths, keyring_module=Keyring(), protect=xor, unprotect=xor
    )
    ok = client.post("/api/settings/keys", json={"name": "pexels", "value": "OLD"})
    assert ok.status_code == 200 and "warning" not in ok.json()
    assert client.get("/api/settings").json()["key_warnings"] == {}

    icacls.grant_exit = 1332
    r = client.post("/api/settings/keys", json={"name": "pexels", "value": "NEW"})
    body = r.json()
    assert r.status_code == 200 and body["set"] is True and "NEW" not in r.text
    assert body["warning"]["code"] == "NS-E120" and "Try again" in body["warning"]["hint"]
    assert "NS-E120" in body["message"] and "not updated" in body["message"]
    assert client.get("/api/settings").json()["key_warnings"]["pexels"]["code"] == "NS-E120", (
        "the line persists"
    )

    icacls.grant_exit = 0  # the next save reaches the file: the line goes
    again = client.post("/api/settings/keys", json={"name": "pexels", "value": "NEWER"})
    assert "warning" not in again.json()
    assert client.get("/api/settings").json()["key_warnings"] == {}


def test_clearing_a_key_whose_file_copy_stays_does_not_say_it_is_gone(
    tmp_home: Path, icacls: FakeIcacls
) -> None:
    """delete_secret failed, so `get` finds the key in the file again: `{set: false}` was a lie, and the next
    page load showed the key as set."""
    from ns_dashboard.adapters import RealKeyringAdapter
    from tests.test_dashboard_api import make_env

    client, state = make_env(tmp_home)
    state.adapters.keyring = RealKeyringAdapter(  # type: ignore[assignment]
        Paths.from_env(), keyring_module=Keyring(), protect=xor, unprotect=xor
    )
    client.post("/api/settings/keys", json={"name": "pixabay", "value": "KEY"})
    icacls.grant_exit = 1332
    r = client.post("/api/settings/keys", json={"name": "pixabay", "value": ""})
    body = r.json()
    assert r.status_code == 200 and body["set"] is True, body
    assert body["warning"]["code"] == "NS-E120" and "NS-E120" in body["message"]
    page = client.get("/api/settings").json()
    assert page["keys"]["pixabay"] is True and page["key_warnings"]["pixabay"]["code"] == "NS-E120"


# ------------------------------------------ a file an older version left open is locked at engine start
def test_the_engine_locks_a_secrets_file_an_older_version_left_open(
    tmp_path: Path, icacls: FakeIcacls
) -> None:
    """0.1.0 to 0.1.14 left `config/secrets.dpapi` with Users:(I)(R) when icacls exited 1332; so did
    the `pre-restore-*` copies. Nothing else would lock them until the next key save."""
    from ns_common.logging import NsLogger
    from ns_supervisor.app import relock_secret_files

    paths = paths_for(tmp_path)
    store_secret(paths, "pexels_api_key", "OLD", protect=xor, lock_down=lambda p: None)
    kept = paths.backup_dir / "pre-restore-20260101-000000" / "config"
    kept.mkdir(parents=True)
    (kept / "secrets.dpapi").write_bytes(b"{}\n")
    log = NsLogger("supervisor", paths.logs, echo=False)

    assert relock_secret_files(paths, log) == 0
    assert {Path(g[0]) for g in icacls.grants} == {secret_path(paths), kept / "secrets.dpapi"}

    icacls.grant_exit = 1332  # a lock that does not hold is a WARN with the code, not a silent pass
    assert relock_secret_files(paths, log) == 2
    records = [
        json.loads(x)
        for f in paths.logs.glob("supervisor-*.jsonl")
        for x in f.read_text("utf-8").splitlines()
    ]
    warned = [r for r in records if r["level"] == "WARN" and "NS-E120" in r["msg"]]
    assert len(warned) == 2 and all("secrets.dpapi" in r["msg"] for r in warned), records


def test_the_engine_has_nothing_to_lock_without_a_secrets_file(tmp_path: Path, icacls: FakeIcacls) -> None:
    assert secret_file.relock_existing(paths_for(tmp_path)) == []
    assert icacls.calls == []


def test_the_engine_start_calls_the_relock() -> None:
    import inspect

    from ns_supervisor import app

    assert "relock_secret_files(paths, sup.log)" in inspect.getsource(app._serve)  # noqa: SLF001


# ---------------------------------------------- a backup zip that carries the keys is locked like the file
def test_a_backup_zip_that_carries_the_keys_is_locked_before_they_go_in(
    tmp_path: Path, icacls: FakeIcacls
) -> None:
    from ns_supervisor import backup

    paths = paths_for(tmp_path)
    store_secret(paths, "pexels_api_key", "K", protect=xor, lock_down=lambda p: None)
    out = tmp_path / "usb" / "b.zip"
    backup.make_backup(paths, out)
    locked = [Path(g[0]) for g in icacls.grants]
    assert out in locked, "the zip is checked under its final name"
    [first, *_] = locked
    assert first.parent == out.parent and atomic.is_tmp_name(first.name), (
        "and while it was still an empty temp file"
    )
    with zipfile.ZipFile(out) as z:
        assert "config/secrets.dpapi" in z.namelist()
    assert [p.name for p in out.parent.iterdir()] == ["b.zip"]


def test_a_backup_that_cannot_lock_its_zip_fails_loudly_and_leaves_nothing(
    tmp_path: Path, icacls: FakeIcacls
) -> None:
    from ns_supervisor import backup

    paths = paths_for(tmp_path)
    store_secret(paths, "pexels_api_key", "K", protect=xor, lock_down=lambda p: None)
    icacls.grant_exit = 1332  # a USB stick or share that takes no ACL
    out = tmp_path / "usb" / "b.zip"
    with pytest.raises(NsError) as ei:
        backup.make_backup(paths, out)
    assert ei.value.code == "NS-E120"
    assert list(out.parent.iterdir()) == [], "no zip, no temp file: the keys were never copied there"


def test_a_backup_without_keys_needs_no_lock(tmp_path: Path, icacls: FakeIcacls) -> None:
    from ns_supervisor import backup

    paths = paths_for(tmp_path)
    (paths.config / "notes.json").write_text("{}", encoding="utf-8")
    icacls.grant_exit = 1332
    backup.make_backup(paths, tmp_path / "plain.zip")
    assert icacls.calls == []
