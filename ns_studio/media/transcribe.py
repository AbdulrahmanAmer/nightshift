"""Transcription: Word/Segment types, Transcriber protocol, fake and faster-whisper engines (component C6).

faster-whisper is imported lazily inside `WhisperTranscriber` so this module imports cleanly in CI.
"""

from __future__ import annotations

import gc
import importlib
import os
import re
import sys
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_common.models import WhisperCfg
from ns_common.paths import Paths
from ns_common.textstats import CJK_RANGES, WORD_PATTERN, split_sentences

_TOKEN = re.compile(WORD_PATTERN)  # the same words word_count() counts, in any script
_CJK_CHAR = re.compile(rf"[{CJK_RANGES}]")
#: faster-whisper returns Japanese and Chinese as BPE pieces of one to three characters, not per character.
_PIECE_LENGTHS = (2, 1, 3, 2, 1)


@dataclass(frozen=True)
class Word:
    """A word with start/end seconds. `sentence_idx` is set by caption alignment (0 for raw ASR words)."""

    w: str
    start: float
    end: float
    sentence_idx: int = 0

    def to_json(self) -> dict[str, Any]:
        """The `words/*.json` record (spec §7.6)."""
        return {"w": self.w, "start": round(self.start, 3), "end": round(self.end, 3),
                "sentence_idx": self.sentence_idx}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Word:
        """Inverse of `to_json`."""
        return cls(str(d["w"]), float(d["start"]), float(d["end"]), int(d.get("sentence_idx", 0)))


@dataclass(frozen=True)
class Segment:
    """A transcript segment (used by the Clipper)."""

    start: float
    end: float
    text: str
    words: tuple[Word, ...] = ()


class Transcriber(Protocol):
    """Speech-to-text interface: word timestamps for caption alignment, segments for the Clipper."""

    name: str

    def words(self, wav: Path, language: str = "en") -> list[Word]:
        """Word-level transcript of `wav`."""
        ...

    def segments(self, wav: Path, language: str = "en") -> list[Segment]:
        """Segment-level transcript (with words) of `wav`."""
        ...

    def unload(self) -> None:
        """Free the model (GPU memory included); a later call loads it again. Safe when nothing is loaded."""
        ...


def sidecar_path(wav: Path) -> Path:
    """Script sidecar of an audio file: `p1.wav` -> `p1.txt` (written by the voice stage)."""
    return wav.with_suffix(".txt")


def _audio_seconds(wav: Path) -> float:
    try:
        with wave.open(str(wav), "rb") as w:
            return w.getnframes() / w.getframerate()
    except (wave.Error, EOFError):
        return ff.duration_s(wav)


# Pause after a word, in "letters" of speaking time: sentence ends are long, clause marks short.
_PAUSE_LETTERS = {
    ".": 9.0, "!": 9.0, "?": 9.0, "…": 9.0, "\u061f": 9.0, "\u3002": 9.0, "\uff01": 9.0, "\uff1f": 9.0,
    ",": 4.0, ";": 4.0, ":": 3.0, "-": 3.0, "\u060c": 4.0, "\u3001": 4.0,
}


def _asr_pieces(text: str) -> list[tuple[str, int, int]]:
    """(piece, start, end) of the words an ASR model would return for `text`.

    Latin-like scripts give one piece per word. Runs of CJK characters are cut into pieces of 1-3 characters
    like faster-whisper's BPE output, so the fake does not hand the aligner one token per script character
    (that hid the caption matching bug on Japanese and Chinese).
    """
    out: list[tuple[str, int, int]] = []
    run: list[re.Match[str]] = []

    def flush() -> None:
        i = k = 0
        while i < len(run):
            size = _PIECE_LENGTHS[k % len(_PIECE_LENGTHS)]
            chunk = run[i : i + size]
            out.append(("".join(m.group(0) for m in chunk), chunk[0].start(), chunk[-1].end()))
            i += size
            k += 1
        run.clear()

    for m in _TOKEN.finditer(text):
        if _CJK_CHAR.fullmatch(m.group(0)):
            if run and run[-1].end() != m.start():
                flush()
            run.append(m)
        else:
            flush()
            out.append((m.group(0), m.start(), m.end()))
    flush()
    return out


def _speech_weights(text: str) -> tuple[list[str], list[float], list[float]]:
    """ASR pieces of `text` with the speaking weight of each and the pause weight that follows it."""
    tokens: list[str] = []
    speak: list[float] = []
    pause: list[float] = []
    pieces = _asr_pieces(text)
    for i, (piece, _start, end) in enumerate(pieces):
        after = text[end : pieces[i + 1][1] if i + 1 < len(pieces) else len(text)]
        tokens.append(piece)
        speak.append(len(piece) + 2.0)
        pause.append(max((_PAUSE_LETTERS.get(c, 0.0) for c in after), default=0.0))
    return tokens, speak, pause


class FakeTranscriber:
    """Spreads the words of the `<wav>.txt` sidecar over the WAV duration with uneven, speech-like timing.

    Long words take longer than short ones and sentence ends are followed by a pause, so word starts are not
    evenly spaced (evenly spaced fakes hid the caption drift bug). Latin-like scripts give one word per script
    word; Japanese and Chinese give multi-character pieces like faster-whisper. The audio length comes from
    the WAV, so the piece count does not matter to `FakeTts`.
    """

    name = "fake"
    unloads = 0  # how often `unload` ran (tests check the orchestrator calls it)

    def unload(self) -> None:
        """Nothing is loaded; counted so tests can see the call."""
        self.unloads += 1

    def words(self, wav: Path, language: str = "en") -> list[Word]:
        """One Word per script token, evenly spaced; NS-E403 when the sidecar is missing."""
        side = sidecar_path(wav)
        if not side.exists():
            raise NsError("NS-E403", f"fake transcriber needs the script sidecar {side.name}")
        tokens, speak, pause = _speech_weights(side.read_text(encoding="utf-8"))
        if not tokens:
            return []
        scale = _audio_seconds(wav) / (sum(speak) + sum(pause))
        out: list[Word] = []
        cursor = 0.0
        for t, sp, pa in zip(tokens, speak, pause, strict=True):
            out.append(Word(t, cursor * scale, (cursor + sp * 0.9) * scale))
            cursor += sp + pa
        return out

    def segments(self, wav: Path, language: str = "en") -> list[Segment]:
        """One Segment per sentence of the sidecar, built from the evenly spaced words."""
        words = self.words(wav, language)
        side = sidecar_path(wav)
        out: list[Segment] = []
        cursor = 0
        for sentence in split_sentences(side.read_text(encoding="utf-8")):
            n = len(_asr_pieces(sentence))
            chunk = tuple(words[cursor : cursor + n])
            cursor += n
            if chunk:
                out.append(Segment(chunk[0].start, chunk[-1].end, sentence, chunk))
        return out


def ensure_gpu_dlls_on_path(search_roots: Sequence[Path] | None = None) -> list[Path]:
    """Put the cuBLAS/cuDNN DLL folders of the `nvidia-*-cu12` wheels on PATH (and the DLL search path).

    Looks for `<root>/nvidia/{cublas,cudnn,cuda_nvrtc}/bin` under every entry of `search_roots` (default
    `sys.path`). Returns the folders added; idempotent. A change request asks to move this into ns_common.
    """
    roots = list(search_roots) if search_roots is not None else [Path(p) for p in sys.path if p]
    current = os.environ.get("PATH", "").split(os.pathsep)
    added: list[Path] = []
    for root in roots:
        for sub in ("cublas", "cudnn", "cuda_nvrtc"):
            d = root / "nvidia" / sub / "bin"
            if d.is_dir() and str(d) not in current:
                current.insert(0, str(d))
                added.append(d)
                add_dir = getattr(os, "add_dll_directory", None)
                if add_dir is not None:
                    add_dir(str(d))
    if added:
        os.environ["PATH"] = os.pathsep.join(current)
    return added


class WhisperTranscriber:
    """faster-whisper backed transcriber; the model loads on first use from `models/whisper/<model>`."""

    name = "whisper"

    def __init__(self, cfg: WhisperCfg, paths: Paths) -> None:
        self.cfg = cfg
        self.paths = paths
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            if self.cfg.device == "cuda":
                ensure_gpu_dlls_on_path()
            try:
                mod = importlib.import_module("faster_whisper")
                model_dir = self.paths.models_whisper / self.cfg.model
                self._model = mod.WhisperModel(
                    str(model_dir), device=self.cfg.device, compute_type=self.cfg.compute_type
                )
            except Exception as e:
                raise NsError("NS-E411", f"whisper failed to load: {e}") from e
        return self._model

    def _run(self, wav: Path, language: str) -> list[Segment]:
        model = self._load()
        try:
            raw_segments, _info = model.transcribe(
                str(wav),
                language=language or None,
                word_timestamps=True,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300},
                condition_on_previous_text=False,
            )
            segs = list(raw_segments)
        except Exception as e:  # decoding, VAD or CUDA errors all surface here (hint: the audio track)
            raise NsError("NS-E408", f"{wav.name}: {type(e).__name__}: {e}") from e
        out: list[Segment] = []
        for s in segs:
            words = tuple(
                Word(str(w.word).strip(), float(w.start), float(w.end))
                for w in (s.words or [])
                if str(w.word).strip()
            )
            out.append(Segment(float(s.start), float(s.end), str(s.text).strip(), words))
        return out

    def unload(self) -> None:
        """Drop the faster-whisper model and give its memory back to the GPU.

        Releasing the `whisper` tenant frees the supervisor's lock but not the weights (about 1.5 GB plus the
        CUDA context) held by this process; the llama-server that starts next is sized for the whole card.
        """
        if self._model is None:
            return
        self._model = None
        gc.collect()
        try:  # torch is only present in venvs that also run the reference TTS engine
            torch = importlib.import_module("torch")
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

    def words(self, wav: Path, language: str = "en") -> list[Word]:
        """All words of `wav` with timestamps (VAD on)."""
        return [w for seg in self._run(wav, language) for w in seg.words]

    def segments(self, wav: Path, language: str = "en") -> list[Segment]:
        """Segments with word timestamps (VAD on)."""
        return self._run(wav, language)
