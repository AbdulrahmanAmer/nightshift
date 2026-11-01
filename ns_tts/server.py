"""C4 ns_tts: the FastAPI TTS service on 127.0.0.1:8091 (spec 15.4).

Loudness normalization is NOT done here (C6 does it). `python -m ns_tts.server --port 8091 --engine ...`.
"""

# fastapi/uvicorn are in the `tts` extra, not in CI's `.[dev]`; drop this line once CR-C4-1 is applied.
# pyright: reportMissingImports=false
from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import time
import zlib
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ns_common.config import load_runtime
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_tts.audio import chunk_text, stitch, trim_silence, wav_bytes, write_wav
from ns_tts.engine import Engine, select_engine
from ns_tts.voices import VoiceLibrary, VoiceSpec

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class SpeechReq(BaseModel):
    """POST /v1/audio/speech body. `cliff_lines` is an additive optional field (spec 7.5)."""

    model: str = "qwen3-tts"
    input: str = Field(min_length=1)
    voice: str
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    response_format: str = "wav"
    seed: int | None = None
    cliff_lines: list[str] = []


class BatchItem(BaseModel):
    """One narration part: `id` becomes the file name `<id>.wav`."""

    id: str
    input: str = Field(min_length=1)
    voice: str
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    cliff_lines: list[str] = []


class BatchReq(BaseModel):
    """POST /v1/audio/speech/batch body."""

    items: list[BatchItem]
    out_dir: str
    seed: int | None = None


class DesignReq(BaseModel):
    """POST /v1/voices/design body."""

    name: str
    description: str
    language: str = "English"


class CloneReq(BaseModel):
    """POST /v1/voices/clone body; `rights_confirmed` must be true."""

    name: str
    ref_audio_path: str
    ref_text: str
    rights_confirmed: bool = False
    language: str = "English"


def voice_seed(voice_name: str) -> int:
    """Fixed per-voice seed so the timbre of one voice is stable across calls (spec 7.5)."""
    return zlib.crc32(voice_name.encode("utf-8")) & 0x7FFFFFFF


def create_app(engine: Engine, library: VoiceLibrary, *, log: NsLogger | None = None) -> FastAPI:
    """Build the app around an engine and a voice library. Engine access is serialized by a lock."""
    app = FastAPI(title="Nightshift TTS")
    lock = threading.Lock()

    def ensure_loaded() -> None:
        if not engine.loaded:
            engine.load()

    def get_voice(name: str) -> VoiceSpec:
        try:
            return library.get(name)
        except KeyError:
            raise HTTPException(404, f"unknown voice {name!r}") from None

    def render(text: str, voice: VoiceSpec, speed: float, seed: int | None, cliffs: list[str]) -> np.ndarray:
        chunks = chunk_text(text, cliff_lines=cliffs)
        if not chunks:
            raise HTTPException(400, "input has no speakable text")
        use_seed = voice_seed(voice.name) if seed is None else seed
        ensure_loaded()
        audios = engine.synth_batch([c.text for c in chunks], voice, speed=speed, seed=use_seed)
        sr = engine.sr
        return stitch(
            [(trim_silence(a, sr), c.pause_after_ms) for a, c in zip(audios, chunks, strict=True)], sr
        )

    @app.exception_handler(NsError)
    async def ns_error_handler(request: Request, exc: NsError) -> JSONResponse:
        missing = exc.data.get("missing_model")
        if missing:
            return JSONResponse(status_code=409, content={"missing_model": missing})
        if log:
            log.error("tts_error", str(exc), code=exc.code)
        return JSONResponse(status_code=500, content={"error": exc.code, "detail": str(exc)})

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "engine": engine.name, "model_loaded": bool(engine.loaded)}

    @app.get("/v1/voices")
    def voices() -> list[dict[str, str]]:
        return [
            {"name": v.name, "mode": v.mode, "source": "builtin" if v.mode == "preset" else "library"}
            for v in library.list()
        ]

    @app.post("/v1/audio/speech")
    def speech(req: SpeechReq) -> Response:
        if req.response_format != "wav":
            raise HTTPException(400, "only response_format 'wav' is supported")
        voice = get_voice(req.voice)
        with lock:
            x = render(req.input, voice, req.speed, req.seed, req.cliff_lines)
            sr = engine.sr
        return Response(content=wav_bytes(x, sr), media_type="audio/wav")

    @app.post("/v1/audio/speech/batch")
    def speech_batch(req: BatchReq) -> dict[str, Any]:
        ids = [it.id for it in req.items]
        if any(not _ID_RE.match(i) for i in ids) or len(set(ids)) != len(ids):
            raise HTTPException(400, "item ids must be unique and file-name safe (letters, digits, . _ -)")
        voices_by_name = {name: get_voice(name) for name in {it.voice for it in req.items}}
        out_dir = Path(req.out_dir)
        files: list[dict[str, Any]] = []
        total = 0.0
        t0 = time.perf_counter()
        with lock:
            for it in req.items:
                x = render(it.input, voices_by_name[it.voice], it.speed, req.seed, it.cliff_lines)
                path = out_dir / f"{it.id}.wav"
                seconds = write_wav(path, x, engine.sr)
                total += seconds
                files.append({"id": it.id, "path": str(path), "seconds": round(seconds, 3)})
        rtf = (time.perf_counter() - t0) / total if total > 0 else 0.0
        if log:
            log.info("tts_batch", items=len(files), audio_s=round(total, 2), rtf=round(rtf, 3))
        return {"files": files, "engine": engine.name, "rtf": round(rtf, 3)}

    @app.post("/v1/voices/design", response_model=None)
    def design(req: DesignReq) -> Any:
        missing = engine.missing_model("design")
        if missing:  # the dashboard/installer downloads it; nothing is fetched here
            return JSONResponse(status_code=409, content={"missing_model": missing})
        try:
            with lock:
                ensure_loaded()
                v = library.add_design(req.name, req.description, engine=engine, language=req.language)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"name": v.name, "sample_path": str(library.sample_path(v.name))}

    @app.post("/v1/voices/clone", response_model=None)
    def clone(req: CloneReq) -> Any:
        if not req.rights_confirmed:
            raise HTTPException(400, "rights_confirmed must be true: you must be allowed to clone this voice")
        missing = engine.missing_model("clone")
        if missing:
            return JSONResponse(status_code=409, content={"missing_model": missing})
        try:
            with lock:
                ensure_loaded()
                v = library.add_clone(
                    req.name,
                    req.ref_audio_path,
                    req.ref_text,
                    req.rights_confirmed,
                    engine=engine,
                    language=req.language,
                )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"name": v.name, "sample_path": str(library.sample_path(v.name))}

    @app.get("/v1/voices/{name}/embedding")
    def embedding(name: str) -> Response:
        get_voice(name)
        try:
            blob = library.export_embedding(name)
        except ValueError as e:
            raise HTTPException(404, str(e)) from e
        return Response(
            content=blob,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{name}.speaker.bin"'},
        )

    return app


def main() -> None:
    """Entry point: `python -m ns_tts.server --port 8091 --engine qwentts|reference|fake`."""
    import uvicorn

    ap = argparse.ArgumentParser(prog="ns_tts.server")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--engine", choices=["qwentts", "reference", "fake"], default=None)
    args = ap.parse_args()
    paths = Paths.from_env()
    fake = args.engine == "fake" or os.environ.get("NS_TTS_FAKE") == "1"
    try:
        rt = load_runtime(paths)
    except NsError:
        if not fake:
            raise
        rt = Runtime()
    log = NsLogger("tts", paths.logs)
    engine = select_engine(rt.tts, paths, args.engine)
    try:
        engine.load()
    except NsError as e:
        log.error("engine_load_failed", str(e), engine=engine.name, code=e.code)
        sys.stderr.write(f"{e}\n")
        raise SystemExit(2) from e
    library = VoiceLibrary(paths.models_tts_voices)
    port = args.port or rt.tts.port
    log.info("tts_start", engine=engine.name, port=port)
    uvicorn.run(create_app(engine, library, log=log), host=rt.tts.host, port=port)


if __name__ == "__main__":
    main()
