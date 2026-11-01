import json
import os
import zipfile
from datetime import datetime
from pathlib import Path

import httpx

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
        **kw,  # type: ignore[arg-type]
    )
    assert out.name == "nightshift-diagnostics-2026-11-01.zip" and out.parent == tmp_path / "desk"
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
    assert z.read("VERSION").decode().strip() == "0.1.0"


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
    assert by["runtime.json"][0] is False and "NS-E203" in by["runtime.json"][1]
    assert by["supervisor"][0] is False
    assert by["venv studio"][0] is False
