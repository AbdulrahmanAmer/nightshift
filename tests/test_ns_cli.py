import zipfile
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from ns_common import __version__
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
    assert r.exit_code == 0 and f"ns {__version__}" in r.output


def test_help_lists_every_command(tmp_path: Path) -> None:
    r = invoke(tmp_path, "--help")
    assert r.exit_code == 0
    names = ("stop", "doctor", "verify", "tune", "update", "backup", "restore", "schedule", "notify")
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
    assert r3.exit_code == 1 and "NS-E703" in r3.output


def test_restore_refuses_while_the_engine_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On the dev install, `ns restore` with the engine up failed on the locked studio.db-wal (WinError 32)
    after restoring 12 files, then put them back. It now refuses up front and changes nothing."""
    from ns_supervisor import backup

    home = tmp_path / "home"
    paths = Paths(home)
    paths.ensure()
    paths.channel_file("a").write_text("slug: a\n", encoding="utf-8")
    out = backup.make_backup(paths, tmp_path / "b.zip")
    paths.channel_file("a").write_text("slug: a\nniche: changed\n", encoding="utf-8")

    class Up:
        def is_up(self) -> bool:
            return True

    monkeypatch.setattr(cli, "_client", lambda paths: Up())
    r = invoke(home, "restore", str(out))
    assert r.exit_code == 1 and "NS-E703" in r.output and "Stop All" in r.output
    assert "changed" in paths.channel_file("a").read_text(encoding="utf-8")   # nothing was restored


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


def test_schedule_failure_uses_e204(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def refuse(script: str, **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, stdout="", stderr="Access is denied")

    monkeypatch.setattr(pwsh, "run_powershell", refuse)
    r = invoke(tmp_path / "home", "schedule", "install", "--time", "04:00")
    assert r.exit_code == 1 and "NS-E204" in r.output and "Access is denied" in r.output


class FakeEngineClient:
    """A supervisor client whose `/status` answers only after the engine was spawned."""

    def __init__(self, *, up: bool) -> None:
        self.up = up
        self.shutdowns: list[float] = []

    def is_up(self) -> bool:
        return self.up

    def shutdown(self, grace_s: float = 30) -> None:
        self.shutdowns.append(grace_s)
        self.up = False


def _passing_report(*a: object, **k: object) -> GateReport:
    return GateReport(
        slice="verify", date="2026-11-01T00:00:00", machine={}, checks=[GateCheck(name="x", passed=True)]
    )


def test_verify_stops_the_headless_engine_it_started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A headless engine left running makes the Dashboard shortcut skip starting the dashboard service.
    fake = FakeEngineClient(up=False)
    monkeypatch.setattr(cli, "_client", lambda paths: fake)
    monkeypatch.setattr(cli, "ensure_running", lambda *a, **k: setattr(fake, "up", True))
    monkeypatch.setattr(verify, "run_verify", _passing_report)
    r = invoke(tmp_path / "home", "verify", "--quick")
    assert r.exit_code == 0 and fake.shutdowns == [30] and fake.up is False


def test_verify_stops_the_engine_it_started_even_when_the_run_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeEngineClient(up=False)

    def boom(*a: object, **k: object) -> GateReport:
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(cli, "_client", lambda paths: fake)
    monkeypatch.setattr(cli, "ensure_running", lambda *a, **k: setattr(fake, "up", True))
    monkeypatch.setattr(verify, "run_verify", boom)
    r = invoke(tmp_path / "home", "verify")
    assert r.exit_code != 0 and fake.shutdowns == [30]


def test_verify_leaves_an_engine_that_was_already_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeEngineClient(up=True)
    monkeypatch.setattr(cli, "_client", lambda paths: fake)
    monkeypatch.setattr(cli, "ensure_running", lambda *a, **k: None)
    monkeypatch.setattr(verify, "run_verify", _passing_report)
    r = invoke(tmp_path / "home", "verify", "--quick")
    assert r.exit_code == 0 and fake.shutdowns == [] and fake.up is True


def test_the_dead_gate_command_is_gone(tmp_path: Path) -> None:
    """`ns gate m1..m4` looked for dev/gate_m1.py ... that never existed; the command is removed."""
    assert "gate" not in invoke(tmp_path, "--help").output.split()
    assert invoke(tmp_path / "home", "gate", "m0").exit_code == 2       # "No such command"
