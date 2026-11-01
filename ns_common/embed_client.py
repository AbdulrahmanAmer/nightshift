from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Sequence
from typing import Protocol

import httpx

from ns_common.errors import NsError
from ns_common.textstats import normalize_tokens


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
            raise NsError("NS-E301", f"embed: {e}") from e

    def health(self) -> bool:
        try:
            return self.client.get(f"{self.base_url}/health", timeout=5).status_code == 200
        except httpx.HTTPError:
            return False


class FakeEmbed:
    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

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
        return [self._one(t) for t in texts]

    def health(self) -> bool:
        return True
