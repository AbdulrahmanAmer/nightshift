"""Transcriber protocol and implementations (component C6; stub until the C6 plan lands)."""

from __future__ import annotations

from typing import Protocol


class Transcriber(Protocol):
    """Speech-to-words interface used by the caption stages."""

    name: str


class FakeTranscriber:
    """Test double that spreads script words over a WAV."""

    name = "fake"


class WhisperTranscriber:
    """faster-whisper backed transcriber (real implementation arrives with C6)."""

    name = "whisper"

    def __init__(self, cfg: object, paths: object) -> None:
        self.cfg, self.paths = cfg, paths
