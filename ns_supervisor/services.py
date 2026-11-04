"""C2 service table: runtime.json to command lines, ports, health URLs and GPU flags (spec 6.1).

`whisper` is virtual (no process): the supervisor only hands out the GPU lock for it, so it is not
part of this table.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

from ns_common.config import embed_server_args
from ns_common.errors import NsError
from ns_common.models import Runtime
from ns_common.paths import Paths

SERVICES: tuple[str, ...] = ("llama", "embed", "tts", "dashboard")
DASHBOARD_PORT = 8100


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

    @property
    def base_url(self) -> str:
        """Loopback base URL of the service."""
        return f"http://{self.host}:{self.port}"

    @property
    def health_url(self) -> str:
        """URL polled by the supervisor until it answers 200."""
        return f"{self.base_url}{self.health_path}"


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


def _llama(rt: Runtime, paths: Paths, alias: str | None) -> ServiceDef:
    cfg = rt.llama
    fl = cfg.flags
    story = cfg.story_model_path
    two_model = bool(story)
    eff = alias or "story"
    model = story if (story and eff == "story") else cfg.model_path
    aliases = eff if two_model else ",".join(cfg.aliases)
    cmd = [
        str(paths.bin_llama / "llama-server.exe"),
        "-m", str(model),
        "--port", str(cfg.port),
        "--host", cfg.host,
        "-ngl", str(fl.n_gpu_layers),
        "-c", str(fl.ctx),
        "--alias", aliases,
    ]  # fmt: skip
    if fl.kv_type != "f16":
        cmd += ["-ctk", fl.kv_type, "-ctv", fl.kv_type]
    if fl.n_cpu_moe > 0:
        cmd += ["--n-cpu-moe", str(fl.n_cpu_moe)]
    if fl.spec_type:
        cmd += ["--spec-type", fl.spec_type, "--spec-draft-n-max", str(fl.spec_draft_n_max or 3), "-np", "1"]
    if fl.threads:
        cmd += ["-t", str(fl.threads)]
    if rt.dev_mode:     # docs/DEV_MODE.md: no llama-server, no model, no GPU
        cmd = _dev_cmd(paths, "llama", cfg.port, cfg.host)
    return ServiceDef(
        name="llama",
        cmd=cmd,
        host=cfg.host,
        port=cfg.port,
        gpu=fl.n_gpu_layers != 0 and not rt.dev_mode,
        variant=llama_variant(rt, alias),
        model_alias=aliases,
        env=_env(paths),
    )


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
    return ServiceDef(name="embed", cmd=cmd, host=cfg.host, port=cfg.port, gpu=False, env=_env(paths))


def _tts(rt: Runtime, paths: Paths) -> ServiceDef:
    cfg = rt.tts
    # The PyTorch fallback imports torch and qwen_tts, which exist only in the `tts` venv (installer
    # fallback.py); the qwentts engine needs just numpy and FastAPI and runs in the supervisor venv.
    venv = "tts" if cfg.engine == "reference" else "supervisor"
    cmd = [venv_python(paths, venv), "-m", "ns_tts.server", "--port", str(cfg.port)]
    if rt.dev_mode:     # the real server with its fake engine: tone-burst narration, voices, design and clone
        cmd = [venv_python(paths, "supervisor"), "-m", "ns_tts.server", "--port", str(cfg.port),
               "--engine", "fake"]  # fmt: skip
    return ServiceDef(
        name="tts", cmd=cmd, host=cfg.host, port=cfg.port, gpu=cfg.device == "cuda" and not rt.dev_mode,
        env=_env(paths),
    )


def _dashboard(paths: Paths) -> ServiceDef:
    cmd = [venv_python(paths, "supervisor"), "-m", "ns_dashboard.server"]
    return ServiceDef(
        name="dashboard",
        cmd=cmd,
        host="127.0.0.1",
        port=DASHBOARD_PORT,
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
