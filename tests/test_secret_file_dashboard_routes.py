"""F-9b break-it round: the dashboard's Settings routes tell the owner what the keys file went through.

`ns backup` and `ns restore` print the omission of the keys file (NS-W116 on a drive without permissions,
NS-E120 when icacls refuses), and the dashboard runs both commands. Before this round:

* Settings > Import answered only "Restart Nightshift to use the restored settings." (the `skipped
  config/secrets.dpapi (...)` line sat in `lines`, which the page never shows);
* Settings > Export kept only the path from `Backup written to` and dropped the `left out ...` line;
* a failed export's hint was the LAST 300 characters of stderr, which cut off the NS-E120 code and message;
* the page toasted a saved key with the one-time NS-W116 in green.

The two page rules are source checks (the page is TypeScript; the build is the `ui-build` job).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ns_common import secret_file
from ns_common.errors import describe
from ns_common.paths import Paths
from tests.test_secret_file_no_permissions import EXFAT_FLAGS, MEMBERS, SECRETS, backup_zip, volume, xor

UI = Path(__file__).resolve().parents[1] / "ns_dashboard" / "ui" / "src"


def _other_home(tmp_path: Path) -> Paths:
    """A home with an old keys file, next to the dashboard's own (`tmp_home` is `tmp_path / "home"`)."""
    paths = Paths(tmp_path / "other")
    paths.config.mkdir(parents=True)
    (paths.config / "ui_prefs.json").write_text('{"theme": "old"}', encoding="utf-8")
    secret_file.store_secret(paths, "pexels_api_key", "OLD", protect=xor, lock_down=lambda p: None)
    return paths


def test_settings_import_names_the_keys_file_a_restore_left_as_it_was(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The REAL `restore_backup` on a drive without permissions, through `RealBackupAdapter.restore`."""
    from ns_dashboard.updater import RealBackupAdapter
    from ns_supervisor import backup
    from tests.test_dashboard_api import make_env

    volume(monkeypatch, EXFAT_FLAGS)
    client, state = make_env(tmp_home)
    other = _other_home(tmp_path)
    zipped = backup_zip(tmp_path, MEMBERS)

    def runner(cmd: list[str], timeout_s: int) -> tuple[int, str, str]:
        assert cmd[-2] == "restore"
        return 0, "\n".join(backup.restore_backup(other, zipped)), ""

    state.adapters.backup = RealBackupAdapter(state.paths, runner)  # type: ignore[assignment]
    r = client.post("/api/settings/import", files={"file": ("b.zip", b"PK\x03\x04data", "application/zip")})
    body = r.json()
    assert r.status_code == 200 and body["restored"] is True, r.text
    [line] = body["left_out"]
    assert line.startswith(f"skipped {SECRETS} (NS-W116"), line
    assert "NS-W116" in body["note"] and "Restart Nightshift" in body["note"], body["note"]
    assert (other.config / "ui_prefs.json").read_text(encoding="utf-8") == '{"theme": "new"}'


def test_settings_import_with_nothing_left_out_says_only_restart(tmp_home: Path) -> None:
    from tests.test_dashboard_api import make_env

    client, state = make_env(tmp_home)
    state.adapters.backup.restore_lines = [  # type: ignore[attr-defined]
        "restored config/secrets.dpapi",
        "skipped config/runtime.json (machine specific)",
    ]
    body = client.post(
        "/api/settings/import", files={"file": ("b.zip", b"PK\x03\x04data", "application/zip")}
    ).json()
    assert body["left_out"] == [] and body["note"] == "Restart Nightshift to use the restored settings."


def test_settings_export_names_what_ns_backup_left_out(tmp_home: Path) -> None:
    from ns_dashboard.updater import RealBackupAdapter
    from tests.test_dashboard_api import make_env

    client, state = make_env(tmp_home)
    zipped = tmp_home / "usb-b.zip"
    zipped.write_bytes(b"PK")
    left = f"left out {SECRETS} (NS-W116: this drive has no file permissions in the zip)"

    def runner(cmd: list[str], timeout_s: int) -> tuple[int, str, str]:
        return 0, f"Backup written to {zipped}\n{left}\n", ""

    state.adapters.backup = RealBackupAdapter(state.paths, runner)  # type: ignore[assignment]
    body = client.post("/api/settings/export").json()
    assert body["path"] == str(zipped)
    assert body["left_out"] == [left] and "NS-W116" in body["note"], body

    def quiet(cmd: list[str], timeout_s: int) -> tuple[int, str, str]:
        return 0, f"Backup written to {zipped}\n", ""

    state.adapters.backup = RealBackupAdapter(state.paths, quiet)  # type: ignore[assignment]
    again = client.post("/api/settings/export").json()
    assert "left_out" not in again and "note" not in again, again


def test_a_failed_export_hint_keeps_the_code_and_message_of_a_coded_failure(tmp_home: Path) -> None:
    from ns_dashboard.updater import RealBackupAdapter
    from tests.test_dashboard_api import err, make_env

    client, state = make_env(tmp_home)
    d = describe("NS-E120")
    coded = f"NS-E120: {d.message}\nHint: {d.hint}\n"
    assert len(coded) > 300, "the point: the old tail-300 cut kept the end of the hint and lost the code"

    state.adapters.backup = RealBackupAdapter(state.paths, lambda c, t: (1, "", coded))  # type: ignore[assignment]
    e = err(client.post("/api/settings/export"))
    assert e["code"] == "backup_failed"
    assert e["hint"].startswith("NS-E120: "), e["hint"]
    assert d.message in e["hint"]

    # a traceback still keeps its end, where the error is
    trace = "Traceback (most recent call last):\n" + "  File x\n" * 80 + "OSError: [Errno 28] No space left"
    state.adapters.backup = RealBackupAdapter(state.paths, lambda c, t: (1, "", trace))  # type: ignore[assignment]
    e = err(client.post("/api/settings/export"))
    assert e["hint"].endswith("No space left") and len(e["hint"]) <= 300


def test_the_settings_page_shows_the_keys_file_notices_and_does_not_toast_them_green() -> None:
    page = (UI / "pages" / "Settings.tsx").read_text(encoding="utf-8")
    # a saved key that came back with a warning (the one-time NS-W116) is a warn toast, not an ok one
    assert 'r?.checked === false || r?.warning ? "warn" : "ok"' in page
    # key_warnings (NS-W116, NS-E120, NS-E119) are drawn under the key's row, not only toasted once
    assert "s?.key_warnings?.[k]" in page and "s.key_warnings[k].code" in page
    # an import / export that left the keys file out says so in the toast, in the warn tone
    assert page.count('left_out?.length ? "warn" : "ok"') == 2
    assert 'r?.note ? `. ${r.note}` : ""' in page
    # the export row keeps the note under the path (a toast is gone in seconds)
    assert "{backup.note && " in page and "left_out?: string[]; note?: string" in page
    types = (UI / "lib" / "types.ts").read_text(encoding="utf-8")
    assert "key_warnings?: Record<string," in types and "left_out?: string[]" in types


def test_the_console_install_prints_the_hint_once_and_the_summary_does_not_say_the_code_twice(
    tmp_path: Path, repo_root: Path
) -> None:
    from installer.stage2.ui import Ui
    from installer.stage2.wizard import build_context, parse_args

    class Said(Ui):
        def __init__(self) -> None:
            super().__init__(interactive=False)
            self.lines: list[str] = []

        def say(self, msg: str = "", style: str | None = None) -> None:
            self.lines.append(msg)

    ui = Said()
    args = parse_args(
        [
            "--home",
            str(tmp_path / "h"),
            "--app-path",
            str(repo_root),
            "--non-interactive",
            "--preflight",
            str(tmp_path / "none.json"),
        ]
    )
    ctx = build_context(args, ui=ui)
    warn = ctx.store.warn  # type: ignore[attr-defined]
    warn(secret_file.unprotected_line(ctx.paths.config / "secrets.dpapi"))
    [(code, detail)] = ctx.warnings
    assert code == "NS-W116" and not detail.startswith("NS-W116"), detail
    assert f"  {code} {detail}".count("NS-W116") == 1, "the summary line says the code once"
    assert any("NTFS" in ln for ln in ui.lines), "the console says what to do, as the window's card does"


def test_a_notice_marker_that_cannot_be_looked_up_gives_the_warning_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths(tmp_path / "home")
    paths.config.mkdir(parents=True)
    marker = secret_file.notice_marker(paths)

    real_exists = Path.exists

    def exists(self: Path, *a: object, **kw: object) -> bool:
        if self == marker:
            raise PermissionError(13, "Access is denied")
        return real_exists(self, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "exists", exists)
    assert secret_file.claim_unprotected_notice(paths) is True


def test_a_restore_does_not_bring_back_the_mark_that_the_warning_was_given_for_another_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ns_supervisor import backup, config_snapshot

    marker = f"config/{secret_file.NOTICE_FILE}"
    assert marker in backup.MACHINE_FILES and secret_file.NOTICE_FILE in config_snapshot.LEFT_ALONE
    home = Paths(tmp_path / "usb-home")
    home.config.mkdir(parents=True)
    z = backup_zip(tmp_path, [(marker, b'{"code": "NS-W116"}'), ("config/ui_prefs.json", b"{}")])
    lines = backup.restore_backup(home, z)
    assert f"skipped {marker} (machine specific)" in lines
    assert not secret_file.notice_marker(home).exists(), "this home still has its one warning to give"
