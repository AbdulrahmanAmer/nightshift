"""Local full-text index (BM25, no embeddings) over the project docs for `search_docs` (component C9)."""
# Dense descriptor tables and long signatures: wrapping them hurts readability.
# ruff: noqa: E501

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ns_common.config import load_install_state
from ns_common.paths import Paths, repo_root

DOC_FILES = (
    "BUILD_SPEC.md",
    "README.md",
    "OPERATOR.md",
    "docs/ENGINEERING.md",
    "contracts/errors.json",
)
STOPWORDS = frozenset(
    "a an and are as at be by can do does for from has have how i if in into is it its me my of on or our so "
    "that the their then there these this to up was we what when where which who why will with you your".split()
)
WORD_RE = re.compile(r"[a-z0-9]+")
MAX_CHUNK_WORDS = 160
SNIPPET_CHARS = 700


def stem(word: str) -> str:
    """Very light stemmer: posting/posted/posts -> post."""
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("ed"):
        return word[:-2]
    if len(word) > 4 and word.endswith("es"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokenize(text: str) -> list[str]:
    """Lower-case, stop-word-free, lightly stemmed tokens."""
    return [stem(w) for w in WORD_RE.findall(text.lower()) if w not in STOPWORDS and len(w) > 1]


@dataclass
class Chunk:
    """One searchable passage with its place in the source file."""

    file: str
    heading: str
    line: int
    text: str


def _flush(chunks: list[Chunk], file: str, heading: str, line: int, buf: list[str]) -> None:
    text = " ".join(buf).strip()
    if text:
        chunks.append(Chunk(file, heading, line, text))
    buf.clear()


def chunk_markdown(file: str, text: str) -> list[Chunk]:
    """Split markdown into passages: each table row, and each paragraph (capped at ~160 words)."""
    chunks: list[Chunk] = []
    heading = ""
    buf: list[str] = []
    start = 1
    in_code = False
    for n, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if not in_code and line.startswith("#"):
            _flush(chunks, file, heading, start, buf)
            heading = line.lstrip("#").strip()
            continue
        if not in_code and line.startswith("|"):
            _flush(chunks, file, heading, start, buf)
            if set(line.replace("|", "").strip()) <= set("-: "):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            chunks.append(Chunk(file, heading, n, " | ".join(c for c in cells if c)))
            continue
        if not line.strip():
            _flush(chunks, file, heading, start, buf)
            continue
        if not buf:
            start = n
        buf.append(line.strip())
        if sum(len(b.split()) for b in buf) >= MAX_CHUNK_WORDS:
            _flush(chunks, file, heading, start, buf)
    _flush(chunks, file, heading, start, buf)
    return chunks


def chunk_errors(file: str, text: str) -> list[Chunk]:
    """One passage per error code."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    return [
        Chunk(file, code, 1, f"{code} {d.get('message', '')} {d.get('hint', '')}".strip())
        for code, d in data.items()
        if isinstance(d, dict)
    ]


class DocsIndex:
    """BM25 over passages of the docs; build once, search many times."""

    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        self._tf: list[Counter[str]] = []
        self._len: list[int] = []
        df: Counter[str] = Counter()
        for c in chunks:
            toks = tokenize(f"{c.heading} {c.text}")
            counts = Counter(toks)
            self._tf.append(counts)
            self._len.append(len(toks))
            df.update(counts.keys())
        self._n = len(chunks)
        self._avg = (sum(self._len) / self._n) if self._n else 0.0
        self._idf = {t: math.log(1 + (self._n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    @classmethod
    def from_root(cls, root: Path, files: tuple[str, ...] = DOC_FILES) -> DocsIndex:
        """Index whichever of `files` exist under `root`."""
        chunks: list[Chunk] = []
        for rel in files:
            f = root / rel
            if not f.is_file():
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            chunks += chunk_errors(rel, text) if rel.endswith(".json") else chunk_markdown(rel, text)
        return cls(chunks)

    def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        """Top `k` passages for `query`: `{file, heading, line, score, text}`."""
        terms = tokenize(query)
        if not terms or not self._n:
            return []
        k1, b = 1.5, 0.3  # long table rows must not be punished: the docs mix tiny and big passages
        scored: list[tuple[float, int]] = []
        for i, tf in enumerate(self._tf):
            score = 0.0
            norm = k1 * (1 - b + b * self._len[i] / (self._avg or 1.0))
            for t in terms:
                f = tf.get(t, 0)
                if f:
                    score += self._idf.get(t, 0.0) * f * (k1 + 1) / (f + norm)
            if score > 0:
                scored.append((score, i))
        scored.sort(key=lambda p: (-p[0], p[1]))
        out = []
        for score, i in scored[: max(1, k)]:
            c = self.chunks[i]
            out.append(
                {
                    "file": c.file,
                    "heading": c.heading,
                    "line": c.line,
                    "score": round(score, 3),
                    "text": c.text[:SNIPPET_CHARS],
                }
            )
        return out


_CACHE: dict[str, DocsIndex] = {}


def docs_root(paths: Paths) -> Path:
    """The app folder that holds the docs (`install_state.app_path`, else this checkout)."""
    try:
        app = load_install_state(paths).app_path
    except (OSError, ValueError):
        app = ""
    return Path(app) if app and Path(app).is_dir() else repo_root()


def get_index(paths: Paths) -> DocsIndex:
    """The shared index for this installation (built on first use)."""
    root = docs_root(paths)
    key = str(root)
    idx = _CACHE.get(key)
    if idx is None:
        idx = DocsIndex.from_root(root)
        _CACHE[key] = idx
    return idx
