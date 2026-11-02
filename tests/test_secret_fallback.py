"""C1/C3: the keys survive a nightly session that cannot reach Credential Manager (review P2)."""

import sys
from pathlib import Path

import pytest

from installer.stage2.secrets import MemoryStore, ResilientStore
from ns_common.paths import Paths
from ns_common.secret_file import load_secret, secret_path, store_secret


def xor_protect(data: bytes) -> bytes:                      # stands in for DPAPI in a test on any OS
    return bytes(b ^ 0x5A for b in data)


class FakeKeyring(MemoryStore):
    """A keyring that refuses (an S4U session) or works."""

    def __init__(self, broken: bool = False) -> None:
        super().__init__()
        self.broken = broken

    def get(self, name: str) -> str | None:
        if self.broken:
            raise RuntimeError("no credential store in this logon session")
        return super().get(name)

    def set(self, name: str, value: str) -> None:
        if self.broken:
            raise RuntimeError("no credential store in this logon session")
        super().set(name, value)


def store(tmp_path: Path, keyring: FakeKeyring) -> ResilientStore:
    return ResilientStore(keyring, Paths(tmp_path), protect=xor_protect, unprotect=xor_protect,
                          lock_down=lambda p: None)


def test_setup_writes_both_and_the_nightly_run_reads_the_file_when_the_keyring_is_empty(tmp_path: Path):
    setup = store(tmp_path, FakeKeyring())
    setup.set("pexels_api_key", "KEY-1")
    assert secret_path(Paths(tmp_path)).is_file()
    assert "KEY-1" not in secret_path(Paths(tmp_path)).read_text(encoding="utf-8")      # not in the clear
    nightly = store(tmp_path, FakeKeyring(broken=True))       # a new session: keyring unavailable
    assert nightly.get("pexels_api_key") == "KEY-1"
    assert load_secret(Paths(tmp_path), "pexels_api_key", unprotect=xor_protect) == "KEY-1"


def test_a_broken_keyring_at_setup_still_stores_the_key_in_the_file(tmp_path: Path):
    s = store(tmp_path, FakeKeyring(broken=True))
    s.set("pixabay_api_key", "KEY-2")
    assert s.get("pixabay_api_key") == "KEY-2"


def test_the_keyring_wins_when_it_has_a_value(tmp_path: Path):
    kr = FakeKeyring()
    kr.set("pexels_api_key", "NEW")
    store_secret(Paths(tmp_path), "pexels_api_key", "OLD", protect=xor_protect)
    assert store(tmp_path, kr).get("pexels_api_key") == "NEW"


def test_it_fails_only_when_both_places_refuse(tmp_path: Path):
    def refuse(data: bytes) -> bytes:
        raise OSError("DPAPI unavailable")

    s = ResilientStore(FakeKeyring(broken=True), Paths(tmp_path), protect=refuse, unprotect=refuse,
                       lock_down=lambda p: None)
    with pytest.raises(OSError, match="could not store"):
        s.set("pexels_api_key", "K")
    assert s.get("pexels_api_key") is None


def test_a_damaged_or_foreign_blob_reads_as_missing(tmp_path: Path):
    paths = Paths(tmp_path)
    store_secret(paths, "pexels_api_key", "K", protect=xor_protect)

    def foreign(blob: bytes) -> bytes:
        raise OSError("blob belongs to another PC")

    assert load_secret(paths, "pexels_api_key", unprotect=foreign) is None
    secret_path(paths).write_text("{not json", encoding="utf-8")
    assert load_secret(paths, "pexels_api_key", unprotect=xor_protect) is None
    secret_path(paths).write_text('{"pexels_api_key": "!!!"}', encoding="utf-8")
    assert load_secret(paths, "pexels_api_key", unprotect=xor_protect) is None





@pytest.mark.skipif(sys.platform != "win32", reason="real DPAPI")
def test_real_dpapi_round_trip(tmp_path: Path):
    paths = Paths(tmp_path)
    store_secret(paths, "pexels_api_key", "real-secret-é")
    assert "real-secret" not in secret_path(paths).read_text(encoding="utf-8")
    assert load_secret(paths, "pexels_api_key") == "real-secret-é"
