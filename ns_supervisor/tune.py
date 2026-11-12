"""C2 `ns tune`: staged llama-server sweep (spec 3) that writes the winning flags to runtime.json.

The same rule as the engine (plan 2026-11-12, rule 6): llama.cpp places the model
(`services.layers_automatic`: no `-ngl`, no `--n-cpu-moe`), so a layer or expert count is never a
candidate and the winner is stored with `n_gpu_layers` 99 (automatic) and `n_cpu_moe` 0. Stages, each
starting from the previous winner: (1) the first load with llama.cpp's own placement (cold, its speed is
not counted), (2) ctx x KV type, (3) MTP `--spec-draft-n-max` 2/3/4, kept only if faster than plain
decoding (`NS-W302` otherwise). Before EVERY
candidate the card is allowed to settle (the memory of the load before it must be given back:
`settle_card`); on the gpu profile a candidate is the median of three speed runs on one load, and a load
that left the card nearly full (it spilled into system memory) is a failed candidate. Speed is tokens/s from
llama-server `/completion` timings on a fixed 400-token prompt and 300-token generation. Every attempt is
recorded in `logs\\tune-<date>.json` with the desktop's MiB and the card's reading after `ready`; if nothing
loads, runtime.json is left untouched.

On the gpu profile the roomiest context among equal results wins. On `cpu-dev` (a PC that runs on the
processor) the SMALLEST that does the work wins, then q8_0 before f16: the probe runs on an empty prompt,
where a larger context costs nothing, but every token of context is KV memory held for the whole night
(D-44). The floor is the smallest size that holds the largest request of every user of the model
(`CONTEXT_USES`, `min_ctx_for`), the same whether or not an optional tool is installed.
"""

from __future__ import annotations

import contextlib
import json
import socket
import statistics
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Protocol

import httpx

from ns_common.acquire import Endpoint, Service
from ns_common.config import save_runtime
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.models import LlamaFlags, Runtime
from ns_common.paths import Paths
from ns_supervisor import procs, vram
from ns_supervisor.benchmark import measure_tok_s
from ns_supervisor.gpu import SmiSource, simulated_mark, smi_source
from ns_supervisor.services import AUTO_LAYERS, ServiceDef, build_service

CTX_OPTIONS = (8192, 16384, 32768)
KV_OPTIONS = ("f16", "q8_0")
MTP_OPTIONS = (2, 3, 4)
#: Speed runs per loaded candidate on the gpu profile; the median counts (single samples spread 81 to 102
#: tok/s on the GPU PC, evidence 5). The processor profile measures once per load: its repeats are loads
#: (D-52).
SPEED_RUNS = 3
#: Before every candidate: the card is read until what is in use is within `SETTLE_SLACK_MB` of this PC's idle
#: card (`vram.idle_mb`), at most `SETTLE_LIMIT_S`, every `SETTLE_POLL_S`. A load stopped 3 to 4 s before the
#: next one still held its memory (evidence 5); what the desktop holds beyond idle is logged, not waited for.
SETTLE_LIMIT_S = 20.0
SETTLE_POLL_S = 0.5
SETTLE_SLACK_MB = 256
EQUIVALENT = 0.95  # within 5 % of the best counts as equal; then the profile's preference decides
#: The context Nightshift Code needs (D-34): `launchers/nightshift-code.ps1` refuses below it and setup
#: writes it as the model's limit (`installer/stage2/steps/s07_configs.py` MIN_CODE_CONTEXT);
#: `tests/test_opencode_budget.py` keeps the three equal. Its own copy here: the engine's `ns tune` does
#: not import the installer.
CODE_MIN_CONTEXT = 16384
#: Why the tune keeps a processor-only PC's context small; the second half of the owner's line.
CPU_WHY = "a larger one would take memory this PC needs for the jobs"
MTP_GAIN = 1.03  # MTP must beat plain decoding by 3 % to be kept
CPU_REPEATS = 2  # cpu-dev: measurements per context candidate (see `pick_context`)

Runner = Callable[[Runtime], tuple[float, float]]
"""Runs llama-server for a candidate runtime and returns (tok_s, load_s); raises NsError on a failed load."""


def is_moe(model_path: str) -> bool:
    """MoE heuristic from the file name (`...-35B-A3B-...`, `...moe...`)."""
    low = model_path.lower()
    return "a3b" in low or "moe" in low


@dataclass(frozen=True)
class ContextUse:
    """One user of the language model's context and the largest request it can make: prompt, thinking and
    answer together, in tokens (the answer limit counts the thinking). `prompts` names the prompt files of
    `ns_studio/prompts` it sends; `tests/test_llm_context_budget.py` renders each at its largest input and
    fails when one needs more than `tokens`."""

    name: str
    where: str
    tokens: int
    prompts: tuple[str, ...] = ()
    profiles: tuple[str, ...] = ("gpu", "cpu-dev")


#: Every user of the language model's context (D-44). Optional tools count: an install can add Nightshift
#: Code later through Repair, and the night's context must not change with that.
CONTEXT_USES: tuple[ContextUse, ...] = (
    ContextUse("Nightshift Code", "launchers/nightshift-code.ps1 $minCtx", CODE_MIN_CONTEXT),
    ContextUse(
        "clip selection",
        "ns_studio/clipper/stages_clip.py:SelectStage (a 720 s window, thinking)",
        14000,
        ("clip_select",),
    ),
    ContextUse(
        "whole-story checks",
        "ns_studio/policy (policy_check), stages_master and series.workshop (shorts_select, continuity)",
        15000,
        ("policy_check", "shorts_select", "continuity"),
    ),
    ContextUse(
        "story writing",
        "ns_studio/text/stages_shorts.py, stages_master.py, series/workshop.py (chapters, critic, revisions)",
        13500,
        (
            "ideas",
            "story_plan",
            "story_draft",
            "long_plan",
            "chapter_draft",
            "critic",
            "revise",
            "expand",
            "split",
            "hook",
            "judge",
            "workshop_concepts",
            "workshop_regenerate",
            "workshop_map",
            "workshop_part_outline",
            "workshop_revise",
        ),
    ),
    ContextUse(
        "pictures and titles",
        "ns_studio/text (visual_plan, seo, long_seo, short_hook), ns_studio/clipper (clip_judge, clip_seo)",
        13500,
        ("visual_plan", "seo", "long_seo", "short_hook", "clip_judge", "clip_seo"),
    ),
    ContextUse(
        "the Assistant",
        "ns_dashboard/assistant/prompt.py:budget_for (sizes itself to the context; 16K plan)",
        16384,
    ),
    ContextUse("ns verify and ns tune probes", "ns_supervisor/verify.py, ns_supervisor/benchmark.py", 1024),
)


def largest_use(rt: Runtime) -> ContextUse:
    """The use of `CONTEXT_USES` with the largest request on `rt`'s profile (the first of equals)."""
    uses = [u for u in CONTEXT_USES if rt.profile in u.profiles]
    return max(uses, key=lambda u: u.tokens)


def min_ctx_for(rt: Runtime) -> int:
    """The smallest of `CTX_OPTIONS` that holds the largest request of every use the install can run
    (`largest_use`). The largest option when even that is too small, which the budget test forbids."""
    need = largest_use(rt).tokens
    return next((c for c in CTX_OPTIONS if c >= need), CTX_OPTIONS[-1])


def ctx_preference(rt: Runtime) -> Callable[[LlamaFlags], tuple[Any, ...]]:
    """Among results within 5 % of the best: gpu, the roomiest context then f16; cpu-dev, the smallest context
    then q8_0 (half the KV memory)."""
    if rt.profile == "cpu-dev":
        return lambda f: (f.ctx, f.kv_type != "q8_0")
    return lambda f: (-f.ctx, f.kv_type != "f16")


def context_note(rt: Runtime, flags: LlamaFlags, floor: int) -> str:
    """The line `ns tune` prints about the context it chose, for a cpu-dev PC ("" on the gpu profile): what it
    is, which use sets the floor and why it is not larger. True in each state: the floor was chosen, or a
    larger one because the floor was more than 5 % slower in every measurement or did not load
    (`pick_context`)."""
    if rt.profile != "cpu-dev":
        return ""
    big = largest_use(rt)
    needs = f"every use of the language model fits in (the largest, {big.name}, needs up to {big.tokens})"
    if flags.ctx == floor:
        return f"context {flags.ctx} tokens: the smallest that {needs}; {CPU_WHY}."
    return (
        f"context {flags.ctx} tokens: the smallest that {needs} is {floor}, but that size was more than 5 % "
        f"slower in every measurement, or did not load, so this one is used; {CPU_WHY}."
    )


def plan_matrix(rt: Runtime) -> dict[str, list[Any]]:
    """The candidates per stage (what `--dry-run` prints)."""
    floor = min_ctx_for(rt)
    return {
        # one first load with the stored layer setting: llama.cpp's own placement on a gpu PC (never a count
        # to step down), the processor on `cpu-dev`. Layer and expert counts are not candidates (rule 6).
        "ngl": [0] if rt.profile == "cpu-dev" else [AUTO_LAYERS],
        "ctx_kv": [(c, kv) for c in CTX_OPTIONS if c >= floor for kv in KV_OPTIONS],
        "mtp_draft_n_max": list(MTP_OPTIONS),
    }


def speed_runs(rt: Runtime) -> int:
    """Speed runs per loaded candidate: `SPEED_RUNS` on the gpu profile, one on the processor profile."""
    return 1 if rt.profile == "cpu-dev" else SPEED_RUNS


def matrix_lines(rt: Runtime) -> list[str]:
    """Human-readable matrix for `--dry-run`."""
    m = plan_matrix(rt)
    repeats = context_repeats(rt)
    runs = 1 + len(m["ctx_kv"]) * repeats + len(m["mtp_draft_n_max"])
    often = f" (each measured {repeats} times)" if repeats > 1 else ""
    placed = ("the processor" if rt.profile == "cpu-dev" else
              "llama.cpp's own placement (no -ngl, no --n-cpu-moe)")  # fmt: skip
    return [
        f"model: {rt.llama.model_path or '(not configured)'}  profile: {rt.profile}",
        f"1. first load with {placed} (that load is the cold one and its speed is not counted)",
        "2. ctx x KV: " + ", ".join(f"{c // 1024}K/{kv}" for c, kv in m["ctx_kv"]) + often,
        f"3. MTP --spec-draft-n-max: {m['mtp_draft_n_max']} (kept only if 3 % faster than plain)",
        "layer and expert counts are not tried: llama.cpp decides what sits on the card and what in memory",
        "before every load: wait for the graphics card to settle (up to 20 s)",
        f"probe: 300 tokens generated after a ~400-token prompt, tok/s from /completion timings; "
        f"{speed_runs(rt)} run{'s' if speed_runs(rt) > 1 else ''} per load, the middle one counts",
        f"about {runs}+ llama-server loads (minutes each on a cold cache)",
    ]


def pick_winner(
    results: list[tuple[LlamaFlags, float]], prefer: Callable[[LlamaFlags], tuple[Any, ...]]
) -> tuple[LlamaFlags, float]:
    """Fastest result; anything within 5 % of it is equal, and the lowest `prefer` key wins among those
    (`ctx_preference` for the context)."""
    best = max(t for _, t in results)
    near = [(f, t) for f, t in results if t >= best * EQUIVALENT]
    return min(near, key=lambda ft: (prefer(ft[0]), -ft[1]))


def context_repeats(rt: Runtime) -> int:
    """How often stage 2 measures each context candidate: twice on the processor profile, where two runs of
    one candidate differ by more than the 5 % the choice turns on (D-52); once on the gpu profile."""
    return CPU_REPEATS if rt.profile == "cpu-dev" else 1


def pick_context(
    rt: Runtime, measured: list[tuple[LlamaFlags, list[float]]], floor: int
) -> tuple[LlamaFlags, float]:
    """The context and KV type from stage 2: `measured` holds each candidate that loaded in EVERY repeat, with
    one speed per repeat (never the run's first, cold load: stage 1's speed is not a sample). Returns the
    winner and the mean of its speeds.

    gpu: as ever, `pick_winner` with the profile's preference (the roomiest within 5 % of the fastest).

    cpu-dev (D-52): the smallest context that does the work (`floor`) is kept unless a larger one is clearly
    faster: EACH of the larger one's measurements more than 5 % above EACH measurement of the floor, in
    either KV type. One sample that is, or one cold or noisy one, decides nothing, so two runs on the same
    PC choose the same context. A floor slower in every measurement, or one that did not load, still gives
    way (D-44). Within the pool that remains (the floor's candidates, or the larger ones that beat it) a
    candidate stands for its SLOWEST measurement, and the profile's preference decides among those within
    5 % of the best: the smallest context, then q8_0."""
    if rt.profile != "cpu-dev":
        flags, speed = pick_winner([(f, sum(ts) / len(ts)) for f, ts in measured], ctx_preference(rt))
        return flags, speed
    at_floor = [(f, ts) for f, ts in measured if f.ctx == floor]
    pool = [(f, ts) for f, ts in measured if f.ctx > floor]
    if at_floor:
        fastest_floor = max(max(ts) for _, ts in at_floor)
        pool = [(f, ts) for f, ts in pool if min(ts) * EQUIVALENT > fastest_floor] or at_floor
    chosen, _ = pick_winner([(f, min(ts)) for f, ts in pool], ctx_preference(rt))
    speeds = next(ts for f, ts in pool if f is chosen)
    return chosen, sum(speeds) / len(speeds)


@dataclass
class Attempt:
    """One llama-server run (or failed load)."""

    stage: str
    flags: dict[str, Any]
    ok: bool
    tok_s: float | None = None
    load_s: float | None = None
    error: str | None = None
    speeds: list[float] | None = None  # every speed run of the load (`tok_s` is their median)
    desktop_mb: int | None = None  # the card's memory in use just before the load: what the desktop holds
    idle_mb: int | None = None  # this PC's idle card (`vram.idle_mb`), when known
    settle_s: float | None = None  # how long the card was waited for
    settled: bool | None = None  # False: still above idle + `SETTLE_SLACK_MB` when the wait ended
    card_used_mb: int | None = None  # the card in use after `ready`
    card_free_mb: int | None = None  # and what was left free
    card_mark: str | None = None  # "simulated (fake_gpu)" when the readings are the simulated card's (H-10)


@dataclass(frozen=True)
class Settle:
    """What the card showed when `settle_card` stopped waiting."""

    used_mb: int | None  # None: nvidia-smi gave no reading
    idle_mb: int | None
    waited_s: float
    calm: bool
    mark: str | None = None


Settler = Callable[[], Settle]
"""Waits for the card to settle before a candidate (`card_settler`); tests give a fake one."""


@dataclass
class RunInfo:
    """What a runner saw of its last load (`ProcessRunner.last`; a runner without it logs less)."""

    speeds: list[float]
    card_used_mb: int | None = None
    card_total_mb: int | None = None


def settle_card(
    smi: SmiSource,
    paths: Paths,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    limit_s: float = SETTLE_LIMIT_S,
    poll_s: float = SETTLE_POLL_S,
) -> Settle:
    """Wait until the card's memory in use is within `SETTLE_SLACK_MB` of this PC's idle card (the memory of
    the load before it given back), reading every `poll_s` for at most `limit_s`. Without a stored idle card
    the wait is for it to stop falling (two readings within `vram.SETTLE_STEP_MB`). Never raises; no reading
    from nvidia-smi ends it at once (the load goes on and says nothing was measured). Bounded twice: by the
    reads and by the clock."""
    mark = simulated_mark(smi)
    idle = vram.idle_mb(paths, mark)
    start = clock()
    max_reads = int(limit_s / poll_s) + 1
    prev: int | None = None
    used: int | None = None
    for n in range(1, max_reads + 1):
        mem = vram.read_memory(smi)
        if mem is None:
            return Settle(None, idle, clock() - start, False, mark)
        used = mem[0]
        if idle is not None:
            calm = used <= idle + SETTLE_SLACK_MB
        else:
            calm = prev is not None and prev - used <= vram.SETTLE_STEP_MB
        if calm or n >= max_reads or clock() - start >= limit_s:
            return Settle(used, idle, clock() - start, calm, mark)
        prev = used
        sleep(poll_s)
    return Settle(used, idle, clock() - start, False, mark)  # (unreachable: the last read returns)


def card_settler(paths: Paths, rt: Runtime) -> Settler:
    """`settle_card` on this install's nvidia-smi (the simulated card under `fake_gpu`)."""
    smi = smi_source(paths, rt)
    return lambda: settle_card(smi, paths)


@dataclass
class TuneOutcome:
    """Result of a sweep."""

    ok: bool
    flags: LlamaFlags | None = None
    tok_s: float | None = None
    attempts: list[Attempt] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    context_note: str = ""
    words: str = ""  # the winner in plain words (`winner_words`)


def winner_words(rt: Runtime, flags: LlamaFlags, tok_s: float) -> str:
    """The sweep's result as a person reads it: the settings, the speed, and who decides the placement."""
    draft = (f"the MTP draft ({flags.spec_draft_n_max or 3} tokens at a time)" if flags.spec_type
             else "no MTP draft")  # fmt: skip
    runs = speed_runs(rt)
    middle = f" (the middle of {runs} runs)" if runs > 1 else ""
    placed = ("It runs on the processor." if rt.profile == "cpu-dev" else
              "llama.cpp decides how much of the model sits on the graphics card and how much in the PC's "
              "memory.")  # fmt: skip
    return (f"Best for this PC: a {flags.ctx}-token context, cache type {flags.kv_type} and {draft}, "
            f"{tok_s:.1f} tokens a second{middle}. {placed}")  # fmt: skip


def tune(
    paths: Paths, rt: Runtime, runner: Runner, *, log: NsLogger | None = None, settle: Settler | None = None
) -> TuneOutcome:
    """Run the staged sweep with `runner`; on success write the winner to runtime.json. `settle` runs before
    every candidate (`card_settler`); what it saw goes into that candidate's row."""
    lg = log or NsLogger("tune", paths.logs)
    out = TuneOutcome(ok=False)
    matrix = plan_matrix(rt)
    if rt.llama.story_model_path:
        out.warnings.append("two-model mode: only the primary (code) model was tuned; see CR-C2-5")
    floor = min_ctx_for(rt)

    def attempt(stage: str, flags: LlamaFlags) -> float | None:
        cand = rt.model_copy(deep=True)
        cand.llama.flags = flags
        seen = settle() if settle is not None else None
        extra: dict[str, Any] = {}
        if seen is not None:
            extra = {"desktop_mb": seen.used_mb, "idle_mb": seen.idle_mb, "settle_s": round(seen.waited_s, 1),
                     "settled": seen.calm, "card_mark": seen.mark}  # fmt: skip
            if not seen.calm and seen.used_mb is not None:
                lg.warn("tune_card_not_settled", stage=stage, used_mb=seen.used_mb, idle_mb=seen.idle_mb,
                        waited_s=round(seen.waited_s, 1))  # fmt: skip
        try:
            tok_s, load_s = runner(cand)
        except NsError as e:
            out.attempts.append(Attempt(stage, flags.model_dump(), False, error=str(e), **extra))
            lg.warn("tune_attempt_failed", stage=stage, error=str(e))
            return None
        info: RunInfo | None = getattr(runner, "last", None)
        if info is not None:
            extra["speeds"] = [round(s, 2) for s in info.speeds]
            extra["card_used_mb"] = info.card_used_mb
            if info.card_used_mb is not None and info.card_total_mb is not None:
                extra["card_free_mb"] = info.card_total_mb - info.card_used_mb
        row = Attempt(stage, flags.model_dump(), True, round(tok_s, 2), round(load_s, 1), **extra)
        out.attempts.append(row)
        lg.info("tune_attempt", stage=stage, tok_s=tok_s, flags=flags.model_dump(), **extra)
        return tok_s

    base = rt.llama.flags.model_copy(
        update={"ctx": floor, "kv_type": "f16", "n_cpu_moe": 0, "spec_type": None, "spec_draft_n_max": None}
    )

    # 1. the first load, with the stored layer setting (automatic on a gpu PC: llama.cpp's own placement)
    current: LlamaFlags | None = None
    current_tok = 0.0
    for ngl in matrix["ngl"]:
        flags = base.model_copy(update={"n_gpu_layers": ngl})
        t = attempt("first", flags)
        if t is not None:
            current, current_tok = flags, t
            break
    if current is None:
        out.warnings.append("the model did not load with the first setting; runtime.json left unchanged")
        _write_log(paths, rt, out)
        return out

    # 2. ctx x KV. Stage 1's load was the run's first, cold one (the model file not yet in memory): its speed
    # is not a sample, so every candidate, the floor's included, is measured here, warm; on the processor
    # profile twice (D-52). A candidate one of whose loads failed is not compared.
    repeats = context_repeats(rt)
    measured: list[tuple[LlamaFlags, list[float]]] = []
    for ctx, kv in matrix["ctx_kv"]:
        f = current.model_copy(update={"ctx": ctx, "kv_type": kv})
        speeds: list[float] = []
        for _ in range(repeats):
            t = attempt("ctx_kv", f)
            if t is None:
                break
            speeds.append(t)
        if len(speeds) == repeats:
            measured.append((f, speeds))
    if measured:
        current, current_tok = pick_context(rt, measured, floor)
    # (else: nothing loaded a second time; stage 1's flags did load once and are kept, with its cold speed)

    # 3. MTP: keep only if clearly faster than the plain baseline. (No expert count is tried: with fit on,
    # llama.cpp decides the experts, and a fixed `--n-cpu-moe` would turn its fit off.)
    mtp: list[tuple[LlamaFlags, float]] = []
    for k in matrix["mtp_draft_n_max"]:
        f = current.model_copy(update={"spec_type": "draft-mtp", "spec_draft_n_max": k})
        t = attempt("mtp", f)
        if t is not None:
            mtp.append((f, t))
    best_mtp = max(mtp, key=lambda ft: ft[1], default=None)
    if best_mtp is not None and best_mtp[1] >= current_tok * MTP_GAIN:
        current, current_tok = best_mtp
    else:
        out.warnings.append("NS-W302")
        lg.warn("mtp_disabled", "MTP was not faster; keeping plain decoding", code="NS-W302")

    out.ok, out.flags, out.tok_s = True, current, current_tok
    out.context_note = context_note(rt, current, floor)
    out.words = winner_words(rt, current, current_tok)
    new_rt = rt.model_copy(deep=True)
    new_rt.llama.flags = current
    save_runtime(paths, new_rt)
    _write_log(paths, rt, out)
    return out


def _write_log(paths: Paths, rt: Runtime, out: TuneOutcome) -> None:
    f = paths.logs / f"tune-{datetime.now():%Y%m%d-%H%M%S}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "ok": out.ok,
        "model": rt.llama.model_path,
        "winner": out.flags.model_dump() if out.flags else None,
        "tok_s": out.tok_s,
        "words": out.words,
        "warnings": out.warnings,
        "attempts": [asdict(a) for a in out.attempts],
    }
    f.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------------------------
# Real runner and GPU reservation
# --------------------------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class ProcessRunner:
    """Starts llama-server on a scratch port for a candidate runtime, times it, stops it."""

    def __init__(
        self,
        paths: Paths,
        *,
        load_timeout_s: float = 300,
        build_def: Callable[[Runtime], ServiceDef] | None = None,
        card: Callable[[Runtime], tuple[int, int] | None] | None = None,
        measure: Callable[[str], float] = measure_tok_s,
    ) -> None:
        self.paths = paths
        self.load_timeout_s = load_timeout_s
        self.build_def = build_def
        self.card = card or self._read_card
        self.measure = measure
        self.job = procs.JobObject()
        self.last: RunInfo | None = None  # what the last load showed (`tune` logs it)

    def _read_card(self, rt: Runtime) -> tuple[int, int] | None:
        """(memory in use, total) of the card now, None without a reading."""
        return vram.read_memory(smi_source(self.paths, rt))

    def __call__(self, rt: Runtime) -> tuple[float, float]:
        """Load the candidate, read the card after `ready` (a load that left it nearly full spilled into
        system memory: a failed candidate, rule 4), time `speed_runs(rt)` speed runs, return their median."""
        self.last = None
        if self.build_def is not None:
            d = self.build_def(rt)
        else:
            scratch = rt.model_copy(deep=True)
            scratch.llama.port = _free_port()
            d = build_service(scratch, self.paths, "llama", "code")
        t0 = time.perf_counter()
        proc = procs.start(d, self.paths.logs, job=self.job)
        try:
            self._wait_healthy(d, proc, t0)
            load_s = time.perf_counter() - t0
            mem = self.card(rt) if d.gpu else None
            used, total = mem if mem is not None else (None, None)
            if used is not None and total is not None and vram.spilled(total, used):
                raise NsError(
                    "NS-E203",
                    f"the load left {total - used} MiB of the graphics card's {total} free (less than "
                    f"{vram.SPILL_FREE_MB}): it spilled into system memory",
                )
            speeds = [self.measure(d.base_url) for _ in range(speed_runs(rt))]
            self.last = RunInfo(speeds, used, total)
            return statistics.median(speeds), load_s
        finally:
            procs.terminate(proc, 10)

    def _wait_healthy(self, d: ServiceDef, proc: procs.Proc, t0: float) -> None:
        while True:
            if not proc.alive():
                raise NsError("NS-E202", f"llama-server exited with code {proc.returncode()} while loading")
            try:
                if httpx.get(d.health_url, timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if time.perf_counter() - t0 > self.load_timeout_s:
                raise NsError("NS-E203", f"llama-server not healthy after {self.load_timeout_s:.0f}s")
            time.sleep(0.5)


class GpuHolder(Protocol):
    """The slice of `SupervisorClient` tune needs to own the GPU while it sweeps."""

    def acquire(
        self, service: Service, owner: str, *, model_alias: str | None = None, timeout_s: int = 7200
    ) -> Endpoint: ...

    def release(self, service: Service, owner: str) -> None: ...

    def is_up(self) -> bool: ...


@contextlib.contextmanager
def gpu_reservation(client: GpuHolder, owner: str = "tune") -> Iterator[bool]:
    """Hold the GPU lock (through the virtual `whisper` tenant, which evicts llama/tts) while sweeping.

    Yields True if the supervisor was reached; without a supervisor the sweep just proceeds.
    """
    held = False
    if client.is_up():
        client.acquire("whisper", owner, timeout_s=600)
        held = True
    try:
        yield held
    finally:
        if held:
            client.release("whisper", owner)
