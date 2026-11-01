import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from ns_common.errors import NsError
from ns_supervisor import procs
from ns_supervisor.services import ServiceDef
from ns_supervisor.testing import free_port, stub_def, wait_until

win_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def _healthy(url: str) -> bool:
    try:
        return httpx.get(url, timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def _pid_listed(pid: int) -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False
    ).stdout
    return f" {pid} " in out or out.strip().endswith(str(pid))


def test_start_logs_output_as_jsonl_and_terminate(tmp_path: Path) -> None:
    d = stub_def("llama", free_port())
    proc = procs.start(d, tmp_path / "logs")
    try:
        assert wait_until(lambda: _healthy(d.health_url))
        assert proc.alive() and proc.pid > 0
    finally:
        procs.terminate(proc, grace_s=5)
    assert not proc.alive()
    files = list((tmp_path / "logs").glob("llama-*.jsonl"))
    assert len(files) == 1 and files[0] == proc.log_path
    recs = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    events = {(r["event"], r["component"]) for r in recs}
    assert ("stdout", "llama") in events and ("stderr", "llama") in events
    assert any("listening" in r["msg"] for r in recs)


def test_terminate_is_idempotent(tmp_path: Path) -> None:
    d = stub_def("embed", free_port(), gpu=False)
    proc = procs.start(d, tmp_path / "logs")
    assert wait_until(lambda: _healthy(d.health_url))
    procs.terminate(proc, grace_s=5)
    procs.terminate(proc, grace_s=5)
    assert proc.returncode() is not None


def test_start_missing_executable_is_e202(tmp_path: Path) -> None:
    d = ServiceDef(
        name="llama", cmd=[str(tmp_path / "nope.exe")], host="127.0.0.1", port=free_port(), gpu=True
    )
    with pytest.raises(NsError) as ei:
        procs.start(d, tmp_path / "logs")
    assert ei.value.code == "NS-E202"


@win_only
def test_terminate_kills_grandchildren(tmp_path: Path) -> None:
    child_file = tmp_path / "child.pid"
    d = stub_def("tts", free_port(), extra=["--spawn-child", str(child_file)])
    proc = procs.start(d, tmp_path / "logs")
    assert wait_until(lambda: _healthy(d.health_url))
    assert wait_until(child_file.exists)
    child_pid = int(child_file.read_text(encoding="utf-8"))
    assert _pid_listed(child_pid)
    procs.terminate(proc, grace_s=5)
    assert wait_until(lambda: not _pid_listed(child_pid), timeout_s=15)


@win_only
def test_job_object_kills_children_when_closed() -> None:
    job = procs.JobObject()
    assert job.available
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        assert job.assign(child.pid) is True
        assert child.poll() is None
        job.close()
        assert not job.available
        assert child.wait(timeout=20) is not None
    finally:
        if child.poll() is None:
            child.kill()
    job.close()  # idempotent


def test_job_object_is_safe_to_use_anywhere() -> None:
    job = procs.JobObject()
    if not job.available:
        assert job.assign(1) is False
    job.close()
    assert job.assign(1) is False


def test_start_assigns_to_job(tmp_path: Path) -> None:
    job = procs.JobObject()
    d = stub_def("embed", free_port(), gpu=False)
    proc = procs.start(d, tmp_path / "logs", job=job)
    try:
        assert wait_until(lambda: _healthy(d.health_url))
        if job.available:
            job.close()
            assert wait_until(lambda: not proc.alive(), timeout_s=20)
    finally:
        procs.terminate(proc, grace_s=5)


def test_vram_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    out = "1234, 8000 MiB\n55, 100 MiB\n1234, 500 MiB\nnoise\n"
    monkeypatch.setattr(procs, "_nvsmi", lambda args: out)
    assert procs.vram_mb(1234) == 8500
    assert procs.vram_mb(55) == 100
    assert procs.vram_mb(999) == 0


def test_vram_unknown_when_driver_reports_na(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(procs, "_nvsmi", lambda args: "77, [N/A]\n")
    assert procs.vram_mb(77) is None


def test_vram_none_without_nvidia_smi(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(procs.subprocess, "run", boom)
    assert procs.vram_mb(1) is None
    assert procs.gpu_memory_used_mb() is None
    assert procs.nvidia_smi_report() is None


def test_gpu_memory_used_sums_gpus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(procs, "_nvsmi", lambda args: "250\n50\n")
    assert procs.gpu_memory_used_mb() == 300
    monkeypatch.setattr(procs, "_nvsmi", lambda args: "")
    assert procs.gpu_memory_used_mb() is None
