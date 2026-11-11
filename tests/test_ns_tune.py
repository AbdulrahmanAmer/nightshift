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
    assert cpu["ngl"] == [0] and cpu["ctx_kv"][0] == (16384, "f16") and len(cpu["ctx_kv"]) == 4


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
