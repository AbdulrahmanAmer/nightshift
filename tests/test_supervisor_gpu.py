from pathlib import Path

import pytest

from ns_common.models import GpuStatus, Runtime
from ns_common.paths import Paths
from ns_supervisor.core import Supervisor
from ns_supervisor.gpu import (
    NullGpuProbe,
    NvidiaSmiGpuProbe,
    parse_nvidia_smi,
    run_nvidia_smi,
)
from ns_supervisor.testing import StubTable, fast_config

LINE = "NVIDIA GeForce RTX 4080, 37, 9300, 16376, 61\n"


def test_parse_full_line() -> None:
    g = parse_nvidia_smi(LINE)
    assert g == GpuStatus(
        name="NVIDIA GeForce RTX 4080", load_pct=37.0, vram_used_mb=9300, vram_total_mb=16376, temp_c=61.0
    )


def test_parse_rtx_5060_ti_line() -> None:
    g = parse_nvidia_smi("NVIDIA GeForce RTX 5060 Ti, 2, 812, 16311, 38\n")
    assert g == GpuStatus(
        name="NVIDIA GeForce RTX 5060 Ti", load_pct=2.0, vram_used_mb=812, vram_total_mb=16311, temp_c=38.0
    )


def test_parse_not_supported_cells_become_none() -> None:
    g = parse_nvidia_smi("Quadro P1000, [N/A], 120, 4096, [Not Supported]\n")
    assert g is not None
    assert g.name == "Quadro P1000" and g.load_pct is None and g.temp_c is None
    assert g.vram_used_mb == 120 and g.vram_total_mb == 4096


def test_parse_takes_the_first_gpu_and_survives_a_comma_in_the_name() -> None:
    g = parse_nvidia_smi("\nGPU, Fancy Edition, 5, 100, 200, 40\nOther, 1, 2, 3, 4\n")
    assert g is not None and g.name == "GPU, Fancy Edition" and g.vram_total_mb == 200


@pytest.mark.parametrize("text", ["", "\n", "garbage line without enough commas", ", 1, 2, 3, 4"])
def test_parse_rejects_junk(text: str) -> None:
    assert parse_nvidia_smi(text) is None


def test_probe_caches_for_the_interval() -> None:
    t = [100.0]
    calls: list[int] = []

    def runner() -> str | None:
        calls.append(1)
        return LINE

    p = NvidiaSmiGpuProbe(min_interval_s=2.0, runner=runner, clock=lambda: t[0])
    first = p.sample()
    t[0] = 101.9
    assert p.sample() is first and len(calls) == 1
    t[0] = 102.0
    assert p.sample() == first and len(calls) == 2


def test_probe_returns_none_and_caches_when_nvidia_smi_is_absent() -> None:
    t = [0.0]
    calls: list[int] = []

    def runner() -> str | None:
        calls.append(1)
        return None

    p = NvidiaSmiGpuProbe(runner=runner, clock=lambda: t[0])
    assert p.sample() is None and p.sample() is None
    assert len(calls) == 1
    t[0] = 5.0
    assert p.sample() is None and len(calls) == 2


def test_run_nvidia_smi_is_none_without_the_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ns_supervisor.gpu.shutil.which", lambda _name: None)
    assert run_nvidia_smi() is None


def test_the_null_probe_reports_nothing() -> None:
    assert NullGpuProbe().sample() is None


def test_a_probe_of_the_simulated_card_says_so_and_a_real_one_never_does() -> None:
    """H-10: the reading carries `simulated: true` only from a probe that asks the simulated card."""
    sim = NvidiaSmiGpuProbe(runner=lambda: LINE, simulated=True).sample()
    real = NvidiaSmiGpuProbe(runner=lambda: LINE).sample()
    assert sim is not None and sim.simulated is True and sim.model_dump(mode="json")["simulated"] is True
    assert real is not None and "simulated" not in real.model_dump(mode="json")


def _sup(tmp_path: Path, runtime: Runtime, **kw: object) -> Supervisor:
    return Supervisor(
        Paths(tmp_path / "ns"),
        runtime,
        config=fast_config(),
        resolver=StubTable(),
        **kw,  # type: ignore[arg-type]
    )


def test_supervisor_status_carries_the_gpu_block(tmp_path: Path) -> None:
    probe = NvidiaSmiGpuProbe(runner=lambda: "RTX, 12.5, 1000, 16000, 50\n")  # nvidia-smi's own line shape
    sup = _sup(tmp_path, Runtime.default("gpu"), gpu_probe=probe)
    assert sup.status()["gpu"] == {
        "name": "RTX",
        "load_pct": 12.5,
        "vram_used_mb": 1000,
        "vram_total_mb": 16000,
        "temp_c": 50.0,
    }


def test_supervisor_status_gpu_is_null_without_a_reading(tmp_path: Path) -> None:
    absent = NvidiaSmiGpuProbe(runner=lambda: None)  # nvidia-smi absent, failing or hanging
    assert _sup(tmp_path, Runtime.default("gpu"), gpu_probe=absent).status()["gpu"] is None


def test_cpu_dev_profile_uses_no_probe(tmp_path: Path) -> None:
    sup = _sup(tmp_path, Runtime.default("cpu-dev"))
    assert isinstance(sup.gpu_probe, NullGpuProbe) and sup.status()["gpu"] is None


def test_gpu_profile_defaults_to_the_nvidia_smi_probe(tmp_path: Path) -> None:
    assert isinstance(_sup(tmp_path, Runtime.default("gpu")).gpu_probe, NvidiaSmiGpuProbe)
