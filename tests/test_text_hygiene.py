"""Tracked text files carry no stray control characters.

A BEL byte (0x07) reached PR 143: the spec said ``run\apphost-setup.json`` and a ``\\a`` turned into a control
character on its way into BUILD_SPEC.md. Nothing showed it on screen, and a reader searching for the file name
found nothing. This test fails on any control character other than tab, newline and carriage return in a
tracked text file, and names the file, the line and the character.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Allowed in text: tab, line feed, carriage return (CRLF files exist in this repository).
ALLOWED = {0x09, 0x0A, 0x0D}
#: Files that are binary by extension: not text, never scanned.
BINARY_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".icns",
    ".bmp",
    ".wav",
    ".mp3",
    ".mp4",
    ".ogg",
    ".ttf",
    ".otf",
    ".woff",
    ".woff2",
    ".zip",
    ".gz",
    ".7z",
    ".exe",
    ".dll",
    ".pyd",
    ".so",
    ".whl",
    ".pdf",
    ".onnx",
    ".gguf",
    ".bin",
    ".db",
    ".sqlite",
    ".pyc",
    ".lnk",
}


def tracked_files() -> list[Path]:
    """Every tracked file; empty (and the test skips) when git or the repository metadata is missing."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True, timeout=60
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [ROOT / name for name in out.decode("utf-8").split("\0") if name]


def control_characters(data: bytes) -> list[tuple[int, int, int]]:
    """``(line, column, code)`` of every control character that text may not carry. Binary data returns []."""
    if b"\0" in data[:8192]:
        return []  # a NUL early on: binary, not text
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []  # not UTF-8 text (an odd encoding or binary): out of scope for this check
    found: list[tuple[int, int, int]] = []
    for lineno, line in enumerate(text.split("\n"), start=1):
        for col, ch in enumerate(line, start=1):
            code = ord(ch)
            if (code < 0x20 or code == 0x7F) and code not in ALLOWED:
                found.append((lineno, col, code))
    return found


def test_the_scanner_finds_a_bel_byte_and_lets_tabs_and_crlf_through():
    assert control_characters(b"run\x07pphost-setup.json\n") == [(1, 4, 7)]
    assert control_characters(b"a\tb\r\nc\n") == []
    assert control_characters(b"\x1b[0m escape\n") == [(1, 1, 27)]
    assert control_characters(b"del\x7f\n") == [(1, 4, 127)]
    assert control_characters(b"binary\0\x07 stuff") == []  # NUL early: binary


def test_no_tracked_text_file_carries_a_control_character():
    files = tracked_files()
    if not files:
        pytest.skip("git ls-files gave nothing here (no repository metadata)")
    problems: list[str] = []
    for path in files:
        if path.suffix.lower() in BINARY_SUFFIXES or not path.is_file():
            continue
        for lineno, col, code in control_characters(path.read_bytes()):
            problems.append(
                f"{path.relative_to(ROOT).as_posix()}:{lineno}:{col} has control character 0x{code:02x}"
            )
    assert not problems, "control characters in tracked text files:\n" + "\n".join(problems[:50])
