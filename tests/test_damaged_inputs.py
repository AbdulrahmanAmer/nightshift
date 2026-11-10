"""The commands an owner runs when something is wrong never crash on the damage they exist to diagnose.

Table-driven: each command (`ns doctor`, `ns doctor --bundle`, `ns verify`, `ns tune`, `ns schedule show`,
`studio status`) against each damaged input it reads (not JSON, valid JSON the model refuses, a folder in
the file's place so it cannot be read, missing; for runtime.json also a byte order mark and a field of a newer
version; for the database also zero bytes). Each case runs the command as the owner types it (CliRunner, the
real `NIGHTSHIFT_HOME`) and asserts: no traceback, a normal exit code, and a row or a line that names the file
and the way out.

Only what is outside these commands is a stand-in: the graphics-card probe, FFmpeg, the Task Scheduler's
answer (`schedule._run`), and for `ns verify` the engine start and the checks after it. Nothing reaches the
real Task Scheduler, registry or Desktop (the bundle goes to a folder of the test).
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from ns_common.config import save_channel, save_install_state, save_runtime
from ns_common.db import Db
from ns_common.models import InstallState, Runtime
from ns_common.paths import Paths
from ns_supervisor.client import SupervisorClient
from tests.helpers import make_channel

RUNTIME_WAY_OUT = "Run Nightshift setup again (INSTALL.cmd)"   # NS-E125's hint


def make_home(root: Path) -> Paths:
    """A whole, valid install: runtime.json, install_state.json, answers, dashboard and schedule records, one
    channel profile and the database."""
    paths = Paths(root)
    paths.ensure()
    save_runtime(paths, Runtime.default("cpu-dev"))
    app = root.parent / "app"
    (app / "ns_common").mkdir(parents=True, exist_ok=True)
    save_install_state(paths, InstallState(app_path=str(app)))
    (paths.config / "answers.json").write_text(json.dumps({"install_code": True}), encoding="utf-8")
    (paths.config / "dashboard.json").write_text(json.dumps({"nightly_time": "03:00"}), encoding="utf-8")
    record = {"run_time": "03:00", "task_name": "Nightshift Daily", "mode": "registered"}
    (paths.config / "schedule.json").write_text(json.dumps(record), encoding="utf-8")
    save_channel(paths, make_channel())
    db = Db(paths.db_path)
    db.migrate()
    db.close()
    return paths


FILES: dict[str, Callable[[Paths], Path]] = {
    "runtime.json": lambda p: p.runtime_file,
    "install_state.json": lambda p: p.install_state_file,
    "schedule.json": lambda p: p.config / "schedule.json",
    "channel profile": lambda p: p.channel_file("after-dark"),
    "studio.db": lambda p: p.db_path,
}


def damage(path: Path, kind: str) -> None:
    yaml = path.suffix == ".yaml"
    if kind == "not-json":
        path.write_text("a: [unclosed" if yaml else "{not json", encoding="utf-8")
    elif kind == "bad-model":
        path.write_text("slug: 42\nformat: nope\n" if yaml else json.dumps({"profile": 42, "llama": "x"}),
                        encoding="utf-8")
    elif kind == "unreadable":      # a folder where the file should be: every open fails, as for a held file
        path.unlink()
        path.mkdir()
    elif kind == "missing":
        path.unlink()
    elif kind == "zero":
        path.write_bytes(b"")
    elif kind == "bom":             # what PowerShell 5.1's Set-Content -Encoding UTF8 writes
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    elif kind == "newer":
        data = json.loads(path.read_text(encoding="utf-8"))
        data["field_of_a_newer_version"] = 1
        path.write_text(json.dumps(data), encoding="utf-8")
    else:
        raise AssertionError(kind)


# (command, file, damage, exit code, what the output must hold: "{file}" is the damaged file's path)
CASES: list[tuple[str, str, str, int, tuple[str, ...]]] = [
    *[("ns doctor", "runtime.json", k, 1, ("[FAIL] runtime.json: NS-E125", "{file}", RUNTIME_WAY_OUT, why))
      for k, why in (("not-json", "not valid JSON"), ("bad-model", "Input should be"),
                     ("unreadable", "could not be read"), ("missing", "does not exist"),
                     ("bom", "byte order mark"), ("newer", "written by a newer Nightshift"))],
    *[("ns doctor", "install_state.json", k, 1, ("[FAIL] install_state:", "{file}", why))
      for k, why in (("not-json", "INSTALL.cmd"), ("bad-model", "INSTALL.cmd"),
                     ("unreadable", "close that program"), ("missing", "INSTALL.cmd"))],
    *[("ns doctor --bundle", "runtime.json", k, 0, ("Diagnostics written", "[FAIL] runtime.json: NS-E125",
                                                     "{file}", RUNTIME_WAY_OUT))
      for k in ("not-json", "bad-model", "unreadable", "missing")],
    *[("ns doctor --bundle", "install_state.json", k, 0, ("Diagnostics written", "[FAIL] install_state:",
                                                           "{file}"))
      for k in ("not-json", "bad-model", "unreadable", "missing")],
    *[("ns verify", "runtime.json", k, 1, ("NS-E125", "{file}", RUNTIME_WAY_OUT))
      for k in ("not-json", "bad-model", "unreadable", "missing")],
    *[("ns tune", "runtime.json", k, 1, ("NS-E125", "{file}", RUNTIME_WAY_OUT))
      for k in ("not-json", "bad-model", "unreadable", "missing")],
    *[("ns schedule show", "schedule.json", k, 0, ("{file}", way))
      for k, way in (("not-json", "ns schedule install --time"), ("bad-model", "ns schedule install --time"),
                     ("unreadable", "close that program"), ("missing", "ns schedule install --time"))],
    *[("studio status", "runtime.json", k, 1, ("NS-E125", "{file}", RUNTIME_WAY_OUT))
      for k in ("not-json", "bad-model", "unreadable")],
    ("studio status", "runtime.json", "missing", 0, ("channel after-dark",)),   # status needs no settings
    *[("studio status", "channel profile", k, 1, ("NS-E320", "after-dark.yaml", "studio channel edit"))
      for k in ("not-json", "bad-model", "unreadable")],
    ("studio status", "channel profile", "missing", 0, ("no channels configured",)),
    ("studio status", "studio.db", "unreadable", 1, ("NS-E327", "{file}", "close that program")),
    *[("studio status", "studio.db", k, 0, ("{file}", "did not exist or was empty", "ns restore"))
      for k in ("missing", "zero")],
]

ARGV = {
    "ns doctor": ["doctor"],
    "ns doctor --bundle": ["doctor", "--bundle", "--out-dir", "{out}"],
    "ns verify": ["verify"],
    "ns tune": ["tune"],
    "ns schedule show": ["schedule", "show"],
    "studio status": ["status"],
}


@pytest.fixture
def stand_ins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, list[Any]]:
    """Everything outside the commands under test; records what `ns verify` would have started."""
    from ns_supervisor import cli as ns_cli
    from ns_supervisor import doctor, procs, schedule, tune

    started: dict[str, list[Any]] = {"engine": [], "tune": [], "client_for": []}

    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    def client_for(rt: Runtime) -> SupervisorClient:
        # the command still reads runtime.json (control.runtime_or_default) and builds its client from it;
        # only the connection is a stand-in, so no test reaches a real engine on the default port
        started["client_for"].append(rt.supervisor_port)
        return SupervisorClient("http://sup", client=httpx.Client(transport=httpx.MockTransport(refuse)))

    monkeypatch.setattr(SupervisorClient, "from_runtime", staticmethod(client_for))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    (tmp_path / "temp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "temp"))
    monkeypatch.setattr(procs, "nvidia_smi_report", lambda smi=None: None)
    monkeypatch.setattr(doctor.ffmpeg, "available", lambda: True)
    task = "Nightshift Daily: Ready, next run 03:00"
    monkeypatch.setattr(schedule, "_run", lambda script, timeout_s=60: task)
    monkeypatch.setattr(ns_cli, "start_gate", lambda *a, **k: started["engine"].append("gate"))
    monkeypatch.setattr(ns_cli, "ensure_running", lambda *a, **k: started["engine"].append("start"))
    monkeypatch.setattr(tune, "ProcessRunner", lambda p: started["tune"].append("runner"))
    return started


@pytest.mark.parametrize(
    ("command", "file", "kind", "code", "says"), CASES, ids=[f"{c}|{f}|{k}" for c, f, k, _, _ in CASES]
)
def test_a_command_says_the_damaged_file_and_the_way_out_and_exits_normally(
    tmp_path: Path, stand_ins: dict[str, list[Any]], command: str, file: str, kind: str, code: int,
    says: tuple[str, ...],
) -> None:
    from ns_studio import cli as studio_cli
    from ns_supervisor import cli as ns_cli

    root = tmp_path / "home" / "ns"
    paths = make_home(root)
    target = FILES[file](paths)
    damage(target, kind)
    argv = [a.replace("{out}", str(tmp_path / "out")) for a in ARGV[command]]
    app = studio_cli.app if command.startswith("studio") else ns_cli.app
    res = CliRunner().invoke(app, argv, env={"NIGHTSHIFT_HOME": str(root)})
    crashed = None if res.exception is None or isinstance(res.exception, SystemExit) else res.exception
    assert crashed is None, f"{type(crashed).__name__}: {crashed}\n{res.output}"
    assert "Traceback" not in res.output
    assert res.exit_code == code, res.output
    for text in says:
        assert text.replace("{file}", str(target)) in res.output, (text, res.output)
    if command == "ns verify":
        assert stand_ins["engine"] == []        # nothing was started on settings that cannot be read
    if command == "ns tune":
        assert stand_ins["tune"] == []
    if command == "ns doctor":                  # every other row is still printed after a failed one
        assert "[ok] python environments" in res.output or "python environments:" in res.output, res.output
        assert "nvidia-smi:" in res.output and "supervisor:" in res.output
    if command == "ns doctor --bundle":
        assert list((tmp_path / "out").glob("nightshift-diagnostics-*.zip"))


def test_every_file_and_command_of_the_table_is_reached() -> None:
    """The table holds each command with each kind of damage of the files it reads (a row added later keeps
    the shape: the four kinds for a JSON file)."""
    seen = {(c, f) for c, f, _, _, _ in CASES}
    for pair in seen:
        kinds = {k for c, f, k, _, _ in CASES if (c, f) == pair}
        assert {"unreadable", "missing"} <= kinds, pair
    assert {c for c, _ in seen} == set(ARGV)


def test_the_commands_and_setup_read_runtime_json_with_one_classifier(tmp_path: Path) -> None:
    """`ns doctor`'s row and setup's decision come from the same `inspect_runtime`: for each kind of file the
    commands use the settings exactly when setup keeps the file as it is (`ok`)."""
    import importlib
    import importlib.util

    from installer.stage2 import runtime_cfg
    from ns_common.errors import NsError

    assert importlib.util.find_spec("ns_common.runtime_read") is not None, "no shared reading of runtime.json"
    runtime_read = importlib.import_module("ns_common.runtime_read")

    assert runtime_cfg.Found is runtime_read.Found
    for kind in ("not-json", "bad-model", "unreadable", "missing", "bom", "newer", None):
        paths = make_home(tmp_path / str(kind) / "ns")
        if kind is not None:
            damage(paths.runtime_file, kind)
        setup_says = runtime_cfg.inspect_runtime(paths)
        try:
            runtime_read.read_runtime(paths)
            used = True
        except NsError as e:
            used = False
            assert e.code == "NS-E125" and str(paths.runtime_file) in e.detail
        assert used == (setup_says.state == "ok"), (kind, setup_says)
