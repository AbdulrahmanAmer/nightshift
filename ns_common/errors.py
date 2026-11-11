from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from ns_common.commandline import render
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
    """The code's message and hint as `contracts/errors.json` has them: the RAW form, with the plain
    `ns ...` and `studio ...` words. Kept as they are in what is stored (a note, an error in the database, a
    log line); a reader renders them where it shows them (`describe_shown`, `ns_common.commandline.render`).
    """
    return load_errors()[code]


def describe_shown(code: str, home: Path | str | None = None) -> ErrorDef:
    """`describe` for a text SHOWN now (an HTTP answer, a console line): every `ns ...` and `studio ...`
    given as the owner of `home` (default: this process's NIGHTSHIFT_HOME) must type it in a new terminal
    (`ns_common.commandline.render`). Never stored: what a plain `ns` reaches changes."""
    d = load_errors()[code]
    return ErrorDef(code=d.code, message=render(d.message, home), hint=render(d.hint, home), kind=d.kind)


class NsError(Exception):
    def __init__(self, code: str, detail: str = "", **data: Any) -> None:
        self.code = code
        self.detail = detail                # raw: stored as it is, rendered where shown (FN-3)
        self.data = data
        self.definition = describe(code)
        super().__init__(str(self))

    def __str__(self) -> str:
        base = f"{self.code}: {self.definition.message}"
        return f"{base} ({self.detail})" if self.detail else base
