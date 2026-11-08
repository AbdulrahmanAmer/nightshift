import json
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_supervisor import backup

NOW = datetime(2026, 11, 1, 9, 30, 0)


def seed(home: Path, marker: str = "one") -> Paths:
    paths = Paths(home)
    paths.ensure()
    paths.runtime_file.write_text(f'{{"machine": "{marker}"}}', encoding="utf-8")
    paths.install_state_file.write_text(f'{{"app_path": "{marker}"}}', encoding="utf-8")
    paths.channel_file("horror-nights").write_text(f"slug: horror-nights\nnote: {marker}\n", encoding="utf-8")
    voice = paths.models_tts_voices / "Alice"
    voice.mkdir(parents=True)
    (voice / "voice.json").write_text('{"name": "Alice"}', encoding="utf-8")
    (voice / "speaker.bin").write_bytes(b"\x01\x02\x03")
    (paths.home / "exemplars").mkdir(exist_ok=True)    # Paths.ensure() creates it since 0.1.12
    (paths.home / "exemplars" / "mine.md").write_text("exemplar", encoding="utf-8")
    con = sqlite3.connect(paths.db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE channels (slug TEXT)")
    con.execute("INSERT INTO channels VALUES (?)", (marker,))
    con.commit()
    con.close()
    # things that must never be backed up
    (paths.models_llm / "model.gguf").write_bytes(b"GGUF")
    (voice / "render.mp4").write_bytes(b"\x00")
    (paths.config / "huge.gguf").write_bytes(b"GGUF")
    (paths.outbox / "video.mp4").write_bytes(b"\x00")
    return paths


def test_backup_contents(tmp_path: Path) -> None:
    paths = seed(tmp_path / "home")
    out = backup.make_backup(paths, tmp_path / "b.zip", now=NOW)
    z = zipfile.ZipFile(out)
    names = set(z.namelist())
    assert {
        "config/runtime.json",
        "config/install_state.json",
        "config/channels/horror-nights.yaml",
        "models/tts/voices/Alice/voice.json",
        "models/tts/voices/Alice/speaker.bin",
        "exemplars/mine.md",
        "data/studio.db",
        backup.MANIFEST,
    } <= names
    assert not [n for n in names if n.endswith((".gguf", ".mp4"))]
    assert not [n for n in names if n.startswith(("models/llm", "outbox"))]
    info = json.loads(z.read(backup.MANIFEST))
    assert info["created"] == "2026-11-01T09:30:00" and "data/studio.db" in info["entries"]


def test_backup_database_is_a_consistent_copy(tmp_path: Path) -> None:
    paths = seed(tmp_path / "home", "dbmark")
    z = zipfile.ZipFile(backup.make_backup(paths, tmp_path / "b.zip", now=NOW))
    z.extract("data/studio.db", tmp_path / "x")
    con = sqlite3.connect(tmp_path / "x" / "data" / "studio.db")
    try:
        assert con.execute("SELECT slug FROM channels").fetchall() == [("dbmark",)]
    finally:
        con.close()


def test_backup_default_location_is_the_desktop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup, "desktop_dir", lambda: tmp_path / "Desktop")
    paths = seed(tmp_path / "home")
    out = backup.make_backup(paths, now=NOW)
    assert out == tmp_path / "Desktop" / "nightshift-backup-2026-11-01.zip" and out.exists()


def test_restore_to_a_new_machine_keeps_machine_config(tmp_path: Path) -> None:
    src = seed(tmp_path / "old", "old-pc")
    z = backup.make_backup(src, tmp_path / "b.zip", now=NOW)
    dst = seed(tmp_path / "new", "new-pc")
    lines = backup.restore_backup(dst, z, now=NOW)
    # settings and data arrive
    assert "note: old-pc" in dst.channel_file("horror-nights").read_text(encoding="utf-8")
    assert (dst.models_tts_voices / "Alice" / "speaker.bin").read_bytes() == b"\x01\x02\x03"
    con = sqlite3.connect(dst.db_path)
    try:
        assert con.execute("SELECT slug FROM channels").fetchall() == [("old-pc",)]
    finally:
        con.close()
    # machine specific files stay
    assert "new-pc" in dst.runtime_file.read_text(encoding="utf-8")
    assert "new-pc" in dst.install_state_file.read_text(encoding="utf-8")
    assert any("skipped config/runtime.json" in ln for ln in lines)
    # what was overwritten is kept
    saved = list((dst.backup_dir).glob("pre-restore-*"))
    assert len(saved) == 1
    assert "new-pc" in (saved[0] / "config" / "channels" / "horror-nights.yaml").read_text(encoding="utf-8")
    assert (saved[0] / "data" / "studio.db").exists()


def test_restore_include_machine(tmp_path: Path) -> None:
    src = seed(tmp_path / "old", "old-pc")
    z = backup.make_backup(src, tmp_path / "b.zip", now=NOW)
    dst = seed(tmp_path / "new", "new-pc")
    backup.restore_backup(dst, z, include_machine=True, now=NOW)
    assert "old-pc" in dst.runtime_file.read_text(encoding="utf-8")


def test_restore_rejects_non_backups_and_unsafe_zips(tmp_path: Path) -> None:
    paths = seed(tmp_path / "home")
    plain = tmp_path / "plain.zip"
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("config/x.json", "{}")
    with pytest.raises(NsError) as e1:
        backup.restore_backup(paths, plain)
    assert e1.value.code == "NS-E703"
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr(backup.MANIFEST, "{}")
        zf.writestr("config/../../evil.txt", "x")
    with pytest.raises(NsError) as e2:
        backup.restore_backup(paths, evil)
    assert e2.value.code == "NS-E703"
    assert not (tmp_path / "evil.txt").exists()
    notzip = tmp_path / "bad.zip"
    notzip.write_text("nope", encoding="utf-8")
    with pytest.raises(NsError) as e3:
        backup.restore_backup(paths, notzip)
    assert e3.value.code == "NS-E703"


def test_restore_ignores_unlisted_paths(tmp_path: Path) -> None:
    paths = seed(tmp_path / "home")
    z = tmp_path / "odd.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr(backup.MANIFEST, "{}")
        zf.writestr("models/llm/evil.gguf", "x")
        zf.writestr("outbox/a.mp4", "x")
    lines = backup.restore_backup(paths, z, now=NOW)
    assert not (paths.models_llm / "evil.gguf").exists() and not (paths.outbox / "a.mp4").exists()
    assert all(ln.startswith("ignored") for ln in lines)


def test_a_restore_that_hits_an_os_error_puts_everything_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disk full or a locked database half way through must not leave a mix of old and new settings."""
    src = seed(tmp_path / "old", "old-pc")
    z = backup.make_backup(src, tmp_path / "b.zip", now=NOW)
    dst = seed(tmp_path / "new", "new-pc")
    voice_file = dst.models_tts_voices / "Alice" / "speaker.bin"
    voice_file.write_bytes(b"NEWPC")
    real = backup.shutil.copyfileobj
    calls = {"n": 0}

    def flaky(fsrc, fdst, *a, **k):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 3:                          # the third restored file runs out of disk
            fdst.write(b"half")
            raise OSError(28, "No space left on device")
        return real(fsrc, fdst, *a, **k)

    monkeypatch.setattr(backup.shutil, "copyfileobj", flaky)
    with pytest.raises(NsError) as ei:
        backup.restore_backup(dst, z, now=NOW)
    monkeypatch.undo()
    assert ei.value.code == "NS-E703" and "No space left" in ei.value.detail and "put back" in ei.value.detail
    # the files restored before the failure are the new PC's again, and the half-written one is whole
    assert "note: new-pc" in dst.channel_file("horror-nights").read_text(encoding="utf-8")
    assert voice_file.read_bytes() == b"NEWPC"
    con = sqlite3.connect(dst.db_path)
    try:
        assert con.execute("SELECT slug FROM channels").fetchall() == [("new-pc",)]
    finally:
        con.close()
