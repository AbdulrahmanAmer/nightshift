"""C2 service table: runtime.json to command lines, ports, health URLs and GPU flags (spec 6.1).

`whisper` is virtual (no process): the supervisor only hands out the GPU lock for it, so it is not
part of this table.
"""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ns_common import devmode
from ns_common.atomic import read_text_retry, write_text_atomic
from ns_common.config import embed_server_args
from ns_common.errors import NsError
from ns_common.models import LlamaFlags, Runtime
from ns_common.paths import Paths
from ns_supervisor.childenv import child_env, window_flags

SERVICES: tuple[str, ...] = ("llama", "embed", "tts", "dashboard")
DASHBOARD_PORT = 8100

# ---- llama.cpp's own fitting (card vram-spill; plan 2026-11-12 rule 2, revision 2). Measured on the GPU PC
# (build 12032): any `-ngl` switches llama.cpp's fit off ("n_gpu_layers already set by user to 99, abort") and
# the driver then spilled; without it fit keeps the whole model on the card when it fits with `--fit-target`
# free, else moves expert tensors to pinned RAM itself. `--load-mode none` keeps the PC's RAM (working set
# 13.7 GB with mmap, 0.8-1.7 GB without); `--cache-ram` caps the prompt cache (8192 MiB by default).
#: The GPU layer count that means "llama.cpp places the layers" (rule 2): 99, the value setup and `ns tune`
#: have always written for "every layer". It is never passed as `-ngl` on a GPU PC whose llama-server has
#: `--fit`; any other value is a choice someone made on purpose and is passed as before (fit then stays off).
#: Kept as an int, not None: runtime.json keeps the format 0.1.14 reads (a rollback needs no rewrite) and
#: the schema in contracts/ is unchanged. The one place that decides: `layers_automatic`.
AUTO_LAYERS = 99
#: The card memory llama.cpp's fit leaves free (its own default, said explicitly), and the once-only retry's.
FIT_TARGET_MB = 1024
FIT_RETRY_TARGET_MB = 2048
#: The prompt cache llama-server keeps in RAM (plan: 2048 MiB instead of its 8192).
CACHE_RAM_MB = 2048
#: llama.cpp prints its fit lines only at `-lv 4` (debug; GPU PC evidence 15: none at the default or at 3).
#: Not passed: what debug level writes per request is not measured, and the engine measures the card itself
#: after `ready` (`fitlog` reads the lines when a build prints them).
#: The flags looked for in `llama-server --help` (once per binary: `llama_caps`).
CAPS_PROBE = ("--fit", "--fit-target", "--load-mode", "--cache-ram")
CAPS_FILE = "llama-caps.json"
HELP_TIMEOUT_S = 20.0
_caps: dict[tuple[str, int, int], frozenset[str]] = {}


def _run_help(exe: Path) -> str:
    """`llama-server --help`'s text ("" when it does not run). The seam tests replace."""
    try:
        r = subprocess.run([str(exe), "--help"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=HELP_TIMEOUT_S, check=False, env=child_env(),
                           creationflags=window_flags())  # fmt: skip
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return (r.stdout or "") + "\n" + (r.stderr or "")


def caps_in(text: str) -> frozenset[str]:
    """Which of `CAPS_PROBE` a `--help` text lists (`--fit` is not `--fit-target`)."""
    return frozenset(f for f in CAPS_PROBE if re.search(rf"(^|[\s,]){re.escape(f)}(?=[\s,=]|$)", text, re.M))


def llama_caps(paths: Paths, exe: Path) -> frozenset[str]:
    """Which of `CAPS_PROBE` this llama-server build has, from its `--help`: once per binary (path, size,
    modified time) in this process, and kept in `run/llama-caps.json` for the next. A missing binary or a help
    that does not run: none, so the command stays as it was before fit (older builds), and nothing is kept
    (the next start asks again)."""
    try:
        st = exe.stat()
    except OSError:
        return frozenset()
    key = (str(exe), st.st_size, st.st_mtime_ns)
    if key in _caps:
        return _caps[key]
    file = paths.home / "run" / CAPS_FILE
    try:
        kept = json.loads(read_text_retry(file, "utf-8-sig"))
        if isinstance(kept, dict) and [kept.get("exe"), kept.get("size"), kept.get("mtime_ns")] == list(key):
            caps = frozenset(c for c in kept.get("caps") or [] if c in CAPS_PROBE)
            _caps[key] = caps
            return caps
    except (OSError, ValueError):
        pass
    text = _run_help(exe)
    if not text.strip():
        return frozenset()
    caps = caps_in(text)
    _caps[key] = caps
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(file, json.dumps({"exe": key[0], "size": key[1], "mtime_ns": key[2],
                                            "caps": sorted(caps)}) + "\n")  # fmt: skip
    except OSError:
        pass  # asked again by the next engine start
    return caps


def layers_automatic(rt: Runtime, flags: LlamaFlags | None = None) -> bool:
    """llama.cpp places the layers (no `-ngl`): a GPU PC whose stored count is `AUTO_LAYERS`."""
    fl = flags or rt.llama.flags
    return rt.profile == "gpu" and not rt.dev_mode and fl.n_gpu_layers == AUTO_LAYERS


@dataclass(frozen=True)
class ServiceDef:
    """How to run one service: command, address, health probe and whether it takes the GPU."""

    name: str
    cmd: list[str]
    host: str
    port: int
    gpu: bool
    health_path: str = "/health"
    variant: str = "default"
    model_alias: str | None = None
    persistent: bool = False
    env: dict[str, str] = field(default_factory=dict)
    step: str = ""  # llama's ladder step (`llama_ladder`): "tuned", "mtp_off" ("" for anything else)

    @property
    def base_url(self) -> str:
        """Loopback base URL of the service."""
        return f"http://{self.host}:{self.port}"

    @property
    def health_url(self) -> str:
        """URL polled by the supervisor until it answers 200."""
        return f"{self.base_url}{self.health_path}"


def fake_gpu(rt: Runtime) -> bool:
    """The simulated graphics card is in effect for these settings (`devmode.fake_gpu_in`: the switch on a dev
    or engine-test install). Then llama, the voice service and whisper are GPU tenants, as on the GPU PC; the
    embedding server stays off the card, as it is there (`CUDA_VISIBLE_DEVICES=-1`)."""
    return devmode.fake_gpu_in(rt.model_dump(mode="json"))


def llama_variant(rt: Runtime, alias: str | None) -> str:
    """Process variant: `default` in one-model mode, else the logical alias (`code` or `story`)."""
    if rt.llama.story_model_path:
        return alias or "story"
    return "default"


def venv_python(paths: Paths, venv: str) -> str:
    """Python of an installer venv; falls back to the running interpreter when it does not exist."""
    exe = paths.venvs / venv / "Scripts" / "python.exe"
    return str(exe) if exe.exists() else sys.executable


def _env(paths: Paths) -> dict[str, str]:
    return {"NIGHTSHIFT_HOME": str(paths.home)}


def _dev_cmd(paths: Paths, service: str, port: int, host: str) -> list[str]:
    """Command of a dev install's stand-in for llama-server and the embedding server (`devfake`)."""
    return [venv_python(paths, "supervisor"), "-m", "ns_supervisor.devfake", "--service", service,
            "--port", str(port), "--host", host]  # fmt: skip


def _llama(
    rt: Runtime, paths: Paths, alias: str | None, *, mtp: bool = True, fit_target: int = FIT_TARGET_MB
) -> ServiceDef:
    """The llama command. On a GPU PC with automatic layers (`layers_automatic`) and a build with `--fit`:
    no `-ngl` and no `--n-cpu-moe` (llama.cpp's fit places the model, experts to RAM only when the card
    cannot hold it), `--fit on --fit-target <fit_target>`. `--load-mode none` and `--cache-ram` where the
    build lists them. A build without `--fit`, the processor profile, or a layer count set on purpose:
    `-ngl` and `--n-cpu-moe` as stored (today's command). `mtp` False: the ladder's MTP-off step."""
    cfg = rt.llama
    fl = cfg.flags
    story = cfg.story_model_path
    two_model = bool(story)
    eff = alias or "story"
    model = story if (story and eff == "story") else cfg.model_path
    aliases = eff if two_model else ",".join(cfg.aliases)
    exe = paths.bin_llama / "llama-server.exe"
    caps = frozenset() if rt.dev_mode else llama_caps(paths, exe)
    fit = "--fit" in caps and layers_automatic(rt)
    cmd = [str(exe), "-m", str(model), "--port", str(cfg.port), "--host", cfg.host]
    if not fit:  # an older build, the processor profile, or a count chosen on purpose: passed as before
        cmd += ["-ngl", str(fl.n_gpu_layers)]
    cmd += ["-c", str(fl.ctx), "--alias", aliases]
    if fit:
        cmd += ["--fit", "on"]
        if "--fit-target" in caps:
            cmd += ["--fit-target", str(fit_target)]
    if "--load-mode" in caps:
        cmd += ["--load-mode", "none"]  # no mmap: the weights are not held in RAM beside the card's copy
    if "--cache-ram" in caps:
        cmd += ["--cache-ram", str(CACHE_RAM_MB)]
    if fl.kv_type != "f16":  # (f16 is llama.cpp's default)
        cmd += ["-ctk", fl.kv_type, "-ctv", fl.kv_type]
    if fl.n_cpu_moe > 0 and not fit:  # with fit, llama.cpp decides the experts (a fixed count turns it off)
        cmd += ["--n-cpu-moe", str(fl.n_cpu_moe)]
    if fl.spec_type and mtp:
        cmd += ["--spec-type", fl.spec_type, "--spec-draft-n-max", str(fl.spec_draft_n_max or 3)]
    # One slot, always: the GPU lock serialises every request anyway, and a build that gives each of N slots
    # ctx/N tokens would fail the Assistant's 9-12k token prompts and whole-story prompts (NS-E301).
    cmd += ["-np", "1"]
    if fl.threads:
        cmd += ["-t", str(fl.threads)]
    if rt.dev_mode:     # docs/DEV_MODE.md: no llama-server, no model, no GPU
        cmd = _dev_cmd(paths, "llama", cfg.port, cfg.host)
    return ServiceDef(
        name="llama",
        cmd=cmd,
        host=cfg.host,
        port=cfg.port,
        gpu=(fl.n_gpu_layers != 0 and not rt.dev_mode) or fake_gpu(rt),
        variant=llama_variant(rt, alias),
        model_alias=aliases,
        env=_env(paths),
        step="tuned" if mtp or not fl.spec_type else "mtp_off",
    )


def fitted(d: ServiceDef) -> bool:
    """llama.cpp's own fit places this load (`--fit on`)."""
    return d.name == "llama" and "--fit" in d.cmd


def mtp_on(d: ServiceDef) -> bool:
    return "--spec-type" in d.cmd


#: The MTP draft's flags and how many values each takes (`_llama`), dropped together by the MTP-off step.
MTP_FLAGS = {"--spec-type": 1, "--spec-draft-n-max": 1}


def ladder_of(d: ServiceDef) -> list[ServiceDef]:
    """Rule 2's settings for a fitted load, in order: `d` as tuned, then (when tuned with the MTP draft) the
    same without it. Step (c), the tuned settings with fit moving experts to RAM, is the first entry again:
    the choice is `vram.choose`'s. One entry when fit does not place the load (nothing to step down)."""
    if not fitted(d) or not mtp_on(d):
        return [d]
    cmd: list[str] = []
    skip = 0
    for a in d.cmd:
        if skip:
            skip -= 1
        elif a in MTP_FLAGS:
            skip = MTP_FLAGS[a]
        else:
            cmd.append(a)
    return [d, dataclasses.replace(d, cmd=cmd, step="mtp_off")]


def llama_ladder(rt: Runtime, paths: Paths, alias: str | None = None) -> list[ServiceDef]:
    """`ladder_of` llama's definition for `rt` (the MTP-off entry is what `_llama(..., mtp=False)` builds)."""
    return ladder_of(_llama(rt, paths, alias))


def with_fit_target(d: ServiceDef, mb: int) -> ServiceDef:
    """`d` with another `--fit-target` (the once-only retry after a spill); `d` itself when it has none."""
    cmd = list(d.cmd)
    if "--fit-target" not in cmd:
        return d
    cmd[cmd.index("--fit-target") + 1] = str(mb)
    return dataclasses.replace(d, cmd=cmd)


def _embed(rt: Runtime, paths: Paths) -> ServiceDef:
    cfg = rt.embed
    cmd = [
        str(paths.bin_llama / "llama-server.exe"),
        "-m", cfg.model_path,
        "--embeddings",
        "--port", str(cfg.port),
        "--host", cfg.host,
        "-ngl", "0",
    ] + embed_server_args(cfg)  # fmt: skip
    if rt.dev_mode:
        cmd = _dev_cmd(paths, "embed", cfg.port, cfg.host)
    # The binary is the CUDA build of llama-server. `-ngl 0` alone still creates a CUDA context and may put
    # large-batch compute buffers on the card, a few hundred MB beside the 14 GB story model. No visible
    # device: the embeddings run on the CPU, at the speed they had.
    env = {**_env(paths), "CUDA_VISIBLE_DEVICES": "-1"}
    return ServiceDef(name="embed", cmd=cmd, host=cfg.host, port=cfg.port, gpu=False, env=env)


def _tts(rt: Runtime, paths: Paths) -> ServiceDef:
    cfg = rt.tts
    # The PyTorch fallback imports torch and qwen_tts, which exist only in the `tts` venv (installer
    # fallback.py); the qwentts engine needs just numpy and FastAPI and runs in the supervisor venv.
    venv = "tts" if cfg.engine == "reference" else "supervisor"
    cmd = [venv_python(paths, venv), "-m", "ns_tts.server", "--port", str(cfg.port)]
    # The real server with its fake engine (tone-burst narration, voices, design and clone): a dev install, or
    # an engine test (`tts.engine: fake`, docs/DEV_MODE.md).
    fake = rt.dev_mode or cfg.engine == "fake"
    if fake:
        cmd = [venv_python(paths, "supervisor"), "-m", "ns_tts.server", "--port", str(cfg.port),
               "--engine", "fake"]  # fmt: skip
    return ServiceDef(
        name="tts",
        cmd=cmd,
        host=cfg.host,
        port=cfg.port,
        gpu=(cfg.device == "cuda" and not fake) or fake_gpu(rt),
        env=_env(paths),
    )


def _dashboard(paths: Paths) -> ServiceDef:
    cmd = [venv_python(paths, "supervisor"), "-m", "ns_dashboard.server"]
    return ServiceDef(
        name="dashboard",
        cmd=cmd,
        host="127.0.0.1",
        port=DASHBOARD_PORT,
        health_path="/api/health",     # the dashboard's routes all live under /api (first dev-mode launch)
        gpu=False,
        persistent=True,
        env=_env(paths),
    )


def build_service(rt: Runtime, paths: Paths, name: str, alias: str | None = None) -> ServiceDef:
    """Service definition for `name` (llama honours `alias` in two-model mode)."""
    if name == "llama":
        return _llama(rt, paths, alias)
    if name == "embed":
        return _embed(rt, paths)
    if name == "tts":
        return _tts(rt, paths)
    if name == "dashboard":
        return _dashboard(paths)
    raise NsError("NS-E202", f"unknown service {name!r}")


def build_service_table(rt: Runtime, paths: Paths) -> dict[str, ServiceDef]:
    """All process-backed services keyed by name."""
    return {name: build_service(rt, paths, name) for name in SERVICES}
