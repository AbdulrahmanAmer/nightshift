"""Plan 2026-11-12 rules 2-4 (card vram-spill, WP1a): llama.cpp's own fit places the story model.

Numbers are the GPU PC's (evidence sections 4, 11, 14): card 16311 MiB; desktop 838 MiB (Firefox open) and
1752 MiB (Firefox and two Code windows); 16k q8_0 + MTP projected 14068 and measured +14136; MTP off +12956;
every expert in RAM (--cpu-moe) +2534. Fit's lines are F2's, word for word.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from ns_common.config import load_runtime, save_runtime
from ns_common.errors import NsError
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_supervisor import fitlog, fitmigrate, gpu, procs, ramroom, services, vram
from ns_supervisor.core import Supervisor
from ns_supervisor.services import ServiceDef
from ns_supervisor.testing import StubTable, fast_config

TOTAL = 16311
NOW = datetime(2026, 11, 12, 21, 0, 0)

F2_LINES = """\
llama_params_fit_impl: projected to use 14591 MiB of device memory vs. 15172 MiB of free device memory
llama_params_fit_impl: cannot meet free memory target of 1024 MiB, need to reduce device memory by 443 MiB
llama_params_fit_impl: context size set by user to 32768 -> no change
llama_params_fit_impl: with only dense weights in device memory there is a total surplus of 11668 MiB
llama_params_fit_impl:   - CUDA0 (NVIDIA GeForce RTX 5060 Ti): 42 layers ( 2 overflowing), 14044 MiB used, 1127 MiB free
common_fit_params: successfully fit params to free device memory
common_fit_params: fitting params to free memory took 3.39 seconds
load_tensors:        CUDA0 model buffer size = 12845.04 MiB
load_tensors:    CUDA_Host model buffer size =   755.53 MiB
"""  # noqa: E501 (llama.cpp's own line)

RUN3_LINES = """\
llama_params_fit_impl: projected to use 14068 MiB of device memory vs. 15172 MiB of free device memory
llama_params_fit_impl: will leave 1103 >= 1024 MiB of free device memory, no changes needed
common_fit_params: successfully fit params to free device memory
"""

FAILED = "W common_fit_params: failed to fit params to free device memory: n_gpu_layers already set by user to 99, abort\n"  # noqa: E501


def llama(*extra: str, mtp: bool = True) -> ServiceDef:
    cmd = ["llama-server.exe", "-m", "D:/m/Qwen3.6-35B-A3B.gguf", "--port", "8090", "--host", "127.0.0.1",
           "-c", "16384", "--alias", "code,story", "--fit", "on", "--fit-target", "1024",
           "--load-mode", "none", "--cache-ram", "2048", "-ctk", "q8_0", "-ctv", "q8_0", *extra]  # fmt: skip
    if mtp:
        cmd += ["--spec-type", "draft-mtp", "--spec-draft-n-max", "3"]
    return ServiceDef(name="llama", cmd=cmd + ["-np", "1"], host="127.0.0.1", port=8090, gpu=True,
                      step="tuned")  # fmt: skip


def reading(desktop: int) -> vram.Reading:
    return vram.Reading(TOTAL, desktop)


def settled_verdict(desktop: int) -> vram.Verdict:
    return vram.judge(reading(desktop), vram.Need(1, False), (), allowance_mb=838, settled=True, mark=None,
                      what="the story model", fit=True)  # fmt: skip


def keep(paths: Paths, d: ServiceDef, **entry: object) -> None:
    key = vram.need_key(d)
    f = paths.home / "run" / "vram-need.json"
    data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"version": 1, "entries": []}
    data["entries"].append({**key, **entry})
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data), encoding="utf-8")


# ------------------------------------------------------------------------------------------ fitlog
def test_fit_lines_of_f2_are_read_word_for_word() -> None:
    fit = fitlog.parse(F2_LINES)
    assert (fit.projected_mb, fit.free_mb, fit.target_mb, fit.reduce_mb) == (14591, 15172, 1024, 443)
    assert (fit.layers, fit.overflow_layers, fit.host_mb, fit.card_model_mb) == (42, 2, 756, 12845)
    assert fit.fitted and not fit.failed and fit.moved
    assert fit.floor_mb() == 15172 - 1024 - 11668  # 2480: C4b measured +2534 with --cpu-moe
    assert "2 layers" in fit.words() and "756 MiB" in fit.words()


def test_fit_lines_inside_the_engines_jsonl_log_are_read() -> None:
    jsonl = "\n".join(json.dumps({"event": "stderr", "msg": line}) for line in RUN3_LINES.splitlines())
    fit = fitlog.parse(jsonl)
    assert fit.projected_mb == 14068 and fit.target_mb == 1024 and not fit.moved and fit.fitted


def test_no_fit_lines_reads_as_nothing_seen_and_failed_reads_as_failed() -> None:
    assert not fitlog.parse("main: server is listening\n").seen
    assert fitlog.parse(FAILED).failed


def test_read_from_takes_only_this_loads_lines(tmp_path: Path) -> None:
    log = tmp_path / "llama.jsonl"
    log.write_text(FAILED, encoding="utf-8")
    start = fitlog.size(log)
    with log.open("a", encoding="utf-8") as f:
        f.write(RUN3_LINES)
    fit = fitlog.parse(fitlog.read_from(log, start))
    assert not fit.failed and fit.projected_mb == 14068
    assert fitlog.read_from(tmp_path / "missing.jsonl", 0) == ""


# ------------------------------------------------------------------------------------------ the command
def gpu_runtime(**flags: object) -> Runtime:
    rt = Runtime.default("gpu")
    rt.llama.model_path = "D:/m/Qwen3.6-35B-A3B.gguf"
    rt.llama.flags = rt.llama.flags.model_copy(update={"kv_type": "q8_0", "spec_type": "draft-mtp",
                                                       "spec_draft_n_max": 3, **flags})  # fmt: skip
    return rt


def with_caps(monkeypatch: pytest.MonkeyPatch, caps: frozenset[str]) -> None:
    monkeypatch.setattr(services, "llama_caps", lambda paths, exe: caps)


def test_a_gpu_pc_with_fit_never_passes_ngl_nor_a_fixed_expert_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_caps(monkeypatch, frozenset(services.CAPS_PROBE))
    d = services.build_service(gpu_runtime(n_cpu_moe=8), Paths(tmp_path), "llama")
    assert "-ngl" not in d.cmd and "--n-cpu-moe" not in d.cmd
    for flag, value in (("--fit", "on"), ("--fit-target", "1024"), ("--load-mode", "none"),
                        ("--cache-ram", "2048"), ("-ctk", "q8_0"), ("--spec-type", "draft-mtp")):  # fmt: skip
        assert d.cmd[d.cmd.index(flag) + 1] == value, (flag, d.cmd)
    assert services.fitted(d) and d.step == "tuned"


def test_an_older_build_or_a_count_set_on_purpose_keeps_todays_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_caps(monkeypatch, frozenset())
    d = services.build_service(gpu_runtime(n_cpu_moe=8), Paths(tmp_path), "llama")
    assert d.cmd[d.cmd.index("-ngl") + 1] == "99" and "--n-cpu-moe" in d.cmd and "--fit" not in d.cmd
    with_caps(monkeypatch, frozenset(services.CAPS_PROBE))
    d = services.build_service(gpu_runtime(n_gpu_layers=30), Paths(tmp_path), "llama")
    assert d.cmd[d.cmd.index("-ngl") + 1] == "30" and not services.fitted(d)
    cpu = Runtime.default("cpu-dev")
    d = services.build_service(cpu, Paths(tmp_path), "llama")
    assert d.cmd[d.cmd.index("-ngl") + 1] == "0" and not services.fitted(d)


def test_the_ladder_is_as_tuned_then_mtp_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with_caps(monkeypatch, frozenset(services.CAPS_PROBE))
    a, b = services.llama_ladder(gpu_runtime(), Paths(tmp_path))
    assert services.mtp_on(a) and not services.mtp_on(b) and b.step == "mtp_off"
    assert b.cmd == services._llama(gpu_runtime(), Paths(tmp_path), None, mtp=False).cmd
    plain = gpu_runtime(spec_type=None, spec_draft_n_max=None)
    assert len(services.llama_ladder(plain, Paths(tmp_path))) == 1


def test_help_is_read_for_the_exact_flags() -> None:
    caps = services.caps_in("  --fit [on|off]   fit\n  --fit-target MiB\n  -lm, --load-mode MODE\n")
    assert caps == frozenset({"--fit", "--fit-target", "--load-mode"})
    assert "--fit" not in services.caps_in("  --fit-target MiB\n  --fit-ctx N\n")


# ------------------------------------------------------------------------------------ the ladder (rule 2)
@pytest.mark.parametrize(("desktop", "step"), [(838, "tuned"), (1752, "mtp_off"), (3600, "experts_in_ram")])
def test_the_ladder_by_the_settled_free_memory(tmp_path: Path, desktop: int, step: str) -> None:
    """Measured needs 14136 (MTP) and 12956 (no MTP): free 15473 beside 838 holds MTP (14136 + 1024);
    beside 1752 (free 14559) only MTP off (12956 + 1024); beside a 3600 desktop neither: fit moves experts."""
    paths = Paths(tmp_path)
    a, b = services.ladder_of(llama())
    keep(paths, a, mb=[14136])
    keep(paths, b, mb=[12956])
    v, chosen, got = vram.choose(paths, [a, b], settled_verdict(desktop))
    assert got == step and v.ok, (desktop, v.numbers())
    assert chosen is (b if step == "mtp_off" else a)


@pytest.mark.parametrize(("desktop", "step"), [(838, "tuned"), (1161, "mtp_off"), (1752, "mtp_off"),
                                               (2400, "experts_in_ram")])  # fmt: skip
def test_the_ladder_with_the_card_deltas_measured_under_fit(tmp_path: Path, desktop: int, step: str) -> None:
    """Evidence 14 (no -ngl, 16k q8_0, load-mode none): C2a +14426 with MTP, C2b +13138 without. Beside
    Firefox (838: free 15473 >= 14426 + 1024) MTP stays; the quiet desktop's top (1161) and 1752 take MTP off;
    2400 (free 13911 < 13138 + 1024) leaves it to fit's experts in RAM."""
    paths = Paths(tmp_path)
    a, b = services.ladder_of(llama())
    keep(paths, a, mb=[14426])
    keep(paths, b, mb=[13138])
    v, chosen, got = vram.choose(paths, [a, b], settled_verdict(desktop))
    assert got == step and v.ok, (desktop, v.numbers())


def test_the_retry_after_a_spill_is_said_as_its_own_step(tmp_path: Path) -> None:
    """After a spill the engine retries with the MTP-off definition ALONE (`options = rest`): its step is
    "mtp_off", never "tuned" (the words in /status, the log and verify's speed row)."""
    paths = Paths(tmp_path)
    _a, b = services.ladder_of(llama())
    _, chosen, got = vram.choose(paths, [b], settled_verdict(838))
    assert chosen is b and got == "mtp_off"
    keep(paths, b, mb=[13138])
    _, _, got = vram.choose(paths, [b], settled_verdict(3600))
    assert got == "experts_in_ram_mtp_off"


def test_a_first_load_is_as_tuned_and_mtp_off_is_estimated_from_a_measured_tuned_need(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    a, b = services.ladder_of(llama())
    _, chosen, step = vram.choose(paths, [a, b], settled_verdict(1752))
    assert chosen is a and step == "tuned"  # nothing measured yet: tried, then measured
    keep(paths, a, mb=[14136])
    _, chosen, step = vram.choose(paths, [a, b], settled_verdict(1752))
    assert chosen is b and step == "mtp_off"  # 14136 - 1180 + 1024 <= 14559


# ------------------------------------------------------------------------------------------ refusal (rule 3)
def test_refused_only_when_card_and_ram_cannot_hold_it(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    d = llama(mtp=False)
    keep(paths, d, mb=[12956], floor_mb=2480, commit_mb=15800)
    want = vram.need(paths, d)
    plenty = ramroom.Room(avail_mb=18000, commit_mb=23000, limit_mb=41400, limit_max_mb=120000)

    def at(desktop: int, room: ramroom.Room | None) -> vram.Verdict:
        return vram.judge(reading(desktop), want, (), allowance_mb=838, settled=True, mark=None,
                          what="the story model", fit=True, room=room)  # fmt: skip

    assert at(6000, plenty).ok and at(6000, plenty).ram_part_mb > 0  # experts to RAM: a pass
    card_short = at(16311 - 3000, plenty)  # 3000 free < floor 2480 + 1024
    assert card_short.refuse and card_short.short == "card"
    ram_short = at(6000, ramroom.Room(avail_mb=3000, commit_mb=23000, limit_mb=41400, limit_max_mb=120000))
    assert ram_short.refuse and ram_short.short == "ram" and "PC's memory" in ram_short.detail()
    # today's limit is short (41.4 - 37.4) but the page file can grow: not refused
    growing = ramroom.Room(avail_mb=18000, commit_mb=37400, limit_mb=41400, limit_max_mb=120000)
    assert at(838, growing).ok
    fixed = ramroom.Room(avail_mb=18000, commit_mb=37400, limit_mb=41400, limit_max_mb=41400)
    assert at(838, fixed).refuse and at(838, fixed).short == "commit"
    assert at(838, None).ok  # nothing read never refuses


def test_others_count_everything_not_ours_so_free_and_others_make_the_total() -> None:
    v = vram.judge(vram.Reading(16311, 1350), vram.Need(14729, True), (), allowance_mb=1536, settled=True,
                   mark=None, what="the story model")  # fmt: skip
    assert v.others_mb == 1350 and v.free_mb + v.others_mb == 16311
    assert v.refuse  # a measured need that does not fit is never "load and hope" (G-53)


# ------------------------------------------------------------------------------------ after ready (rule 4)
def test_a_spill_is_free_below_256_or_fits_failed_line_and_never_shared_memory() -> None:
    assert vram.spilled(16311, 16311 - 200)
    assert not vram.spilled(16311, 16311 - 300)
    assert vram.spilled(16311, 10000, fitlog.parse(FAILED))
    assert not vram.spilled(16311, 16311 - 1103, fitlog.parse(RUN3_LINES))


def test_a_fitted_load_keeps_the_whole_models_need(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    d = llama()
    assert vram.record_fit(paths, d, 14136, fitlog.parse(RUN3_LINES), 15800, None, NOW) == 14136
    n = vram.need(paths, d)
    assert (n.mb, n.measured, n.lower, n.offset_mb, n.commit_mb) == (14136, True, False, 68, 15800)
    # a later load that moved experts: fit's projection + the offset, never the smaller delta
    assert vram.record_fit(paths, d, 14520, fitlog.parse(F2_LINES), 16900, None, NOW) == 14591 + 68
    assert vram.need(paths, d).floor_mb == 2480
    # no fit lines (build 12032 below -lv 4) with the card left near fit's target: a lower bound
    other = llama("--x")
    vram.record_fit(paths, other, 14426, fitlog.FitResult(), None, None, NOW, free_after_mb=1047)
    assert vram.need(paths, other).lower


# ------------------------------------------------------------------------------------------ migration
def test_migration_moves_a_tuned_install_once_and_keeps_a_count_set_by_hand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths(tmp_path / "ns")
    with_caps(monkeypatch, frozenset(services.CAPS_PROBE))
    rt = gpu_runtime(ctx=32768, kv_type="f16", n_cpu_moe=8)
    save_runtime(paths, rt)
    new = fitmigrate.migrate(paths, rt)
    fl = load_runtime(paths).llama.flags
    got = (fl.n_gpu_layers, fl.ctx, fl.kv_type, fl.n_cpu_moe, fl.spec_type)
    assert got == (99, 16384, "q8_0", 0, "draft-mtp")
    assert new.llama.flags == fl and fitmigrate.marker(paths).exists()
    # once: a context he sets later is his
    later = new.model_copy(deep=True)
    later.llama.flags.ctx = 32768
    assert fitmigrate.migrate(paths, later).llama.flags.ctx == 32768

    # an owner's own edit on disk stays his too
    save_runtime(paths, later)
    assert fitmigrate.migrate(paths, load_runtime(paths)).llama.flags.ctx == 32768

    hand = Paths(tmp_path / "hand")
    save_runtime(hand, gpu_runtime(n_gpu_layers=30))
    assert fitmigrate.migrate(hand, load_runtime(hand)).llama.flags.n_gpu_layers == 30


def test_migration_runs_again_on_the_file_a_rollback_put_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan's release path: update (the gate's verify starts the new engine: moved), `ns update
    --rollback` (config/runtime.json back from the settings copy, byte for byte; run/ keeps the marker),
    update again."""
    paths = Paths(tmp_path / "ns")
    with_caps(monkeypatch, frozenset(services.CAPS_PROBE))
    save_runtime(paths, gpu_runtime(ctx=32768, kv_type="f16"))
    rc5_bytes = paths.runtime_file.read_bytes()
    fitmigrate.migrate(paths, load_runtime(paths))
    assert load_runtime(paths).llama.flags.ctx == 16384 and fitmigrate.marker(paths).exists()
    paths.runtime_file.write_bytes(rc5_bytes)  # the rollback's settings copy
    again = fitmigrate.migrate(paths, load_runtime(paths))
    assert (again.llama.flags.ctx, again.llama.flags.kv_type) == (16384, "q8_0")
    assert load_runtime(paths).llama.flags.ctx == 16384


def test_migration_takes_tunes_own_smaller_count_and_skips_without_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths(tmp_path / "ns")
    paths.logs.mkdir(parents=True)
    (paths.logs / "tune-20261112-135400.json").write_text(json.dumps({"winner": {"n_gpu_layers": 80}}))
    with_caps(monkeypatch, frozenset(services.CAPS_PROBE))
    assert fitmigrate.migrate(paths, gpu_runtime(n_gpu_layers=80)).llama.flags.n_gpu_layers == 99
    old = Paths(tmp_path / "old")
    with_caps(monkeypatch, frozenset())
    rt = gpu_runtime(ctx=32768)
    assert fitmigrate.migrate(old, rt) is rt and not fitmigrate.marker(old).exists()


# ------------------------------------------------------------------------------------------ the page file
def test_a_system_managed_page_file_may_grow_to_three_times_ram_within_the_disk() -> None:
    disk = lambda d: SimpleNamespace(total=1000 * 1024**3, free=158 * 1024**3)  # noqa: E731
    grow = ramroom.growth_mb(["?:\\pagefile.sys"], 32666, 9500, "C:", disk)
    assert grow == 3 * 32666 - 9500
    assert ramroom.growth_mb(["C:\\pagefile.sys 4096 16384"], 32666, 9500, "C:", disk) == 16384 - 9500
    tight = lambda d: SimpleNamespace(total=1000 * 1024**3, free=1 * 1024**3)  # noqa: E731
    assert ramroom.growth_mb(["?:\\pagefile.sys"], 32666, 9500, "C:", tight) == 1024
    assert ramroom.Room(commit_mb=37400, limit_mb=41400, limit_max_mb=None).commit_room_mb == 4000


# -------------------------------------------------------------------------- the engine's spill retry (rule 4)
FITTED_STUB = ["--fit", "on", "--fit-target", "1024", "-c", "16384"]
MTP_STUB = ["--spec-type", "draft-mtp", "--spec-draft-n-max", "3"]


class PcCard(gpu.FakeSmi):
    """The GPU PC's card (16311 MiB, an 838 MiB desktop); the running llama takes `take(its command)` MiB."""

    def __init__(self, paths: Paths, sup: Supervisor, take: Callable[[list[str]], int]) -> None:
        super().__init__(paths, tenants=sup._gpu_tenants)
        self.sup, self.take = sup, take

    def card(self) -> gpu.Card:
        r = self.sup.services.get("llama")
        mib = self.take(r.definition.cmd) if r is not None and r.proc is not None else 0
        return gpu.Card(total_mb=TOTAL, base_mb=838, services={"llama": mib})


def spill_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, take: Callable[[list[str]], int],
                 extra: list[str]) -> tuple[Supervisor, list[list[str]]]:  # fmt: skip
    monkeypatch.setattr(ramroom, "read", lambda: ramroom.Room())  # the laptop's memory decides nothing here
    paths = Paths(tmp_path / "ns")
    starts: list[list[str]] = []

    def starter(d: ServiceDef, logs: Path, job: object = None) -> procs.Proc:
        starts.append(list(d.cmd))
        return procs.start(d, logs, job=job)

    sup = Supervisor(paths, Runtime.default("gpu"), config=fast_config(),
                     resolver=StubTable(extra={"llama": extra}), starter=starter)  # fmt: skip
    sup.smi = gpu.SmiSource(PcCard(paths, sup, take))
    return sup, starts


def run_acquire(sup: Supervisor) -> tuple[dict[str, object] | NsError, dict[str, object]]:
    async def main() -> tuple[dict[str, object] | NsError, dict[str, object]]:
        try:
            try:
                got: dict[str, object] | NsError = await sup.acquire("llama", "opencode", model_alias="code")
            except NsError as e:
                got = e
            return got, sup.status()["services"]["llama"]
        finally:
            await sup.stop_all()
            sup.close_job()

    return asyncio.run(main())


def test_a_spill_after_ready_is_stopped_and_loaded_once_more_with_the_mtp_draft_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With MTP the load leaves 73 MiB free (16311 - 838 - 15400): a spill. The engine stops it and loads the
    MTP-off step (13138: 2335 free), which stays; /status says that step."""
    sup, starts = spill_engine(tmp_path, monkeypatch, lambda cmd: 15400 if "--spec-type" in cmd else 13138,
                               FITTED_STUB + MTP_STUB)  # fmt: skip
    got, llama_status = run_acquire(sup)
    assert not isinstance(got, NsError), got
    assert len(starts) == 2 and "--spec-type" in starts[0] and "--spec-type" not in starts[1], starts
    assert llama_status["step"] == "mtp_off" and llama_status["mtp"] is False, llama_status
    spills = [json.loads(ln) for f in sup.paths.logs.glob("supervisor-*.jsonl")
              for ln in f.read_text(encoding="utf-8").splitlines() if '"vram_spill"' in ln]  # fmt: skip
    assert len(spills) == 1 and spills[0]["data"]["free_after_mb"] == 73, spills


def test_a_second_spill_is_ns_e203_after_exactly_one_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup, starts = spill_engine(tmp_path, monkeypatch, lambda cmd: 15400, FITTED_STUB + MTP_STUB)
    got, _ = run_acquire(sup)
    assert isinstance(got, NsError) and got.code == "NS-E203", got
    assert len(starts) == 2, starts


def test_without_a_next_step_the_retry_asks_fit_for_2048_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup, starts = spill_engine(tmp_path, monkeypatch, lambda cmd: 15400 if "1024" in cmd else 13000,
                               FITTED_STUB)  # fmt: skip
    got, _ = run_acquire(sup)
    assert not isinstance(got, NsError), got
    assert len(starts) == 2 and starts[1][starts[1].index("--fit-target") + 1] == "2048", starts


def test_the_retry_does_not_start_for_an_acquire_that_lost_the_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop All, Release or the holder gone while the spilled load was stopped and the card settled: no
    second spawn (the GPU handover invariant, `_guard`)."""
    sup, starts = spill_engine(tmp_path, monkeypatch, lambda cmd: 15400 if "--spec-type" in cmd else 13138,
                               FITTED_STUB + MTP_STUB)  # fmt: skip
    kept = vram.record_failure

    def lost(*a: object, **kw: object) -> None:
        kept(*a, **kw)  # type: ignore[arg-type]
        sup.lock.state.owner = "studio:another"  # someone else holds the card now

    monkeypatch.setattr(vram, "record_failure", lost)
    got, _ = run_acquire(sup)
    assert isinstance(got, NsError) and got.code == "NS-E202", got
    assert len(starts) == 1, starts
