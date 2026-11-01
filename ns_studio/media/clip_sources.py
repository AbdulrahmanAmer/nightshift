"""Stock clip sources (component C6; stub until the C6 plan lands)."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ClipSource(Protocol):
    """A searchable stock footage provider."""

    name: str


class FakeClipSource:
    """Test double producing synthetic FFmpeg clips."""

    name = "fake"

    def __init__(self, root: Path) -> None:
        self.root = root


class PexelsSource:
    """Pexels video search."""

    name = "pexels"

    def __init__(self, key: str, cache_dir: Path) -> None:
        self.key, self.cache_dir = key, cache_dir


class PixabaySource:
    """Pixabay video search."""

    name = "pixabay"

    def __init__(self, key: str, cache_dir: Path) -> None:
        self.key, self.cache_dir = key, cache_dir
