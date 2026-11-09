"""C2 `ns verify [--quick]`: health, completion, TTS, whisper, embed, NVENC, idle VRAM, benchmarks.

Writes `logs\\verify-report.json` shaped like `GateReport` (spec 15.6) with the spec 14.6 table as
`bench.*` checks. Every check is tolerant: a missing component becomes a failed check carrying the
NS code and fix hint, never an exception.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from ns_common import __version__, ffmpeg
from ns_common.acquire import Endpoint, Service
from ns_common.embed_client import EmbedClient
from ns_common.errors import NsError
from ns_common.llm_client import LlmClient
from ns_common.logging import NsLogger
from ns_common.models import GateCheck, GateReport, Runtime
from ns_common.paths import Paths
from ns_common.tts_client import TtsClient
from ns_common.tts_timeout import save_rtf
from ns_supervisor import check_names as names
from ns_supervisor import procs
from ns_supervisor.benchmark import measure_tok_s
from ns_supervisor.childenv import child_env

OWNER = "verify"
TTS_SENTENCE = "Nightshift verification sentence. The quick brown fox jumps over the lazy dog."
NOT_MEASURED = "short_job_s,master_job_s,nightly_run_s"


class Controller(Protocol):
    """What verify needs from the supervisor (`SupervisorClient` and the test `SyncSupervisor`)."""

    def acquire(
        self, service: Service, owner: str, *, model_alias: str | None = None, timeout_s: int = 7200
    ) -> Endpoint: ...

    def release(self, service: Service, owner: str) -> None: ...

    def status(self) -> dict[str, Any]: ...

    def stop_all(self) -> None: ...


@dataclass(frozen=True)
class Targets:
    """Spec 14.6 targets for the detected profile (32 GB column, or the 16 GB bracket)."""

    tok_s: float
    load_s: float
    rtf: float
    short_job_s: float
    idle_vram_mb: float
    profile: str


def targets_for(ram_gb: float | None) -> Targets:
    """32 GB column when RAM >= 28 GB, the bracketed 16 GB column otherwise (unknown RAM: 16 GB)."""
    if ram_gb is not None and ram_gb >= 28:
        return Targets(40, 30, 0.5, 180, 300, "32gb")
    return Targets(30, 30, 0.8, 300, 300, "16gb")


def ram_gb() -> float | None:
    """Installed RAM in GB (Windows `GlobalMemoryStatusEx`); None when unavailable."""
    if sys.platform != "win32":
        return None
    import ctypes

    class _Mem(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    m = _Mem()
    m.dwLength = ctypes.sizeof(_Mem)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        return None
    return round(m.ullTotalPhys / 1024**3, 1)


# --------------------------------------------------------------------------------------------
# Probes that touch the machine (replaced in tests)
# --------------------------------------------------------------------------------------------
WHISPER_SNIPPET = """
import json, sys, time
try:
    from ns_common import gpu_dlls
    gpu_dlls.ensure_on_path()
except ImportError:
    pass
from faster_whisper import WhisperModel
t = time.perf_counter()
WhisperModel(sys.argv[1], device=sys.argv[2], compute_type=sys.argv[3], local_files_only=True)
print(json.dumps({"ok": True, "load_s": round(time.perf_counter() - t, 2)}))
"""


def whisper_probe(paths: Paths, rt: Runtime) -> float:
    """Load the whisper model in the studio venv (the process that will use it); returns load seconds."""
    py = paths.venvs / "studio" / "Scripts" / "python.exe"
    if not py.exists():
        raise NsError("NS-E202", f"studio venv missing: {py}")
    local = paths.models_whisper / rt.whisper.model
    model = str(local) if local.is_dir() else rt.whisper.model
    cmd = [str(py), "-c", WHISPER_SNIPPET, model, rt.whisper.device, rt.whisper.compute_type]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False, env=child_env())
    except (OSError, subprocess.SubprocessError) as e:
        raise NsError("NS-E202", f"whisper probe could not run: {e}") from e
    if r.returncode != 0:
        tail = " | ".join((r.stderr or r.stdout).strip().splitlines()[-3:])
        low = tail.lower()
        code = "NS-E202"
        if "cublas" in low or "cudnn" in low or "dll" in low:
            code = "NS-E112"
        raise NsError(code, f"whisper failed to load on {rt.whisper.device}: {tail}")
    try:
        return float(json.loads(r.stdout.strip().splitlines()[-1])["load_s"])
    except (ValueError, KeyError, IndexError) as e:
        raise NsError("NS-E202", f"whisper probe gave no result: {r.stdout[-200:]}") from e


def nvenc_probe() -> None:
    """Encode a 2 second colour source with `h264_nvenc`. Raises `NS-W401` when NVENC is unusable."""
    if not ffmpeg.available():
        raise NsError("NS-E401", "ffmpeg/ffprobe not found on PATH")
    try:
        ffmpeg.run(
            [
                "-f", "lavfi", "-i", "color=c=0x2060a0:s=1280x720:r=30:d=2",
                "-c:v", "h264_nvenc", "-f", "null", "-",
            ],  # fmt: skip
            timeout_s=120,
        )
    except NsError as e:
        raise NsError("NS-W401", e.detail) from e


def x264_probe() -> None:
    """Encode a 2 second colour source with `libx264` (a dev install: no NVENC, the CPU encoder renders)."""
    if not ffmpeg.available():
        raise NsError("NS-E401", "ffmpeg/ffprobe not found on PATH")
    ffmpeg.run(
        [
            "-f", "lavfi", "-i", "color=c=0x2060a0:s=1280x720:r=30:d=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-f", "null", "-",
        ],  # fmt: skip
        timeout_s=120,
    )


def fake_job(paths: Paths) -> float | None:
    """`studio run --fake-services --format shorts_only --count 1`; seconds, or None if studio is absent."""
    exe = paths.venvs / "studio" / "Scripts" / "studio.exe"
    studio = str(exe) if exe.exists() else shutil.which("studio")
    if studio is None:
        return None
    t0 = time.perf_counter()
    try:
        r = subprocess.run(
            [studio, "run", "--fake-services", "--format", "shorts_only", "--count", "1"],
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
            env=child_env(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise NsError("NS-E202", f"fake-services job could not run: {e}") from e
    if r.returncode != 0:
        tail = " | ".join((r.stderr or r.stdout).strip().splitlines()[-3:])
        raise NsError("NS-E202", f"fake-services job exited {r.returncode}: {tail}")
    return round(time.perf_counter() - t0, 1)


def tts_engine_kind(base_url: str) -> str | None:
    """`engine_kind` from the voice service's /health (qwentts, reference, fake); None when it is absent."""
    import httpx

    try:
        body = httpx.get(f"{base_url}/health", timeout=10).json()
    except (httpx.HTTPError, ValueError):
        return None
    kind = body.get("engine_kind") if isinstance(body, dict) else None
    return str(kind) if kind else None


@dataclass
class Deps:
    """Machine probes, replaceable in tests."""

    whisper: Callable[[Paths, Runtime], float] = whisper_probe
    nvenc: Callable[[], None] = nvenc_probe
    x264: Callable[[], None] = x264_probe       # a dev install probes the CPU encoder instead of NVENC
    job: Callable[[Paths], float | None] = fake_job
    vram_used: Callable[[], int | None] = procs.gpu_memory_used_mb
    gpu_name: Callable[[], str | None] = procs.gpu_name
    ram: Callable[[], float | None] = ram_gb
    tts_engine: Callable[[str], str | None] = tts_engine_kind
    sleep: Callable[[float], None] = time.sleep


@dataclass
class Out:
    """Result of one check body."""

    passed: bool
    value: float | str | None = None
    limit: str | None = None
    log: str | None = None
    data: Any = None


def explain(e: NsError) -> str:
    """`NS-E203: message (detail) | Hint: ...`"""
    return f"{e} | Hint: {e.definition.hint}"


@dataclass
class _Recorder:
    log: NsLogger
    checks: list[GateCheck] = field(default_factory=list)
    benchmarks: dict[str, float] = field(default_factory=dict)

    def run(self, name: str, fn: Callable[[], Out], code: str = "NS-E202") -> Out | None:
        """Run a check body; a raised error becomes a failed check. Returns the Out when it ran."""
        try:
            out = fn()
        except NsError as e:
            self.checks.append(GateCheck(name=name, passed=False, log=explain(e)))
            self.log.warn("check_failed", name=name, code=e.code, detail=e.detail)
            return None
        except Exception as e:  # tolerant by design: any probe failure is reported, not raised
            wrapped = NsError(code, f"{type(e).__name__}: {e}")
            self.checks.append(GateCheck(name=name, passed=False, log=explain(wrapped)))
            self.log.error("check_error", name=name, error=str(e))
            return None
        self.checks.append(
            GateCheck(name=name, passed=out.passed, value=out.value, limit=out.limit, log=out.log)
        )
        self.log.info("check", name=name, passed=out.passed, value=out.value)
        return out

    def bench(self, key: str, value: float, limit: str, ok: bool, soft: bool = False) -> None:
        """Add a spec 14.6 row. `soft` rows are logged but never fail the report."""
        self.benchmarks[key] = value
        log = None if ok else ("over the soft limit; logged only" if soft else "target missed")
        self.checks.append(
            GateCheck(name=f"bench.{key}", passed=ok or soft, value=value, limit=limit, log=log)
        )


def _need(ep: Endpoint | None, service: str) -> Endpoint:
    if ep is None:
        raise NsError("NS-E202", f"{service} is not available (see the {service}.start check)")
    return ep


def run_verify(
    paths: Paths, rt: Runtime, ctl: Controller, *, quick: bool = False, deps: Deps | None = None
) -> GateReport:
    """Run all checks against the supervisor `ctl` and return the report (not yet written)."""
    d = deps or Deps()
    rec = _Recorder(NsLogger("verify", paths.logs))
    ram = d.ram()
    tg = targets_for(ram)

    def acquire(service: Service, alias: str | None = None) -> tuple[Endpoint, float]:
        t0 = time.perf_counter()
        ep = ctl.acquire(service, OWNER, model_alias=alias, timeout_s=600)
        return ep, time.perf_counter() - t0

    def release(service: Service) -> None:
        try:
            ctl.release(service, OWNER)
        except NsError as e:
            rec.log.warn("release_failed", service=service, detail=e.detail)

    # -- supervisor ------------------------------------------------------------------------
    def supervisor() -> Out:
        st = ctl.status()
        states = {n: s.get("state") for n, s in st.get("services", {}).items()}
        return Out(True, "up", log=json.dumps(states))

    def cold_start() -> Out:
        ctl.stop_all()
        return Out(True, "all services stopped", log="so the model load below is measured cold")

    rec.run(names.SUPERVISOR_STATUS, supervisor)
    if not quick:
        rec.run(names.SUPERVISOR_COLD_START, cold_start)

    # -- llama -----------------------------------------------------------------------------
    llama: list[Endpoint] = []

    def llama_start() -> Out:
        ep, secs = acquire("llama", "code")
        llama.append(ep)
        return Out(True, round(secs, 1), log=f"{ep.base_url} alias={ep.model_alias}")

    started = rec.run(names.LLAMA_START, llama_start, "NS-E203")
    ep_llama = llama[0] if llama else None
    if started is not None and not quick:
        secs = float(started.value or 0)
        rec.bench("model_load_s", secs, f"<= {tg.load_s:.0f} s (soft)", secs <= tg.load_s, soft=True)
    rec.run(names.LLAMA_HEALTH, lambda: Out(LlmClient(_need(ep_llama, "llama").base_url, "code").health()))

    def llama_json() -> Out:
        out = LlmClient(_need(ep_llama, "llama").base_url, "code").complete_json(
            prompt_name="verify",
            system="You answer with JSON only.",
            user='Reply with {"ok": true}.',
            schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
            temperature=0.0,
            max_tokens=64,
        )
        return Out(out.get("ok") is True, "json ok" if out.get("ok") is True else json.dumps(out))

    rec.run(names.LLAMA_JSON, llama_json, "NS-E301")

    if not quick:

        def llama_speed() -> Out:
            return Out(True, measure_tok_s(_need(ep_llama, "llama").base_url))

        sp = rec.run(names.LLAMA_TOK_S, llama_speed, "NS-E301")
        if sp is not None:
            v = float(sp.value or 0)
            rec.bench("code_tok_s", v, f">= {tg.tok_s:.0f} tok/s", v >= tg.tok_s)
    if ep_llama is not None:
        release("llama")

    # -- embed -----------------------------------------------------------------------------
    emb: list[Endpoint] = []

    def embed_start() -> Out:
        ep, secs = acquire("embed")
        emb.append(ep)
        return Out(True, round(secs, 1), log=ep.base_url)

    rec.run(names.EMBED_START, embed_start, "NS-E203")

    def embed_vec() -> Out:
        vecs = EmbedClient(_need(emb[0] if emb else None, "embed").base_url).embed(["nightshift verify"])
        ok = len(vecs) == 1 and len(vecs[0]) > 0
        return Out(ok, float(len(vecs[0])) if ok else 0.0, log="embedding dimension")

    rec.run(names.EMBED_VECTOR, embed_vec, "NS-E301")
    if emb:
        release("embed")

    # -- tts -------------------------------------------------------------------------------
    tts: list[Endpoint] = []

    def tts_start() -> Out:
        ep, secs = acquire("tts")
        tts.append(ep)
        if d.tts_engine(ep.base_url) == "reference":
            rec.checks.append(GateCheck(
                name=names.TTS_ENGINE, passed=True, value="reference",
                log="NS-W301: the voice service runs the slower PyTorch reference engine, not qwentts.cpp",
            ))
        return Out(True, round(secs, 1), log=ep.base_url)

    rec.run(names.TTS_START, tts_start, "NS-E203")

    def tts_modes() -> Out:
        client = TtsClient(_need(tts[0] if tts else None, "tts").base_url)
        voices = client.voices()
        by_mode: dict[str, str] = {}
        for v in voices:
            by_mode.setdefault(str(v.get("mode")), str(v.get("name")))
        if not by_mode:
            raise NsError("NS-E202", "tts reports no voices")
        order = ["preset", "design", "clone"]
        picked = [(m, by_mode[m]) for m in order if m in by_mode]
        picked = picked or list(by_mode.items())[:1]
        if quick:
            picked = picked[:1]
        rtfs: list[float] = []
        with tempfile.TemporaryDirectory(prefix="ns-verify-") as tmp:
            for mode, voice in picked:
                t0 = time.perf_counter()
                audio_s = client.speak(TTS_SENTENCE, voice, 1.0, Path(tmp) / f"{mode}.wav")
                rtf = (time.perf_counter() - t0) / audio_s if audio_s > 0 else float("inf")
                rtfs.append(rtf)
                ok = audio_s > 0 and (quick or rtf <= tg.rtf)
                rec.checks.append(
                    GateCheck(
                        name=names.tts_mode(mode),
                        passed=ok,
                        value=round(rtf, 3),
                        limit=f"<= {tg.rtf} RTF" if not quick else None,
                        log=f"voice={voice} audio_s={audio_s}",
                    )
                )
        worst = max(rtfs)
        save_rtf(paths, worst, quick=quick)  # the voice stage sizes its batch time limit from it (gap L4)
        if not quick:
            rec.bench("tts_rtf", round(worst, 3), f"<= {tg.rtf}", worst <= tg.rtf)
        return Out(True, ",".join(m for m, _ in picked), log="modes exercised")

    rec.run(names.TTS_VOICES, tts_modes, "NS-E202")
    if tts:
        release("tts")

    # A real install on a PC without an NVIDIA card (profile cpu-dev, answer `allow_cpu`) renders with libx264
    # and has no nvidia-smi: the encoder check runs on the processor and nothing measures video memory.
    no_card = rt.profile == "cpu-dev"

    # -- whisper ---------------------------------------------------------------------------
    def whisper() -> Out:
        ctl.acquire("whisper", OWNER, timeout_s=600)
        try:
            if rt.dev_mode:     # a dev install has no faster-whisper and no model (the studio fakes it)
                return Out(True, 0.0, log="dev mode: fake speech recognition, no model to load")
            secs = d.whisper(paths, rt)
        finally:
            release("whisper")
        return Out(True, round(secs, 1), log=f"{rt.whisper.model} on {rt.whisper.device}")

    rec.run(names.WHISPER_LOAD, whisper, "NS-E411")

    # -- NVENC -----------------------------------------------------------------------------
    def nvenc() -> Out:
        if rt.dev_mode or no_card:
            d.x264()
            why = "dev mode" if rt.dev_mode else "no graphics card (cpu-dev profile)"
            return Out(True, "libx264 ok", log=f"{why}: 2 s colour source encoded on the processor")
        d.nvenc()
        return Out(True, "h264_nvenc ok", log="2 s colour source encoded")

    rec.run(names.NVENC_PROBE, nvenc, "NS-W401")

    # -- fake-services job -----------------------------------------------------------------
    if not quick:

        def job() -> Out:
            secs = d.job(paths)
            if secs is None:
                return Out(True, "skipped", log="studio is not installed yet")
            return Out(True, secs, log="studio run --fake-services --format shorts_only --count 1")

        rec.run(names.JOB_FAKE_SERVICES, job, "NS-E202")

    # -- idle VRAM -------------------------------------------------------------------------
    def idle_vram() -> Out:
        if rt.dev_mode or no_card:
            why = "dev mode" if rt.dev_mode else "cpu-dev profile"
            return Out(True, "n/a", log=f"{why}: no graphics card to measure")
        try:
            ctl.stop_all()
        except NsError as e:  # no supervisor means nothing of ours is loaded; still measure the GPU
            rec.log.warn("stop_all_failed", e.detail)
        used: int | None = None
        for _ in range(10):
            used = d.vram_used()
            if used is None or used <= tg.idle_vram_mb:
                break
            d.sleep(0.5)
        if used is None:
            raise NsError("NS-E102", "nvidia-smi is not available")
        ok = used <= tg.idle_vram_mb
        rec.bench("idle_vram_mb", float(used), f"<= {tg.idle_vram_mb:.0f} MB", ok)
        return Out(ok, float(used), f"<= {tg.idle_vram_mb:.0f} MB", "GPU memory in use after unload")

    rec.run(names.VRAM_IDLE, idle_vram, "NS-E102")

    machine: dict[str, str | float | None] = {
        "gpu": None if rt.dev_mode or no_card else d.gpu_name(),
        "ram_gb": ram,
        "disk_type": None,
        "os": platform.platform(),
        "nightshift": __version__,
        "profile": rt.profile,
        "targets": tg.profile,
        "not_measured": NOT_MEASURED,
    }
    return GateReport(
        slice="verify-quick" if quick else "verify",
        date=datetime.now().isoformat(timespec="seconds"),
        machine=machine,
        checks=rec.checks,
        benchmarks=rec.benchmarks,
        dev_mode=rt.dev_mode,
    )


def report_path(paths: Paths) -> Path:
    """`logs\\verify-report.json`."""
    return paths.logs / "verify-report.json"


def write_report(paths: Paths, report: GateReport) -> Path:
    """Write the report as JSON; returns the file."""
    f = report_path(paths)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(report.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return f


def format_lines(report: GateReport) -> list[str]:
    """Console table: one line per check, failures with their code and hint."""
    lines = []
    for c in report.checks:
        mark = "PASS" if c.passed else "FAIL"
        val = "" if c.value is None else f" = {c.value}"
        lim = f"  [{c.limit}]" if c.limit else ""
        lines.append(f"[{mark}] {c.name}{val}{lim}")
        if c.log and (not c.passed or c.log.startswith("NS-W")):
            lines.append(f"       {c.log}")
    lines.append("Result: " + ("ALL CHECKS PASSED" if report.passed else "SOME CHECKS FAILED"))
    return lines


