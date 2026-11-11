"""The model files' check record, and what a setup run over a finished install owes them.

Card model-file-damaged.

Standard library only: the setup window reads it (``controller``), next to the checker and the engine.

* ``logs/model-check.json`` holds one entry per model file, keyed by its path under the install folder
  (``models/llm/x.gguf``): ``verdict`` (the checker's word: ``ok``, ``bad hash``, ``missing``, ``unreadable``,
  ``ok (no pin)``), ``sha256_ok`` (True, False, or None when no hash was read), ``bytes`` and ``mtime_ns`` of
  the file when it was checked, ``checked_at`` (UTC) and ``seconds``. The one checker
  (``installer.stage2.verify_downloads.check_item``) writes it after each file, wherever it runs: ``ns verify
  --downloads``, a Repair (s03) and the engine after a model failed to load. Written whole each time
  (``write_text_atomic``); a torn or unreadable file reads as empty, and a lost entry only means that file
  counts as not checked.
* A ``bad hash`` entry is evidence only while the file still has the size and date it was checked with: a
  file replaced since (a Repair) is not "damaged" by an old entry.
* ``repair_owed(home)`` says why a setup run over a finished install has model work to do, from cheap reads
  only (stat calls, the ``.ok`` markers setup writes, the record): a recorded damaged file, a model file whose
  marker no longer matches it (gone, or its size or date changed), or a damaged file set aside (``.bad``) by a
  Repair that did not finish.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ns_common.atomic import read_text_retry, write_text_atomic

RECORD = "model-check.json"
BAD = "bad hash"          # verify_downloads.BAD: the one verdict that is evidence of damage
SET_ASIDE = ".bad"        # a damaged model file renamed by a Repair until its replacement verifies


def record_path(home: Path) -> Path:
    """``<home>/logs/model-check.json``."""
    return Path(home) / "logs" / RECORD


def key_of(home: Path, path: Path) -> str:
    """The record's key for a file: its path under the install folder with forward slashes."""
    try:
        return Path(path).relative_to(Path(home)).as_posix()
    except ValueError:
        return Path(path).as_posix()


def read_record(home: Path) -> dict[str, dict[str, Any]]:
    """The record, or an empty one when it is missing, torn or not what it should be."""
    try:
        data = json.loads(read_text_retry(record_path(home)))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def write_entry(home: Path, key: str, entry: dict[str, Any]) -> None:
    """Put one file's entry into the record (the whole file is written again, atomically)."""
    data = read_record(home)
    data[key] = entry
    target = record_path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(target, json.dumps(data, indent=2, sort_keys=True) + "\n")


def still_same(entry: dict[str, Any], path: Path) -> bool:
    """True when ``path`` has the size and date the entry was written with."""
    try:
        st = Path(path).stat()
    except OSError:
        return False
    return entry.get("bytes") == st.st_size and entry.get("mtime_ns") == st.st_mtime_ns


def recorded_damage(home: Path) -> dict[str, dict[str, Any]]:
    """The entries that say ``bad hash`` about a file that is still the one that was checked."""
    return {
        key: entry for key, entry in read_record(home).items()
        if entry.get("verdict") == BAD and still_same(entry, Path(home) / key)
    }


def marker_moved(marker: Path) -> str | None:
    """Why the file of a setup ``.ok`` marker is no longer the file setup verified (None: it still is)."""
    target = marker.with_name(marker.name[: -len(".ok")])
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None                     # not a marker setup can use: the step's own check decides
    if not isinstance(data, dict):
        return None
    try:
        st = target.stat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return None
    if data.get("size") != st.st_size:
        return "size changed"
    if data.get("mtime_ns") != st.st_mtime_ns:
        return "changed since it was downloaded"
    return None


def repair_owed(home: Path) -> list[str]:
    """One plain line per model file a setup run over this install has to check or fetch again."""
    root = Path(home) / "models"
    lines = [f"{key}: its SHA256 did not match at the check of {entry.get('checked_at') or '?'}"
             for key, entry in sorted(recorded_damage(home).items())]
    if not root.is_dir():
        return lines
    for folder, _dirs, files in os.walk(root):
        for name in sorted(files):
            path = Path(folder) / name
            if name.endswith(".ok"):
                why = marker_moved(path)
                if why is not None:
                    lines.append(f"{key_of(home, path)[: -len('.ok')]}: {why}")
            elif name.endswith(SET_ASIDE):
                lines.append(f"{key_of(home, path)}: a damaged copy set aside by an unfinished Repair")
    return lines
