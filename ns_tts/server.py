"""C4 ns_tts: the FastAPI TTS service on 127.0.0.1:8091 (spec 15.4).

Loudness normalization is NOT done here (C6 does it). `python -m ns_tts.server --port 8091 --engine ...`.
"""

# fastapi/uvicorn are in the `tts` extra, not in CI's `.[dev]`; drop this line once CR-C4-1 is applied.
# pyright: reportMissingImports=false
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import os
import re
import sys
import threading
import time
import wave
import zlib
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, cast

import anyio
import numpy as np
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ns_common.config import load_runtime
from ns_common.errors import NsError
from ns_common.languages import qwen_language
from ns_common.logging import NsLogger
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_common.tts_timeout import TTS_TIMEOUT_CAP_S, tts_timeout_s
from ns_tts.audio import chunk_text, stitch, trim_silence, wav_bytes, write_wav
from ns_tts.engine import BatchInterrupted, Engine, design_model_message, fake_speech_for, select_engine
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
    language: str | None = None  # ISO code or name; the channel's language (default: the voice's own)


class BatchItem(BaseModel):
    """One narration part: `id` becomes the file name `<id>.wav`."""

    id: str
    input: str = Field(min_length=1)
    voice: str
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    cliff_lines: list[str] = []
    language: str | None = None


class BatchReq(BaseModel):
    """POST /v1/audio/speech/batch body."""

    items: list[BatchItem]
    out_dir: str
    seed: int | None = None
    # the studio client's own timeout (`ns_common.tts_timeout`): no process is stopped before it gives up
    timeout_s: float | None = Field(default=None, ge=1, le=TTS_TIMEOUT_CAP_S)
    # reuse the `<id>.wav` files an interrupted earlier attempt left in out_dir (same text, voice, speed)
    resume: bool = False


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


def missing_body(missing: str, code: str, detail: str) -> dict[str, str]:
    """409 body for an absent model: `missing_model` (what callers test for) plus the code and plain text."""
    return {"missing_model": missing, "error": code, "detail": detail}


def voice_seed(voice_name: str) -> int:
    """Fixed per-voice seed so the timbre of one voice is stable across calls (spec 7.5)."""
    return zlib.crc32(voice_name.encode("utf-8")) & 0x7FFFFFFF


def create_app(engine: Engine, library: VoiceLibrary, *, log: NsLogger | None = None) -> FastAPI:
    """Build the app around an engine and a voice library. Engine access is serialized by a lock."""
    app = FastAPI(title="Nightshift TTS")
    lock = threading.Lock()
    warned: set[str] = set()

    def language_name(code: str | None) -> str:
        """The `--lang` name for a request's language; unknown is English, with one warning per code."""
        name, known = qwen_language(code)
        if not known and (code or "") not in warned:
            warned.add(code or "")
            if log:
                log.warn("tts_language_unknown", f"{code!r} is not a Qwen3-TTS language; using English")
        return name

    def with_language(voice: VoiceSpec, code: str | None) -> VoiceSpec:
        """`voice` speaking the request's language (the voice's own when the request names none)."""
        return dataclasses.replace(voice, language=language_name(code)) if code else voice

    def ensure_loaded() -> None:
        if not engine.loaded:
            engine.load()

    @contextmanager
    def watching(request: Request) -> Iterator[None]:
        """While the request runs, an engine that waits on purpose (the fake's developer delay) can ask
        whether the client has gone away. The engine's thread is a worker thread of this app, so it may call
        back into the event loop; with no such engine this does nothing."""
        watch = getattr(engine, "watch_client", None)
        if not callable(watch):
            yield
            return

        def gone() -> bool:
            return bool(anyio.from_thread.run(request.is_disconnected))

        with cast("AbstractContextManager[None]", watch(gone)):
            yield

    def get_voice(name: str) -> VoiceSpec:
        try:
            return library.get(name)
        except KeyError:
            raise HTTPException(404, f"unknown voice {name!r}") from None

    def render_many(
        entries: list[tuple[str, VoiceSpec, float, list[str]]],
        seed: int | None,
        *,
        min_timeout_s: float = 0.0,
        salvage: dict[int, np.ndarray] | None = None,
    ) -> list[np.ndarray]:
        """Narration for each (text, voice, speed, cliff lines), in order.

        Every sentence chunk of the request that shares a voice, language and speed goes to the engine in ONE
        `synth_batch` call, so qwentts starts one `qwen-tts` process for a whole job instead of one per
        sentence (each start loads the 1.7B talker and the codec).
        """
        plans = [chunk_text(text, cliff_lines=cliffs) for text, _v, _s, cliffs in entries]
        if any(not chunks for chunks in plans):
            raise HTTPException(400, "input has no speakable text")
        ensure_loaded()
        groups: dict[tuple[str, str, float], list[tuple[int, int]]] = {}
        for i, (_text, voice, speed, _cliffs) in enumerate(entries):
            for j in range(len(plans[i])):
                groups.setdefault((voice.name, voice.language, speed), []).append((i, j))
        audio: dict[tuple[int, int], np.ndarray] = {}
        # No process may be stopped before the studio's client gives up (one formula, ns_common.tts_timeout).
        engine.batch_timeout_floor_s = max(min_timeout_s, tts_timeout_s(sum(len(p) for p in plans)))

        def stitched(i: int) -> np.ndarray:
            return stitch(
                [(trim_silence(audio[(i, j)], engine.sr), c.pause_after_ms) for j, c in enumerate(plans[i])],
                engine.sr,
            )

        try:
            for refs in groups.values():
                _t, voice, speed, _c = entries[refs[0][0]]
                use_seed = voice_seed(voice.name) if seed is None else seed
                texts = [plans[i][j].text for i, j in refs]
                try:
                    made = engine.synth_batch(texts, voice, speed=speed, seed=use_seed)
                except BatchInterrupted as e:  # keep what was finished: a retry resumes from it
                    for k, a in e.done.items():
                        audio[refs[k]] = a
                    if salvage is not None:
                        for i, chunks in enumerate(plans):
                            if all((i, j) in audio for j in range(len(chunks))):
                                salvage[i] = stitched(i)
                    raise
                for ref, a in zip(refs, made, strict=True):
                    audio[ref] = a
        finally:
            engine.batch_timeout_floor_s = 0.0
        return [stitched(i) for i in range(len(plans))]

    def render(text: str, voice: VoiceSpec, speed: float, seed: int | None, cliffs: list[str]) -> np.ndarray:
        return render_many([(text, voice, speed, cliffs)], seed)[0]

    @app.exception_handler(NsError)
    async def ns_error_handler(request: Request, exc: NsError) -> JSONResponse:
        missing = exc.data.get("missing_model")
        if missing:
            return JSONResponse(status_code=409, content=missing_body(missing, exc.code, str(exc)))
        if log:
            log.error("tts_error", str(exc), code=exc.code)
        return JSONResponse(status_code=500, content={"error": exc.code, "detail": str(exc)})

    @app.get("/health")
    def health() -> dict[str, Any]:
        # engine_kind: "qwentts", "reference" (PyTorch fallback: `ns verify` reports NS-W301) or "fake"
        body: dict[str, Any] = {
            "ok": True,
            "engine": engine.name,
            "engine_kind": engine.name,
            "model_loaded": bool(engine.loaded),
        }
        # The fake says how it speaks: voice_mode "sapi" (the text, in the Windows voice named by `voice`) or
        # "tones" (bursts; `voice_fallback`, `voice_warning` and `voice_warning_code` (NS-W303) when a SAPI
        # voice was wanted but is missing).
        describe = getattr(engine, "describe", None)
        info = describe() if callable(describe) else None
        if isinstance(info, dict):
            body.update(info)
        return body

    @app.get("/v1/voices")
    def voices() -> list[dict[str, str]]:
        return [
            {"name": v.name, "mode": v.mode, "source": "builtin" if v.mode == "preset" else "library"}
            for v in library.list()
        ]

    @app.post("/v1/audio/speech")
    def speech(req: SpeechReq, request: Request) -> Response:
        if req.response_format != "wav":
            raise HTTPException(400, "only response_format 'wav' is supported")
        voice = with_language(get_voice(req.voice), req.language)
        with lock, watching(request):
            x = render(req.input, voice, req.speed, req.seed, req.cliff_lines)
            sr = engine.sr
        return Response(content=wav_bytes(x, sr), media_type="audio/wav")

    def item_signature(text: str, voice: VoiceSpec, speed: float, cliffs: list[str]) -> str:
        """What makes two renders of an item interchangeable (the seed is left out on purpose)."""
        base = "\x1f".join([voice.name, voice.language, f"{speed}", text, "\x1e".join(cliffs)])
        return hashlib.sha256(base.encode("utf-8")).hexdigest()

    def kept_seconds(wav: Path, sig_file: Path, sig: str) -> float | None:
        """Seconds of a WAV an earlier interrupted attempt left with the same signature, else None."""
        try:
            if wav.is_file() and sig_file.read_text(encoding="utf-8").strip() == sig:
                with wave.open(str(wav), "rb") as w:
                    return w.getnframes() / w.getframerate()
        except (OSError, EOFError, wave.Error):
            pass
        return None

    @app.post("/v1/audio/speech/batch")
    def speech_batch(req: BatchReq, request: Request) -> dict[str, Any]:
        ids = [it.id for it in req.items]
        if any(not _ID_RE.match(i) for i in ids) or len(set(ids)) != len(ids):
            raise HTTPException(400, "item ids must be unique and file-name safe (letters, digits, . _ -)")
        voices_by_name = {name: get_voice(name) for name in {it.voice for it in req.items}}
        out_dir = Path(req.out_dir)
        found: dict[str, dict[str, Any]] = {}
        total = 0.0
        t0 = time.perf_counter()
        with lock, watching(request):
            entries = [
                (it.input, with_language(voices_by_name[it.voice], it.language), it.speed, it.cliff_lines)
                for it in req.items
            ]
            sigs = [item_signature(*e) for e in entries]
            todo: list[int] = []
            for k, it in enumerate(req.items):
                wav, sig_file = out_dir / f"{it.id}.wav", out_dir / f"{it.id}.sig"
                kept = kept_seconds(wav, sig_file, sigs[k]) if req.resume else None
                if kept is None:
                    todo.append(k)
                else:  # chunks an interrupted attempt already wrote
                    found[it.id] = {"id": it.id, "path": str(wav), "seconds": round(kept, 3)}
            if log and len(todo) < len(req.items):
                log.info("tts_resume", reused=len(req.items) - len(todo), remaining=len(todo))
            salvage: dict[int, np.ndarray] = {}

            def write_item(k: int, x: np.ndarray) -> float:
                wav = out_dir / f"{req.items[k].id}.wav"
                seconds = write_wav(wav, x, engine.sr)
                (out_dir / f"{req.items[k].id}.sig").write_text(sigs[k], encoding="utf-8")
                info = {"id": req.items[k].id, "path": str(wav), "seconds": round(seconds, 3)}
                found[req.items[k].id] = info
                return seconds

            try:
                made = render_many(
                    [entries[k] for k in todo], req.seed,
                    min_timeout_s=req.timeout_s or 0.0, salvage=salvage,
                ) if todo else []  # fmt: skip
            except BatchInterrupted as e:
                for pos, x in salvage.items():  # whole items finished before the stop stay on disk
                    write_item(todo[pos], x)
                if log:
                    log.warn(
                        "tts_batch_interrupted", str(e), kept=len(salvage), cancelled=e.cancelled,
                        remaining=len(todo) - len(salvage),
                    )
                raise
            for pos, x in enumerate(made):
                write_item(todo[pos], x)
            for it in req.items:  # a finished batch leaves no resume markers behind
                (out_dir / f"{it.id}.sig").unlink(missing_ok=True)
            files = [found[it.id] for it in req.items]
            total = sum(float(f["seconds"]) for f in files)
        rtf = (time.perf_counter() - t0) / total if total > 0 else 0.0
        if log:
            log.info("tts_batch", items=len(files), audio_s=round(total, 2), rtf=round(rtf, 3))
        return {"files": files, "engine": engine.name, "rtf": round(rtf, 3)}

    @app.post("/v1/audio/speech/cancel")
    def speech_cancel() -> dict[str, bool]:
        """Stop the batch that is running (the studio's client timed out): kills its `qwen-tts` process."""
        cancel = getattr(engine, "cancel", None)
        stopped = bool(cancel()) if callable(cancel) else False
        if log:
            log.warn("tts_cancel", "the studio asked to cancel the running batch", stopped=stopped)
        return {"cancelled": stopped}

    @app.post("/v1/voices/design", response_model=None)
    def design(req: DesignReq, request: Request) -> Any:
        missing = engine.missing_model("design")
        if missing:  # the installer or `ns models get voicedesign` downloads it; nothing is fetched here
            return JSONResponse(
                status_code=409,
                content=missing_body(missing, "NS-E314", design_model_message(missing)),
            )
        try:
            with lock, watching(request):
                ensure_loaded()
                v = library.add_design(
                    req.name, req.description, engine=engine, language=language_name(req.language)
                )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"name": v.name, "sample_path": str(library.sample_path(v.name))}

    @app.post("/v1/voices/clone", response_model=None)
    def clone(req: CloneReq, request: Request) -> Any:
        if not req.rights_confirmed:
            raise HTTPException(400, "rights_confirmed must be true: you must be allowed to clone this voice")
        missing = engine.missing_model("clone")
        if missing:
            return JSONResponse(
                status_code=409,
                content=missing_body(
                    missing, "NS-E202",
                    f"the voice cloning model is not installed ({missing}): "
                    "run the installer's model step again",
                ),
            )
        try:
            with lock, watching(request):
                ensure_loaded()
                v = library.add_clone(
                    req.name,
                    req.ref_audio_path,
                    req.ref_text,
                    req.rights_confirmed,
                    engine=engine,
                    language=language_name(req.language),
                )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"name": v.name, "sample_path": str(library.sample_path(v.name))}

    @app.get("/v1/voices/{name}/embedding")
    def embedding(name: str) -> Response:
        get_voice(name)
        try:
            files = library.embedding_files(name)
            blob = library.export_embedding(name)
        except ValueError as e:
            raise HTTPException(404, str(e)) from e
        if len(files) > 1:  # qwentts voices: speaker.spk + speaker.rvq + ref.txt travel as one zip
            media, fname = "application/zip", f"{name}.voice.zip"
        else:
            media, fname = "application/octet-stream", f"{name}.{files[0].name}"
        return Response(
            content=blob,
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
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
    speech = fake_speech_for(rt.tts, dev_mode=rt.dev_mode)  # "sapi" only on an engine-test install
    engine = select_engine(rt.tts, paths, args.engine, fake_speech=speech)
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
