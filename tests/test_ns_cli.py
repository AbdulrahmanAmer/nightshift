import zipfile
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from ns_common.config import save_runtime
from ns_common.models import GateCheck, GateReport, Runtime
from ns_common.paths import Paths
from ns_supervisor import cli, pwsh, verify
from ns_supervisor.client import SupervisorClient

runner = CliRunner()


def invoke(home: Path, *args: str):  # type: ignore[no-untyped-def]
    return runner.invoke(cli.app, list(args), env={"NIGHTSHIFT_HOME": str(home)})


def down_client(paths: Paths) -> SupervisorClient:
    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    return SupervisorClient("http://sup", client=httpx.Client(transport=httpx.MockTransport(refuse)))


def test_bare_ns_prints_version(tmp_path: Path) -> None:
    r = invoke(tmp_path)
    assert r.exit_code == 0 and "ns 0.1.0" in r.output


def test_help_lists_every_command(tmp_path: Path) -> None:
    r = invoke(tmp_path, "--help")
    assert r.exit_code == 0
    names = ("stop", "doctor", "verify", "tune", "gate", "update", "backup", "restore", "schedule", "notify")
    for cmd in names:
        assert cmd in r.output, cmd
    r2 = invoke(tmp_path, "schedule", "--help")
    for sub in ("install", "remove", "show"):
        assert sub in r2.output


def test_stop_without_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_client", down_client)
    r = invoke(tmp_path / "home", "stop")
    assert r.exit_code == 0 and "not running" in r.output


def test_tune_dry_run_prints_matrix_and_writes_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    rt = Runtime.default("gpu")
    rt.llama.model_path = "C:/m/model.gguf"
    save_runtime(Paths(home), rt)
    before = Paths(home).runtime_file.read_text(encoding="utf-8")
    r = invoke(home, "tune", "--dry-run")
    assert r.exit_code == 0 and "-ngl" in r.output and "MTP" in r.output
    assert Paths(home).runtime_file.read_text(encoding="utf-8") == before


def test_tune_without_runtime_json_fails_with_code(tmp_path: Path) -> None:
    r = invoke(tmp_path / "home", "tune")
    assert r.exit_code == 1 and "NS-E203" in r.output


def test_gate_without_script_exits_2(tmp_path: Path) -> None:
    r = invoke(tmp_path / "home", "gate", "m42")
    assert r.exit_code == 2 and "gate_m42.py" in r.output


def test_update_needs_zip_or_rollback(tmp_path: Path) -> None:
    r = invoke(tmp_path / "home", "update")
    assert r.exit_code == 2 and "zip" in r.output


def test_update_with_missing_zip_is_e701(tmp_path: Path) -> None:
    r = invoke(tmp_path / "home", "update", str(tmp_path / "nope.zip"))
    assert r.exit_code == 1 and "NS-E701" in r.output


def test_backup_then_restore_roundtrip(tmp_path: Path) -> None:
    home = tmp_path / "home"
    paths = Paths(home)
    paths.ensure()
    paths.channel_file("a").write_text("slug: a\n", encoding="utf-8")
    out = tmp_path / "b.zip"
    r = invoke(home, "backup", "--out", str(out))
    assert r.exit_code == 0 and out.exists()
    paths.channel_file("a").unlink()
    r2 = invoke(home, "restore", str(out))
    assert r2.exit_code == 0 and paths.channel_file("a").exists()
    r3 = invoke(home, "restore", str(tmp_path / "missing.zip"))
    assert r3.exit_code == 1 and "NS-E701" in r3.output


def test_doctor_bundle_writes_zip(tmp_path: Path) -> None:
    home = tmp_path / "home"
    Paths(home).ensure()
    r = invoke(home, "doctor", "--bundle", "--out-dir", str(tmp_path / "desk"))
    assert r.exit_code == 0
    zips = list((tmp_path / "desk").glob("nightshift-diagnostics-*.zip"))
    assert len(zips) == 1 and "bundle-info.json" in zipfile.ZipFile(zips[0]).namelist()


def test_notify_is_silent_without_powershell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pwsh, "find_powershell", lambda: None)
    r = invoke(tmp_path, "notify", "Nightshift", "done")
    assert r.exit_code == 0


@pytest.mark.parametrize("passed,code", [(True, 0), (False, 1)])
def test_verify_exit_code_follows_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, passed: bool, code: int
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(cli, "_client", down_client)
    monkeypatch.setattr(cli, "ensure_running", lambda *a, **k: None)
    seen: dict[str, object] = {}

    def fake_run(paths, rt, ctl, *, quick=False, deps=None):  # type: ignore[no-untyped-def]
        seen["quick"] = quick
        return GateReport(
            slice="verify",
            date="2026-11-01T00:00:00",
            machine={},
            checks=[GateCheck(name="x", passed=passed)],
        )

    monkeypatch.setattr(verify, "run_verify", fake_run)
    r = invoke(home, "verify", "--quick")
    assert r.exit_code == code and seen["quick"] is True
    assert (Paths(home).logs / "verify-report.json").exists()
    assert "Report:" in r.output
