"""Exemplar stories used as craft references in the writing prompts (component C5)."""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml

from ns_common.paths import Paths

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


@cache
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


def _owner_exemplars(folder: str) -> list[dict[str, Any]]:
    """The owner's own example stories from `<home>/exemplars` (spec A.5): `<niche folder>/` or the top level.

    A folder with a `meta.yaml` in our format is used as is; otherwise every `.md` / `.txt` file is one
    exemplar (title from the file name, no structure notes). At most three; empty when nothing is there.
    """
    root = Paths.from_env().exemplars
    for d in (root / folder, root):
        if not d.is_dir():
            continue
        try:
            if (d / "meta.yaml").is_file():
                meta = yaml.safe_load((d / "meta.yaml").read_text(encoding="utf-8"))
                return [
                    {
                        "title": e["title"],
                        "text": (d / e["file"]).read_text(encoding="utf-8").replace("\r\n", "\n").strip(),
                        "structure": e.get("structure", ""),
                        "pov_tone": e.get("pov_tone", ""),
                        "why": e.get("why", "Chosen by the channel owner."),
                    }
                    for e in meta["exemplars"][:3]
                ]
            files = sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in (".md", ".txt"))
        except (OSError, KeyError, TypeError, yaml.YAMLError):
            continue  # an unreadable folder falls through to the shipped exemplars
        out = []
        for p in files[:3]:
            text = p.read_text(encoding="utf-8").replace("\r\n", "\n").strip()
            if text:
                out.append({"title": p.stem, "text": text, "structure": "", "pov_tone": "",
                            "why": "An example story chosen by the channel owner."})
        if out:
            return out
    return []


def load_exemplars(niche: str) -> list[dict[str, Any]]:
    """Up to three `{title, text, structure, pov_tone, why}` exemplars for `niche`.

    The owner's own stories in `<home>/exemplars` win over the shipped ones. `niche` is either a folder name
    (`horror`, `mystery`, `revenge`, `wholesome_twist`) or free text that `niche_folder` maps to one.
    """
    folder = niche if niche in FOLDERS else niche_folder(niche)
    return _owner_exemplars(folder) or [dict(e) for e in _load(folder)]
