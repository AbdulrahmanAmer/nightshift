"""s08_smoke: ``ns verify --quick`` and the documented fallbacks for the untestable parts (component C1).

NVENC failing switches the encoder to libx264 (NS-W401); a failing TTS engine switches to the PyTorch
reference engine and builds its venv (NS-W301, NS-E108 when torch cannot see the GPU). Whisper is tested by
a 3 second transcription in the studio venv (cuBLAS and cuDNN load lazily, so the model load alone proves
nothing): on CUDA it falls back to the CPU with int8 (NS-W404, captions are slower, the install goes on)
until the next run, which probes CUDA again and switches back when it passes; only
a CPU that cannot transcribe either stops the install (NS-E411). Checks are matched on the real names
`ns verify` reports (`ns_supervisor.check_names`). `ns verify --quick` has no MTP check: whether MTP is kept
is decided by `ns tune` (s06), so there is no MTP branch here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.downloader import QWENTTS_EXES
from installer.stage2.errors import make_error
from installer.stage2.fallback import ensure_tts_venv, fallback_items
from installer.stage2.runner import RunResult
from installer.stage2.runtime_cfg import ensure_baseline, update_runtime
from installer.stage2.steps.s05_opencode_prewarm import confirm_local_code
from installer.stage2.sysinfo import drive_type
from installer.stage2.whisper_probe import transcribe_probe
from ns_common import ports, vcruntime
from ns_common.avblock import looks_blocked
from ns_supervisor import check_names as names
from ns_supervisor.health_cfg import timeout_for_drive, write_health_timeout
from ns_supervisor.services import DASHBOARD_PORT


def qwentts_binaries_ok(ctx: StepContext) -> bool:
    """True when the unpacked qwentts.cpp folder holds the two programs the voice service starts."""
    return all((ctx.paths.bin_qwentts / exe).is_file() for exe in QWENTTS_EXES)


def check_vc_runtime(ctx: StepContext) -> None:
    """NS-E114 before llama-server is started when the Visual C++ runtime DLLs are not in System32.

    Without them the program does not start at all and ``ns verify`` can only say that a service failed.
    Not checked in ``--simulate`` (the PC running the simulation says nothing about the target).
    """
    if ctx.simulate:
        return
    absent = vcruntime.missing()
    if absent:
        where = vcruntime.system_dir()
        raise make_error("NS-E114", f"{', '.join(absent)} not found in {where}; llama-server.exe needs them")


def runtime_ports(ctx: StepContext) -> dict[str, int]:
    """The ports the services bind, from runtime.json (service name -> port)."""
    rt = ensure_baseline(ctx)
    return {
        "llama": rt.llama.port, "embed": rt.embed.port, "tts": rt.tts.port,
        "supervisor": rt.supervisor_port, "dashboard": DASHBOARD_PORT,
    }


def check_ports(ctx: StepContext) -> None:
    """NS-E116 naming the port and its owner when another program already holds one the services need.

    Without it a taken 8080 ended as "the story model did not pass" (NS-E203): llama-server could not bind,
    or a foreign server answered the health check. Not checked in ``--simulate``.
    """
    if ctx.simulate:
        return
    ports.ensure_free(runtime_ports(ctx))


def stop_own_engine(ctx: StepContext) -> None:
    """Stop an engine left over from an earlier try before the port check looks at the ports.

    ``ns verify`` starts a detached engine (and its llama-server). When the console was closed while it ran,
    the engine lives on for up to 30 minutes, and a rerun would stop with NS-E116 against Nightshift's own
    python.exe. ``ns stop`` is a no-op when nothing runs (s10 makes the same call).
    """
    ctx.ui.say("Stopping any Nightshift engine left over from an earlier try ...")
    ctx.run_tool("ns", ["stop"], timeout=120)


def prepare_verify(ctx: StepContext) -> None:
    """Before ``ns verify``: no report from an earlier run, and a start timeout that fits the install drive.

    A Retry whose ``ns verify`` dies before it writes could otherwise pass on the old report. A cold load of
    the 14 GB story model from a hard disk takes longer than the 120 s the supervisor used to wait.
    """
    (ctx.paths.logs / "verify-report.json").unlink(missing_ok=True)
    kind = drive_type(ctx.preflight, ctx.paths.home)
    write_health_timeout(ctx.paths, timeout_for_drive(kind))


def check_programs_present(ctx: StepContext) -> None:
    """NS-E113 when a program s02 unpacked has been removed since (antivirus), before anything is started.

    ``llama-server.exe`` missing used to end as "the story model did not pass" (NS-E203), and a removed
    ``qwen-tts.exe`` as a quiet switch to the 7 GB PyTorch engine (NS-W301). The ``.done`` flag of the unpack
    says the file was there; with it gone the cause is a scanner, so say so and name the file.
    """
    if ctx.simulate:
        return
    llama, voice = ctx.paths.bin_llama, ctx.paths.bin_qwentts
    gone: list[Path] = []
    if any(llama.glob(".ns-llama*.done")) and not (llama / "llama-server.exe").is_file():
        gone.append(llama / "llama-server.exe")
    if (voice / ".ns-qwentts.done").exists():
        gone += [voice / exe for exe in QWENTTS_EXES if not (voice / exe).is_file()]
    if gone:
        listed = ", ".join(str(p) for p in gone)
        detail = f"{listed} was unpacked by setup and has been removed since"
        raise make_error("NS-E113", detail, path=str(gone[0]))


def load_report_rows(ctx: StepContext, res: RunResult) -> list[dict[str, Any]] | None:
    """The check rows of the ``ns verify`` JSON (stdout, else logs/verify-report.json)."""
    candidates = [res.stdout.strip()]
    report_file = ctx.paths.logs / "verify-report.json"
    if report_file.is_file():
        candidates.append(report_file.read_text(encoding="utf-8-sig"))
    for text in candidates:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < start:
            continue
        try:
            data: Any = json.loads(text[start : end + 1])
        except ValueError:
            continue
        checks = data.get("checks") if isinstance(data, dict) else None
        if isinstance(checks, list):
            return [c for c in checks if isinstance(c, dict)]
    return None


def parse_report(ctx: StepContext, res: RunResult) -> dict[str, bool] | None:
    """Check name -> passed from the ``ns verify`` JSON (stdout, else logs/verify-report.json)."""
    rows = load_report_rows(ctx, res)
    if rows is None:
        return None
    return {str(c.get("name", "")).lower(): bool(c.get("passed")) for c in rows}


def blocked_rows(rows: list[dict[str, Any]] | None) -> str:
    """The text of a failed check whose log says Windows blocked a program (Smart App Control, antivirus).

    Without this a blocked ``llama-server.exe`` ended as "the story model did not pass" (NS-E203) and a
    blocked ``qwen-tts.exe`` as a quiet switch to the PyTorch engine (NS-W301). Empty when none did.
    """
    for row in rows or []:
        log = str(row.get("log") or "")
        if not row.get("passed") and looks_blocked(log):
            return f"{row.get('name', 'a check')}: {log}"
    return ""


def failing(report: dict[str, bool], prefix: str) -> bool:
    """True when any check of the area ``prefix`` (for example ``names.TTS_PREFIX``) failed."""
    return any(name.startswith(prefix) and not ok for name, ok in report.items())


class S08Smoke(Step):
    """Quick verification with automatic fallbacks."""

    id = "s08_smoke"
    title = "Test everything"

    def check(self, ctx: StepContext) -> bool:
        """True after a passing run."""
        return ctx.flag("smoke") == "passed" and ctx.flag("smoke_version") == ctx.manifest.version

    def run(self, ctx: StepContext) -> None:
        """Run ``ns verify --quick`` (twice at most: once more after the TTS fallback)."""
        ensure_baseline(ctx)
        check_vc_runtime(ctx)
        check_programs_present(ctx)
        stop_own_engine(ctx)
        check_ports(ctx)
        warned: set[str] = set()
        whisper_load_failed = False
        wants_binary = ctx.flag("qwentts") == "binary" and not ctx.flag("tts_fallback", False)
        if wants_binary and not qwentts_binaries_ok(ctx):
            ctx.ui.say("The voice program (qwen-tts.exe) is missing from the download.", "yellow")
            self._switch_to_reference(ctx)
        for attempt in (1, 2):
            ctx.ui.say("Testing the language model, voice, captions and video encoder ...")
            prepare_verify(ctx)
            res = ctx.run_tool("ns", ["verify", "--quick"], timeout=3600)
            rows = load_report_rows(ctx, res)
            report = parse_report(ctx, res)
            if rows is not None:
                ctx.ui.verify_checks(rows)
            if report is None:
                raise make_error(
                    "NS-E202", f"ns verify --quick gave no report (exit {res.returncode}): {res.text[-300:]}"
                )
            ctx.ui.table(
                "Smoke test", ["Check", "Result"],
                [[name, "ok" if ok else "fail"] for name, ok in report.items()], status_col=1,
            )
            blocked = blocked_rows(rows)
            if blocked:
                raise make_error("NS-E113", blocked)
            whisper_load_failed = failing(report, names.WHISPER_PREFIX)
            for prefix, what in ((names.LLAMA_PREFIX, "story model"), (names.EMBED_PREFIX, "memory index")):
                if failing(report, prefix):
                    bad = ", ".join(n for n, ok in report.items() if n.startswith(prefix) and not ok)
                    raise make_error("NS-E203", f"the {what} did not pass its test ({bad})")
            if failing(report, names.NVENC_PREFIX) and "NS-W401" not in warned:
                update_runtime(ctx, self._use_libx264)
                ctx.warn("NS-W401", "runtime.json now uses libx264")
                warned.add("NS-W401")
            if not failing(report, names.TTS_PREFIX):
                break
            if attempt == 2 or ctx.flag("tts_fallback", False):
                raise make_error("NS-E108", "the reference TTS engine also failed its test")
            self._switch_to_reference(ctx)
        self._check_whisper(ctx, whisper_load_failed)
        if ctx.ensure_answers().install_code and ctx.paths.opencode_file.exists():
            confirm_local_code(ctx)              # needs the opencode.json that s07 wrote
        ctx.set_flag("smoke", "passed")
        ctx.set_flag("smoke_version", ctx.manifest.version)

    @staticmethod
    def _check_whisper(ctx: StepContext, load_failed: bool) -> None:
        """Transcribe a 3 second test WAV; on CUDA a failure switches whisper to the CPU (NS-W404).

        The CPU fallback is not sticky: with a graphics card the CUDA probe runs on every pass (a driver
        update or a restored DLL fixes it) and a pass flips runtime.json back to CUDA; only a failing probe
        keeps or sets the CPU and warns.
        """
        if ctx.dev_mode:        # no faster-whisper and no model: the studio's transcriber is the fake
            ctx.ui.say("Dev mode: speech recognition is a fake, so it is not tested.")
            return
        cfg = ensure_baseline(ctx).whisper
        if ctx.profile != "cpu-dev":
            ctx.ui.say("Testing speech recognition on the graphics card ...")
            cuda = cfg.model_copy(update={
                "device": "cuda", "compute_type": cfg.compute_type if cfg.device == "cuda" else "int8"})
            res = None if load_failed else transcribe_probe(ctx, cuda)
            if res is not None and res.ok:
                if cfg.device != "cuda":
                    update_runtime(ctx, lambda rt: S08Smoke._use_whisper(rt, cuda.device, cuda.compute_type))
                    ctx.ui.say("Speech recognition works on the graphics card again; captions use it.")
                return
            why = "the model did not load" if res is None else res.text[-300:]
            update_runtime(ctx, lambda rt: S08Smoke._use_whisper(rt, "cpu", "int8"))
            ctx.warn("NS-W404", f"runtime.json runs whisper on the CPU ({why})")
            cfg = ensure_baseline(ctx).whisper
        elif load_failed:
            raise make_error("NS-E411", "speech recognition did not load on the processor")
        ctx.ui.say("Testing speech recognition on the processor ...")
        res = transcribe_probe(ctx, cfg)
        if not res.ok:
            raise make_error("NS-E411", f"speech recognition failed on the processor: {res.text[-300:]}")

    @staticmethod
    def _use_whisper(rt: Any, device: str, compute_type: str) -> None:
        rt.whisper.device = device
        rt.whisper.compute_type = compute_type

    @staticmethod
    def _use_libx264(rt: Any) -> None:
        rt.video.encoder = "libx264"
        rt.video.preset = "veryfast"

    @staticmethod
    def _switch_to_reference(ctx: StepContext) -> None:
        ctx.set_flag("tts_fallback", True)
        ctx.ui.say("The voice engine failed its test; switching to the PyTorch engine (about 7 GB more).")
        ctx.download(fallback_items(ctx))
        ensure_tts_venv(ctx)
        update_runtime(ctx, lambda rt: setattr(rt.tts, "engine", "reference"))
        ctx.warn("NS-W301", "runtime.json now uses the reference TTS engine")

    def verify(self, ctx: StepContext) -> None:
        """The smoke flag is set and runtime.json still validates."""
        if ctx.flag("smoke") != "passed":
            raise make_error("NS-E202", "the smoke test did not pass")
        ensure_baseline(ctx)
