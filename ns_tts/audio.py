"""C4 ns_tts: text chunking and PCM helpers (pure numpy + stdlib, spec 7.5).

Audio is float32 mono in [-1, 1] everywhere inside the service; WAV files are 16-bit PCM.
"""

from __future__ import annotations

import io
import re
import struct
import wave
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ns_common.textstats import split_sentences

PAUSE_CLAUSE_MS = 120  # between the pieces of one sentence that had to be split (not in the spec)
PAUSE_SENTENCE_MS = 350
PAUSE_PARAGRAPH_MS = 700
PAUSE_CLIFF_MS = 1200

_CLAUSE_SPLIT = re.compile("(?<=[,;:—–])\\s+")
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")


@dataclass(frozen=True)
class Chunk:
    """One synthesis unit and the silence (milliseconds) to insert after it."""

    text: str
    pause_after_ms: int


@dataclass(frozen=True)
class _Unit:
    text: str
    first_in_paragraph: bool
    cliff: bool
    continues_sentence: bool


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _wrap_words(piece: str, max_chars: int) -> list[str]:
    """Greedy word wrap; a single token longer than max_chars is cut hard."""
    if len(piece) <= max_chars:
        return [piece]
    lines: list[str] = []
    line = ""
    for word in piece.split():
        while len(word) > max_chars:
            if line:
                lines.append(line)
                line = ""
            lines.append(word[:max_chars])
            word = word[max_chars:]
        if line and len(line) + 1 + len(word) > max_chars:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    if line:
        lines.append(line)
    return lines


def _split_long(sentence: str, max_chars: int) -> list[str]:
    """Split one sentence longer than max_chars on clause punctuation, then on words."""
    if len(sentence) <= max_chars:
        return [sentence]
    pieces: list[str] = []
    cur = ""
    for clause in _CLAUSE_SPLIT.split(sentence):
        if cur and len(cur) + 1 + len(clause) > max_chars:
            pieces.append(cur)
            cur = clause
        else:
            cur = f"{cur} {clause}" if cur else clause
    if cur:
        pieces.append(cur)
    out: list[str] = []
    for p in pieces:
        out.extend(_wrap_words(p, max_chars))
    return out


def chunk_text(
    text: str,
    *,
    max_chars: int = 280,
    min_chars: int = 25,
    cliff_lines: Iterable[str] | None = None,
) -> list[Chunk]:
    """Split narration into synthesis chunks with the pause that follows each (spec 7.5).

    Sentences come from `ns_common.textstats.split_sentences`. A sentence shorter than `min_chars` is
    merged with the next one in the same paragraph (never into or across a cliff line). A chunk is capped
    at `max_chars` by splitting on clause punctuation, then on words. A blank line starts a new paragraph.
    `cliff_lines` are sentence texts (whitespace-normalised equality) that get 1200 ms of silence before
    them. The pause after a chunk depends on what follows it: another piece of the same sentence 120 ms,
    a cliff line 1200 ms, a new paragraph 700 ms, otherwise 350 ms; the last chunk gets 0.
    """
    cliffs = {_norm(c) for c in (cliff_lines or ())}
    paragraphs = [p for p in _PARAGRAPH_SPLIT.split(text.replace("\r\n", "\n")) if p.strip()]
    units: list[_Unit] = []
    for para in paragraphs:
        merged: list[tuple[str, bool]] = []  # (text, starts with a cliff line)
        for s in split_sentences(para):
            is_cliff = _norm(s) in cliffs
            if merged and len(merged[-1][0]) < min_chars and not is_cliff:
                merged[-1] = (f"{merged[-1][0]} {s}", merged[-1][1])
            else:
                merged.append((s, is_cliff))
        for i, (sentence, is_cliff) in enumerate(merged):
            for j, piece in enumerate(_split_long(sentence, max_chars)):
                units.append(
                    _Unit(
                        text=piece,
                        first_in_paragraph=(i == 0 and j == 0),
                        cliff=is_cliff and j == 0,
                        continues_sentence=j > 0,
                    )
                )
    chunks: list[Chunk] = []
    for k, u in enumerate(units):
        if k + 1 == len(units):
            pause = 0
        else:
            nxt = units[k + 1]
            if nxt.continues_sentence:
                pause = PAUSE_CLAUSE_MS
            elif nxt.cliff:
                pause = PAUSE_CLIFF_MS
            elif nxt.first_in_paragraph:
                pause = PAUSE_PARAGRAPH_MS
            else:
                pause = PAUSE_SENTENCE_MS
        chunks.append(Chunk(u.text, pause))
    return chunks


def trim_silence(x: np.ndarray, sr: int, thresh_db: float = -45.0) -> np.ndarray:
    """Cut leading and trailing samples quieter than `thresh_db` dBFS, keeping 10 ms of margin.

    Audio that is entirely below the threshold is reduced to its first 50 ms rather than emptied.
    """
    if x.size == 0:
        return x
    amp = 10.0 ** (thresh_db / 20.0)
    idx = np.nonzero(np.abs(x) > amp)[0]
    if idx.size == 0:
        return x[: sr // 20]
    pad = sr // 100
    return x[max(0, int(idx[0]) - pad) : min(len(x), int(idx[-1]) + 1 + pad)]


def stitch(chunks: list[tuple[np.ndarray, int]], sr: int) -> np.ndarray:
    """Concatenate (audio, pause_after_ms) pairs into one float32 array."""
    parts: list[np.ndarray] = []
    for audio, pause_ms in chunks:
        parts.append(audio.astype(np.float32))
        if pause_ms > 0:
            parts.append(np.zeros(int(sr * pause_ms / 1000), dtype=np.float32))
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


def wav_bytes(x: np.ndarray, sr: int) -> bytes:
    """Encode float audio as a 16-bit PCM mono WAV file in memory."""
    buf = io.BytesIO()
    pcm = np.round(np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def write_wav(path: Path, x: np.ndarray, sr: int) -> float:
    """Write 16-bit PCM mono WAV (creating parent folders); returns the duration in seconds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(wav_bytes(x, sr))
    return len(x) / sr


def _decode_pcm(raw: bytes, width: int) -> np.ndarray:
    if width == 1:
        return (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    if width == 2:
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        return v.astype(np.float32) / float(1 << 23)
    if width == 4:
        return np.frombuffer(raw, dtype="<i4").astype(np.float32) / float(1 << 31)
    raise ValueError(f"unsupported PCM sample width {width}")


def _read_riff(data: bytes) -> tuple[np.ndarray, int, int]:
    """Parse a RIFF/WAVE file by hand (the stdlib `wave` module rejects IEEE-float WAVs)."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    pos = 12
    fmt: tuple[int, int, int, int] | None = None  # tag, channels, rate, bits
    pcm: bytes | None = None
    while pos + 8 <= len(data):
        cid = data[pos : pos + 4]
        size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        body = data[pos + 8 : pos + 8 + size]
        if cid == b"fmt ":
            tag, ch, rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])
            if tag == 0xFFFE and len(body) >= 26:  # WAVE_FORMAT_EXTENSIBLE: real tag in the sub-format
                tag = struct.unpack("<H", body[24:26])[0]
            fmt = (tag, ch, rate, bits)
        elif cid == b"data":
            pcm = body
        pos += 8 + size + (size & 1)
    if fmt is None or pcm is None:
        raise ValueError("WAV file has no fmt or data chunk")
    tag, ch, rate, bits = fmt
    width = bits // 8
    pcm = pcm[: len(pcm) // width * width]
    if tag == 3 and bits in (32, 64):
        x = np.frombuffer(pcm, dtype="<f4" if bits == 32 else "<f8").astype(np.float32)
    elif tag == 1:
        x = _decode_pcm(pcm, width)
    else:
        raise ValueError(f"unsupported WAV format tag {tag} / {bits} bits")
    return x, ch, rate


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Read a WAV file as (float32 mono samples, sample rate). Multi-channel audio is averaged.

    Handles PCM 8/16/24/32-bit and IEEE float 32/64-bit; raises ValueError on anything else.
    """
    x, ch, rate = _read_riff(path.read_bytes())
    if ch > 1:
        x = x[: len(x) // ch * ch].reshape(-1, ch).mean(axis=1)
    return np.ascontiguousarray(x, dtype=np.float32), int(rate)
