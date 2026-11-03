"""Whisper smoke test for the installer (component C1, CR-C1-4 b).

``ns verify --quick`` only *loads* the whisper model. CTranslate2 loads cuBLAS and cuDNN lazily, at the first
matrix product, so a PC without those DLLs passes the load and fails on the first real transcription (the
first video's captions stage). This module transcribes a generated 3 second WAV in the studio venv, on the
device runtime.json names, so s08 can switch to the CPU before the owner finds out the slow way.
"""

from __future__ import annotations

from installer.stage2.context import StepContext
from installer.stage2.runner import RunResult
from installer.stage2.web.samples import write_sine_wav
from ns_common.models import WhisperCfg

TEST_SECONDS = 3.0
TIMEOUT_S = 900          # a CPU run loads the 1.5 GB model and decodes one 30 s window

#: Runs in the studio venv. argv: model dir, device, compute type, wav. The DLL folders are put on PATH the
#: same way the real transcriber does it (``ns_common.gpu_dlls``); a tone has no words, the decoder runs.
SNIPPET = """
import json, sys, time
try:
    from ns_common import gpu_dlls
    gpu_dlls.ensure_on_path()
except ImportError:
    pass
from faster_whisper import WhisperModel
t = time.perf_counter()
model = WhisperModel(sys.argv[1], device=sys.argv[2], compute_type=sys.argv[3], local_files_only=True)
segments, _info = model.transcribe(sys.argv[4], language="en", beam_size=1, vad_filter=False)
n = len(list(segments))
print(json.dumps({"ok": True, "segments": n, "seconds": round(time.perf_counter() - t, 2)}))
"""


def write_probe_wav(ctx: StepContext) -> str:
    """Write the 3 second test WAV under logs\\ (kept for the diagnostics zip) and return its path."""
    return str(write_sine_wav(ctx.paths.logs / "whisper-probe.wav", TEST_SECONDS))


def transcribe_probe(ctx: StepContext, cfg: WhisperCfg) -> RunResult:
    """Transcribe the test WAV with ``cfg`` (model dir, device, compute type) in the studio venv."""
    wav = write_probe_wav(ctx)
    argv = [str(ctx.venv_python("studio")), "-c", SNIPPET, cfg.model, cfg.device, cfg.compute_type, wav]
    return ctx.runner.run(
        argv, cwd=ctx.app_path, env={"PYTHONUTF8": "1"}, timeout=TIMEOUT_S, tag=f"whisper probe {cfg.device}",
        meta={"device": cfg.device, "compute_type": cfg.compute_type, "model": cfg.model},
    )
