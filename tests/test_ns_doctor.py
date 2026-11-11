import json
import os
import zipfile
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from ns_common import __version__
from ns_common.config import save_runtime
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_supervisor import doctor
from ns_supervisor.client import SupervisorClient


def seed(tmp_path: Path) -> Paths:
    paths = Paths(tmp_path / "ns")
    paths.ensure()
    paths.runtime_file.write_text('{"profile": "gpu"}', encoding="utf-8")
    paths.install_state_file.write_text('{"version": "0.1.0"}', encoding="utf-8")
    (paths.logs / "supervisor-20261101.jsonl").write_text('{"event": "x"}\n', encoding="utf-8")
    (paths.logs / "verify-report.json").write_text('{"slice": "verify"}', encoding="utf-8")
    for i in range(4):
        job = paths.jobs_dir / f"chan-20261101-000{i}"
        (job / "audio").mkdir(parents=True)
        (job / "long").mkdir()
        (job / "idea.json").write_text("{}", encoding="utf-8")
        (job / "audio" / "s1.wav").write_bytes(b"RIFF")
        (job / "long" / "master.mp4").write_bytes(b"\x00")
        (job / "thumb.jpg").write_bytes(b"\xff")
        os.utime(job, (1_700_000_000 + i * 100, 1_700_000_000 + i * 100))
    return paths


def build(paths: Paths, tmp_path: Path, **kw: object) -> zipfile.ZipFile:
    out = doctor.build_bundle(
        paths,
        out_dir=tmp_path / "desk",
        now=datetime(2026, 11, 1, 12, 0),
        freeze=lambda p, venv: "pkg==1.0\n" if venv == "studio" else None,  # type: ignore[arg-type]
        smi=lambda: "NVIDIA-SMI 570 | RTX 5060 Ti",
        setup_dir=tmp_path / "local-nightshift",
        temp_dir=tmp_path / "temp",
        **kw,  # type: ignore[arg-type]
    )
    assert out.name == "nightshift-diagnostics-2026-11-01-1200.zip" and out.parent == tmp_path / "desk"
    return zipfile.ZipFile(out)


def test_bundle_contains_the_diagnostic_files(tmp_path: Path) -> None:
    z = build(seed(tmp_path), tmp_path)
    names = set(z.namelist())
    assert {
        "logs/supervisor-20261101.jsonl",
        "logs/verify-report.json",
        "verify-report.json",
        "install_state.json",
        "runtime.json",
        "nvidia-smi.txt",
        "pip-freeze/studio.txt",
        "VERSION",
        "bundle-info.json",
    } <= names
    assert z.read("nvidia-smi.txt").decode() == "NVIDIA-SMI 570 | RTX 5060 Ti"
    assert z.read("pip-freeze/studio.txt").decode() == "pkg==1.0\n"
    assert z.read("VERSION").decode().strip() == __version__  # the shipped VERSION file


def test_bundle_skips_absent_venvs_and_says_so(tmp_path: Path) -> None:
    z = build(seed(tmp_path), tmp_path)
    assert "pip-freeze/supervisor.txt" not in z.namelist() and "pip-freeze/tts.txt" not in z.namelist()
    notes = json.loads(z.read("bundle-info.json"))["notes"]
    assert any("supervisor.txt" in n for n in notes)


def test_bundle_has_last_three_jobs_without_media(tmp_path: Path) -> None:
    z = build(seed(tmp_path), tmp_path)
    names = z.namelist()
    jobs = {n.split("/")[1] for n in names if n.startswith("jobs/")}
    assert jobs == {"chan-20261101-0001", "chan-20261101-0002", "chan-20261101-0003"}
    assert all(not n.endswith((".wav", ".mp4", ".jpg")) for n in names)
    assert "jobs/chan-20261101-0003/idea.json" in names


def test_bundle_tolerates_missing_files_and_nvidia_smi(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "empty")
    out = doctor.build_bundle(
        paths,
        out_dir=tmp_path / "desk",
        now=datetime(2026, 11, 1),
        freeze=lambda p, v: None,
        smi=lambda: None,
        setup_dir=tmp_path / "local-nightshift",
        temp_dir=tmp_path / "temp",
    )
    z = zipfile.ZipFile(out)
    assert "not available" in z.read("nvidia-smi.txt").decode()
    notes = json.loads(z.read("bundle-info.json"))["notes"]
    assert any("runtime.json: not found" in n for n in notes)


def test_bundle_prefers_version_file_of_the_app(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "VERSION").write_text("0.9.9\n", encoding="utf-8")
    z = build(seed(tmp_path), tmp_path, app=app)
    assert z.read("VERSION").decode().strip() == "0.9.9"


def test_big_logs_are_truncated(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    paths = seed(tmp_path)
    monkeypatch.setattr(doctor, "MAX_LOG_BYTES", 100)
    (paths.logs / "big.jsonl").write_bytes(b"a" * 500 + b"TAIL")
    z = build(paths, tmp_path)
    data = z.read("logs/big.jsonl")
    assert len(data) == 100 and data.endswith(b"TAIL")


def test_diagnose_reports_missing_pieces(tmp_path: Path) -> None:
    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no")

    client = SupervisorClient("http://sup", client=httpx.Client(transport=httpx.MockTransport(refuse)))
    rows = doctor.diagnose(Paths(tmp_path / "ns"), client)
    by = {name: (ok, detail) for name, ok, detail in rows}
    # NS-E125 naming the missing file and its way out, not NS-E203 "health check timed out" (FN-5/D-44 small3)
    assert by["runtime.json"][0] is False and "NS-E125" in by["runtime.json"][1]
    assert "does not exist" in by["runtime.json"][1] and "NS-E203" not in by["runtime.json"][1]
    assert by["supervisor"][0] is False
    assert by["venv studio"][0] is False


def test_bundle_holds_the_setup_evidence_without_keys(tmp_path: Path) -> None:
    paths = seed(tmp_path)
    report = (paths.logs / "install-report.json")
    report.write_text(json.dumps({"outcome": "failed", "code": "NS-E105"}), encoding="utf-8")
    (paths.config / "wizard_flags.json").write_text('{"tts_fallback": true}', encoding="utf-8")
    (paths.config / "answers.json").write_text(
        json.dumps({"home_path": "D:/n", "pexels_api_key": "SECRET-1", "pixabay_api_key": "SECRET-2"}),
        encoding="utf-8",
    )
    local = tmp_path / "local-nightshift"
    (local / "bootstrap").mkdir(parents=True)
    (local / "bootstrap" / "preflight.json").write_text('{"gpu": {"name": "RTX"}}', encoding="utf-8")
    (local / "bootstrap" / "setup-ui.jsonl").write_text('{"text": "hello"}\n', encoding="utf-8")
    (local / "setup-server.err").write_text("Traceback: boom\n", encoding="utf-8")
    z = build(paths, tmp_path)
    names = set(z.namelist())
    assert {
        "install-report.json",
        "setup/wizard_flags.json",
        "setup/answers.json",
        "setup/preflight.json",
        "setup/setup-server.err",
        "setup/bootstrap/setup-ui.jsonl",
    } <= names
    assert json.loads(z.read("setup/answers.json")) == {"home_path": "D:/n"}
    assert b"SECRET" not in b"".join(z.read(n) for n in names)
    assert json.loads(z.read("install-report.json"))["code"] == "NS-E105"


def test_bundle_falls_back_to_the_stage_one_hand_over_in_temp(tmp_path: Path) -> None:
    temp = tmp_path / "temp"
    temp.mkdir()
    (temp / "nightshift-preflight.json").write_text('{"profile": "gpu"}', encoding="utf-8")
    z = build(seed(tmp_path), tmp_path)
    assert json.loads(z.read("setup/preflight.json")) == {"profile": "gpu"}
    notes = json.loads(z.read("bundle-info.json"))["notes"]
    assert any("install-report.json: not found" in n for n in notes)


def test_two_bundles_on_one_day_do_not_overwrite_each_other(tmp_path: Path) -> None:
    paths = seed(tmp_path)
    first = doctor.build_bundle(paths, out_dir=tmp_path / "desk", now=datetime(2026, 11, 1, 9, 5),
                                freeze=lambda p, v: None, smi=lambda: None)
    second = doctor.build_bundle(paths, out_dir=tmp_path / "desk", now=datetime(2026, 11, 1, 9, 6),
                                 freeze=lambda p, v: None, smi=lambda: None)
    assert first.name == "nightshift-diagnostics-2026-11-01-0905.zip" and first != second and first.is_file()


# ---------------------------------------------------------------------------------------------- D-44
# The row "nvidia-smi" asks for an NVIDIA driver only of an install that uses a graphics card. A dev install
# (docs/DEV_MODE.md) and an install that runs on the processor (profile cpu-dev: the engine test, a PC with no
# NVIDIA card) have none to ask; they say so, whether or not the PC happens to have a driver.
class Down:
    base_url = "http://127.0.0.1:1"

    def is_up(self) -> bool:
        return False


def smi_row(paths: Paths, monkeypatch: pytest.MonkeyPatch, report: str | None) -> tuple[bool, str]:
    from ns_supervisor import procs

    monkeypatch.setattr(procs, "nvidia_smi_report", lambda smi=None: report)
    rows = {name: (ok, detail) for name, ok, detail in doctor.diagnose(paths, Down())}  # type: ignore[arg-type]
    return rows["nvidia-smi"]


CPU_SAYS = "not needed (this install runs on the processor: profile cpu-dev)"


def test_a_cpu_install_does_not_need_nvidia_smi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = Paths(tmp_path / "ns")
    save_runtime(paths, Runtime.default("cpu-dev"))
    assert smi_row(paths, monkeypatch, None) == (True, CPU_SAYS)
    # a PC that has a driver anyway: the sentence is still true (this install does not use it)
    assert smi_row(paths, monkeypatch, "NVIDIA-SMI 570") == (True, CPU_SAYS)


def test_a_gpu_install_without_the_driver_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths(tmp_path / "ns")
    save_runtime(paths, Runtime.default("gpu"))
    assert smi_row(paths, monkeypatch, None) == (False, "no NVIDIA driver found")
    assert smi_row(paths, monkeypatch, "NVIDIA-SMI 570") == (True, "ok")


def test_an_unreadable_runtime_keeps_the_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No runtime.json (setup has not finished): the kind of install is unknown, so the probe decides, as
    before D-44. (A runtime.json that is not valid JSON makes `diagnose` raise today: see the report.)"""
    paths = Paths(tmp_path / "ns")
    assert smi_row(paths, monkeypatch, None) == (False, "no NVIDIA driver found")
    assert smi_row(paths, monkeypatch, "NVIDIA-SMI 570") == (True, "ok")


def test_a_dev_install_keeps_its_own_words_even_on_the_cpu_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths(tmp_path / "ns")
    rt = Runtime.default("cpu-dev")
    rt.dev_mode = True
    save_runtime(paths, rt)
    assert smi_row(paths, monkeypatch, None) == (True, "not needed (dev mode)")


def test_ns_doctor_exits_0_on_a_cpu_install_when_the_rest_is_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine-test install of QA: profile cpu-dev, no NVIDIA card, the engine up: `ns doctor` is clean."""
    from typer.testing import CliRunner

    import ns_supervisor.cli as ns_cli
    from ns_common.config import save_install_state
    from ns_common.models import InstallState
    from ns_supervisor import procs

    home = tmp_path / "ns"
    paths = Paths(home)
    rt = Runtime.default("cpu-dev")
    for name in ("llm.gguf", "embed.gguf"):
        (tmp_path / name).write_bytes(b"gguf")
    rt.llama.model_path, rt.embed.model_path = str(tmp_path / "llm.gguf"), str(tmp_path / "embed.gguf")
    save_runtime(paths, rt)
    app = tmp_path / "app"
    (app / "ns_common").mkdir(parents=True)
    save_install_state(paths, InstallState(app_path=str(app)))
    for venv in ("studio", "supervisor"):
        py = paths.venvs / venv / "Scripts" / "python.exe"
        py.parent.mkdir(parents=True)
        py.write_bytes(b"MZ")
    monkeypatch.setattr(procs, "nvidia_smi_report", lambda smi=None: None)
    monkeypatch.setattr(doctor.ffmpeg, "available", lambda: True)  # (the PC's FFmpeg is not what is tested)
    up = SupervisorClient("http://sup", client=httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))))
    monkeypatch.setattr(ns_cli, "_client", lambda p: up)
    res = CliRunner().invoke(ns_cli.app, ["doctor"], env={"NIGHTSHIFT_HOME": str(home)})
    assert f"[ok] nvidia-smi: {CPU_SAYS}" in res.output, res.output
    assert "[FAIL]" not in res.output and res.exit_code == 0, res.output
    # and the same install on the GPU profile, with no driver, fails on that row alone
    save_runtime(paths, Runtime.default("gpu").model_copy(update={"llama": rt.llama, "embed": rt.embed}))
    res = CliRunner().invoke(ns_cli.app, ["doctor"], env={"NIGHTSHIFT_HOME": str(home)})
    assert "[FAIL] nvidia-smi: no NVIDIA driver found" in res.output and res.exit_code == 1, res.output
    assert res.output.count("[FAIL]") == 1, res.output


# ------------------------------------------------------------------------- D-44 x H-10: the row's table
# The row "nvidia-smi" for every (profile x fake_gpu switch x NVIDIA driver on the PC) the merged code can
# reach. "test" is an engine-test install (fake_footage), "dev" a dev install; "set, ignored" is the switch
# typed into a normal install (NS-W121). The simulated card answers whenever the switch is in effect, the
# processor and dev installs say they need no driver, and every other install asks the real tool.
#
# | profile                 | switch            | driver | nvidia-smi row                    | fake_gpu row |
# |-------------------------|-------------------|--------|-----------------------------------|--------------|
# | gpu                     | off               | yes/no | ok "ok" / FAIL "no NVIDIA driver" | -            |
# | gpu                     | on (test install) | any    | ok "simulated (fake_gpu): <name>" | ok "on: ..." |
# | gpu                     | ignored (normal)  | yes/no | ok "ok" / FAIL "no NVIDIA driver" | FAIL NS-W121 |
# | cpu-dev                 | off               | any    | ok "not needed (... cpu-dev)"     | -            |
# | cpu-dev                 | on (test install) | any    | ok "simulated (fake_gpu): <name>" | ok "on: ..." |
# | cpu-dev                 | ignored (normal)  | any    | ok "not needed (... cpu-dev)"     | FAIL NS-W121 |
# | any, dev install        | off               | any    | ok "not needed (dev mode)"        | -            |
# | any, dev install        | on                | any    | ok "simulated (fake_gpu): <name>" | ok "on: ..." |
# | unknown: missing/no JSON| cannot be read    | yes/no | ok "ok" / FAIL "no NVIDIA driver" | -            |
# | unknown: JSON, invalid  | on (test install) | any    | ok "simulated (fake_gpu): <name>" | ok "on: ..." |
# The last row: the runtime.json row is the FAIL (NS-E125); the switch is read as every tolerant reader
# (is_dev, the commands row) reads it, so the nvidia-smi this install's commands ask is the simulated card.
SIM_SAYS = "simulated (fake_gpu): Simulated GPU (fake_gpu)"
NO_DRIVER = (False, "no NVIDIA driver found")
REAL = {True: (True, "ok"), False: NO_DRIVER}


def table_home(paths: Paths, profile: str, switch: str) -> None:
    """runtime.json for one cell: `profile` gpu, cpu-dev, dev (a dev install on cpu-dev), missing, notjson,
    invalid (JSON that fails validation); `switch` off, on (a test install), ignored (a normal install)."""
    if profile == "missing":
        return
    paths.ensure()
    if profile == "notjson":
        paths.runtime_file.write_text("{not json", encoding="utf-8")
        return
    rt = Runtime.default("gpu" if profile in ("gpu", "invalid") else "cpu-dev")
    data = rt.model_dump(mode="json")
    data["dev_mode"] = profile == "dev"
    if switch == "on":
        data["fake_footage"] = True     # an engine-test install
    if switch in ("on", "ignored"):
        data["fake_gpu"] = True
    if profile == "invalid":
        data["supervisor_port"] = "not a port"
    paths.runtime_file.write_text(json.dumps(data), encoding="utf-8")


CELLS = [
    *[("gpu", "off", d, REAL[d], None) for d in (True, False)],
    *[("gpu", "on", d, (True, SIM_SAYS), True) for d in (True, False)],
    *[("gpu", "ignored", d, REAL[d], False) for d in (True, False)],
    *[("cpu-dev", "off", d, (True, CPU_SAYS), None) for d in (True, False)],
    *[("cpu-dev", "on", d, (True, SIM_SAYS), True) for d in (True, False)],
    *[("cpu-dev", "ignored", d, (True, CPU_SAYS), False) for d in (True, False)],
    *[("dev", "off", d, (True, "not needed (dev mode)"), None) for d in (True, False)],
    *[("dev", "on", d, (True, SIM_SAYS), True) for d in (True, False)],
    *[(p, "off", d, REAL[d], None) for p in ("missing", "notjson") for d in (True, False)],
    *[("invalid", "on", d, (True, SIM_SAYS), True) for d in (True, False)],
]


@pytest.mark.parametrize(
    ("profile", "switch", "driver", "row", "fake_row"), CELLS,
    ids=[f"{p}|{s}|{'driver' if d else 'no-driver'}" for p, s, d, _, _ in CELLS],
)
def test_the_nvidia_smi_row_is_true_in_every_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str, switch: str, driver: bool,
    row: tuple[bool, str], fake_row: bool | None,
) -> None:
    from ns_supervisor import gpu, procs

    paths = Paths(tmp_path / "ns")
    table_home(paths, profile, switch)
    asked: list[object] = []

    def real_tool(smi: object = None) -> str | None:   # the PC's own nvidia-smi, with or without a driver
        asked.append(smi)
        return "NVIDIA-SMI 570" if driver else None

    monkeypatch.setattr(procs, "nvidia_smi_report", real_tool)
    got = doctor.diagnose(paths, Down())  # type: ignore[arg-type]
    rows = {name: (ok, detail) for name, ok, detail in got}
    assert rows["nvidia-smi"] == row, got
    assert all(s is gpu.REAL_SMI for s in asked), asked     # only the real tool is ever asked for the driver
    if row[1] == SIM_SAYS or row[1].startswith("not needed"):
        assert asked == []                                   # these rows never ask the PC's driver
    if fake_row is None:
        assert "fake_gpu" not in rows
    else:
        assert rows["fake_gpu"][0] is fake_row, rows["fake_gpu"]
        want = "on: the simulated" if fake_row else "NS-W121: "
        assert rows["fake_gpu"][1].startswith(want), rows["fake_gpu"]
    if profile in ("notjson", "invalid"):
        assert rows["runtime.json"][0] is False and "NS-E125" in rows["runtime.json"][1], rows["runtime.json"]


# ---------------------------------------------------------------------------------------------- db-integrity
def test_doctor_shows_the_database_check(tmp_path: Path) -> None:
    """Row "database": what the last check at a start found, with its time, and "restored from the backup of
    <date> on <date>" while that restore is younger than 7 days. `ns doctor` never checks nor restores."""
    import sqlite3
    from datetime import UTC, timedelta

    from ns_common import db as dbmod
    from ns_common.db import Db

    paths = seed(tmp_path)

    def row() -> tuple[bool, str] | None:
        rows = {name: (ok, detail) for name, ok, detail in doctor.diagnose(paths, Down())}  # type: ignore[arg-type]
        return rows.get("database")

    assert row() == (True, "not checked yet (it is checked when Nightshift starts)")
    paths.backup_dir.mkdir(parents=True, exist_ok=True)
    for target in (paths.db_path, paths.backup_dir / "studio-20261101.db"):
        db = Db(target)
        db.migrate()
        db.close()
    copy_made = datetime(2026, 11, 1, 4, 0).timestamp()
    os.utime(paths.backup_dir / "studio-20261101.db", (copy_made, copy_made))
    with paths.db_path.open("r+b") as fh:  # 4 KiB of 0xFF over page 3: SQLite's quick check fails
        fh.seek(2 * 4096)
        fh.write(b"\xff" * 4096)
    opener = getattr(dbmod, "open_studio_db", None)
    assert opener is not None, "the one opener of studio.db"
    opener(paths, None, note=lambda message: None).close()

    ok, said = row() or (False, "")
    today = datetime.now().strftime("%Y-%m-%d")
    assert ok and said.startswith(f"ok (checked {today} ") and said.endswith(
        f"; restored from the backup of 2026-11-01 on {today}"
    ), said
    restored = paths.home / "run" / "db-restored.json"
    info = json.loads(restored.read_text(encoding="utf-8"))
    info["restored_at"] = (datetime.now(UTC) - timedelta(days=8)).isoformat(timespec="seconds")
    restored.write_text(json.dumps(info), encoding="utf-8")
    ok, said = row() or (False, "")
    assert ok and "restored" not in said, "a restore older than 7 days is no longer shown"
    conn = sqlite3.connect(paths.db_path)
    assert conn.execute("PRAGMA quick_check").fetchall() == [("ok",)]
    conn.close()
