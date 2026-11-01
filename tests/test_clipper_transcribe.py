"""C8 Clipper: transcription stage logic (sidecar mode, speaker heuristic, NS-E406)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ns_common.errors import NsError
from ns_studio.clipper import transcribe_stage as ts
from ns_studio.clipper.models import Segment, Transcript, Word
from tests.fixtures.clipper.make_sample import build_transcript, make_sample


def seg(i: int, start: float, end: float, text: str, speaker: int | None = None) -> Segment:
    return Segment(i=i, start=start, end=end, text=text, speaker=speaker)


def tr_of(segments: list[Segment], n_words: int = 40) -> Transcript:
    words = [Word(w=f"w{k}", start=k * 0.3, end=k * 0.3 + 0.2, seg=0) for k in range(n_words)]
    return Transcript(source="x.mp4", duration_s=60.0, segments=segments, words=words)


class FakeT:
    name = "fake"


class RealishT:
    """Stands in for C6's Transcriber: objects for segments, dicts for words."""

    name = "whisper"

    def segments(self, wav: Path, language: str = "en") -> list[Any]:
        return [
            SimpleNamespace(start=0.0, end=6.0, text=" Hello there. "),
            SimpleNamespace(start=8.0, end=14.0, text="General Kenobi."),
        ]

    def words(self, wav: Path, language: str = "en") -> list[Any]:
        out = []
        for k in range(40):
            t = k * 0.35
            out.append({"word": f" w{k}", "start": t, "end": t + 0.25})
        return out


def test_sidecar_loads_and_validates(tmp_path: Path) -> None:
    sample = make_sample(tmp_path, with_video=False)
    assert len(ts.load_sidecar(sample.transcript).segments) == 6
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(NsError) as ei:
        ts.load_sidecar(bad)
    assert ei.value.code == "NS-E406"


def test_speaker_changes_on_pause_for_the_fixture() -> None:
    out = ts.assign_speakers(Transcript.model_validate(build_transcript()))
    assert [s.speaker for s in out.segments] == [0, 1, 0, 1, 0, 1]


def test_no_change_on_short_pause() -> None:
    out = ts.assign_speakers(tr_of([seg(0, 0, 5, "One."), seg(1, 5.5, 9, "Two.")]))
    assert [s.speaker for s in out.segments] == [0, 0]


def test_question_then_answer_switches_speaker() -> None:
    out = ts.assign_speakers(tr_of([seg(0, 0, 5, "Is it ready?"), seg(1, 5.5, 9, "Yes it is.")]))
    assert [s.speaker for s in out.segments] == [0, 1]


def test_question_without_pause_does_not_switch() -> None:
    out = ts.assign_speakers(tr_of([seg(0, 0, 5, "Is it ready?"), seg(1, 5.1, 9, "Yes it is.")]))
    assert [s.speaker for s in out.segments] == [0, 0]


def test_existing_speakers_are_kept() -> None:
    out = ts.assign_speakers(tr_of([seg(0, 0, 5, "A.", 1), seg(1, 20, 25, "B.", 1)]))
    assert [s.speaker for s in out.segments] == [1, 1]


def test_finalize_requires_thirty_words() -> None:
    with pytest.raises(NsError) as ei:
        ts.finalize(tr_of([seg(0, 0, 5, "A.")], n_words=29))
    assert ei.value.code == "NS-E406"
    assert ts.finalize(tr_of([seg(0, 0, 5, "A.")], n_words=30)).segments[0].speaker == 0


def test_fake_mode_reads_sidecar(tmp_path: Path) -> None:
    sample = make_sample(tmp_path, with_video=False)
    out = ts.run_transcription(
        FakeT(), tmp_path / "audio.wav", sidecar=sample.transcript, source_name="source.mp4", duration_s=90.2
    )
    assert out.source == "source.mp4" and out.duration_s == 90.2
    assert out.segments[1].speaker == 1 and len(out.words) > 200


def test_fake_mode_without_sidecar_is_e406(tmp_path: Path) -> None:
    with pytest.raises(NsError) as ei:
        ts.run_transcription(FakeT(), tmp_path / "a.wav", sidecar=None, source_name="s.mp4", duration_s=10)
    assert ei.value.code == "NS-E406"


def test_real_mode_assembles_transcript_from_a_transcriber(tmp_path: Path) -> None:
    out = ts.run_transcription(
        RealishT(), tmp_path / "a.wav", sidecar=None, source_name="s.mp4", duration_s=20.0
    )
    assert [s.text for s in out.segments] == ["Hello there.", "General Kenobi."]
    assert out.words[0].w == "w0" and out.words[0].seg == 0
    assert out.words[-1].seg == 1  # t = 13.65 s lies in the second segment
    assert [s.speaker for s in out.segments] == [0, 1]  # 2 s pause
