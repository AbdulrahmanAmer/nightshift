# Nightshift M0 / C2 - Runtime (supervisor, GPU lock, `ns` tools, launchers) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the C2 runtime: a supervisor that starts, health-checks, swaps and unloads the model services under one GPU lock with guaranteed child cleanup (Windows Job Object), a typed client implementing `ns_common.acquire.Acquirer`, the `ns` console tools (stop, doctor, verify, tune, gate, update, backup, restore, schedule, notify) and the PowerShell launchers. Everything is proven in CI on `windows-latest` against a stub HTTP service; the first real run happens on the partner PC through `ns verify`.

**Architecture:** `ns_supervisor.core.Supervisor` is a plain asyncio object with no web dependency: service table (`services.py`), process helpers (`procs.py`), FIFO `GpuLock` (`lock.py`), idle/crash/auto-exit monitor, event fan-out. `app.py` is a thin FastAPI wrapper (spec 15.3) so most tests run without FastAPI installed; `client.py` is the sync httpx client. `cli.py` only wires typer commands to one module per tool (`verify.py`, `doctor.py`, `tune.py`, `update.py`, `backup.py`, `schedule.py`, `notify.py`). `testing.py` holds the helpers used by tests (free ports, polling, a sync facade over the core, stub service definitions).

**Tech Stack:** Python 3.12, asyncio, httpx, typer, pydantic (via `ns_common`), FastAPI + uvicorn (`app.py` only; tests `pytest.importorskip("fastapi")` until the CR adds it to `dev`), `ctypes` for the Job Object, PowerShell 5.1 for launchers, scheduled task and toast.

**Spec:** `BUILD_SPEC.md` 6 (all), 14.6, 15.1-15.3, 15.9, 15.11-15.12, 21.4, 22 (J2, J3, J7, J8, J10); `docs/ENGINEERING.md` 2, 4, 5, 6, 8, 9. Contracts pinned to `main` at `c33275c` (`ns_common` Paths, Runtime, config loaders, NsError, NsLogger, acquire).

## Global Constraints

- Nothing executes locally. Commit, push, read CI (`gh run list --branch feat/c2-runtime`), fix until green. No green claim without a run URL.
- Owned paths: `ns_supervisor/`, `launchers/`, `tests/test_supervisor*.py`, `tests/test_ns_*.py`, `tests/test_launchers.py`, `tests/fixtures/supervisor/`. Never edit `ns_common/`, `contracts/` (except appending to `CHANGE_REQUESTS.md`), `pyproject.toml`, `.github/`.
- Error codes only from `contracts/errors.json` (E201 GPU busy, E202 crash or start failure, E203 health timeout, E701/E702 update). A needed new code is a CR.
- Logging through `NsLogger` (component `supervisor`); `print` only in CLI entry points (use `typer.echo`).
- No heavy imports at module top level; `fastapi`/`uvicorn` imports only in `app.py`.
- Supervisor listens on 127.0.0.1 only. Service ports: llama 8080, embed 8081, tts 8091, supervisor 8099, dashboard 8100.
- PowerShell: every `*.ps1` starts with `Set-StrictMode -Version Latest` and `$ErrorActionPreference = 'Stop'`, ASCII only, no `Invoke-Expression`, no execution-policy bypass.
- Timers (idle unload 600 s, health poll 500 ms / 120 s, crash window 600 s / 3, auto-exit 1800 s) live in `SupervisorConfig` and are injected short in tests.
- Worker allocation: all tasks `sonnet` except Task 4 (core: lock/crash/idle interplay) `opus`.

---

## File structure created by this plan

```
ns_supervisor/
  services.py   # ServiceDef, build_service_table(), llama variants
  procs.py      # Proc, start(), terminate(), JobObject, vram_mb(), gpu_memory_used_mb()
  lock.py       # GpuLock, GpuState
  core.py       # Supervisor, SupervisorConfig, ServiceRuntime
  schedule_cfg.py  # config\schedule.json reader, minutes_until_next_run()
  app.py        # FastAPI app + __main__ (python -m ns_supervisor.app [--headless])
  client.py     # SupervisorClient (Acquirer), ensure_running()
  cli.py        # typer wiring
  verify.py doctor.py tune.py update.py backup.py schedule.py notify.py
  testing.py    # test helpers (free_port, wait_until, SyncSupervisor, stub_def)
launchers/
  _common.ps1 nightshift-dashboard.ps1 nightshift-studio.ps1 open-outbox.ps1 stop-all.ps1
  update.ps1 diagnostics.ps1 nightshift-code.ps1
tests/fixtures/supervisor/stub_service.py
tests/test_supervisor_services.py test_supervisor_procs.py test_supervisor_lock.py test_supervisor_core.py
tests/test_supervisor_app.py test_supervisor_client.py
tests/test_ns_cli.py test_ns_verify.py test_ns_doctor.py test_ns_tune.py test_ns_update.py
tests/test_ns_backup.py test_ns_schedule.py test_launchers.py
contracts/CHANGE_REQUESTS.md   (append only)
```

---

### Task 1: Service table (C2, `sonnet`)

**Files:** Create `ns_supervisor/services.py`, `tests/test_supervisor_services.py`.

**Interfaces:**
```python
@dataclass(frozen=True)
class ServiceDef:
    name: str; cmd: list[str]; host: str; port: int; gpu: bool; health_path: str = "/health"
    variant: str = "default"; model_alias: str | None = None; persistent: bool = False
    cwd: str | None = None
    base_url / health_url properties
SERVICES = ("llama", "embed", "tts", "dashboard")
def build_service(rt: Runtime, paths: Paths, name: str, alias: str | None = None) -> ServiceDef
def build_service_table(rt, paths) -> dict[str, ServiceDef]
def llama_variant(rt, alias) -> str     # "default" in one-model mode, else the alias
```

- [ ] Write tests: llama command line exactly as spec 6.1 (`-m`, `--port`, `--host`, `-ngl`, `-c`, `--alias code,story`), q8_0 adds `-ctk q8_0 -ctv q8_0`, `n_cpu_moe` adds `--n-cpu-moe N` only when > 0, MTP adds `--spec-type draft-mtp --spec-draft-n-max K -np 1`; two-model mode picks `story_model_path` for alias `story`; embed has `--embeddings -ngl 0`; tts is `<python> -m ns_tts.server --port 8091`; dashboard `-m ns_dashboard.server`; GPU flags; whisper absent.
- [ ] Implement `services.py`.
- [ ] Commit `feat(c2): service table from runtime.json`.

### Task 2: Process helpers and Job Object (C2, `sonnet`)

**Files:** Create `ns_supervisor/procs.py`, `tests/fixtures/supervisor/stub_service.py`, `ns_supervisor/testing.py`, `tests/test_supervisor_procs.py`.

**Interfaces:**
```python
class JobObject:                      # Windows only; no-op elsewhere
    def __init__(self) -> None; available: bool
    def assign(self, pid: int) -> bool; def close(self) -> None
@dataclass
class Proc: service: str; popen: Popen[bytes]; pid: int; log_path: Path; started_at: float
    def alive(self) -> bool
def start(service: ServiceDef, logs_dir: Path, *, job: JobObject | None = None) -> Proc
def terminate(proc: Proc, grace_s: float = 10) -> None
def vram_mb(pid: int) -> int | None
def gpu_memory_used_mb() -> int | None
```

- [ ] Stub service script: `/health` (optional ready delay, 503 until), `POST /completion` with `timings`, `/v1/chat/completions`, `/v1/embeddings`, `/v1/audio/speech` (WAV), `/v1/voices`; flags `--die-after`, `--die-once-marker`, `--ready-delay`, `--tok-s`.
- [ ] Tests: start stub, health 200, stdout lines land in `logs\<service>-YYYYMMDD.jsonl`; `terminate` ends the process; Job Object smoke test (Windows only): assign a sleeping child, close the job handle, child dies; `vram_mb` parsing with `nvidia-smi` mocked and `None` when absent.
- [ ] Implement `procs.py`, `testing.py`.
- [ ] Commit `feat(c2): process start/terminate with Job Object and VRAM probe`.

### Task 3: GPU lock (C2, `sonnet`)

**Files:** Create `ns_supervisor/lock.py`, `tests/test_supervisor_lock.py`.

**Interfaces:**
```python
@dataclass
class GpuState: owner: str | None = None; service: str | None = None
class GpuLock:
    state: GpuState
    async def acquire(self, owner: str, service: str, timeout_s: float | None) -> None   # NS-E201 on timeout
    def release(self, owner: str) -> bool
    def waiters(self) -> list[tuple[str, str]]
```

- [ ] Tests: FIFO order with three waiters; per-waiter timeout raises `NS-E201` and does not break the queue; release by a non-owner is a no-op; same owner re-acquire swaps the service; cancelled waiter is skipped; `timeout_s=0` fails immediately when busy.
- [ ] Implement `lock.py`.
- [ ] Commit `feat(c2): FIFO GPU lock with per-waiter timeout`; push; read CI.

### Task 4: Supervisor core (C2, `opus`)

**Files:** Create `ns_supervisor/core.py`, `ns_supervisor/schedule_cfg.py`, `tests/test_supervisor_core.py`.

**Interfaces:**
```python
@dataclass(frozen=True)
class SupervisorConfig:
    idle_unload_s=600; health_interval_s=0.5; health_timeout_s=120; term_grace_s=10
    crash_window_s=600; crash_max=3; monitor_interval_s=1.0; auto_exit_idle_s=1800; schedule_lookahead_s=3600
class Supervisor:
    def __init__(self, paths, runtime, *, config=None, resolver=None, job=None, clock=None, now=None)
    async def acquire(service, owner, model_alias=None, wait=True, timeout_s=7200) -> dict   # {endpoint, model_alias}
    async def release(service, owner) -> bool
    async def stop_all() -> None; async def shutdown(grace_s) -> None; async def warmup(service) -> str
    def status() -> dict; def publish(kind, data); def subscribe() -> Queue
    async def run_monitor() -> None; exit_requested: asyncio.Event
```

- [ ] Tests against stub processes with injected short timers: acquire starts and returns the endpoint; second owner blocks until the first releases (serialization); tenant swap terminates the resident service (llama to tts, tts to whisper); idle unload after release; embed never locks and refcounts owners; crash then restart on next acquire; three crashes inside the window gives state `error` and `NS-E202`; health timeout gives `NS-E203` and kills the child; lock timeout gives `NS-E201`; two-model mode swap on alias change; `stop_all` clears errors; auto-exit decision with and without a schedule within 60 minutes; events published on state changes; state file written with child pids.
- [ ] Implement `core.py`, `schedule_cfg.py`.
- [ ] Commit `feat(c2): supervisor core (lock, swap, health, crash policy, idle, auto-exit)`.

### Task 5: FastAPI app and client (C2, `sonnet`)

**Files:** Create `ns_supervisor/app.py`, `ns_supervisor/client.py`, `tests/test_supervisor_app.py`, `tests/test_supervisor_client.py`; append CR for `fastapi`/`uvicorn`.

- [ ] App tests (`pytest.importorskip("fastapi")`): `/status`, `/acquire`, `/release`, `/stop_all`, `/warmup`, `/shutdown`, `/events?limit=1`, error mapping (E201 409, E202 502, E203 504).
- [ ] Client tests with `httpx.MockTransport` (no FastAPI needed): request bodies, `Endpoint` mapping, error body to `NsError`, `ensure_running` fast path.
- [ ] Implement both; `python -m ns_supervisor.app [--headless]`.
- [ ] Commit `feat(c2): FastAPI supervisor app and SupervisorClient`; push; read CI.

### Task 6: CLI skeleton, `ns stop`, `ns gate`, `ns schedule`, `ns notify` (C2, `sonnet`)

**Files:** Modify `ns_supervisor/cli.py`; create `schedule.py`, `notify.py`, `tests/test_ns_cli.py`, `tests/test_ns_schedule.py`.

- [ ] Tests: `--help` lists every command; bare `ns` prints the version (existing scaffold test); `ns stop` falls back to the recorded pid file when the supervisor is down; schedule script text carries `WakeToRun`, `StartWhenAvailable`, 6 h limit, S4U, the time; remove and show; notify passes title/message through the environment and is a no-op without PowerShell.
- [ ] Implement.
- [ ] Commit `feat(c2): ns stop, gate, schedule and notify`.

### Task 7: `ns verify` (C2, `sonnet`)

**Files:** Create `ns_supervisor/verify.py`, `tests/test_ns_verify.py`.

- [ ] Tests: report validates as `GateReport`; every check present and failing with an NS code and hint when components are missing; benchmark checks carry the 14.6 limits; `--quick` skips benchmarks; full flow against stub services through `SyncSupervisor`.
- [ ] Implement checks: supervisor health per service, LLM JSON completion + tok/s, TTS per voice mode + RTF, whisper load (studio venv subprocess), embed vector, NVENC probe, idle VRAM, optional fake-services job. Write `logs\verify-report.json`.
- [ ] Commit `feat(c2): ns verify with 14.6 benchmark table`.

### Task 8: `ns doctor [--bundle]` (C2, `sonnet`)

**Files:** Create `ns_supervisor/doctor.py`, `tests/test_ns_doctor.py`.

- [ ] Tests: bundle contains logs, install_state, runtime, verify-report, nvidia-smi output, pip freeze per present venv (skipped when absent), last 3 job dirs without media, VERSION; no media.
- [ ] Implement; default output `Desktop\nightshift-diagnostics-<date>.zip` (registry Desktop path, `--out-dir` override).
- [ ] Commit `feat(c2): ns doctor and diagnostics bundle`; push; read CI.

### Task 9: `ns tune` (C2, `sonnet`)

**Files:** Create `ns_supervisor/tune.py`, `tests/test_ns_tune.py`.

- [ ] Tests: staged matrix (ngl step-down, ctx x KV, MoE `--n-cpu-moe`, MTP); `--dry-run` prints the matrix and writes nothing; winner selection (within 5 % prefers larger ctx then f16); all-fail keeps `runtime.json`; real runner against the stub `/completion` timings.
- [ ] Implement; sweep log `logs\tune-<date>.json`; MTP kept only if faster (`NS-W302` otherwise).
- [ ] Commit `feat(c2): ns tune sweep`.

### Task 10: `ns update`, `ns backup`, `ns restore` (C2, `sonnet`)

**Files:** Create `ns_supervisor/update.py`, `ns_supervisor/backup.py`, `tests/test_ns_update.py`, `tests/test_ns_backup.py`.

- [ ] Tests (temp directory trees): VERSION and `.sha256` verification (`NS-E701`), zip-slip rejection, extract to `<app>.new`, preserved `.git`/`config`/untracked, swap with `<app>.prev`, step failure rolls back (`NS-E702`), `--rollback`; backup contents and exclusions, DB snapshot, restore skipping machine files.
- [ ] Implement.
- [ ] Commit `feat(c2): ns update with rollback, backup and restore`; push; read CI.

### Task 11: Launchers (C2, `sonnet`)

**Files:** Create `launchers/*.ps1`, `tests/test_launchers.py`.

- [ ] Test: parse each script with `[System.Management.Automation.Language.Parser]::ParseFile` via `powershell` (skip if absent); head lines, ASCII, no `Invoke-Expression`.
- [ ] Write the scripts.
- [ ] Commit `feat(c2): PowerShell launchers`.

### Task 12: Close-out (C2, `sonnet`)

- [ ] Append CRs (`fastapi`/`uvicorn` in `dev`, `POST /events`, missing error codes) to `contracts/CHANGE_REQUESTS.md`.
- [ ] CI green on the branch; `gh pr create --fill`; report with run URLs.
