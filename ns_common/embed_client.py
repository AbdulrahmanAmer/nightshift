from __future__ import annotations

import hashlib
import math
import re
import struct
import unicodedata
from collections.abc import Sequence
from typing import Protocol

import httpx

from ns_common.errors import NsError
from ns_common.textstats import CJK_RANGES, normalize_tokens, split_sentences

#: llama-server rejects an input longer than its physical batch (512 tokens by default) with HTTP 500.
MAX_EMBED_TOKENS = 512
#: Estimated tokens of the opening the studio embeds in one input (as `ContinuityStage` stores it). It stays
#: under the 512-token default batch, so it also fits a server started without `-b`/`-ub`.
EMBED_WINDOW_TOKENS = 480

_CJK = re.compile(rf"[{CJK_RANGES}가-힯ᄀ-ᇿ㄰-㆏]")
# Tokens per character by script. nomic-embed-text-v1.5 uses the bert-base-uncased vocabulary: about one
# token per CJK/Hangul character, roughly 2.5 characters per Cyrillic token, about 4 per Latin token (the
# English average is nearer 4.5), and short pieces for other scripts.
_LATIN_PER_CHAR = 0.25
_CYRILLIC_PER_CHAR = 0.4
_OTHER_PER_CHAR = 0.5


def approx_tokens(text: str) -> int:
    """Tokenizer-free token estimate for the embedding model, by script.

    Latin ~4 characters per token, Cyrillic ~2.5, other alphabets ~2, CJK and Hangul 1 per character.
    Spaces and punctuation count as Latin text. `FakeEmbed` uses this same estimate to refuse inputs.
    """
    total = 0.0
    for ch in text:
        if _CJK.match(ch):
            total += 1.0
        elif "Ѐ" <= ch <= "ԯ":
            total += _CYRILLIC_PER_CHAR
        elif ord(ch) < 0x250 or not unicodedata.category(ch).startswith("L"):
            total += _LATIN_PER_CHAR
        else:
            total += _OTHER_PER_CHAR
    return math.ceil(total)


def _clip_to_tokens(text: str, budget: int) -> str:
    """The longest prefix of `text` within `budget` estimated tokens, cut at a word boundary when there is one."""
    lo, hi = 0, len(text)
    while lo < hi:  # the estimate grows with the prefix, so binary search the cut
        mid = (lo + hi + 1) // 2
        if approx_tokens(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    cut = text[:lo]
    space = cut.rfind(" ")
    return cut[:space] if space > lo // 2 else cut


def embed_window(text: str, batch_tokens: int = MAX_EMBED_TOKENS) -> str:
    """The opening of `text` that fits one embedding input; short texts are unchanged.

    The budget is `EMBED_WINDOW_TOKENS`, capped at 90 % of the server's `batch_tokens` (`EmbedCfg.batch_size`).
    Whole sentences are kept while they fit; a first sentence over the budget is cut at a word boundary.
    """
    budget = max(32, min(EMBED_WINDOW_TOKENS, int(batch_tokens * 0.9)))
    if approx_tokens(text) <= budget:
        return text
    parts: list[str] = []
    for sentence in split_sentences(text):
        candidate = " ".join([*parts, sentence])
        if approx_tokens(candidate) > budget:
            break
        parts.append(sentence)
    return " ".join(parts) if parts else _clip_to_tokens(text, budget)


class Embed(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def health(self) -> bool: ...


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 0.0 if na == 0 or nb == 0 else dot / (na * nb)


def to_blob(vec: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def from_blob(b: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(b) // 4}f", b))


class EmbedClient:
    def __init__(self, base_url: str, *, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=120)

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            r = self.client.post(f"{self.base_url}/v1/embeddings", json={"input": texts})
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            return [[float(x) for x in d["embedding"]] for d in data]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise NsError("NS-E316", f"embed: {e}") from e

    def health(self) -> bool:
        try:
            return self.client.get(f"{self.base_url}/health", timeout=5).status_code == 200
        except httpx.HTTPError:
            return False


class FakeEmbed:
    """Hashed bag-of-words vectors. Like llama-server it refuses an input over `max_tokens`.

    Tokens are counted with `approx_tokens` (about chars / 4 for Latin, 1 per CJK character), so a non-Latin
    story that overflows the real server overflows this fake too.
    """

    def __init__(self, dim: int = 256, *, max_tokens: int = MAX_EMBED_TOKENS) -> None:
        self.dim = dim
        self.max_tokens = max_tokens

    def _one(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in normalize_tokens(text):
            h = hashlib.blake2b(tok.encode(), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "little") % self.dim
            sign = 1.0 if h[4] % 2 == 0 else -1.0
            v[idx] += sign
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed(self, texts: list[str]) -> list[list[float]]:
        for t in texts:
            if approx_tokens(t) > self.max_tokens:
                raise NsError(
                    "NS-E316", f"embed: input is too large to process ({approx_tokens(t)} tokens estimated)"
                )
        return [self._one(t) for t in texts]

    def health(self) -> bool:
        return True
