"""F-9b: a keys file on a drive without permissions (FAT, exFAT) is saved with a one-time warning.

PR 175 made `restrict_to_user` raise `NS-E120` when icacls fails or the read-back is wrong. On a volume that
keeps no permissions at all (FAT and exFAT: `GetVolumeInformationW` lacks `FILE_PERSISTENT_ACLS`, 0x8) that
is every save: setup could not keep the keys for the nightly run, Settings > Keys always said the copy was not
updated, a restore of any backup that held `secrets.dpapi` failed in full (`NS-E703`), `ns backup` printed a
traceback and an engine-start lock failure reached only the log.

The volume's answer is FAKED here (`secret_file._volume_flags`), and icacls with it (`subprocess.run`, or the
`_run_icacls` seam the F-9 tests use): no real FAT disk is touched. QA proves it on an exFAT virtual disk.
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
from ns_common.errors import NsError, describe
from ns_common.paths import Paths
from ns_common.secret_file import delete_secret, load_secret, secret_path, store_secret

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the lock is a Windows ACL set with icacls")

SID = "S-1-5-21-111-222-333-1001"
GOOD = f"secrets.dpapi\r\nD:PAI(A;;FA;;;SY)(A;;FA;;;{SID})\r\n"
NO_MAPPING = "No mapping between account names and security IDs was done."
#: what NTFS answers (FILE_PERSISTENT_ACLS 0x8 among the others) and what exFAT answers (no 0x8)
NTFS_FLAGS = 0x03E706FF
EXFAT_FLAGS = 0x00020206
SECRETS = "config/secrets.dpapi"


def xor(data: bytes) -> bytes:  # stands in for DPAPI
    return bytes(b ^ 0x5A for b in data)


class Volume:
    """The faked `GetVolumeInformationW`: `answers` is consumed one per call, the last one repeats."""

    def __init__(self, *answers: int | BaseException) -> None:
        self.answers = list(answers) or [NTFS_FLAGS]
        self.asked: list[Path] = []

    def __call__(self, path: Path) -> int:
        self.asked.append(Path(path))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


class Icacls:
    """Stands in for icacls behind `subprocess.run`: records each call, answers `grant_exit` to a grant."""

    def __init__(self, real: Callable[..., Any]) -> None:
        self.real = real
        self.grant_exit = 0
        self.calls: list[list[str]] = []

    def __call__(self, args: Any, *a: Any, **kw: Any) -> Any:
        argv = [str(x) for x in args]
        if not argv or not (argv[0].lower().endswith("icacls.exe") or argv[0].lower() == "icacls"):
            return self.real(args, *a, **kw)
        self.calls.append(argv[1:])
        if "/save" in argv:
            Path(argv[argv.index("/save") + 1]).write_bytes(GOOD.encode("utf-16-le"))
            return subprocess.CompletedProcess(argv, 0, "Successfully processed 1 files", "")
        if self.grant_exit:
            return subprocess.CompletedProcess(argv, self.grant_exit, "", NO_MAPPING)
        return subprocess.CompletedProcess(argv, 0, f"processed file: {argv[1]}", "")


@pytest.fixture
def icacls(monkeypatch: pytest.MonkeyPatch) -> Icacls:
    """icacls faked at `subprocess.run`, the account's SID faked; `restrict_to_user` is the real one."""
    fake = Icacls(subprocess.run)
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(secret_file, "current_user_sid", lambda: SID)
    return fake


def volume(monkeypatch: pytest.MonkeyPatch, *answers: int | BaseException) -> Volume:
    fake = Volume(*answers)
    monkeypatch.setattr(secret_file, "_volume_flags", fake)
    return fake


def paths_for(tmp_path: Path) -> Paths:
    p = Paths(tmp_path / "home")
    p.config.mkdir(parents=True)
    return p


def config_names(paths: Paths) -> list[str]:
    return sorted(p.name for p in paths.config.iterdir())


class Notices:
    def __init__(self) -> None:
        self.got: list[tuple[str, str]] = []

    def __call__(self, code: str, line: str) -> None:
        self.got.append((code, line))


# ------------------------------------------------------------------------------------- the volume's answer
def test_the_volume_answer_is_read_from_the_persistent_acls_flag_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v = volume(monkeypatch, NTFS_FLAGS, EXFAT_FLAGS, OSError(21, "The device is not ready"))
    assert secret_file.volume_keeps_permissions(tmp_path) is True
    assert secret_file.volume_keeps_permissions(tmp_path) is False
    # the call itself failed: assume the drive DOES keep permissions, so a lock that then fails is NS-E120
    assert secret_file.volume_keeps_permissions(tmp_path) is True
    assert v.asked == [tmp_path] * 3


def test_this_disk_really_keeps_permissions(tmp_path: Path) -> None:
    """The real call, on the NTFS volume of the test's temp folder (the laptop's D:, the runner's D:)."""
    flags = secret_file._volume_flags(tmp_path / "not-made-yet" / "secrets.dpapi")  # noqa: SLF001
    assert flags & secret_file.FILE_PERSISTENT_ACLS
    assert secret_file.volume_keeps_permissions(tmp_path) is True


# ------------------------------------------------------------------------- (1) a save on such a drive
def test_a_key_saved_on_a_drive_without_permissions_is_kept_and_warned_about_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, icacls: Icacls
) -> None:
    volume(monkeypatch, EXFAT_FLAGS)
    paths = paths_for(tmp_path)
    said = Notices()
    store_secret(paths, "pexels_api_key", "K1", protect=xor, notice=said)
    assert load_secret(paths, "pexels_api_key", unprotect=xor) == "K1", "the nightly run has the key"
    assert icacls.calls == [], "no icacls on a drive that has no permissions to set"
    [(code, line)] = said.got
    assert code == "NS-W116" and "secrets.dpapi" in line, line
    marker = secret_file.notice_marker(paths)
    assert json.loads(marker.read_text(encoding="utf-8"))["code"] == "NS-W116"
    store_secret(paths, "pixabay_api_key", "K2", protect=xor, notice=said)
    assert delete_secret(paths, "pexels_api_key", notice=said) is True
    assert len(said.got) == 1, "the warning appears once"
    assert load_secret(paths, "pixabay_api_key", unprotect=xor) == "K2"
    assert load_secret(paths, "pexels_api_key", unprotect=xor) is None
    assert config_names(paths) == ["keys_unprotected.json", "secrets.dpapi"], "no temp file left"


def test_the_warning_says_the_drive_cannot_keep_the_file_private_and_that_it_is_shown_once() -> None:
    w = describe("NS-W116")
    assert w.kind == "warning"
    assert "Windows cannot keep" in w.message and "private" in w.message and "API keys" in w.message
    for words in ("FAT", "exFAT", "NTFS", "NIGHTSHIFT_HOME", "once", "Credential Manager"):
        assert words in w.hint, words


def test_a_save_without_a_listener_does_not_use_up_the_one_time_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, icacls: Icacls
) -> None:
    volume(monkeypatch, EXFAT_FLAGS)
    paths = paths_for(tmp_path)
    store_secret(paths, "pexels_api_key", "K1", protect=xor)  # nobody to tell: the warning waits
    assert not secret_file.notice_marker(paths).exists()
    said = Notices()
    store_secret(paths, "pexels_api_key", "K2", protect=xor, notice=said)
    assert [c for c, _ in said.got] == ["NS-W116"]


def test_a_volume_check_that_fails_is_taken_as_permissions_so_a_failing_lock_is_still_ns_e120(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, icacls: Icacls
) -> None:
    volume(monkeypatch, OSError(1117, "The request could not be performed because of an I/O device error"))
    paths = paths_for(tmp_path)
    store_secret(paths, "pexels_api_key", "OLD", protect=xor)
    before = secret_path(paths).read_bytes()
    icacls.grant_exit = 1332
    said = Notices()
    with pytest.raises(NsError) as ei:
        store_secret(paths, "pexels_api_key", "NEW", protect=xor, notice=said)
    assert ei.value.code == "NS-E120"
    assert secret_path(paths).read_bytes() == before and said.got == []
    assert not secret_file.notice_marker(paths).exists()


def test_a_drive_that_answers_no_permissions_then_permissions_does_not_flip_the_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, icacls: Icacls
) -> None:
    """One answer per save, decided once for that save: the temp file and the final name never get two."""
    v = volume(monkeypatch, EXFAT_FLAGS, NTFS_FLAGS, EXFAT_FLAGS, NTFS_FLAGS)
    paths = paths_for(tmp_path)
    said = Notices()
    store_secret(paths, "pexels_api_key", "K1", protect=xor, notice=said)  # exFAT: saved, warned
    assert icacls.calls == []
    store_secret(paths, "pexels_api_key", "K2", protect=xor, notice=said)  # NTFS: locked as F-9 does
    grants = [c for c in icacls.calls if "/inheritance:r" in c]
    assert len(grants) == 2, "the empty temp file and the final name"
    icacls.calls.clear()
    store_secret(paths, "pexels_api_key", "K3", protect=xor, notice=said)  # exFAT again: no second warning
    assert icacls.calls == []
    assert len(said.got) == 1 and secret_file.notice_marker(paths).exists()
    assert len(v.asked) == 3, "one question per save"
    icacls.grant_exit = 1332  # NTFS again, and the lock fails: an error, as on any drive with permissions
    with pytest.raises(NsError) as ei:
        store_secret(paths, "pexels_api_key", "K4", protect=xor, notice=said)
    assert ei.value.code == "NS-E120" and load_secret(paths, "pexels_api_key", unprotect=xor) == "K3"


def test_two_writers_racing_for_the_one_time_warning_give_it_once(tmp_path: Path) -> None:
    target = tmp_path / "config" / "keys_unprotected.json"
    assert atomic.create_text_atomic(target, '{"first": true}\n') is True
    assert atomic.create_text_atomic(target, '{"second": true}\n') is False
    assert json.loads(target.read_text(encoding="utf-8")) == {"first": True}
    assert [p.name for p in target.parent.iterdir()] == ["keys_unprotected.json"], "no temp file left"


# ------------------------------------------------------- (1) the setup window and its install report
def test_setup_saves_the_key_on_a_drive_without_permissions_and_reports_ns_w116_once(
    tmp_path: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch, icacls: Icacls
) -> None:
    from installer.stage2.bridge import BridgeUi
    from installer.stage2.secrets import MemoryStore, ResilientStore
    from installer.stage2.wizard import build_context, parse_args
    from tests.test_installer_web_controller import Setup

    volume(monkeypatch, EXFAT_FLAGS)
    out = io.StringIO()
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
            "--ui-bridge",
            "stdio",
        ]
    )
    ctx = build_context(args, ui=BridgeUi(out=out, inp=io.StringIO("")))
    wizard_store = ctx.store
    assert isinstance(wizard_store, ResilientStore)
    # the wizard's own store with its own warning channel; only Credential Manager and DPAPI are fakes
    store = ResilientStore(MemoryStore(), ctx.paths, protect=xor, unprotect=xor, warn=wizard_store.warn)
    store.set("pexels_api_key", "K1")
    store.set("pixabay_api_key", "K2")
    assert load_secret(ctx.paths, "pexels_api_key", unprotect=xor) == "K1"
    assert load_secret(ctx.paths, "pixabay_api_key", unprotect=xor) == "K2"
    assert [code for code, _ in ctx.warnings] == ["NS-W116"], "once, though two keys were saved"
    logged = "".join(f.read_text(encoding="utf-8") for f in ctx.paths.logs.glob("installer-*.jsonl"))
    assert "secret_file_unprotected" in logged and "NS-W116" in logged

    window = Setup(tmp_path, monkeypatch)
    for raw in out.getvalue().splitlines():
        window.ctl._on_event(json.loads(raw))  # noqa: SLF001
    [notice] = [n for n in window.snap()["notices"] if n["code"] == "NS-W116"]
    assert "private" in notice["title"] and "NTFS" in notice["whatToDo"], notice
    report = window.ctl._write_install_report("done")  # noqa: SLF001
    assert report is not None
    assert "NS-W116" in json.loads(report.read_text(encoding="utf-8"))["warnings"]


# ------------------------------------------------------------------- (1) Settings > Keys in the dashboard
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


def test_settings_keys_save_on_a_drive_without_permissions_and_say_ns_w116_once(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, icacls: Icacls
) -> None:
    from ns_dashboard.adapters import RealKeyringAdapter
    from tests.test_dashboard_api import make_env

    volume(monkeypatch, EXFAT_FLAGS)
    client, state = make_env(tmp_home)
    paths = Paths.from_env()
    state.adapters.keyring = RealKeyringAdapter(  # type: ignore[assignment]
        paths, keyring_module=Keyring(), protect=xor, unprotect=xor
    )
    r = client.post("/api/settings/keys", json={"name": "pexels", "value": "K1"})
    body = r.json()
    assert r.status_code == 200 and body["set"] is True and "K1" not in r.text
    assert body["warning"]["code"] == "NS-W116" and "NTFS" in body["warning"]["hint"]
    assert "NS-W116" in body["message"] and "not updated" not in body["message"], body["message"]
    assert load_secret(paths, "pexels_api_key", unprotect=xor) == "K1", "the nightly copy IS saved"
    page = client.get("/api/settings").json()
    assert page["key_warnings"]["pexels"]["code"] == "NS-W116"
    assert icacls.calls == []

    again = client.post("/api/settings/keys", json={"name": "pixabay", "value": "K2"})
    assert "warning" not in again.json(), "the warning appears once"
    # a restarted dashboard (a new adapter) does not show it again either: the marker is in config/
    state.adapters.keyring = RealKeyringAdapter(  # type: ignore[assignment]
        paths, keyring_module=Keyring(), protect=xor, unprotect=xor
    )
    third = client.post("/api/settings/keys", json={"name": "pexels", "value": "K3"})
    assert "warning" not in third.json()
    assert client.get("/api/settings").json()["key_warnings"] == {}
    cleared = client.post("/api/settings/keys", json={"name": "pexels", "value": ""})
    assert cleared.json()["set"] is False and load_secret(paths, "pexels_api_key", unprotect=xor) is None


# --------------------------------------------------------------------------------- (2) a restore
def backup_zip(tmp_path: Path, members: list[tuple[str, bytes]]) -> Path:
    from ns_supervisor import backup

    z = tmp_path / "b.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr(backup.MANIFEST, "{}")
        for name, data in members:
            zf.writestr(name, data)
    return z


MEMBERS = [
    ("config/ui_prefs.json", b'{"theme": "new"}'),
    (SECRETS, b'{"pexels_api_key": "bmV3"}\n'),
    ("models/tts/voices/v1/voice.json", b'{"mode": "clone"}'),
    ("exemplars/e1.txt", b"exemplar"),
]


def old_home(tmp_path: Path) -> tuple[Paths, bytes]:
    paths = paths_for(tmp_path)
    (paths.config / "ui_prefs.json").write_text('{"theme": "old"}', encoding="utf-8")
    store_secret(paths, "pexels_api_key", "OLD", protect=xor, lock_down=lambda p: None)
    return paths, secret_path(paths).read_bytes()


def assert_the_rest_was_restored(paths: Paths) -> None:
    assert (paths.config / "ui_prefs.json").read_text(encoding="utf-8") == '{"theme": "new"}'
    voice = paths.home / "models" / "tts" / "voices" / "v1" / "voice.json"
    assert voice.read_bytes() == b'{"mode": "clone"}'
    assert (paths.home / "exemplars" / "e1.txt").read_bytes() == b"exemplar"
    assert not [p for p in paths.config.iterdir() if atomic.is_tmp_name(p.name)]
    assert not list(paths.backup_dir.rglob("secrets.dpapi")), "no copy of a keys file the restore left alone"


def test_ns_restore_onto_a_drive_without_permissions_skips_only_the_keys_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, icacls: Icacls
) -> None:
    from typer.testing import CliRunner

    from ns_supervisor import cli

    volume(monkeypatch, EXFAT_FLAGS)
    paths, before = old_home(tmp_path)
    z = backup_zip(tmp_path, MEMBERS)

    class Down:
        def is_up(self) -> bool:
            return False

    monkeypatch.setattr(cli, "_client", lambda p: Down())
    r = CliRunner().invoke(cli.app, ["restore", str(z)], env={"NIGHTSHIFT_HOME": str(paths.home)})
    assert r.exit_code == 0, r.output
    assert f"skipped {SECRETS} (NS-W116" in r.output, r.output
    assert "restored config/ui_prefs.json" in r.output and "restored exemplars/e1.txt" in r.output
    assert secret_path(paths).read_bytes() == before, "the old keys file is untouched"
    assert_the_rest_was_restored(paths)
    assert icacls.calls == []


def test_a_restore_where_icacls_fails_skips_only_the_keys_file(tmp_path: Path, icacls: Icacls) -> None:
    from ns_supervisor import backup

    paths, before = old_home(tmp_path)
    icacls.grant_exit = 1332
    lines = backup.restore_backup(paths, backup_zip(tmp_path, MEMBERS))
    [skipped] = [ln for ln in lines if SECRETS in ln]
    assert skipped.startswith(f"skipped {SECRETS} (NS-E120") and "1332" in skipped, skipped
    assert "left as it was" in skipped
    assert "restored config/ui_prefs.json" in lines and "restored models/tts/voices/v1/voice.json" in lines
    assert secret_path(paths).read_bytes() == before
    assert_the_rest_was_restored(paths)


def test_a_restored_keys_file_whose_check_fails_puts_the_old_one_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ns_supervisor import backup

    paths, before = old_home(tmp_path)

    def lock(p: Path) -> None:  # every temp file locks; the check on the restored final name fails
        if p == secret_path(paths) and p.read_bytes() != before:
            raise secret_file.SecretFileLockError(p, "the read-back showed Users")

    monkeypatch.setattr(backup, "restrict_to_user", lock)
    lines = backup.restore_backup(paths, backup_zip(tmp_path, MEMBERS))
    assert any(ln.startswith(f"skipped {SECRETS} (NS-E120") for ln in lines), lines
    assert secret_path(paths).read_bytes() == before
    assert_the_rest_was_restored(paths)


# ------------------------------------------------------------------------------------- (3) ns backup
def test_ns_backup_prints_the_coded_error_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, icacls: Icacls
) -> None:
    from typer.testing import CliRunner

    from ns_supervisor import cli

    paths, _ = old_home(tmp_path)
    icacls.grant_exit = 1332
    out = tmp_path / "out" / "b.zip"
    r = CliRunner().invoke(cli.app, ["backup", "--out", str(out)], env={"NIGHTSHIFT_HOME": str(paths.home)})
    assert r.exit_code == 1, r.output
    assert r.exception is None or isinstance(r.exception, SystemExit), repr(r.exception)
    assert "Traceback" not in r.output
    assert f"NS-E120: {describe('NS-E120').message}" in r.output
    assert f"Hint: {describe('NS-E120').hint}" in r.output
    assert not out.exists() and list(out.parent.iterdir()) == [], "no zip, no temp file"


def test_ns_backup_to_a_drive_without_permissions_leaves_the_keys_out_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, icacls: Icacls
) -> None:
    from typer.testing import CliRunner

    from ns_supervisor import cli

    paths, _ = old_home(tmp_path)
    usb = tmp_path / "usb"

    def flags(p: Path) -> int:  # the home is on NTFS, the stick the backup goes to is exFAT
        return EXFAT_FLAGS if usb == Path(p) or usb in Path(p).parents else NTFS_FLAGS

    monkeypatch.setattr(secret_file, "_volume_flags", flags)
    out = usb / "b.zip"
    r = CliRunner().invoke(cli.app, ["backup", "--out", str(out)], env={"NIGHTSHIFT_HOME": str(paths.home)})
    assert r.exit_code == 0, r.output
    assert f"Backup written to {out}" in r.output
    assert f"left out {SECRETS} (NS-W116" in r.output, r.output
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        manifest = json.loads(z.read("backup-manifest.json"))
    assert SECRETS not in names and SECRETS not in manifest["entries"]
    assert "config/ui_prefs.json" in names
    assert icacls.calls == [], "nothing to lock: the keys never went to that drive"


# ------------------------------------------------------------------------------- (4) the engine start
def notes_shown(tmp_home: Path) -> list[dict[str, Any]]:
    """What the dashboard shows the owner (`GET /api/notes`, drawn on the Queue page)."""
    from tests.test_dashboard_api import make_env

    client, _ = make_env(tmp_home)
    r = client.get("/api/notes")
    assert r.status_code == 200
    return list(r.json())


def test_an_engine_start_lock_failure_is_a_coded_note_the_dashboard_shows(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, icacls: Icacls
) -> None:
    from ns_common.logging import NsLogger
    from ns_supervisor.app import relock_secret_files

    paths = Paths(tmp_home)
    paths.config.mkdir(parents=True)
    store_secret(paths, "pexels_api_key", "K", protect=xor, lock_down=lambda p: None)
    icacls.grant_exit = 1332
    log = NsLogger("supervisor", paths.logs, echo=False)
    assert relock_secret_files(paths, log) == 1
    [note] = notes_shown(tmp_home)
    assert note["message"].startswith("NS-E120: ") and "secrets.dpapi" in note["message"], note
    assert "1332" in note["message"]
    assert relock_secret_files(paths, log) == 1
    assert len(notes_shown(tmp_home)) == 1, "the same problem is not queued twice"


def test_an_engine_start_on_a_drive_without_permissions_leaves_the_one_time_ns_w116_note(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, icacls: Icacls
) -> None:
    from ns_common.logging import NsLogger
    from ns_supervisor.app import relock_secret_files

    volume(monkeypatch, EXFAT_FLAGS)
    paths = Paths(tmp_home)
    paths.config.mkdir(parents=True)
    store_secret(paths, "pexels_api_key", "K", protect=xor)  # saved by 0.1.14: nobody was told
    log = NsLogger("supervisor", paths.logs, echo=False)
    assert relock_secret_files(paths, log) == 0, "a drive without permissions is not a lock failure"
    [note] = notes_shown(tmp_home)
    assert note["message"].startswith("NS-W116: ") and "NTFS" in note["message"], note
    assert icacls.calls == []
    relock_secret_files(paths, log)
    assert len(notes_shown(tmp_home)) == 1, "once"


# ----------------------------------------------------------------------------- (5) the NS-E120 hint
def test_the_ns_e120_hint_names_drives_without_permissions() -> None:
    hint = describe("NS-E120").hint
    assert "If the drive is FAT or exFAT Windows cannot keep it private" in hint
    assert "move NIGHTSHIFT_HOME to an NTFS drive" in hint
    assert "Try again" in hint


def test_every_place_that_words_the_keys_file_codes_says_the_same_about_exfat(repo_root: Path) -> None:
    """errors.json is the source; the setup window reads it (no operator_copy.json entry), the operator guide
    has its own row for each code, and stage 1 never raises them (its built-in table does not hold them)."""
    copy = json.loads((repo_root / "installer" / "stage2" / "web" / "operator_copy.json").read_text("utf-8"))
    stage1 = (repo_root / "installer" / "stage1.ps1").read_text(encoding="utf-8")
    guide = (repo_root / "OPERATOR.md").read_text(encoding="utf-8")
    for code in ("NS-E120", "NS-W116"):
        entry = copy.get(code)
        if entry is not None:
            assert "exFAT" in entry["whatToDo"] and "NTFS" in entry["whatToDo"], code
        assert f"'{code}'" not in stage1, f"stage1.ps1 has its own wording for {code}: keep it in step"
        [row] = [ln for ln in guide.splitlines() if ln.startswith(f"| {code} |")]
        assert "exFAT" in row and "NTFS" in row and "NIGHTSHIFT_HOME" in row, row
