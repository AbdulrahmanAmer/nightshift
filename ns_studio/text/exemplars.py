"""Exemplar stories used as craft references in the writing prompts (component C5)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

EXEMPLARS_DIR = Path(__file__).resolve().parents[1] / "exemplars"
FOLDERS = ("horror", "mystery", "revenge", "wholesome_twist")
_MAP: list[tuple[str, tuple[str, ...]]] = [
    ("horror", ("horror", "scary", "creepy", "haunt")),
    ("mystery", ("mystery", "detective", "missing", "whodunit")),
    ("revenge", ("revenge", "payback", "karma", "petty")),
]


def niche_folder(niche: str) -> str:
    """Map a free-text niche to an exemplar folder; anything unknown falls back to `wholesome_twist`."""
    low = niche.lower()
    for folder, keys in _MAP:
        if any(k in low for k in keys):
            return folder
    return "wholesome_twist"


@lru_cache(maxsize=None)
def _load(folder_name: str) -> tuple[dict[str, str], ...]:
    folder = EXEMPLARS_DIR / folder_name
    meta = yaml.safe_load((folder / "meta.yaml").read_text(encoding="utf-8"))
    out: list[dict[str, str]] = []
    for e in meta["exemplars"][:3]:
        text = (folder / e["file"]).read_text(encoding="utf-8").replace("\r\n", "\n").strip()
        out.append(
            {
                "title": e["title"],
                "text": text,
                "structure": e["structure"],
                "pov_tone": e["pov_tone"],
                "why": e["why"],
            }
        )
    return tuple(out)


def load_exemplars(niche: str) -> list[dict[str, Any]]:
    """Up to three `{title, text, structure, pov_tone, why}` exemplars for `niche`.

    `niche` is either a folder name (`horror`, `mystery`, `revenge`, `wholesome_twist`) or free text
    that `niche_folder` maps to one.
    """
    folder = niche if niche in FOLDERS else niche_folder(niche)
    return [dict(e) for e in _load(folder)]
