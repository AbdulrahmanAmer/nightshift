"""C2 `ns tune`: staged llama-server sweep (spec 3) that writes the winning flags to runtime.json.

Stages, each starting from the previous winner: (1) `-ngl` 99 stepping down only when the model does
not load, (2) ctx x KV type, (3) `--n-cpu-moe` for MoE files, (4) MTP `--spec-draft-n-max` 2/3/4, kept
only if faster than plain decoding (`NS-W302` otherwise). Speed is tokens/s from llama-server
`/completion` timings on a fixed 400-token prompt and 300-token generation. Every attempt is recorded in
`logs\\tune-<date>.json`; if nothing loads, runtime.json is left untouched.

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
from ns_supervisor import procs
from ns_supervisor.benchmark import measure_tok_s
from ns_supervisor.services import ServiceDef, build_service

NGL_STEPS = (99, 80, 60, 40, 20)
CTX_OPTIONS = (8192, 16384, 32768)
KV_OPTIONS = ("f16", "q8_0")
MOE_OPTIONS = (0, 4, 8, 12, 16)
MTP_OPTIONS = (2, 3, 4)
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
        "ngl": [0] if rt.profile == "cpu-dev" else list(NGL_STEPS),
        "ctx_kv": [(c, kv) for c in CTX_OPTIONS if c >= floor for kv in KV_OPTIONS],
        "n_cpu_moe": list(MOE_OPTIONS) if is_moe(rt.llama.model_path) else [],
        "mtp_draft_n_max": list(MTP_OPTIONS),
    }


def matrix_lines(rt: Runtime) -> list[str]:
    """Human-readable matrix for `--dry-run`."""
    m = plan_matrix(rt)
    repeats = context_repeats(rt)
    runs = 1 + len(m["ctx_kv"]) * repeats + max(0, len(m["n_cpu_moe"]) - 1) + len(m["mtp_draft_n_max"])
    often = f" (each measured {repeats} times)" if repeats > 1 else ""
    return [
        f"model: {rt.llama.model_path or '(not configured)'}  profile: {rt.profile}",
        f"1. -ngl (first that loads; that load is the cold one and its speed is not counted): {m['ngl']}",
        "2. ctx x KV: " + ", ".join(f"{c // 1024}K/{kv}" for c, kv in m["ctx_kv"]) + often,
        "3. --n-cpu-moe: " + (str(m["n_cpu_moe"]) if m["n_cpu_moe"] else "skipped (not a MoE file)"),
        f"4. MTP --spec-draft-n-max: {m['mtp_draft_n_max']} (kept only if 3 % faster than plain)",
        "probe: 300 tokens generated after a ~400-token prompt, tok/s from /completion timings",
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


@dataclass
class TuneOutcome:
    """Result of a sweep."""

    ok: bool
    flags: LlamaFlags | None = None
    tok_s: float | None = None
    attempts: list[Attempt] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    context_note: str = ""


def tune(paths: Paths, rt: Runtime, runner: Runner, *, log: NsLogger | None = None) -> TuneOutcome:
    """Run the staged sweep with `runner`; on success write the winner to runtime.json."""
    lg = log or NsLogger("tune", paths.logs)
    out = TuneOutcome(ok=False)
    matrix = plan_matrix(rt)
    if rt.llama.story_model_path:
        out.warnings.append("two-model mode: only the primary (code) model was tuned; see CR-C2-5")
    floor = min_ctx_for(rt)

    def attempt(stage: str, flags: LlamaFlags) -> float | None:
        cand = rt.model_copy(deep=True)
        cand.llama.flags = flags
        try:
            tok_s, load_s = runner(cand)
        except NsError as e:
            out.attempts.append(Attempt(stage, flags.model_dump(), False, error=str(e)))
            lg.warn("tune_attempt_failed", stage=stage, error=str(e))
            return None
        out.attempts.append(Attempt(stage, flags.model_dump(), True, round(tok_s, 2), round(load_s, 1)))
        lg.info("tune_attempt", stage=stage, tok_s=tok_s, flags=flags.model_dump())
        return tok_s

    base = rt.llama.flags.model_copy(
        update={"ctx": floor, "kv_type": "f16", "n_cpu_moe": 0, "spec_type": None, "spec_draft_n_max": None}
    )

    # 1. -ngl: first value that loads
    current: LlamaFlags | None = None
    current_tok = 0.0
    for ngl in matrix["ngl"]:
        flags = base.model_copy(update={"n_gpu_layers": ngl})
        t = attempt("ngl", flags)
        if t is not None:
            current, current_tok = flags, t
            break
    if current is None:
        out.warnings.append("no -ngl value loaded the model; runtime.json left unchanged")
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

    # 3. --n-cpu-moe (MoE only)
    if matrix["n_cpu_moe"]:
        results = [(current, current_tok)]
        for n in matrix["n_cpu_moe"]:
            if n == current.n_cpu_moe:
                continue
            f = current.model_copy(update={"n_cpu_moe": n})
            t = attempt("n_cpu_moe", f)
            if t is not None:
                results.append((f, t))
        current, current_tok = pick_winner(results, lambda f: (f.n_cpu_moe,))

    # 4. MTP: keep only if clearly faster than the plain baseline
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
    ) -> None:
        self.paths = paths
        self.load_timeout_s = load_timeout_s
        self.build_def = build_def
        self.job = procs.JobObject()

    def __call__(self, rt: Runtime) -> tuple[float, float]:
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
            return measure_tok_s(d.base_url), load_s
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
