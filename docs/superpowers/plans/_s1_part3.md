### Task 6: TTS service `ns_tts` (WP-C, `sonnet`)

**Files:**
- Create: `ns_tts/engine.py`, `ns_tts/audio.py`, `ns_tts/voices.py`, `ns_tts/server.py`, `tests/test_tts_engine.py`, `tests/test_tts_audio.py`, `tests/test_tts_server.py`

**Interfaces:**
- Produces:
```python
# ns_tts/engine.py
class Engine(Protocol):
    name: str; sr: int
    def load(self) -> None
    def synth(self, text: str, voice: "VoiceSpec", *, speed: float = 1.0, seed: int | None = None) -> np.ndarray   # float32 mono
class FakeEngine(Engine): name="fake"; sr=24000   # tone bursts like FakeTts, 1 burst per word
class ReferenceEngine(Engine): name="reference"   # qwen_tts.Qwen3TTSModel.from_pretrained(dir, device_map, dtype, attn_implementation="sdpa"); methods generate_custom_voice / generate_voice_design / generate_voice_clone
class FasterEngine(Engine): name="faster"         # faster_qwen3_tts.FasterQwen3TTS.from_pretrained(dir); generate_custom_voice / generate_voice_design / generate_voice_clone (non-streaming)
def select_engine(cfg: TtsCfg, models_dir: Path, *, prefer: str | None = None) -> Engine
    # "faster" → FasterEngine; "reference" → ReferenceEngine; env NS_TTS_FAKE=1 → FakeEngine. Both real engines import lazily in load().
# ns_tts/voices.py
@dataclass class VoiceSpec: name: str; mode: Literal["preset","design","clone"]; speaker: str|None=None; description: str|None=None; ref_audio: str|None=None; ref_text: str|None=None; language: str="English"
PRESETS = ["Ryan", "Aiden", "Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ono_Anna", "Sohee"]
class VoiceLibrary: __init__(path: Path)  # voices.json; list() -> list[VoiceSpec]; get(name) -> VoiceSpec (presets built in); add_design(name, description); add_clone(name, ref_audio_path, ref_text, rights_confirmed) raises ValueError unless rights_confirmed
# ns_tts/audio.py
def chunk_text(text: str, *, max_chars: int = 280, min_chars: int = 25) -> list[Chunk]    # Chunk(text, pause_after_ms) per spec §7.5: 350 sentence, 700 paragraph, 1200 before a cliff (text containing "\n\n" boundaries marks paragraph; a sentence ending with "…" or a line that equals the marked cliff gets 1200)
def trim_silence(x: np.ndarray, sr: int, thresh_db: float = -45.0) -> np.ndarray
def stitch(chunks: list[tuple[np.ndarray, int]], sr: int) -> np.ndarray     # (audio, pause_after_ms) pairs → one array
def write_wav(path: Path, x: np.ndarray, sr: int) -> float                   # 16-bit PCM; returns seconds
# ns_tts/server.py
def create_app(engine: Engine, library: VoiceLibrary) -> FastAPI      # routes per spec §15.4
# python -m ns_tts.server --port 8091 [--engine faster|reference|fake] reads runtime.json via Paths
```
- Model directories: `models\tts\Qwen3-TTS-12Hz-1.7B-CustomVoice`, `…-VoiceDesign`, `…-Base` (0.6B variants in `cpu-dev`). The engine loads the model that matches the voice mode on first use and keeps up to three loaded on GPU profiles; on `cpu-dev` it unloads the previous.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_tts_audio.py
import numpy as np

from ns_tts.audio import chunk_text, stitch, trim_silence


def test_chunk_text_pauses_and_caps():
    text = "First sentence here. Second one is short.\n\nNew paragraph starts. " + "A very long sentence " * 20 + "ends here."
    chunks = chunk_text(text)
    assert chunks[0].text.startswith("First sentence") and chunks[0].pause_after_ms == 350
    assert chunks[1].pause_after_ms == 700            # before paragraph break
    assert all(len(c.text) <= 280 for c in chunks)
    assert len(chunks) >= 4


def test_chunk_merges_tiny_sentences():
    chunks = chunk_text("No. Not again. The door was already open when I got home.")
    assert chunks[0].text.startswith("No. Not again.")


def test_trim_and_stitch():
    sr = 24000
    sil = np.zeros(sr // 2, dtype=np.float32)
    tone = 0.3 * np.sin(2 * np.pi * 440 * np.arange(sr // 4) / sr).astype(np.float32)
    x = np.concatenate([sil, tone, sil])
    t = trim_silence(x, sr)
    assert abs(len(t) - len(tone)) < sr // 50
    out = stitch([(tone, 350), (tone, 0)], sr)
    assert abs(len(out) - (2 * len(tone) + int(0.35 * sr))) < 10
```

```python
# tests/test_tts_engine.py
from pathlib import Path

from ns_tts.engine import FakeEngine, select_engine
from ns_tts.voices import VoiceLibrary, VoiceSpec
from ns_common.models import TtsCfg


def test_fake_engine_duration():
    e = FakeEngine(); e.load()
    x = e.synth("one two three four five", VoiceSpec(name="Ryan", mode="preset", speaker="Ryan"))
    assert 1.5 <= len(x) / e.sr <= 2.5


def test_select_engine_env_fake(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("NS_TTS_FAKE", "1")
    assert select_engine(TtsCfg(), tmp_path).name == "fake"


def test_voice_library(tmp_path: Path):
    lib = VoiceLibrary(tmp_path / "voices.json")
    assert lib.get("Ryan").mode == "preset"
    lib.add_design("velvet", "deep calm male narrator, slow")
    assert lib.get("velvet").description.startswith("deep")
    import pytest
    with pytest.raises(ValueError):
        lib.add_clone("me", str(tmp_path / "ref.wav"), "hello", rights_confirmed=False)
    lib.add_clone("me", str(tmp_path / "ref.wav"), "hello", rights_confirmed=True)
    assert {v.name for v in lib.list()} >= {"Ryan", "velvet", "me"}
    assert VoiceLibrary(tmp_path / "voices.json").get("me").ref_text == "hello"
```

```python
# tests/test_tts_server.py
from pathlib import Path

from fastapi.testclient import TestClient

from ns_tts.engine import FakeEngine
from ns_tts.server import create_app
from ns_tts.voices import VoiceLibrary


def _client(tmp_path):
    e = FakeEngine(); e.load()
    return TestClient(create_app(e, VoiceLibrary(tmp_path / "voices.json")))


def test_health_and_voices(tmp_path: Path):
    c = _client(tmp_path)
    assert c.get("/health").json() == {"ok": True, "engine": "fake", "model_loaded": True}
    assert any(v["name"] == "Ryan" for v in c.get("/v1/voices").json())


def test_speech_returns_wav(tmp_path: Path):
    c = _client(tmp_path)
    r = c.post("/v1/audio/speech", json={"model": "qwen3-tts", "input": "hello there friend", "voice": "Ryan", "speed": 1.0, "response_format": "wav"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("audio/wav") and r.content[:4] == b"RIFF"


def test_batch_writes_files(tmp_path: Path):
    c = _client(tmp_path)
    r = c.post("/v1/audio/speech/batch", json={"items": [{"id": "a", "input": "one two", "voice": "Ryan", "speed": 1.0}], "out_dir": str(tmp_path)})
    body = r.json()
    assert body["engine"] == "fake" and body["files"][0]["id"] == "a" and Path(body["files"][0]["path"]).exists()


def test_clone_requires_rights(tmp_path: Path):
    c = _client(tmp_path)
    r = c.post("/v1/voices/clone", json={"name": "x", "ref_audio_path": "r.wav", "ref_text": "t", "rights_confirmed": False})
    assert r.status_code == 400
```

Requires `fastapi` + `httpx` in the dev extra for the test client: add `"fastapi>=0.115"` to `dev` in `pyproject.toml` (lightweight; no uvicorn needed for tests).

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement `ns_tts/audio.py`**

```python
from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ns_common.textstats import split_sentences

PAUSE_SENTENCE, PAUSE_PARAGRAPH, PAUSE_CLIFF = 350, 700, 1200


@dataclass(frozen=True)
class Chunk:
    text: str
    pause_after_ms: int


def _split_long(s: str, max_chars: int) -> list[str]:
    if len(s) <= max_chars:
        return [s]
    out, cur = [], ""
    for piece in s.replace(";", ";|").replace(",", ",|").replace(" — ", " —|").split("|"):
        if len(cur) + len(piece) > max_chars and cur:
            out.append(cur.strip()); cur = piece
        else:
            cur += piece
    if cur.strip():
        out.append(cur.strip())
    final: list[str] = []
    for o in out:                       # hard wrap anything still too long on word boundaries
        words, line = o.split(), ""
        for w in words:
            if len(line) + len(w) + 1 > max_chars and line:
                final.append(line); line = w
            else:
                line = f"{line} {w}".strip()
        if line:
            final.append(line)
    return final


def chunk_text(text: str, *, max_chars: int = 280, min_chars: int = 25, cliff_lines: set[str] | None = None) -> list[Chunk]:
    cliff_lines = cliff_lines or set()
    chunks: list[Chunk] = []
    paragraphs = [p for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
    for pi, para in enumerate(paragraphs):
        sentences = split_sentences(para)
        merged: list[str] = []
        for s in sentences:
            if merged and len(merged[-1]) < min_chars:
                merged[-1] = f"{merged[-1]} {s}"
            else:
                merged.append(s)
        for si, s in enumerate(merged):
            pieces = _split_long(s, max_chars)
            for pj, piece in enumerate(pieces):
                last_in_sentence = pj == len(pieces) - 1
                last_in_para = last_in_sentence and si == len(merged) - 1
                if not last_in_sentence:
                    pause = 120
                elif s in cliff_lines or (si + 1 < len(merged) and merged[si + 1] in cliff_lines):
                    pause = PAUSE_CLIFF
                elif last_in_para and pi < len(paragraphs) - 1:
                    pause = PAUSE_PARAGRAPH
                elif last_in_para:
                    pause = 0
                else:
                    pause = PAUSE_SENTENCE
                chunks.append(Chunk(piece, pause))
    return chunks


def trim_silence(x: np.ndarray, sr: int, thresh_db: float = -45.0) -> np.ndarray:
    if x.size == 0:
        return x
    amp = 10 ** (thresh_db / 20)
    idx = np.where(np.abs(x) > amp)[0]
    if idx.size == 0:
        return x[: sr // 20]
    pad = sr // 100
    return x[max(0, idx[0] - pad): min(len(x), idx[-1] + pad)]


def stitch(chunks: list[tuple[np.ndarray, int]], sr: int) -> np.ndarray:
    parts: list[np.ndarray] = []
    for audio, pause_ms in chunks:
        parts.append(audio.astype(np.float32))
        if pause_ms > 0:
            parts.append(np.zeros(int(sr * pause_ms / 1000), dtype=np.float32))
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


def write_wav(path: Path, x: np.ndarray, sr: int) -> float:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(x, -1.0, 1.0)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((pcm * 32767).astype("<i2").tobytes())
    return len(x) / sr
```

- [ ] **Step 4: Implement `ns_tts/voices.py`**

```python
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

PRESETS = ["Ryan", "Aiden", "Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ono_Anna", "Sohee"]
LANG = {"en": "English", "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "de": "German", "fr": "French",
        "ru": "Russian", "pt": "Portuguese", "es": "Spanish", "it": "Italian"}


@dataclass
class VoiceSpec:
    name: str
    mode: Literal["preset", "design", "clone"]
    speaker: str | None = None
    description: str | None = None
    ref_audio: str | None = None
    ref_text: str | None = None
    language: str = "English"


class VoiceLibrary:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._custom: dict[str, VoiceSpec] = {}
        if path.exists():
            for d in json.loads(path.read_text(encoding="utf-8")):
                self._custom[d["name"]] = VoiceSpec(**d)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([asdict(v) for v in self._custom.values()], indent=2), encoding="utf-8")

    def list(self) -> list[VoiceSpec]:
        return [VoiceSpec(name=p, mode="preset", speaker=p) for p in PRESETS] + list(self._custom.values())

    def get(self, name: str) -> VoiceSpec:
        if name in PRESETS:
            return VoiceSpec(name=name, mode="preset", speaker=name)
        if name in self._custom:
            return self._custom[name]
        raise KeyError(name)

    def add_design(self, name: str, description: str, language: str = "English") -> VoiceSpec:
        v = VoiceSpec(name=name, mode="design", description=description, language=language)
        self._custom[name] = v; self._save(); return v

    def add_clone(self, name: str, ref_audio_path: str, ref_text: str, *, rights_confirmed: bool, language: str = "English") -> VoiceSpec:
        if not rights_confirmed:
            raise ValueError("rights_confirmed must be true to clone a voice")
        v = VoiceSpec(name=name, mode="clone", ref_audio=ref_audio_path, ref_text=ref_text, language=language)
        self._custom[name] = v; self._save(); return v
```

- [ ] **Step 5: Implement `ns_tts/engine.py`**

```python
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from ns_common.models import TtsCfg
from ns_common.textstats import word_count
from ns_tts.voices import VoiceSpec

MODEL_DIRS = {"preset": "CustomVoice", "design": "VoiceDesign", "clone": "Base"}


class Engine(Protocol):
    name: str
    sr: int

    def load(self) -> None: ...
    def synth(self, text: str, voice: VoiceSpec, *, speed: float = 1.0, seed: int | None = None) -> np.ndarray: ...


class FakeEngine:
    name = "fake"
    sr = 24000

    def load(self) -> None:
        self.loaded = True

    def synth(self, text: str, voice: VoiceSpec, *, speed: float = 1.0, seed: int | None = None) -> np.ndarray:
        words = max(1, word_count(text))
        n = int(max(0.4, words / (155 * speed) * 60) * self.sr)
        t = np.arange(n) / self.sr
        burst = (np.mod(np.arange(n), max(1, n // words)) < int(0.08 * self.sr)).astype(np.float32)
        return (0.3 * np.sin(2 * math.pi * 440 * t) * burst).astype(np.float32)


def _speed(x: np.ndarray, speed: float) -> np.ndarray:
    if abs(speed - 1.0) < 1e-3:
        return x
    idx = np.arange(0, len(x), speed)
    return np.interp(idx, np.arange(len(x)), x).astype(np.float32)


class _QwenBase:
    """Shared logic: picks the model dir by voice mode, lazy-loads, dispatches to the engine-specific generate_* calls."""

    sr = 24000

    def __init__(self, models_dir: Path, size: str, device: str) -> None:
        self.models_dir, self.size, self.device = models_dir, size, device
        self.models: dict[str, Any] = {}

    def _dir(self, mode: str) -> Path:
        return self.models_dir / f"Qwen3-TTS-12Hz-{self.size}-{MODEL_DIRS[mode]}"

    def load(self) -> None:
        self._get("preset")

    def _get(self, mode: str) -> Any:
        if mode not in self.models:
            if self.device == "cpu" and self.models:
                self.models.clear()
            self.models[mode] = self._load_model(self._dir(mode))
        return self.models[mode]

    def _load_model(self, path: Path) -> Any:
        raise NotImplementedError

    def synth(self, text: str, voice: VoiceSpec, *, speed: float = 1.0, seed: int | None = None) -> np.ndarray:
        if seed is not None:
            import torch
            torch.manual_seed(seed)
        m = self._get(voice.mode)
        if voice.mode == "preset":
            wavs, sr = m.generate_custom_voice(text=text, language=voice.language, speaker=voice.speaker)
        elif voice.mode == "design":
            wavs, sr = m.generate_voice_design(text=text, language=voice.language, instruct=voice.description)
        else:
            wavs, sr = m.generate_voice_clone(text=text, language=voice.language, ref_audio=voice.ref_audio, ref_text=voice.ref_text)
        self.sr = int(sr)
        return _speed(np.asarray(wavs[0], dtype=np.float32).reshape(-1), speed)


class ReferenceEngine(_QwenBase):
    name = "reference"

    def _load_model(self, path: Path) -> Any:
        import torch
        from qwen_tts import Qwen3TTSModel
        dtype = torch.float32 if self.device == "cpu" else torch.bfloat16
        return Qwen3TTSModel.from_pretrained(str(path), device_map=self.device if self.device == "cpu" else "cuda:0",
                                             dtype=dtype, attn_implementation="sdpa")


class FasterEngine(_QwenBase):
    name = "faster"

    def _load_model(self, path: Path) -> Any:
        from faster_qwen3_tts import FasterQwen3TTS
        m = FasterQwen3TTS.from_pretrained(str(path))
        m.warmup(prefill_len=100)
        return m


def select_engine(cfg: TtsCfg, models_dir: Path, *, prefer: str | None = None) -> Engine:
    if os.environ.get("NS_TTS_FAKE") == "1" or prefer == "fake":
        return FakeEngine()
    size = "0.6B" if cfg.device == "cpu" else "1.7B"
    want = prefer or cfg.engine
    return FasterEngine(models_dir, size, cfg.device) if want == "faster" else ReferenceEngine(models_dir, size, cfg.device)
```

- [ ] **Step 6: Implement `ns_tts/server.py`**

```python
from __future__ import annotations

import argparse
import io
import time
import wave
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

from ns_common.config import load_runtime
from ns_common.paths import Paths
from ns_tts.audio import chunk_text, stitch, trim_silence, write_wav
from ns_tts.engine import Engine, select_engine
from ns_tts.voices import VoiceLibrary


class SpeechReq(BaseModel):
    model: str = "qwen3-tts"
    input: str
    voice: str
    speed: float = 1.0
    response_format: str = "wav"
    seed: int | None = None


class BatchItem(BaseModel):
    id: str
    input: str
    voice: str
    speed: float = 1.0


class BatchReq(BaseModel):
    items: list[BatchItem]
    out_dir: str
    seed: int | None = None


class DesignReq(BaseModel):
    name: str
    description: str


class CloneReq(BaseModel):
    name: str
    ref_audio_path: str
    ref_text: str
    rights_confirmed: bool = False


def _render(engine: Engine, text: str, voice, speed: float, seed: int | None) -> np.ndarray:
    pieces = [(trim_silence(engine.synth(c.text, voice, speed=speed, seed=seed), engine.sr), c.pause_after_ms) for c in chunk_text(text)]
    return stitch(pieces, engine.sr)


def create_app(engine: Engine, library: VoiceLibrary) -> FastAPI:
    app = FastAPI(title="Nightshift TTS")
    state = {"loaded": hasattr(engine, "loaded") or bool(getattr(engine, "models", {}))}

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "engine": engine.name, "model_loaded": bool(state["loaded"] or getattr(engine, "models", None))}

    @app.get("/v1/voices")
    def voices() -> list[dict]:
        return [{"name": v.name, "mode": v.mode, "source": "builtin" if v.mode == "preset" else "library"} for v in library.list()]

    @app.post("/v1/audio/speech")
    def speech(req: SpeechReq) -> Response:
        try:
            voice = library.get(req.voice)
        except KeyError:
            raise HTTPException(404, f"unknown voice {req.voice}") from None
        x = _render(engine, req.input, voice, req.speed, req.seed)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(engine.sr)
            w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
        return Response(content=buf.getvalue(), media_type="audio/wav")

    @app.post("/v1/audio/speech/batch")
    def batch(req: BatchReq) -> dict:
        t0, total = time.perf_counter(), 0.0
        out_dir = Path(req.out_dir)
        files = []
        for it in req.items:
            voice = library.get(it.voice)
            x = _render(engine, it.input, voice, it.speed, req.seed)
            secs = write_wav(out_dir / f"{it.id}.wav", x, engine.sr)
            total += secs
            files.append({"id": it.id, "path": str(out_dir / f"{it.id}.wav"), "seconds": round(secs, 3)})
        rtf = (time.perf_counter() - t0) / total if total else 0.0
        return {"files": files, "engine": engine.name, "rtf": round(rtf, 3)}

    @app.post("/v1/voices/design")
    def design(req: DesignReq) -> dict:
        v = library.add_design(req.name, req.description)
        sample = library.path.parent / f"{req.name}.sample.wav"
        write_wav(sample, _render(engine, "This is a short sample of the designed voice.", v, 1.0, None), engine.sr)
        return {"name": v.name, "sample_path": str(sample)}

    @app.post("/v1/voices/clone")
    def clone(req: CloneReq) -> dict:
        try:
            v = library.add_clone(req.name, req.ref_audio_path, req.ref_text, rights_confirmed=req.rights_confirmed)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        sample = library.path.parent / f"{req.name}.sample.wav"
        write_wav(sample, _render(engine, "This is a short sample of the cloned voice.", v, 1.0, None), engine.sr)
        return {"name": v.name, "sample_path": str(sample)}

    return app


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--engine", choices=["faster", "reference", "fake"], default=None)
    a = ap.parse_args()
    paths = Paths.from_env()
    rt = load_runtime(paths)
    engine = select_engine(rt.tts, paths.models_tts, prefer=a.engine)
    engine.load()
    lib = VoiceLibrary(paths.models_tts_voices / "voices.json")
    uvicorn.run(create_app(engine, lib), host="127.0.0.1", port=a.port or rt.tts.port)


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: Run** → all three test files pass.

- [ ] **Step 8: Commit**

```bash
git add ns_tts pyproject.toml tests/test_tts_*.py
git commit -m "feat(tts): engine abstraction, chunk/stitch audio, voice library, FastAPI server"
```

---

### Task 7: Voice stage (WP-E, `sonnet`)

**Files:**
- Create: `ns_studio/media/voice.py`, `ns_studio/media/stages_media.py` (starts the media stage list), `tests/test_voice_stage.py`

**Interfaces:**
- Produces:
```python
# voice.py
def narration_items(parts: list[dict], voice_name: str, speed: float) -> list[TtsItem]
    # one item per part body: id f"p{n}" text=part text; plus f"p{n}_hook" for part hook (when hook != first sentence)
def loudnorm(in_wav: Path, out_wav: Path, *, i: float = -16.0, tp: float = -1.5, lra: float = 11.0) -> None   # ffmpeg loudnorm single pass, 48 kHz output
def measure_lufs(wav: Path) -> tuple[float, float]     # (integrated LUFS, true peak dBTP) via ebur128 parse
class VoiceStage: name="voice"; tenant="tts"; inputs=("parts.json",); outputs=("audio/manifest.json",)
    # calls ctx.services.tts.speak_batch(items, job_dir/audio/raw, seed=hash(job_id)); loudnorm each to audio/<id>.wav;
    # writes audio/manifest.json = {"<id>": {"path", "seconds", "lufs", "tp"}}; updates parts.json est_seconds with measured body seconds
```
`stages_media.py` registers `VoiceStage` and appends `"voice"` to `FORMAT_STAGES["shorts_only"]`; later tasks append `captions`, `clips`, `render`, `qc`, `outbox`.

- [ ] **Step 1: Write failing test**

```python
# tests/test_voice_stage.py
import json
from pathlib import Path

import pytest

from ns_common import ffmpeg as ff
from ns_studio.media import stages_media  # noqa: F401
from ns_studio.media.voice import loudnorm, measure_lufs, narration_items
from ns_studio.stages import REGISTRY
from tests.helpers import make_ctx

pytestmark = pytest.mark.skipif(not ff.available(), reason="ffmpeg required")

PARTS = [{"n": 1, "text": "The doorbell camera pings at 3:12 every night. I stopped checking the clips a week ago. Tonight my phone buzzed while I was standing on the porch.",
          "hook": "My doorbell camera records someone every night at 3:12.", "word_count": 28, "est_seconds": 10.8, "cliffhanger": False}]


def test_items_include_body_and_hook():
    items = narration_items(PARTS, "Ryan", 1.0)
    assert [i.id for i in items] == ["p1", "p1_hook"]


def test_voice_stage_normalizes(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.write_json("parts.json", PARTS)
    REGISTRY["voice"].run(ctx)
    man = ctx.read_json("audio/manifest.json")
    assert set(man) == {"p1", "p1_hook"}
    lufs, tp = measure_lufs(Path(man["p1"]["path"]))
    assert -19 <= lufs <= -13 and tp <= -1.0
    parts = ctx.read_json("parts.json")
    assert abs(parts[0]["est_seconds"] - man["p1"]["seconds"]) < 0.01
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement**

```python
# ns_studio/media/voice.py
from __future__ import annotations

import json
import re
import subprocess
import zlib
from pathlib import Path

from ns_common import ffmpeg as ff
from ns_common.textstats import split_sentences
from ns_common.tts_client import TtsItem
from ns_studio.context import JobContext


def narration_items(parts: list[dict], voice_name: str, speed: float) -> list[TtsItem]:
    items: list[TtsItem] = []
    for p in parts:
        items.append(TtsItem(f"p{p['n']}", p["text"], voice_name, speed))
        hook = (p.get("hook") or "").strip()
        if hook and hook != split_sentences(p["text"])[0]:
            items.append(TtsItem(f"p{p['n']}_hook", hook, voice_name, speed))
    return items


def loudnorm(in_wav: Path, out_wav: Path, *, i: float = -16.0, tp: float = -1.5, lra: float = 11.0) -> None:
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    ff.run(["-i", str(in_wav), "-af", f"loudnorm=I={i}:TP={tp}:LRA={lra}", "-ar", "48000", "-ac", "1", str(out_wav)])


_I = re.compile(r"I:\s*(-?[\d.]+) LUFS")
_TP = re.compile(r"Peak:\s*(-?[\d.]+) dBFS")


def measure_lufs(wav: Path) -> tuple[float, float]:
    cmd = [ff.ffmpeg_exe(), "-hide_banner", "-nostats", "-i", str(wav), "-af", "ebur128=peak=true", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = proc.stderr[proc.stderr.rfind("Summary:"):]
    i = _I.search(tail); tp = _TP.search(tail)
    return (float(i.group(1)) if i else float("nan"), float(tp.group(1)) if tp else float("nan"))


class VoiceStage:
    name, tenant, inputs, outputs = "voice", "tts", ("parts.json",), ("audio/manifest.json",)

    def run(self, ctx: JobContext) -> None:
        parts = ctx.read_json("parts.json")
        voice = ctx.channel.voice
        voice_name = voice.name or voice.speaker or "Ryan"
        items = narration_items(parts, voice_name, voice.speed)
        raw_dir = ctx.subdir("audio/raw")
        seed = zlib.crc32(ctx.job_id.encode()) & 0x7FFFFFFF
        files = ctx.services.tts.speak_batch(items, raw_dir, seed=seed)
        manifest: dict[str, dict] = {}
        for f in files:
            out = ctx.job_dir / "audio" / f"{f.id}.wav"
            loudnorm(f.path, out)
            lufs, tp = measure_lufs(out)
            manifest[f.id] = {"path": str(out), "seconds": round(ff.duration_s(out), 3), "lufs": lufs, "tp": tp}
        for p in parts:
            p["est_seconds"] = manifest[f"p{p['n']}"]["seconds"]
        ctx.write_json("parts.json", parts)
        ctx.write_json("audio/manifest.json", manifest)
```

```python
# ns_studio/media/stages_media.py
from __future__ import annotations

from ns_studio.orchestrator import FORMAT_STAGES
from ns_studio.stages import register
from ns_studio.media.voice import VoiceStage

for _st in (VoiceStage(),):
    register(_st)
    FORMAT_STAGES["shorts_only"].append(_st.name)
```

- [ ] **Step 4: Run** → 2 passed.

- [ ] **Step 5: Commit**

```bash
git add ns_studio/media/voice.py ns_studio/media/stages_media.py tests/test_voice_stage.py
git commit -m "feat(media): voice stage with batch TTS and loudness normalization"
```

---
