"""s08_smoke: ``ns verify --quick`` and the documented fallbacks for the untestable parts (component C1).

NVENC failing switches the encoder to libx264 (NS-W401); a failing TTS engine switches to the PyTorch
reference engine and builds its venv (NS-W301, NS-E108 when torch cannot see the GPU); MTP that is not
faster is turned off (NS-W302); faster-whisper that cannot load on CUDA stops the install with NS-E112.
"""

from __future__ import annotations

import json
from typing import Any

from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.errors import make_error
from installer.stage2.fallback import ensure_tts_venv, fallback_items
from installer.stage2.runner import RunResult
from installer.stage2.runtime_cfg import ensure_baseline, update_runtime


def parse_report(ctx: StepContext, res: RunResult) -> dict[str, bool] | None:
    """Check name -> passed from the ``ns verify`` JSON (stdout, else logs/verify-report.json)."""
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
            return {
                str(c.get("name", "")).lower(): bool(c.get("passed")) for c in checks if isinstance(c, dict)
            }
    return None


def failing(report: dict[str, bool], part: str) -> bool:
    """True when any check whose name contains ``part`` failed."""
    return any(part in name and not ok for name, ok in report.items())


class S08Smoke(Step):
    """Quick verification with automatic fallbacks."""

    id = "s08_smoke"
    title = "Test everything"

    def check(self, ctx: StepContext) -> bool:
        """True after a passing run."""
        return ctx.flag("smoke") == "passed"

    def run(self, ctx: StepContext) -> None:
        """Run ``ns verify --quick`` (twice at most: once more after the TTS fallback)."""
        ensure_baseline(ctx)
        warned: set[str] = set()
        for attempt in (1, 2):
            ctx.ui.say("Testing the language model, voice, captions and video encoder ...")
            res = ctx.run_tool("ns", ["verify", "--quick"], timeout=3600)
            report = parse_report(ctx, res)
            if report is None:
                raise make_error(
                    "NS-E202", f"ns verify --quick gave no report (exit {res.returncode}): {res.text[-300:]}"
                )
            ctx.ui.table(
                "Smoke test", ["Check", "Result"],
                [[name, "ok" if ok else "fail"] for name, ok in report.items()], status_col=1,
            )
            if failing(report, "whisper"):
                raise make_error("NS-E112", "faster-whisper did not load on the GPU")
            for part in ("llm", "embed"):
                if failing(report, part):
                    raise make_error("NS-E203", f"the {part} service did not become healthy")
            if failing(report, "nvenc") and "NS-W401" not in warned:
                update_runtime(ctx, self._use_libx264)
                ctx.warn("NS-W401", "runtime.json now uses libx264")
                warned.add("NS-W401")
            rt = ensure_baseline(ctx)
            if failing(report, "mtp") and rt.llama.flags.spec_type and "NS-W302" not in warned:
                update_runtime(ctx, self._no_mtp)
                ctx.warn("NS-W302", "runtime.json now uses plain decoding")
                warned.add("NS-W302")
            if not failing(report, "tts"):
                break
            if attempt == 2 or ctx.flag("tts_fallback", False):
                raise make_error("NS-E108", "the reference TTS engine also failed its test")
            self._switch_to_reference(ctx)
        ctx.set_flag("smoke", "passed")

    @staticmethod
    def _use_libx264(rt: Any) -> None:
        rt.video.encoder = "libx264"
        rt.video.preset = "veryfast"

    @staticmethod
    def _no_mtp(rt: Any) -> None:
        rt.llama.flags.spec_type = None
        rt.llama.flags.spec_draft_n_max = None

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
