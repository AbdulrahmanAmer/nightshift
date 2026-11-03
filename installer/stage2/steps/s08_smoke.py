"""s08_smoke: ``ns verify --quick`` and the documented fallbacks for the untestable parts (component C1).

NVENC failing switches the encoder to libx264 (NS-W401); a failing TTS engine switches to the PyTorch
reference engine and builds its venv (NS-W301, NS-E108 when torch cannot see the GPU). Whisper is tested by
a 3 second transcription in the studio venv (cuBLAS and cuDNN load lazily, so the model load alone proves
nothing): on CUDA it falls back to the CPU with int8 (NS-W404, captions are slower, the install goes on); only
a CPU that cannot transcribe either stops the install (NS-E202). Checks are matched on the real names
`ns verify` reports (`ns_supervisor.check_names`). `ns verify --quick` has no MTP check: whether MTP is kept
is decided by `ns tune` (s06), so there is no MTP branch here.
"""

from __future__ import annotations

import json
from typing import Any

from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.downloader import QWENTTS_EXES
from installer.stage2.errors import make_error
from installer.stage2.fallback import ensure_tts_venv, fallback_items
from installer.stage2.runner import RunResult
from installer.stage2.runtime_cfg import ensure_baseline, update_runtime
from installer.stage2.steps.s05_opencode_prewarm import confirm_local_code
from installer.stage2.whisper_probe import transcribe_probe
from ns_supervisor import check_names as names


def qwentts_binaries_ok(ctx: StepContext) -> bool:
    """True when the unpacked qwentts.cpp folder holds the two programs the voice service starts."""
    return all((ctx.paths.bin_qwentts / exe).is_file() for exe in QWENTTS_EXES)


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
        warned: set[str] = set()
        whisper_load_failed = False
        wants_binary = ctx.flag("qwentts") == "binary" and not ctx.flag("tts_fallback", False)
        if wants_binary and not qwentts_binaries_ok(ctx):
            ctx.ui.say("The voice program (qwen-tts.exe) is missing from the download.", "yellow")
            self._switch_to_reference(ctx)
        for attempt in (1, 2):
            ctx.ui.say("Testing the language model, voice, captions and video encoder ...")
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
        """Transcribe a 3 second test WAV; on CUDA a failure switches whisper to the CPU (NS-W404)."""
        cfg = ensure_baseline(ctx).whisper
        where = "graphics card" if cfg.device == "cuda" else "processor"
        ctx.ui.say(f"Testing speech recognition on the {where} ...")
        if cfg.device == "cuda":
            res = None if load_failed else transcribe_probe(ctx, cfg)
            if res is not None and res.ok:
                return
            why = "the model did not load" if res is None else res.text[-300:]
            update_runtime(ctx, S08Smoke._use_cpu_whisper)
            ctx.warn("NS-W404", f"runtime.json now runs whisper on the CPU ({why})")
            cfg = ensure_baseline(ctx).whisper
        elif load_failed:
            raise make_error("NS-E202", "speech recognition did not load on the processor")
        res = transcribe_probe(ctx, cfg)
        if not res.ok:
            raise make_error("NS-E202", f"speech recognition failed on the processor: {res.text[-300:]}")

    @staticmethod
    def _use_cpu_whisper(rt: Any) -> None:
        rt.whisper.device = "cpu"
        rt.whisper.compute_type = "int8"

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
