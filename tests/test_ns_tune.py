import json
import subprocess
from pathlib import Path

import pytest

from ns_common.acquire import Endpoint
from ns_common.config import load_runtime, save_runtime
from ns_common.errors import NsError
from ns_common.models import LlamaFlags, Runtime
from ns_common.paths import Paths
from ns_supervisor import tune, vram
from ns_supervisor.gpu import SmiSource
from ns_supervisor.testing import free_port, stub_def


def rt_for(model: str = "C:/m/Qwen3-14B-Q4_K_M.gguf", profile: str = "gpu") -> Runtime:
    rt = Runtime.default("gpu" if profile == "gpu" else "cpu-dev")
    rt.llama.model_path = model
    return rt


def test_matrix_for_a_dense_gpu_model() -> None:
    m = tune.plan_matrix(rt_for())
    assert m["ngl"] == [99]          # llama.cpp's own placement: one first load, never a count to step down
    assert m["ctx_kv"] == [(16384, "f16"), (16384, "q8_0"), (32768, "f16"), (32768, "q8_0")]
    assert m["mtp_draft_n_max"] == [2, 3, 4] and set(m) == {"ngl", "ctx_kv", "mtp_draft_n_max"}


def test_matrix_for_moe_and_cpu_dev() -> None:
    moe = tune.plan_matrix(rt_for("C:/m/Qwen3.6-35B-A3B-Q4.gguf"))
    assert "n_cpu_moe" not in moe and moe["ngl"] == [99]         # no expert count, not even for a MoE file
    cpu = tune.plan_matrix(rt_for(profile="cpu-dev"))
    assert cpu["ngl"] == [0] and cpu["ctx_kv"][0] == (16384, "f16") and len(cpu["ctx_kv"]) == 4


def test_dry_run_lines_list_every_stage() -> None:
    text = "\n".join(tune.matrix_lines(rt_for("C:/m/x-A3B.gguf")))
    for token in ("own placement", "16K/f16", "32K/q8_0", "MTP", "400-token", "settle", "3 runs"):
        assert token in text
    assert "[0, 4, 8, 12, 16]" not in text and "skipped (not a MoE" not in text


def test_pick_winner_prefers_roomier_config_within_five_percent() -> None:
    small = LlamaFlags(ctx=16384, kv_type="f16")
    big_q8 = LlamaFlags(ctx=32768, kv_type="q8_0")
    big_f16 = LlamaFlags(ctx=32768, kv_type="f16")
    prefer = lambda f: (-f.ctx, f.kv_type != "f16")  # noqa: E731
    assert tune.pick_winner([(small, 50.0), (big_q8, 48.0), (big_f16, 49.0)], prefer)[0] is big_f16
    assert tune.pick_winner([(small, 50.0), (big_q8, 40.0)], prefer)[0] is small


class Fake:
    """Deterministic runner: speed is a function of the flags."""

    def __init__(self, mtp_factor: float = 1.2, fail_first: bool = False) -> None:
        self.calls: list[LlamaFlags] = []
        self.mtp_factor = mtp_factor
        self.fail_first = fail_first

    def __call__(self, rt: Runtime) -> tuple[float, float]:
        f = rt.llama.flags
        self.calls.append(f)
        if self.fail_first:
            raise NsError("NS-E202", "out of memory")
        tok = 60.0
        tok *= 0.8 if f.ctx == 32768 else 1.0
        tok *= 0.99 if f.kv_type == "q8_0" else 1.0
        if f.spec_type:
            tok *= self.mtp_factor
        return tok, 12.0


def prepared(tmp_path: Path, model: str = "C:/m/Qwen3-14B-Q4_K_M.gguf") -> tuple[Paths, Runtime]:
    paths = Paths(tmp_path / "ns")
    rt = rt_for(model)
    save_runtime(paths, rt)
    return paths, rt


def test_sweep_never_tries_a_layer_or_expert_count_and_stores_none(tmp_path: Path) -> None:
    """Rule 6: the candidates are context, cache type and the draft. A runtime.json that holds a layer count
    and an expert count (an older tune's winner) is stored back with llama.cpp's own placement."""
    paths = Paths(tmp_path / "ns")
    rt = rt_for("C:/m/Qwen3.6-35B-A3B.gguf")             # a MoE file: the old sweep tried n_cpu_moe 4..16
    rt.llama.flags.n_gpu_layers, rt.llama.flags.n_cpu_moe = 80, 8
    save_runtime(paths, rt)
    runner = Fake()
    out = tune.tune(paths, rt, runner)
    assert out.ok and out.flags is not None
    assert {(f.n_gpu_layers, f.n_cpu_moe) for f in runner.calls} == {(99, 0)}   # nothing else was loaded
    assert (out.flags.n_gpu_layers, out.flags.n_cpu_moe) == (99, 0)
    assert (out.flags.ctx, out.flags.kv_type) == (16384, "f16")  # 32K is >5 % slower
    assert out.flags.spec_type == "draft-mtp" and out.flags.spec_draft_n_max in (2, 3, 4)
    assert out.warnings == []
    saved = load_runtime(paths)
    assert saved.llama.flags == out.flags
    assert saved.tts == rt.tts and saved.llama.model_path == rt.llama.model_path
    assert [a.stage for a in out.attempts] == ["first"] + ["ctx_kv"] * 4 + ["mtp"] * 3
    log = next(paths.logs.glob("tune-*.json"))
    data = json.loads(log.read_text(encoding="utf-8"))
    assert data["ok"] is True and len(data["attempts"]) == len(out.attempts)
    assert data["winner"]["n_gpu_layers"] == 99 and data["winner"]["n_cpu_moe"] == 0


def test_mtp_is_dropped_when_not_faster(tmp_path: Path) -> None:
    paths, rt = prepared(tmp_path)
    out = tune.tune(paths, rt, Fake(mtp_factor=1.01))
    assert out.ok and out.flags is not None and out.flags.spec_type is None
    assert out.warnings == ["NS-W302"]
    assert load_runtime(paths).llama.flags.spec_type is None


def test_the_draft_is_measured_on_the_same_loads_as_everything_else(tmp_path: Path) -> None:
    """The old sweep measured n_cpu_moe only without the draft and never combined the two (evidence 5): the
    MoE file now takes exactly the dense file's candidates, the draft among them."""
    paths, rt = prepared(tmp_path, "C:/m/Qwen3.6-35B-A3B.gguf")
    out = tune.tune(paths, rt, Fake())
    stages = [a.stage for a in out.attempts]
    assert stages.count("mtp") == 3 and all("n_cpu_moe" not in s for s in stages)
    assert out.flags is not None and out.flags.spec_type == "draft-mtp"


def test_nothing_loads_keeps_runtime_json(tmp_path: Path) -> None:
    paths, rt = prepared(tmp_path)
    before = paths.runtime_file.read_text(encoding="utf-8")
    out = tune.tune(paths, rt, Fake(fail_first=True))
    assert out.ok is False and out.flags is None
    assert paths.runtime_file.read_text(encoding="utf-8") == before
    assert len(out.attempts) == 1 and not out.attempts[0].ok          # no count to step down to
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


def test_two_model_mode_warns_that_only_the_code_model_is_tuned(tmp_path: Path) -> None:
    paths, rt = prepared(tmp_path)
    rt.llama.story_model_path = "C:/m/story.gguf"
    out = tune.tune(paths, rt, Fake())
    assert out.ok and any("two-model" in w for w in out.warnings)


# ---------------------------------------------------------------------------------------------- D-44
# A PC that runs on the processor holds the context's memory (the KV cache) for the whole night: the tune
# measures speed on an empty prompt, where a larger context costs nothing, so among equal results the smallest
# context that does the work wins there (the card's decision). Nightshift Code refuses below 16384 (D-34).
class Flat:
    """A runner whose speed does not depend on the context (the tune's own probe: an empty prompt)."""

    def __init__(self, factor: dict[int, float] | None = None, fail_ctx: tuple[int, ...] = ()) -> None:
        self.calls: list[LlamaFlags] = []
        self.factor = factor or {}
        self.fail_ctx = fail_ctx

    def __call__(self, rt: Runtime) -> tuple[float, float]:
        f = rt.llama.flags
        self.calls.append(f)
        if f.ctx in self.fail_ctx:
            raise NsError("NS-E202", "out of memory")
        return 50.0 * self.factor.get(f.ctx, 1.0), 5.0


def install(tmp_path: Path, profile: str, code: bool | str | None) -> tuple[Paths, Runtime]:
    """An install on `profile`; `code` is what answers.json says about Nightshift Code (None: no file)."""
    paths = Paths(tmp_path / "ns")
    rt = rt_for(profile=profile)
    save_runtime(paths, rt)
    if code is not None:
        (paths.config / "answers.json").write_text(json.dumps({"install_code": code}), encoding="utf-8")
    return paths, rt


@pytest.mark.parametrize(
    ("code", "ctx"), [(False, 16384), (True, 16384), (None, 16384), ("{not json", 16384)]
)
def test_on_the_cpu_profile_the_smallest_context_that_does_the_work_wins(
    tmp_path: Path, code: bool | str | None, ctx: int
) -> None:
    """One floor whatever answers.json says about Nightshift Code (D-44): the night's context does not depend
    on an optional tool, and a damaged answers file changes nothing."""
    paths, rt = install(tmp_path, "cpu-dev", code)
    runner = Flat()
    out = tune.tune(paths, rt, runner)
    assert out.ok and out.flags is not None
    assert (out.flags.ctx, out.flags.kv_type) == (ctx, "q8_0")       # q8_0 before f16 among equals
    assert min(f.ctx for f in runner.calls) == ctx                    # nothing below the floor is ever tried
    assert load_runtime(paths).llama.flags == out.flags
    again = tune.tune(paths, load_runtime(paths), Flat())             # the second run chooses the same
    assert again.flags is not None and (again.flags.ctx, again.flags.kv_type) == (ctx, "q8_0")


def test_on_the_gpu_profile_the_roomier_context_still_wins_among_equals(tmp_path: Path) -> None:
    for code in (False, True, None):
        paths, rt = install(tmp_path / str(code), "gpu", code)
        out = tune.tune(paths, rt, Flat())
        assert out.flags is not None and (out.flags.ctx, out.flags.kv_type) == (32768, "f16")


def test_a_slower_floor_or_a_bigger_size_that_does_not_load_is_decided_by_the_numbers(tmp_path: Path) -> None:
    paths, rt = install(tmp_path / "a", "cpu-dev", False)
    out = tune.tune(paths, rt, Flat(factor={16384: 0.8}))               # the floor is more than 5 % slower
    assert out.flags is not None and out.flags.ctx == 32768
    paths, rt = install(tmp_path / "b", "cpu-dev", False)
    out = tune.tune(paths, rt, Flat(fail_ctx=(32768,)))                 # a bigger one that does not load
    assert out.flags is not None and out.flags.ctx == 16384


def down_client(paths: Paths):  # type: ignore[no-untyped-def]
    import httpx

    from ns_supervisor.client import SupervisorClient

    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    return SupervisorClient("http://sup", client=httpx.Client(transport=httpx.MockTransport(refuse)))


def ns_tune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: Flat, profile: str, code: bool | None
) -> str:
    """What `ns tune` prints (the command, as the owner types it and as setup runs it)."""
    from typer.testing import CliRunner

    from ns_supervisor import cli

    paths, _ = install(tmp_path, profile, code)
    monkeypatch.setattr(cli, "_client", down_client)
    monkeypatch.setattr(tune, "ProcessRunner", lambda p: runner)
    res = CliRunner().invoke(cli.app, ["tune"], env={"NIGHTSHIFT_HOME": str(paths.home)})
    assert res.exit_code == 0, res.output
    return res.output


WHY = "a larger one would take memory this PC needs for the jobs."
FITS = "every use of the language model fits in (the largest, Nightshift Code, needs up to 16384)"


def test_the_tune_line_says_the_context_and_why_on_the_cpu_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    said = ""
    for i, code in enumerate((False, True)):
        said = ns_tune(tmp_path / str(i), monkeypatch, Flat(), "cpu-dev", code)
        assert f"context 16384 tokens: the smallest that {FITS}; {WHY}" in said
    # setup shows the last eight lines of `ns tune`: the line is among them, before the closing one
    lines = said.strip().splitlines()
    assert lines[-2].startswith("context 16384 tokens") and lines[-1] == "Saved to runtime.json."


def test_the_tune_line_is_true_when_the_smallest_context_was_slower_or_did_not_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    said = ns_tune(tmp_path / "a", monkeypatch, Flat(factor={16384: 0.8}), "cpu-dev", False)
    assert f"context 32768 tokens: the smallest that {FITS} is 16384" in said
    assert "more than 5 % slower in every measurement, or did not load, so this one is used" in said
    assert f"smallest that {FITS};" not in said


def test_the_gpu_profile_says_nothing_new_about_the_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    said = ns_tune(tmp_path, monkeypatch, Flat(), "gpu", True)
    assert "memory this PC needs" not in said and "context 32768 tokens" not in said
    assert said.rstrip().splitlines()[-1] == "Saved to runtime.json."


# ---------------------------------------------------------------------------------------------- D-52
# QA on rc4 typed `ns tune` twice on a processor-only PC: run 1 saved 32768 (its first load, cold, stood for
# the floor at 6.0 tok/s against 11.2), run 2 saved 16384 (6.3, 6.3, 6.1, 6.2). One measurement per candidate,
# the first of them cold: the 5 % rule decided on noise.
Speeds = dict[tuple[int, str], list[float | None]]


class ByCandidate:
    """A runner that answers per context candidate, in the order that candidate is asked (the last answer
    repeats; None: that load fails). The run's FIRST call answers `cold` whatever it is asked (stage 1's
    load); a call with MTP on answers 1.0, so it never wins and the context stage's choice stays."""

    def __init__(self, cold: float, speeds: Speeds) -> None:
        self.calls: list[LlamaFlags] = []
        self.cold = cold
        self.left = {k: list(v) for k, v in speeds.items()}

    def __call__(self, rt: Runtime) -> tuple[float, float]:
        f = rt.llama.flags
        self.calls.append(f)
        if len(self.calls) == 1:
            return self.cold, 60.0
        if f.spec_type:
            return 1.0, 5.0
        answers = self.left[(f.ctx, f.kv_type)]
        t = answers.pop(0) if len(answers) > 1 else answers[0]
        if t is None:
            raise NsError("NS-E202", "out of memory")
        return t, 5.0


def chosen_context(tmp_path: Path, runner: ByCandidate, profile: str = "cpu-dev") -> tuple[int, str]:
    paths, rt = install(tmp_path, profile, False)
    out = tune.tune(paths, rt, runner)
    assert out.ok and out.flags is not None, out.warnings
    assert load_runtime(paths).llama.flags == out.flags
    return out.flags.ctx, out.flags.kv_type


def flat(speed: float) -> Speeds:
    return {(c, kv): [speed] for c in (16384, 32768) for kv in ("f16", "q8_0")}


def test_the_first_load_of_a_run_is_never_a_sample(tmp_path: Path) -> None:
    """The run's first load is the cold one (the model file is not in memory yet): 6.0 where every later load
    measures 10.0. It decides nothing: the processor profile keeps the floor with q8_0, exactly as when every
    load measures the same, and the gpu profile its roomiest."""
    runner = ByCandidate(6.0, flat(10.0))
    assert chosen_context(tmp_path / "cpu", runner) == (16384, "q8_0")
    asked = [(f.ctx, f.kv_type) for f in runner.calls if not f.spec_type]
    # stage 1 once (cold, not counted), then every candidate twice, the floor's own included
    assert asked[0] == (16384, "f16") and len(asked) == 1 + 4 * tune.CPU_REPEATS, asked
    assert asked[1:] == [(c, kv) for c in (16384, 32768) for kv in ("f16", "q8_0") for _ in range(2)], asked
    gpu = ByCandidate(6.0, flat(10.0))
    assert chosen_context(tmp_path / "gpu", gpu, "gpu") == (32768, "f16")
    assert len([f for f in gpu.calls if not f.spec_type]) == 1 + 4      # once each on the gpu profile


def test_on_the_cpu_profile_a_larger_context_wins_only_when_faster_in_every_measurement(
    tmp_path: Path,
) -> None:
    floor: Speeds = {(16384, "f16"): [10.0, 10.0], (16384, "q8_0"): [10.0, 10.0]}
    # faster in one measurement only (10.4 is within 5 % of the floor's 10.0): the floor is kept
    once = {**floor, (32768, "f16"): [10.6, 10.4], (32768, "q8_0"): [10.6, 10.4]}
    assert chosen_context(tmp_path / "once", ByCandidate(8.0, once)) == (16384, "q8_0")
    # more than 5 % faster in both measurements: the larger one is used (q8_0 before f16 among equals)
    both = {**floor, (32768, "f16"): [10.6, 10.6], (32768, "q8_0"): [10.6, 10.6]}
    assert chosen_context(tmp_path / "both", ByCandidate(8.0, both)) == (32768, "q8_0")
    # ... but not against a floor measurement that was as fast once (one of the floor's four reached 10.3)
    near = {**both, (16384, "q8_0"): [10.0, 10.3]}
    assert chosen_context(tmp_path / "near", ByCandidate(8.0, near))[0] == 16384
    # one of the larger one's two loads fails: that candidate is not compared at all
    fails = {**floor, (32768, "f16"): [12.0, None], (32768, "q8_0"): [None]}
    assert chosen_context(tmp_path / "fails", ByCandidate(8.0, fails)) == (16384, "q8_0")
    # the floor itself does not load a second time in either KV type: a larger one that does is used (D-44)
    no_floor: Speeds = {
        (16384, "f16"): [None], (16384, "q8_0"): [10.0, None],
        (32768, "f16"): [9.0, 9.0], (32768, "q8_0"): [9.0, 9.0],
    }  # fmt: skip
    assert chosen_context(tmp_path / "nofloor", ByCandidate(8.0, no_floor)) == (32768, "q8_0")


def test_the_d52_numbers_choose_the_same_context_twice(tmp_path: Path) -> None:
    """QA's two runs replayed: the cold 6.0 first, then numbers near 9.9, 11.2 and 10.1 with the scatter two
    warm loads of one candidate show; and the second run's 6.3, 6.3, 6.1, 6.2. Both keep 16384."""
    run1: Speeds = {
        (16384, "f16"): [9.8, 10.1], (16384, "q8_0"): [9.9, 10.0],
        (32768, "f16"): [11.2, 10.3], (32768, "q8_0"): [10.1, 10.2],
    }  # fmt: skip
    run2: Speeds = {
        (16384, "f16"): [6.3, 6.2], (16384, "q8_0"): [6.3, 6.3],
        (32768, "f16"): [6.1, 6.4], (32768, "q8_0"): [6.2, 6.1],
    }  # fmt: skip
    assert chosen_context(tmp_path / "run1", ByCandidate(6.0, run1))[0] == 16384
    assert chosen_context(tmp_path / "run2", ByCandidate(6.3, run2))[0] == 16384


def test_nothing_loads_a_second_time_keeps_what_loaded_once(tmp_path: Path) -> None:
    """Every stage 2 load fails (a PC at the edge of its memory): the flags that did load in stage 1 are kept,
    the floor with f16, and the line says the floor was chosen."""
    nothing: Speeds = {(c, kv): [None] for c in (16384, 32768) for kv in ("f16", "q8_0")}
    paths, rt = install(tmp_path, "cpu-dev", False)
    out = tune.tune(paths, rt, ByCandidate(7.0, nothing))
    assert out.ok and out.flags is not None and (out.flags.ctx, out.flags.kv_type) == (16384, "f16")
    assert out.context_note.startswith("context 16384 tokens: the smallest that ")


def test_pick_context_alone() -> None:
    cpu, gpu = rt_for(profile="cpu-dev"), rt_for()
    f16, q8 = LlamaFlags(ctx=16384, kv_type="f16"), LlamaFlags(ctx=16384, kv_type="q8_0")
    big = LlamaFlags(ctx=32768, kv_type="f16")
    # cpu-dev: each of the larger one's measurements must be more than 5 % above each of the floor's
    assert tune.pick_context(cpu, [(f16, [10.0, 10.0]), (big, [10.6, 10.5])], 16384)[0] is f16
    assert tune.pick_context(cpu, [(f16, [10.0, 10.0]), (big, [10.6, 10.6])], 16384)[0] is big
    three = [(f16, [10.0, 10.0]), (q8, [10.0, 10.2]), (big, [10.6, 10.6])]
    assert tune.pick_context(cpu, three, 16384)[0] is q8          # one floor measurement was as fast once
    assert tune.pick_context(cpu, [(big, [3.0, 3.0])], 16384)[0] is big           # the floor did not load
    flags, speed = tune.pick_context(cpu, [(f16, [10.0, 12.0]), (q8, [9.0, 9.0])], 16384)
    assert flags is f16 and speed == 11.0                 # f16's slowest (10.0) is more than 5 % above q8_0's
    # gpu: one measurement each, the roomiest within 5 % of the fastest, as before
    assert tune.pick_context(gpu, [(f16, [50.0]), (big, [49.0])], 16384)[0] is big
    assert tune.pick_context(gpu, [(f16, [50.0]), (big, [40.0])], 16384)[0] is f16


def test_the_dry_run_says_how_often_each_context_is_measured() -> None:
    cpu = "\n".join(tune.matrix_lines(rt_for(profile="cpu-dev")))
    assert "each measured 2 times" in cpu and "its speed is not counted" in cpu
    assert "about 12+ llama-server loads" in cpu           # 1 + 4 candidates x 2 + 3 MTP
    gpu = "\n".join(tune.matrix_lines(rt_for()))
    assert "each measured" not in gpu and "about 8+ llama-server loads" in gpu    # 1 + 4 + 3


# ---------------------------------------------------------------------------------------------- WP1b
# The card's settle before every candidate, the median of three speed runs, a spill as a failed candidate, the
# winner in words. A fake card (nvidia-smi's answers from a list) and a fake llama stand in for the GPU PC.
class FakeCard:
    """nvidia-smi: each read answers the next `used` (the last repeats), of `total` MiB; None: no tool."""

    def __init__(self, used: list[int], total: int = 16000) -> None:
        self.used, self.total, self.reads = list(used), total, 0

    def __call__(self, args: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        self.reads += 1
        used = self.used.pop(0) if len(self.used) > 1 else self.used[0]
        return subprocess.CompletedProcess(args, 0, f"{used}, {self.total}\n", "")


def smi_of(card: FakeCard) -> SmiSource:
    return SmiSource(card)


class Clock:
    """A clock that only the fake sleep moves, so a wait takes no real time."""

    def __init__(self) -> None:
        self.now, self.sleeps = 0.0, 0

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s
        self.sleeps += 1


def set_idle(paths: Paths, mb: int) -> None:
    from datetime import datetime

    for _ in range(vram.IDLE_AGREE + 1):          # `record_idle` takes a baseline from agreeing readings
        vram.record_idle(paths, mb, None, datetime.now())
    assert vram.idle_mb(paths) == mb


def test_the_card_is_waited_for_until_it_is_within_256_of_idle(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "ns")
    set_idle(paths, 900)
    card, clock = FakeCard([15000, 9000, 3000, 1100]), Clock()      # the last load's memory is given back
    got = tune.settle_card(smi_of(card), paths, sleep=clock.sleep, clock=clock)
    assert got.calm and got.used_mb == 1100 and got.idle_mb == 900
    assert card.reads == 4 and clock.sleeps == 3 and got.waited_s == 1.5


def test_the_settle_gives_up_after_20_seconds_and_says_so(tmp_path: Path) -> None:
    """A desktop that holds more than idle + 256 (a game, a window) is not waited for forever."""
    paths = Paths(tmp_path / "ns")
    set_idle(paths, 900)
    card, clock = FakeCard([5000]), Clock()
    got = tune.settle_card(smi_of(card), paths, sleep=clock.sleep, clock=clock)
    assert got.calm is False and got.used_mb == 5000 and 20.0 <= got.waited_s <= 20.5
    assert card.reads <= 42                                          # bounded by the reads as well


def test_without_an_idle_card_the_wait_is_for_the_memory_to_stop_falling(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "ns")
    card, clock = FakeCard([14000, 9000, 4000, 1500, 1490]), Clock()
    got = tune.settle_card(smi_of(card), paths, sleep=clock.sleep, clock=clock)
    assert got.calm and got.idle_mb is None and got.used_mb == 1490 and card.reads == 5


def test_no_nvidia_smi_ends_the_settle_at_once(tmp_path: Path) -> None:
    def absent(args: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("nvidia-smi")

    clock = Clock()
    got = tune.settle_card(SmiSource(absent), Paths(tmp_path / "ns"), sleep=clock.sleep, clock=clock)
    assert got.used_mb is None and got.calm is False and clock.sleeps == 0


def test_every_candidate_starts_after_a_settle_and_its_row_says_what_the_card_held(tmp_path: Path) -> None:
    paths, rt = prepared(tmp_path)
    events: list[str] = []

    def settle() -> tune.Settle:
        events.append("settle")
        return tune.Settle(used_mb=1234, idle_mb=900, waited_s=2.5, calm=events.count("settle") != 3)

    class Run(Fake):
        def __call__(self, rt_: Runtime) -> tuple[float, float]:
            events.append("load")
            self.last = tune.RunInfo([58.0, 60.0, 61.0], card_used_mb=14000, card_total_mb=16000)
            return super().__call__(rt_)

    out = tune.tune(paths, rt, Run(), settle=settle)
    assert events == ["settle", "load"] * len(out.attempts) and len(out.attempts) == 8
    row = out.attempts[0]
    assert (row.desktop_mb, row.idle_mb, row.settle_s, row.settled) == (1234, 900, 2.5, True)
    assert (row.speeds, row.card_used_mb, row.card_free_mb) == ([58.0, 60.0, 61.0], 14000, 2000)
    assert out.attempts[2].settled is False                          # the third wait ended unsettled: said
    data = json.loads(next(paths.logs.glob("tune-*.json")).read_text(encoding="utf-8"))
    assert data["attempts"][1]["desktop_mb"] == 1234 and data["attempts"][1]["card_free_mb"] == 2000


def test_a_failed_load_row_still_has_the_desktop_reading(tmp_path: Path) -> None:
    paths, rt = prepared(tmp_path)
    settle = lambda: tune.Settle(used_mb=1500, idle_mb=None, waited_s=0.0, calm=True)  # noqa: E731
    out = tune.tune(paths, rt, Fake(fail_first=True), settle=settle)
    assert out.attempts[0].ok is False and out.attempts[0].desktop_mb == 1500


def process_runner(paths: Paths, card: FakeCard, speeds: list[float]) -> tune.ProcessRunner:
    feed = iter(speeds)
    return tune.ProcessRunner(
        paths,
        build_def=lambda rt: stub_def("llama", free_port(), extra=["--tok-s", "33", "--ready-delay", "0.1"]),
        card=lambda rt: vram.read_memory(smi_of(card)),
        measure=lambda url: next(feed),
    )


def test_a_candidate_is_the_median_of_three_speed_runs(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "ns")
    runner = process_runner(paths, FakeCard([12000]), [81.0, 102.0, 95.0])    # the spread evidence 5 saw
    tok_s, _ = runner(rt_for())
    assert tok_s == 95.0
    assert runner.last is not None and runner.last.speeds == [81.0, 102.0, 95.0]
    assert (runner.last.card_used_mb, runner.last.card_total_mb) == (12000, 16000)
    cpu = process_runner(paths, FakeCard([12000]), [7.0])                # the processor: one run per load
    assert cpu(rt_for(profile="cpu-dev"))[0] == 7.0


def test_a_load_that_left_the_card_nearly_full_is_a_failed_candidate(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "ns")
    runner = process_runner(paths, FakeCard([15900]), [90.0, 90.0, 90.0])      # 100 MiB free: it spilled
    with pytest.raises(NsError) as e:
        runner(rt_for())
    assert e.value.code == "NS-E203" and "spilled into system memory" in e.value.detail
    assert runner.last is None


def test_the_winner_is_said_in_words_not_json(tmp_path: Path) -> None:
    paths, rt = prepared(tmp_path)
    out = tune.tune(paths, rt, Fake())
    assert out.words == (
        "Best for this PC: a 16384-token context, cache type f16 and the MTP draft (2 tokens at a time), "
        "72.0 tokens a second (the middle of 3 runs). llama.cpp decides how much of the model sits on the "
        "graphics card and how much in the PC's memory."
    )
    assert "{" not in out.words and "n_gpu_layers" not in out.words
    plain = tune.winner_words(rt, LlamaFlags(ctx=32768, kv_type="q8_0"), 41.25)
    assert "no MTP draft" in plain and "41.2 tokens a second (the middle of 3 runs)" in plain
    cpu = tune.winner_words(rt_for(profile="cpu-dev"), LlamaFlags(ctx=16384, kv_type="q8_0"), 6.3)
    assert "It runs on the processor." in cpu and "middle of" not in cpu


def test_ns_tune_prints_the_words_and_settles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        tune, "card_settler", lambda p, r: lambda: seen.append("s") or tune.Settle(1000, 900, 0.0, True)
    )
    said = ns_tune(tmp_path, monkeypatch, Flat(), "gpu", True)
    assert "Best for this PC:" in said and "tokens a second" in said and '"ctx"' not in said
    assert len(seen) == 8 and said.rstrip().splitlines()[-1] == "Saved to runtime.json."
