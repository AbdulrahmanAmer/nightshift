"""Tail `logs/*.jsonl` with component and level filters (component C9)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "WARNING": 30, "ERROR": 40}
MAX_BYTES = 512 * 1024


def component_of(file: Path) -> str:
    """`supervisor-20261101.jsonl` -> `supervisor`."""
    stem = file.stem
    head, _, tail = stem.rpartition("-")
    return head if head and tail.isdigit() else stem


def _tail_lines(file: Path, max_bytes: int) -> list[str]:
    try:
        size = file.stat().st_size
        with file.open("rb") as fh:
            start = max(0, size - max_bytes)
            fh.seek(start)
            raw = fh.read()
    except OSError:
        return []
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # the first line is probably cut in half
    return lines


def tail(
    logs_dir: Path,
    *,
    component: str | None = None,
    level: str | None = None,
    n: int = 200,
    after: str | None = None,
    max_bytes: int = MAX_BYTES,
) -> dict[str, Any]:
    """Last `n` records, oldest first, with `level` as a minimum and `after` as an exclusive timestamp."""
    n = max(1, min(int(n), 2000))
    files = sorted(logs_dir.glob("*.jsonl")) if logs_dir.exists() else []
    by_component: dict[str, list[Path]] = {}
    for f in files:
        by_component.setdefault(component_of(f), []).append(f)
    components = sorted(by_component)
    minimum = LEVELS.get((level or "").upper(), 0)
    records: list[dict[str, Any]] = []
    for name, comp_files in by_component.items():
        if component and name != component:
            continue
        for f in comp_files[-2:]:
            for line in _tail_lines(f, max_bytes):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or "ts" not in rec:
                    continue
                if LEVELS.get(str(rec.get("level", "")).upper(), 20) < minimum:
                    continue
                if after and str(rec["ts"]) <= after:
                    continue
                rec.setdefault("component", name)
                records.append(rec)
    records.sort(key=lambda r: str(r["ts"]))
    return {"lines": records[-n:], "components": components}
