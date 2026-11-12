"""One-time move of an existing GPU install's llama settings to llama.cpp's own fit (plan 2026-11-12, R1).

What an install of 0.1.15-rc7 or older stored: `n_gpu_layers` 99 (setup's and `ns tune`'s "every layer"),
`ctx` 32768 or 16384, `kv_type` f16 or q8_0, `n_cpu_moe` 0, 8 or 12 (setup's tier values, or tune's). Under
the rule those are: layers placed by llama.cpp (`services.AUTO_LAYERS`), a 16384-token context (every use
fits: `tune.min_ctx_for`), q8_0 KV, no fixed expert count. The MTP draft and the threads stay as tuned.

How a layer count set on purpose is told from one Nightshift wrote: 99 is what setup and tune write for "every
layer" and stays 99 (now meaning "llama.cpp places them"). A smaller count is Nightshift's only when it is the
winner of the newest `ns tune` log (`logs/tune-*.json`, tune's stage 1 stepping down from 99): then it becomes
99 too. Any other count was set by hand (`ns config`, an edit) and is kept: `services._llama` passes it as
`-ngl`, so fit stays off for that install as its owner chose.

Once: the marker `run/migrated-<MIGRATION>` is written after the file was saved (or found already right), and
the move is never made again, so a context the owner sets later is his. Only on the GPU profile, not on a dev
install, and only when the installed llama-server has `--fit` (an older build keeps its settings and the old
command). Never raises: a failure is logged and the engine starts on the file as it was.
"""

from __future__ import annotations

import json
from pathlib import Path

from ns_common.config import save_runtime
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_supervisor import services

#: The version that brought the move: the marker's name, fixed (a later version never moves again).
MIGRATION = "0.1.15-fit"
FIT_CTX = 16384
FIT_KV = "q8_0"


def marker(paths: Paths) -> Path:
    return paths.home / "run" / f"migrated-{MIGRATION}"


def _tune_winner_layers(paths: Paths) -> int | None:
    """`n_gpu_layers` of the newest `ns tune` log's winner (None: no log, or it does not read)."""
    try:
        logs = sorted(paths.logs.glob("tune-*.json"))
        if not logs:
            return None
        data = json.loads(logs[-1].read_text(encoding="utf-8"))
        winner = data.get("winner") if isinstance(data, dict) else None
        n = winner.get("n_gpu_layers") if isinstance(winner, dict) else None
        return n if isinstance(n, int) and not isinstance(n, bool) else None
    except (OSError, ValueError):
        return None


def moved(paths: Paths, rt: Runtime) -> tuple[Runtime, list[str]]:
    """`rt` with the move applied, and one entry per change (`ctx 32768 -> 16384`). Pure: writes nothing."""
    new = rt.model_copy(deep=True)
    fl = new.llama.flags
    said: list[str] = []
    if fl.n_gpu_layers != services.AUTO_LAYERS and fl.n_gpu_layers == _tune_winner_layers(paths):
        said.append(f"n_gpu_layers {fl.n_gpu_layers} (ns tune's) -> automatic")
        fl.n_gpu_layers = services.AUTO_LAYERS
    if fl.ctx != FIT_CTX:
        said.append(f"ctx {fl.ctx} -> {FIT_CTX}")
        fl.ctx = FIT_CTX
    if fl.kv_type != FIT_KV:
        said.append(f"kv_type {fl.kv_type} -> {FIT_KV}")
        fl.kv_type = FIT_KV
    if fl.n_cpu_moe != 0:
        said.append(f"n_cpu_moe {fl.n_cpu_moe} -> 0 (llama.cpp moves experts itself)")
        fl.n_cpu_moe = 0
    return new, said


def migrate(paths: Paths, rt: Runtime, log: NsLogger | None = None) -> Runtime:
    """Apply the move once (see the module). Returns the settings the engine runs with."""
    lg = log or NsLogger("supervisor", paths.logs)
    try:
        if rt.profile != "gpu" or rt.dev_mode or marker(paths).exists():
            return rt
        caps = services.llama_caps(paths, paths.bin_llama / "llama-server.exe")
        if "--fit" not in caps:
            return rt  # an older build (or none yet): asked again at the next start
        new, said = moved(paths, rt)
        if said:
            save_runtime(paths, new)
            lg.info("llama_fit_migrated", "runtime.json: " + "; ".join(said), changes=said)
        marker(paths).parent.mkdir(parents=True, exist_ok=True)
        marker(paths).write_text("; ".join(said) or "nothing to change", encoding="utf-8")
        return new
    except (OSError, ValueError, NsError) as e:  # the file as it was; the next start tries again
        lg.warn("llama_fit_migrate_failed", str(e))
        return rt
