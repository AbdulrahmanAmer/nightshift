"""Transcription: Word/Segment types, Transcriber protocol, fake and faster-whisper engines (component C6).

faster-whisper is imported lazily inside `WhisperTranscriber` so this module imports cleanly in CI.
"""

from __future__ import annotations

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
from ns_common.textstats import split_sentences

_TOKEN = re.compile(r"[A-Za-z0-9']+")


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


def sidecar_path(wav: Path) -> Path:
    """Script sidecar of an audio file: `p1.wav` -> `p1.txt` (written by the voice stage)."""
    return wav.with_suffix(".txt")


def _audio_seconds(wav: Path) -> float:
    try:
        with wave.open(str(wav), "rb") as w:
            return w.getnframes() / w.getframerate()
    except (wave.Error, EOFError):
        return ff.duration_s(wav)


class FakeTranscriber:
    """Spreads the words of the `<wav>.txt` sidecar evenly over the WAV duration.

    Word count follows `ns_common.textstats.word_count`, the same count `FakeTts` uses for its tone bursts,
    so fake timings line up with the fake audio.
    """

    name = "fake"

    def words(self, wav: Path, language: str = "en") -> list[Word]:
        """One Word per script token, evenly spaced; NS-E403 when the sidecar is missing."""
        side = sidecar_path(wav)
        if not side.exists():
            raise NsError("NS-E403", f"fake transcriber needs the script sidecar {side.name}")
        tokens = _TOKEN.findall(side.read_text(encoding="utf-8"))
        if not tokens:
            return []
        dur = _audio_seconds(wav)
        step = dur / len(tokens)
        return [Word(t, i * step, i * step + step * 0.8) for i, t in enumerate(tokens)]

    def segments(self, wav: Path, language: str = "en") -> list[Segment]:
        """One Segment per sentence of the sidecar, built from the evenly spaced words."""
        words = self.words(wav, language)
        side = sidecar_path(wav)
        out: list[Segment] = []
        cursor = 0
        for sentence in split_sentences(side.read_text(encoding="utf-8")):
            n = len(_TOKEN.findall(sentence))
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
                raise NsError("NS-E202", f"whisper failed to load: {e}") from e
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
        except Exception as e:
            raise NsError("NS-E202", f"whisper transcription failed: {e}") from e
        out: list[Segment] = []
        for s in segs:
            words = tuple(
                Word(str(w.word).strip(), float(w.start), float(w.end))
                for w in (s.words or [])
                if str(w.word).strip()
            )
            out.append(Segment(float(s.start), float(s.end), str(s.text).strip(), words))
        return out

    def words(self, wav: Path, language: str = "en") -> list[Word]:
        """All words of `wav` with timestamps (VAD on)."""
        return [w for seg in self._run(wav, language) for w in seg.words]

    def segments(self, wav: Path, language: str = "en") -> list[Segment]:
        """Segments with word timestamps (VAD on)."""
        return self._run(wav, language)
