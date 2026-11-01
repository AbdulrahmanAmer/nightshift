import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from ns_common import ffmpeg
from ns_common.acquire import Endpoint, Service
from ns_common.errors import NsError
from ns_common.models import GateReport, Runtime
from ns_common.paths import Paths, contracts_dir
from ns_supervisor import verify
from ns_supervisor.testing import StubTable, SyncSupervisor


def good_deps(**over: Any) -> verify.Deps:
    d = verify.Deps(
        whisper=lambda paths, rt: 1.5,
        nvenc=lambda: None,
        job=lambda paths: 3.0,
        vram_used=lambda: 120,
        gpu_name=lambda: "Test GPU",
        ram=lambda: 32.0,
        sleep=lambda s: None,
    )
    for k, v in over.items():
        setattr(d, k, v)
    return d


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[tuple[Paths, Runtime, SyncSupervisor]]:
    paths = Paths(tmp_path / "ns")
    rt = Runtime.default("gpu")
    sync = SyncSupervisor(paths, rt, resolver=StubTable())
    yield paths, rt, sync
    sync.close()


def test_full_report_against_stub_services(engine: tuple[Paths, Runtime, SyncSupervisor]) -> None:
    paths, rt, sync = engine
    report = verify.run_verify(paths, rt, sync, deps=good_deps())
    failed = [c for c in report.checks if not c.passed]
    assert report.passed, failed
    names = {c.name for c in report.checks}
    assert names >= {
        "supervisor.status",
        "supervisor.cold_start",
        "llama.start",
        "llama.health",
        "llama.json_completion",
        "llama.tok_s",
        "embed.start",
        "embed.vector",
        "tts.start",
        "tts.preset",
        "tts.clone",
        "whisper.load",
        "nvenc.probe",
        "job.fake_services",
        "vram.idle",
        "bench.model_load_s",
        "bench.code_tok_s",
        "bench.tts_rtf",
        "bench.idle_vram_mb",
    }
    assert set(report.benchmarks) == {"model_load_s", "code_tok_s", "tts_rtf", "idle_vram_mb"}
    assert report.benchmarks["code_tok_s"] == 42.0
    assert report.machine["gpu"] == "Test GPU" and report.machine["targets"] == "32gb"
    assert report.slice == "verify"
    # nothing stays loaded or held after verify
    st = sync.status()
    assert st["gpu_owner"] is None
    assert all(st["services"][n]["state"] == "stopped" for n in ("llama", "embed", "tts"))


def test_report_file_is_gate_report_shaped_and_schema_valid(
    engine: tuple[Paths, Runtime, SyncSupervisor],
) -> None:
    paths, rt, sync = engine
    report = verify.run_verify(paths, rt, sync, quick=True, deps=good_deps())
    f = verify.write_report(paths, report)
    assert f == paths.logs / "verify-report.json"
    data = json.loads(f.read_text(encoding="utf-8"))
    GateReport.model_validate(data)
    schema = json.loads((contracts_dir() / "schemas" / "gate_report.schema.json").read_text("utf-8"))
    jsonschema.validate(data, schema)
    assert set(data) == {"slice", "date", "machine", "checks", "benchmarks"}


def test_quick_skips_benchmarks_and_the_job(engine: tuple[Paths, Runtime, SyncSupervisor]) -> None:
    paths, rt, sync = engine
    report = verify.run_verify(paths, rt, sync, quick=True, deps=good_deps())
    assert report.passed, [c for c in report.checks if not c.passed]
    names = {c.name for c in report.checks}
    assert "llama.tok_s" not in names and "job.fake_services" not in names
    assert "supervisor.cold_start" not in names and "tts.clone" not in names
    assert report.slice == "verify-quick"
    assert set(report.benchmarks) == {"idle_vram_mb"}


def test_slow_llm_fails_the_benchmark_row(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "ns")
    rt = Runtime.default("gpu")
    sync = SyncSupervisor(paths, rt, resolver=StubTable(extra={"llama": ["--tok-s", "10"]}))
    try:
        report = verify.run_verify(paths, rt, sync, deps=good_deps())
    finally:
        sync.close()
    row = next(c for c in report.checks if c.name == "bench.code_tok_s")
    assert row.passed is False and row.value == 10.0 and row.limit == ">= 40 tok/s"
    assert report.passed is False


class DeadSupervisor:
    """A supervisor that is not running: every call fails like SupervisorClient would."""

    def acquire(
        self, service: Service, owner: str, *, model_alias: str | None = None, timeout_s: int = 7200
    ) -> Endpoint:
        raise NsError("NS-E202", "supervisor unreachable")

    def release(self, service: Service, owner: str) -> None:
        raise NsError("NS-E202", "supervisor unreachable")

    def status(self) -> dict[str, Any]:
        raise NsError("NS-E202", "supervisor unreachable")

    def stop_all(self) -> None:
        raise NsError("NS-E202", "supervisor unreachable")


def test_missing_components_are_failed_checks_with_code_and_hint(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "ns")

    def boom_whisper(p: Paths, r: Runtime) -> float:
        raise NsError("NS-E202", "studio venv missing")

    def boom_nvenc() -> None:
        raise NsError("NS-W401", "no encoder")

    deps = good_deps(whisper=boom_whisper, nvenc=boom_nvenc, vram_used=lambda: None)
    report = verify.run_verify(paths, Runtime.default("gpu"), DeadSupervisor(), deps=deps)
    assert report.passed is False
    by_name = {c.name: c for c in report.checks}
    for name in ("supervisor.status", "llama.start", "embed.start", "tts.start", "whisper.load"):
        assert by_name[name].passed is False
        assert by_name[name].log is not None and "NS-E2" in by_name[name].log and "Hint:" in by_name[name].log
    assert "NS-W401" in (by_name["nvenc.probe"].log or "")
    assert "NS-E102" in (by_name["vram.idle"].log or "")
    # dependent checks report instead of crashing
    assert by_name["llama.health"].passed is False
    assert by_name["llama.json_completion"].passed is False
    GateReport.model_validate(report.model_dump(mode="json"))


def test_unexpected_probe_exception_is_reported_not_raised(
    engine: tuple[Paths, Runtime, SyncSupervisor],
) -> None:
    paths, rt, sync = engine

    def explode(p: Paths, r: Runtime) -> float:
        raise RuntimeError("kaboom")

    report = verify.run_verify(paths, rt, sync, quick=True, deps=good_deps(whisper=explode))
    w = next(c for c in report.checks if c.name == "whisper.load")
    assert w.passed is False and "kaboom" in (w.log or "")


def test_targets_follow_ram() -> None:
    assert verify.targets_for(32).tok_s == 40 and verify.targets_for(32).rtf == 0.5
    t16 = verify.targets_for(16)
    assert t16.tok_s == 30 and t16.rtf == 0.8 and t16.short_job_s == 300
    assert verify.targets_for(None).profile == "16gb"


def test_format_lines_marks_failures() -> None:
    from ns_common.models import GateCheck

    rep = GateReport(
        slice="verify",
        date="2026-11-01T00:00:00",
        machine={},
        checks=[
            GateCheck(name="a", passed=True, value=1.0, limit="<= 2"),
            GateCheck(name="b", passed=False, log="NS-E203: x | Hint: y"),
        ],
    )
    lines = verify.format_lines(rep)
    assert lines[0].startswith("[PASS] a = 1.0") and lines[1].startswith("[FAIL] b")
    assert "Hint: y" in lines[2] and lines[-1].endswith("SOME CHECKS FAILED")


def test_whisper_probe_needs_the_studio_venv(tmp_path: Path) -> None:
    with pytest.raises(NsError) as ei:
        verify.whisper_probe(Paths(tmp_path / "ns"), Runtime.default("gpu"))
    assert ei.value.code == "NS-E202" and "studio venv" in ei.value.detail


def test_nvenc_probe_error_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ffmpeg, "available", lambda: False)
    with pytest.raises(NsError) as e1:
        verify.nvenc_probe()
    assert e1.value.code == "NS-E401"

    def failing(args: list[str], **kw: Any) -> None:
        raise NsError("NS-E401", "Unknown encoder 'h264_nvenc'")

    monkeypatch.setattr(ffmpeg, "available", lambda: True)
    monkeypatch.setattr(ffmpeg, "run", failing)
    with pytest.raises(NsError) as e2:
        verify.nvenc_probe()
    assert e2.value.code == "NS-W401" and "h264_nvenc" in e2.value.detail


def test_fake_job_is_skipped_without_studio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify.shutil, "which", lambda name: None)
    assert verify.fake_job(Paths(tmp_path / "ns")) is None


@pytest.mark.parametrize(
    "stderr,code",
    [
        ("RuntimeError: Library cublas64_12.dll is not found", "NS-E112"),
        ("Could not load cudnn_ops64_9.dll", "NS-E112"),
        ("ValueError: unknown model size", "NS-E202"),
    ],
)
def test_whisper_probe_classifies_cuda_dll_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: str, code: str
) -> None:
    import subprocess

    paths = Paths(tmp_path / "ns")
    py = paths.venvs / "studio" / "Scripts" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        verify.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr=stderr),
    )
    with pytest.raises(NsError) as ei:
        verify.whisper_probe(paths, Runtime.default("gpu"))
    assert ei.value.code == code
