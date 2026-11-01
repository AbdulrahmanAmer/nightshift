from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

from ns_common.paths import contracts_dir


@dataclass(frozen=True)
class ErrorDef:
    code: str
    message: str
    hint: str
    kind: Literal["error", "warning"]


@lru_cache(maxsize=1)
def load_errors() -> dict[str, ErrorDef]:
    raw = json.loads((contracts_dir() / "errors.json").read_text(encoding="utf-8"))
    out: dict[str, ErrorDef] = {}
    for code, d in raw.items():
        kind: Literal["error", "warning"] = "warning" if code.startswith("NS-W") else "error"
        out[code] = ErrorDef(code=code, message=d["message"], hint=d["hint"], kind=kind)
    return out


def describe(code: str) -> ErrorDef:
    return load_errors()[code]


class NsError(Exception):
    def __init__(self, code: str, detail: str = "", **data: Any) -> None:
        self.code = code
        self.detail = detail
        self.data = data
        self.definition = describe(code)
        super().__init__(str(self))

    def __str__(self) -> str:
        base = f"{self.code}: {self.definition.message}"
        return f"{base} ({self.detail})" if self.detail else base
