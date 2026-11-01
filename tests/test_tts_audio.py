import struct
from pathlib import Path

import numpy as np
import pytest

from ns_tts.audio import (
    PAUSE_CLAUSE_MS,
    PAUSE_CLIFF_MS,
    PAUSE_PARAGRAPH_MS,
    PAUSE_SENTENCE_MS,
    chunk_text,
    read_wav,
    stitch,
    trim_silence,
    write_wav,
)

S1 = "The door was already open when I got home."
S2 = "Nothing inside had been touched at all."
S3 = "Then the phone started ringing in the kitchen."


def test_pause_constants_match_spec():
    assert (PAUSE_SENTENCE_MS, PAUSE_PARAGRAPH_MS, PAUSE_CLIFF_MS) == (350, 700, 1200)


def test_sentence_and_paragraph_pauses():
    chunks = chunk_text(f"{S1} {S2}\n\n{S3}")
    assert [c.text for c in chunks] == [S1, S2, S3]
    assert [c.pause_after_ms for c in chunks] == [350, 700, 0]


def test_crlf_blank_line_is_a_paragraph_break():
    chunks = chunk_text(f"{S1}\r\n\r\n{S3}")
    assert [c.pause_after_ms for c in chunks] == [700, 0]


def test_cliff_line_gets_1200_before_it():
    chunks = chunk_text(f"{S1} {S2} {S3}", cliff_lines={S3})
    assert [c.pause_after_ms for c in chunks] == [350, 1200, 0]


def test_cliff_line_at_paragraph_start_beats_paragraph_pause():
    chunks = chunk_text(f"{S1}\n\n{S3}", cliff_lines=[f"  {S3}  "])
    assert [c.pause_after_ms for c in chunks] == [1200, 0]


def test_short_sentence_before_cliff_is_not_merged_into_it():
    chunks = chunk_text(f"Wait. {S3}", cliff_lines={S3})
    assert [c.text for c in chunks] == ["Wait.", S3]
    assert chunks[0].pause_after_ms == 1200


def test_tiny_sentences_merge_with_the_next():
    chunks = chunk_text("No. Not again. The door was already open when I got home.")
    assert len(chunks) == 1
    assert chunks[0].text == "No. Not again. The door was already open when I got home."


def test_short_last_sentence_of_paragraph_stays_alone():
    chunks = chunk_text(f"{S1} Run.\n\n{S3}")
    assert [c.text for c in chunks] == [S1, "Run.", S3]
    assert [c.pause_after_ms for c in chunks] == [350, 700, 0]


def test_long_sentence_is_wrapped_to_cap():
    text = "A very long sentence " * 20 + "ends here."
    chunks = chunk_text(text)
    assert len(chunks) >= 2
    assert all(len(c.text) <= 280 for c in chunks)
    assert " ".join(c.text for c in chunks) == text
    assert all(c.pause_after_ms == PAUSE_CLAUSE_MS for c in chunks[:-1])
    assert chunks[-1].pause_after_ms == 0


def test_long_sentence_prefers_clause_boundaries():
    text = ", ".join(f"clause number {i} has a handful of words in it" for i in range(12)) + "."
    chunks = chunk_text(text)
    assert len(chunks) >= 2
    assert all(len(c.text) <= 280 for c in chunks)
    assert all(c.text.endswith(",") for c in chunks[:-1])


def test_token_longer_than_cap_is_cut_hard():
    chunks = chunk_text("x" * 600 + ".")
    assert all(len(c.text) <= 280 for c in chunks)
    assert sum(len(c.text) for c in chunks) == 601


def test_empty_text_has_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("  \n\n  ") == []


def _tone(sr: int, seconds: float) -> np.ndarray:
    n = int(sr * seconds)
    return (0.3 * np.sin(2 * np.pi * 440 * np.arange(n) / sr)).astype(np.float32)


def test_trim_silence_keeps_tone_and_small_margin():
    sr = 24000
    sil = np.zeros(sr // 2, dtype=np.float32)
    tone = _tone(sr, 0.25)
    t = trim_silence(np.concatenate([sil, tone, sil]), sr)
    assert len(tone) <= len(t) <= len(tone) + 2 * (sr // 100) + 2


def test_trim_silence_all_quiet_keeps_50ms():
    sr = 24000
    assert len(trim_silence(np.zeros(sr, dtype=np.float32), sr)) == sr // 20
    assert trim_silence(np.zeros(0, dtype=np.float32), sr).size == 0


def test_stitch_inserts_pauses():
    sr = 24000
    tone = _tone(sr, 0.25)
    out = stitch([(tone, 350), (tone, 0)], sr)
    assert len(out) == 2 * len(tone) + int(0.35 * sr)
    assert out.dtype == np.float32
    assert stitch([], sr).size == 0


def test_wav_round_trip(tmp_path: Path):
    sr = 24000
    x = _tone(sr, 0.2)
    secs = write_wav(tmp_path / "a" / "b.wav", x, sr)
    assert secs == pytest.approx(0.2)
    y, sr2 = read_wav(tmp_path / "a" / "b.wav")
    assert sr2 == sr and y.dtype == np.float32 and len(y) == len(x)
    assert np.allclose(x, y, atol=1e-4)


def _riff(tag: int, channels: int, rate: int, bits: int, payload: bytes) -> bytes:
    fmt = struct.pack("<HHIIHH", tag, channels, rate, rate * channels * bits // 8, channels * bits // 8, bits)
    data = b"data" + struct.pack("<I", len(payload)) + payload
    body = b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_read_float32_wav(tmp_path: Path):
    p = tmp_path / "f.wav"
    p.write_bytes(_riff(3, 1, 22050, 32, np.array([0.5, -0.25], dtype="<f4").tobytes()))
    y, sr = read_wav(p)
    assert sr == 22050 and y.tolist() == [0.5, -0.25]


def test_read_24bit_wav(tmp_path: Path):
    p = tmp_path / "w24.wav"
    p.write_bytes(_riff(1, 1, 16000, 24, b"\x00\x00\x40" + b"\x00\x00\xc0"))
    y, _ = read_wav(p)
    assert y.tolist() == [0.5, -0.5]


def test_read_stereo_is_downmixed(tmp_path: Path):
    p = tmp_path / "s.wav"
    pcm = np.array([16384, -16384, 16384, 16384], dtype="<i2").tobytes()
    p.write_bytes(_riff(1, 2, 24000, 16, pcm))
    y, _ = read_wav(p)
    assert y.tolist() == [0.0, 0.5]


def test_read_rejects_garbage(tmp_path: Path):
    p = tmp_path / "bad.wav"
    p.write_bytes(b"not a wav file at all")
    with pytest.raises(ValueError):
        read_wav(p)
