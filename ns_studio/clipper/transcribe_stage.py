"""C8 Clipper: transcription (spec 19.2 C3).

Real mode asks C6's Transcriber for `segments()` and `words()` of `audio.wav`; fake mode reads the
`<video>.transcript.json` sidecar that the fixture generator writes. Either way the result gets a speaker
index per segment (heuristic: a pause of 1.2 s or more, or a question followed by an answer, switches
speaker) and must hold at least 30 words (NS-E406).
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ns_common.errors import NsError
from ns_studio.clipper.models import Segment, Transcript, Word

MIN_WORDS = 30
SPEAKER_PAUSE_S = 1.2
QA_MIN_PAUSE_S = 0.3


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    for n in names:
        if isinstance(obj, dict):
            if n in obj:
                return obj[n]
        elif hasattr(obj, n):
            return getattr(obj, n)
    return default


def load_sidecar(path: Path) -> Transcript:
    """Read and validate a sidecar transcript."""
    try:
        return Transcript.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise NsError("NS-E406", f"bad sidecar {path.name}: {e}") from e


def assign_speakers(tr: Transcript) -> Transcript:
    """Give every segment without a speaker a 0/1 index using the pause and question-answer heuristic."""
    segs = sorted(tr.segments, key=lambda s: (s.start, s.i))
    out: list[Segment] = []
    speaker = 0
    prev: Segment | None = None
    for s in segs:
        if s.speaker is not None:
            speaker = s.speaker
            out.append(s)
        else:
            if prev is not None:
                pause = s.start - prev.end
                answer = prev.text.rstrip().endswith("?") and pause >= QA_MIN_PAUSE_S
                if pause >= SPEAKER_PAUSE_S or answer:
                    speaker = 1 - (speaker % 2)
            out.append(s.model_copy(update={"speaker": speaker}))
        prev = s
    return tr.model_copy(update={"segments": out})


def _segment_index(segments: list[Segment], t: float) -> int:
    for s in segments:
        if s.start <= t <= s.end:
            return s.i
    return min(segments, key=lambda s: min(abs(s.start - t), abs(s.end - t))).i if segments else 0


def transcribe_with(
    transcriber: Any, wav: Path, *, source_name: str, duration_s: float, language: str = "en"
) -> Transcript:
    """Run a Transcriber (`segments(wav)` and `words(wav)`) and assemble a `Transcript`."""
    raw_segs: Iterable[Any] = transcriber.segments(wav, language)
    raw_words: Iterable[Any] = transcriber.words(wav, language)
    segments = [
        Segment(
            i=i,
            start=float(_get(s, "start", default=0.0)),
            end=float(_get(s, "end", default=0.0)),
            text=str(_get(s, "text", default="")).strip(),
            speaker=_get(s, "speaker"),
        )
        for i, s in enumerate(raw_segs)
    ]
    words: list[Word] = []
    for w in raw_words:
        start = float(_get(w, "start", default=0.0))
        end = float(_get(w, "end", default=start))
        words.append(
            Word(
                w=str(_get(w, "w", "word", "text", default="")).strip(),
                start=start,
                end=max(end, start),
                seg=_segment_index(segments, (start + end) / 2.0),
            )
        )
    return Transcript(
        source=source_name, duration_s=duration_s, language=language, segments=segments, words=words
    )


def finalize(tr: Transcript) -> Transcript:
    """Assign speakers and enforce the 30-word minimum (NS-E406)."""
    if len(tr.words) < MIN_WORDS:
        raise NsError("NS-E406", f"{len(tr.words)} words")
    return assign_speakers(tr)


def run_transcription(
    transcriber: Any,
    wav: Path,
    *,
    sidecar: Path | None,
    source_name: str,
    duration_s: float,
    language: str = "en",
) -> Transcript:
    """Produce the final transcript; fake transcribers must come with a sidecar."""
    if getattr(transcriber, "name", "") == "fake":
        if sidecar is None or not sidecar.is_file():
            raise NsError("NS-E406", "the fake transcriber needs a <video>.transcript.json sidecar")
        tr = load_sidecar(sidecar)
        tr = tr.model_copy(update={"source": source_name, "duration_s": duration_s})
    else:
        tr = transcribe_with(
            transcriber, wav, source_name=source_name, duration_s=duration_s, language=language
        )
    return finalize(tr)
