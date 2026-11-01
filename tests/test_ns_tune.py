import json
from pathlib import Path

import pytest

from ns_common.acquire import Endpoint
from ns_common.config import load_runtime, save_runtime
from ns_common.errors import NsError
from ns_common.models import LlamaFlags, Runtime
from ns_common.paths import Paths
from ns_supervisor import tune
from ns_supervisor.testing import free_port, stub_def


def rt_for(model: str = "C:/m/Qwen3-14B-Q4_K_M.gguf", profile: str = "gpu") -> Runtime:
    rt = Runtime.default("gpu" if profile == "gpu" else "cpu-dev")
    rt.llama.model_path = model
    return rt


def test_matrix_for_a_dense_gpu_model() -> None:
    m = tune.plan_matrix(rt_for())
    assert m["ngl"] == [99, 80, 60, 40, 20]
    assert m["ctx_kv"] == [(16384, "f16"), (16384, "q8_0"), (32768, "f16"), (32768, "q8_0")]
    assert m["n_cpu_moe"] == [] and m["mtp_draft_n_max"] == [2, 3, 4]


def test_matrix_for_moe_and_cpu_dev() -> None:
    moe = tune.plan_matrix(rt_for("C:/m/Qwen3.6-35B-A3B-Q4.gguf"))
    assert moe["n_cpu_moe"] == [0, 4, 8, 12, 16]
    cpu = tune.plan_matrix(rt_for(profile="cpu-dev"))
    assert cpu["ngl"] == [0] and (8192, "f16") in cpu["ctx_kv"] and len(cpu["ctx_kv"]) == 6


def test_dry_run_lines_list_every_stage() -> None:
    text = "\n".join(tune.matrix_lines(rt_for("C:/m/x-A3B.gguf")))
    for token in ("-ngl", "16K/f16", "32K/q8_0", "[0, 4, 8, 12, 16]", "MTP", "400-token"):
        assert token in text


def test_pick_winner_prefers_roomier_config_within_five_percent() -> None:
    small = LlamaFlags(ctx=16384, kv_type="f16")
    big_q8 = LlamaFlags(ctx=32768, kv_type="q8_0")
    big_f16 = LlamaFlags(ctx=32768, kv_type="f16")
    prefer = lambda f: (-f.ctx, f.kv_type != "f16")  # noqa: E731
    assert tune.pick_winner([(small, 50.0), (big_q8, 48.0), (big_f16, 49.0)], prefer)[0] is big_f16
    assert tune.pick_winner([(small, 50.0), (big_q8, 40.0)], prefer)[0] is small


class Fake:
    """Deterministic runner: speed is a function of the flags."""

    def __init__(self, fail_ngl_above: int = 999, mtp_factor: float = 1.2) -> None:
        self.calls: list[LlamaFlags] = []
        self.fail_ngl_above = fail_ngl_above
        self.mtp_factor = mtp_factor

    def __call__(self, rt: Runtime) -> tuple[float, float]:
        f = rt.llama.flags
        self.calls.append(f)
        if f.n_gpu_layers > self.fail_ngl_above:
            raise NsError("NS-E202", "out of memory")
        tok = 60.0 - (f.n_gpu_layers < 99) * 5
        tok *= 0.8 if f.ctx == 32768 else 1.0
        tok *= 0.99 if f.kv_type == "q8_0" else 1.0
        tok *= 1.0 + 0.02 * f.n_cpu_moe / 4
        if f.spec_type:
            tok *= self.mtp_factor
        return tok, 12.0


def prepared(tmp_path: Path, model: str = "C:/m/Qwen3-14B-Q4_K_M.gguf") -> tuple[Paths, Runtime]:
    paths = Paths(tmp_path / "ns")
    rt = rt_for(model)
    save_runtime(paths, rt)
    return paths, rt


def test_sweep_steps_ngl_down_only_on_load_failure_and_keeps_faster_mtp(tmp_path: Path) -> None:
    paths, rt = prepared(tmp_path)
    runner = Fake(fail_ngl_above=80)
    out = tune.tune(paths, rt, runner)
    assert out.ok and out.flags is not None
    assert out.flags.n_gpu_layers == 80
    assert (out.flags.ctx, out.flags.kv_type) == (16384, "f16")  # 32K is >5 % slower
    assert out.flags.spec_type == "draft-mtp" and out.flags.spec_draft_n_max in (2, 3, 4)
    assert out.warnings == []
    saved = load_runtime(paths)
    assert saved.llama.flags == out.flags
    assert saved.tts == rt.tts and saved.llama.model_path == rt.llama.model_path
    stages = [a.stage for a in out.attempts]
    assert stages[:2] == ["ngl", "ngl"] and out.attempts[0].ok is False and out.attempts[1].ok is True
    log = next(paths.logs.glob("tune-*.json"))
    data = json.loads(log.read_text(encoding="utf-8"))
    assert data["ok"] is True and len(data["attempts"]) == len(out.attempts)


def test_mtp_is_dropped_when_not_faster(tmp_path: Path) -> None:
    paths, rt = prepared(tmp_path)
    out = tune.tune(paths, rt, Fake(mtp_factor=1.01))
    assert out.ok and out.flags is not None and out.flags.spec_type is None
    assert out.warnings == ["NS-W302"]
    assert load_runtime(paths).llama.flags.spec_type is None


def test_moe_stage_runs_for_moe_files(tmp_path: Path) -> None:
    paths, rt = prepared(tmp_path, "C:/m/Qwen3.6-35B-A3B.gguf")
    runner = Fake()
    out = tune.tune(paths, rt, runner)
    moe_tried = {a.flags["n_cpu_moe"] for a in out.attempts if a.stage == "n_cpu_moe"}
    assert moe_tried == {4, 8, 12, 16}  # 0 was already measured by the earlier stages
    assert out.flags is not None and out.flags.n_cpu_moe == 8  # 8, 12, 16 are within 5 %: lowest wins


def test_nothing_loads_keeps_runtime_json(tmp_path: Path) -> None:
    paths, rt = prepared(tmp_path)
    before = paths.runtime_file.read_text(encoding="utf-8")

    def never(rt_: Runtime) -> tuple[float, float]:
        raise NsError("NS-E203", "timeout")

    out = tune.tune(paths, rt, never)
    assert out.ok is False and out.flags is None
    assert paths.runtime_file.read_text(encoding="utf-8") == before
    assert len(out.attempts) == 5 and all(not a.ok for a in out.attempts)
    assert any("unchanged" in w for w in out.warnings)
    assert next(paths.logs.glob("tune-*.json")).exists()


def test_process_runner_times_a_stub_service(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "ns")
    port = free_port()
    runner = tune.ProcessRunner(
        paths, build_def=lambda rt: stub_def("llama", port, extra=["--tok-s", "33", "--ready-delay", "0.3"])
    )
    tok_s, load_s = runner(rt_for())
    assert tok_s == 33.0 and load_s >= 0.3


def test_process_runner_reports_failed_loads(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "ns")
    dies = tune.ProcessRunner(
        paths, build_def=lambda rt: stub_def("llama", free_port(), extra=["--exit-immediately"])
    )
    with pytest.raises(NsError) as e1:
        dies(rt_for())
    assert e1.value.code == "NS-E202"
    hangs = tune.ProcessRunner(
        paths,
        load_timeout_s=1.0,
        build_def=lambda rt: stub_def("llama", free_port(), extra=["--never-ready"]),
    )
    with pytest.raises(NsError) as e2:
        hangs(rt_for())
    assert e2.value.code == "NS-E203"


def test_gpu_reservation_holds_and_releases_through_whisper() -> None:
    events: list[tuple[str, str, str]] = []

    class FakeClient:
        def __init__(self, up: bool) -> None:
            self.up = up

        def is_up(self) -> bool:
            return self.up

        def acquire(self, service, owner, *, model_alias=None, timeout_s=7200):  # type: ignore
            events.append(("acquire", service, owner))
            return Endpoint("inproc")

        def release(self, service, owner) -> None:  # type: ignore[no-untyped-def]
            events.append(("release", service, owner))

    with tune.gpu_reservation(FakeClient(True)) as held:
        assert held is True and events == [("acquire", "whisper", "tune")]
    assert events[-1] == ("release", "whisper", "tune")
    events.clear()
    with tune.gpu_reservation(FakeClient(False)) as held2:
        assert held2 is False
    assert events == []
