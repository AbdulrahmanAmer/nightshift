"""Local full-text index (BM25, no embeddings) over the project docs for `search_docs` (component C9)."""
# Dense descriptor tables and long signatures: wrapping them hurts readability.
# ruff: noqa: E501

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ns_common.config import load_install_state
from ns_common.paths import Paths, repo_root
from ns_dashboard.assistant.registry import ToolSpec

#: What the Assistant may quote as fact: what the owner reads (README, operator guide), the error catalogue and the
#: generated list of what the Assistant itself can do. BUILD_SPEC.md and docs/ENGINEERING.md describe a build, not
#: this install (they name tools that do not exist and builder rules), so they are not indexed.
DOC_FILES = (
    "README.md",
    "OPERATOR.md",
    "contracts/errors.json",
)
CAPABILITIES_FILE = "capabilities (generated)"
ALLOWED_SOURCES = frozenset({*DOC_FILES, CAPABILITIES_FILE})
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
            _flush(chunks, file, heading, start, buf)
            in_code = not in_code
            continue
        if in_code:
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
    def from_root(cls, root: Path, files: tuple[str, ...] = DOC_FILES, extra: Iterable[Chunk] = ()) -> DocsIndex:
        """Index whichever of `files` exist under `root`, plus the `extra` chunks (the generated capability list)."""
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
        return cls([*chunks, *extra])

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


#: Plain facts the registry cannot generate: things people ask the Assistant to do that Nightshift never does.
FACTS: tuple[tuple[str, str], ...] = (
    (
        "upload to YouTube",
        "Can the Assistant upload, publish or post videos to YouTube? No. Nightshift never uploads. The owner posts "
        "each video by hand on YouTube and then pastes the link on the Queue page (Mark as posted).",
    ),
    (
        "approve or reject a video",
        "Can the Assistant approve or reject a video? No. Only the owner approves or rejects, on the Queue page (press A "
        "to approve, R to reject). The Assistant can prepare the decision with review_brief.",
    ),
)


def capability_chunks(specs: Iterable[ToolSpec], no_tool: Mapping[tuple[str, str], str]) -> list[Chunk]:
    """The generated "what can the Assistant do" passages: one per tool, one per thing it must never do."""
    out: list[Chunk] = []
    for s in sorted(specs, key=lambda x: x.name):
        how = "It asks the owner to confirm first." if s.side_effect else "It only reads and runs at once."
        out.append(Chunk(CAPABILITIES_FILE, s.name, 0, f"The Assistant tool {s.name}: {s.description} {how}"))
    for (method, path), reason in sorted(no_tool.items()):
        if not reason.startswith(("never", "destructive")):
            continue  # plumbing routes (downloads, streams) are not things anyone asks for
        words = reason.removeprefix("never:").strip()
        verb = path.rstrip("/").rsplit("/", 1)[-1] or path
        out.append(
            Chunk(CAPABILITIES_FILE, f"cannot: {verb}", 0, f"The Assistant cannot do this ({method} {path}): {words}.")
        )
    out += [Chunk(CAPABILITIES_FILE, heading, 0, text) for heading, text in FACTS]
    return out


def get_index(paths: Paths, capabilities: Callable[[], list[Chunk]] | None = None) -> DocsIndex:
    """The shared index for this installation (built on first use). `capabilities` adds the generated passages."""
    root = docs_root(paths)
    key = f"{root}|{'cap' if capabilities else 'plain'}"
    idx = _CACHE.get(key)
    if idx is None:
        idx = DocsIndex.from_root(root, extra=capabilities() if capabilities else ())
        _CACHE[key] = idx
    return idx
