"""C1/C2: toasts are sent from an AppUserModelID that the installer registers (and uninstall removes).

`ns_supervisor.notify` sends toasts as AUMID "Nightshift"; nothing registered it, so Windows dropped them.
"""

import base64
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from installer.stage2 import winenv
from installer.stage2.runner import RunResult, SubprocessRunner
from installer.stage2.simulate import FakeRunner
from installer.stage2.steps.s09_shortcuts_scheduler import S09ShortcutsScheduler
from installer.stage2.testing import example_answers, make_test_context
from ns_supervisor import notify

ROOT = Path(__file__).resolve().parents[1]
BS = chr(92)
PS = shutil.which("powershell") or shutil.which("pwsh")
needs_windows = pytest.mark.skipif(
    sys.platform != "win32" or PS is None, reason="Windows PowerShell needed"
)


def script_of(call) -> str:  # type: ignore[no-untyped-def]
    return base64.b64decode(call.argv[call.argv.index("-EncodedCommand") + 1]).decode("utf-16-le")


def app_with_icon(tmp_path: Path) -> tuple[Path, Path]:
    app = tmp_path / "app"
    (app / "launchers").mkdir(parents=True)
    (app / "launchers" / "nightshift-dashboard.ps1").write_text("# launcher", encoding="utf-8")
    (app / "launchers" / "open-outbox.ps1").write_text("# launcher", encoding="utf-8")
    icon = app / "ns_dashboard" / "static" / "brand" / "nightshift.ico"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"ico")
    return app, icon


def step_ctx(tmp_path: Path, runner: FakeRunner):  # type: ignore[no-untyped-def]
    app, icon = app_with_icon(tmp_path)
    answers = example_answers()
    ctx = make_test_context(tmp_path, raw_answers=answers, resolve=True, app_path=app, runner=runner)
    return ctx, icon


# ---------------------------------------------------------------------------------------------- the step
def test_toasts_use_the_registered_id() -> None:
    assert notify.AUMID == "Nightshift"
    assert f"CreateToastNotifier('{notify.AUMID}')" in notify.TOAST_SCRIPT


def test_s09_registers_the_id_and_stamps_it_on_the_dashboard_shortcut(tmp_path: Path) -> None:
    runner = FakeRunner(tmp_path / "desktop")
    ctx, icon = step_ctx(tmp_path, runner)
    step = S09ShortcutsScheduler()
    assert step.check(ctx) is False
    step.run(ctx)
    step.verify(ctx)
    reg = runner.called("ps:aumid-register")
    assert len(reg) == 1 and reg[0].meta["aumid"] == "Nightshift" and reg[0].meta["icon"] == str(icon)
    text = script_of(reg[0])
    assert "'DisplayName' -Value 'Nightshift'" in text and f"'IconUri' -Value '{icon}'" in text
    stamp = runner.called("ps:aumid-shortcut")
    assert len(stamp) == 1 and stamp[0].meta["lnk"].endswith("Nightshift Dashboard.lnk")
    stamp_script = script_of(stamp[0])
    assert "[NsAumid]::Set(" in stamp_script and "SHGetPropertyStoreFromParsingName" in stamp_script
    assert ctx.flag("aumid") == "registry+shortcut" and step.check(ctx) is True


def test_s09_without_the_ico_registers_the_name_only(tmp_path: Path) -> None:
    runner = FakeRunner(tmp_path / "desktop")
    ctx, icon = step_ctx(tmp_path, runner)
    icon.unlink()
    S09ShortcutsScheduler().run(ctx)
    assert "IconUri" not in script_of(runner.called("ps:aumid-register")[0])


def test_s09_keeps_the_registry_key_when_the_property_store_fails(tmp_path: Path) -> None:
    runner = FakeRunner(tmp_path / "desktop", results={"ps:aumid-shortcut": RunResult(1, "", "E_FAIL")})
    ctx, _ = step_ctx(tmp_path, runner)
    step = S09ShortcutsScheduler()
    step.run(ctx)
    step.verify(ctx)
    assert ctx.flag("aumid") == "registry" and step.check(ctx) is True


def test_s09_toast_registration_failure_never_fails_the_install(tmp_path: Path) -> None:
    runner = FakeRunner(tmp_path / "desktop", results={"ps:aumid-register": RunResult(1, "", "denied")})
    ctx, _ = step_ctx(tmp_path, runner)
    step = S09ShortcutsScheduler()
    step.run(ctx)
    step.verify(ctx)
    assert ctx.flag("aumid") == "failed" and runner.called("ps:aumid-shortcut") == []
    assert "notifications could not be registered" in ctx.ui.text


# ---------------------------------------------------------------------------------------------- real Windows
@needs_windows
def test_registry_key_is_written_and_removed(tmp_path: Path) -> None:
    import winreg

    aumid = "NightshiftTest" + uuid.uuid4().hex[:8]
    icon = tmp_path / "nightshift.ico"
    icon.write_bytes(b"ico")
    runner = SubprocessRunner()
    key_path = BS.join(["Software", "Classes", "AppUserModelId", aumid])
    try:
        assert winenv.register_aumid(runner, aumid, "Nightshift", icon) is True
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            assert winreg.QueryValueEx(key, "DisplayName")[0] == "Nightshift"
            assert winreg.QueryValueEx(key, "IconUri")[0] == str(icon)
        assert winenv.register_aumid(runner, aumid, "Nightshift", icon) is True      # idempotent
    finally:
        assert winenv.unregister_aumid(runner, aumid) is True
    with pytest.raises(FileNotFoundError):
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path)
    assert winenv.unregister_aumid(runner, aumid) is True                            # already gone


@needs_windows
def test_property_store_stamps_and_reads_back_the_id_on_a_real_shortcut(tmp_path: Path) -> None:
    runner = SubprocessRunner()
    lnk = tmp_path / "Nightshift Dashboard.lnk"
    assert winenv.create_shortcut(runner, lnk, "powershell.exe", "-NoProfile", tmp_path, "test") is True
    script = winenv.set_shortcut_aumid_script(lnk, "Nightshift")
    stamp = winenv.run_ps(runner, script, tag="test", timeout=120)
    assert stamp.ok, stamp.text
    assert winenv.set_shortcut_aumid(runner, lnk, "Nightshift") is True
    res = winenv.run_ps(runner, winenv.get_shortcut_aumid_script(lnk), tag="test", timeout=120)
    assert res.ok, res.text
    assert res.stdout.strip() == "Nightshift"


@needs_windows
def test_setting_the_id_on_a_missing_shortcut_fails_cleanly(tmp_path: Path) -> None:
    assert winenv.set_shortcut_aumid(SubprocessRunner(), tmp_path / "nope.lnk", "Nightshift") is False


# ---------------------------------------------------------------------------------------------- uninstall
UNINSTALL = ROOT / "installer" / "uninstall.ps1"


def test_uninstall_script_removes_the_registration_unless_system_is_skipped() -> None:
    text = UNINSTALL.read_text(encoding="ascii")
    key = BS.join(["HKCU:", "Software", "Classes", "AppUserModelId", "Nightshift"])
    assert key in text
    assert text.index("if (-not $SkipSystem) {") < text.index(key)


@needs_windows
def test_uninstall_dry_run_announces_the_registration_removal() -> None:
    import winreg

    key_path = BS.join(["Software", "Classes", "AppUserModelId", "Nightshift"])
    try:
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path).Close()
        pytest.skip("a real Nightshift toast registration exists on this machine")
    except FileNotFoundError:
        pass
    winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path).Close()
    try:
        env = {k: v for k, v in os.environ.items() if k != "NIGHTSHIFT_HOME"}
        env["NS_NONINTERACTIVE"] = "1"
        proc = subprocess.run(
            [str(PS), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(UNINSTALL), "-DryRun",
             "-HomeDir", str(ROOT / "does-not-exist")],
            capture_output=True, text=True, timeout=300, env=env,
        )  # fmt: skip
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "[dry-run] remove toast registration" in proc.stdout
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path).Close()      # a dry run leaves it
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
