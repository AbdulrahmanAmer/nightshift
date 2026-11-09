"""Count the boxes of docs/FINALIZATION.md per section; `--check` fails on a ticked box without evidence.

    python dev/finalization_status.py          # a table: section, done, open, and the open ids
    python dev/finalization_status.py --check  # exit 1 when a ticked box has no `evidence:` or an id repeats

Every item is one line `- [ ] F-<section><n> <text>` or `- [x] ... evidence: <pointer>`; the section letter
must match the heading it sits under. `tests/test_finalization.py` runs the check in CI.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs" / "FINALIZATION.md"
ITEM = re.compile(r"^- \[(?P<mark>[ xX])\] (?P<id>F-(?P<section>[A-Z])\d+) (?P<text>.+)$")
HEADING = re.compile(r"^## (?P<section>[A-Z])\. ")


@dataclass(frozen=True)
class Item:
    id: str
    section: str
    done: bool
    text: str
    line: int


def parse(text: str) -> tuple[list[Item], list[str]]:
    """Items in order plus the problems found (section mismatch, duplicate id, done without evidence)."""
    items: list[Item] = []
    problems: list[str] = []
    section = ""
    seen: set[str] = set()
    for n, line in enumerate(text.splitlines(), start=1):
        h = HEADING.match(line)
        if h:
            section = h.group("section")
            continue
        m = ITEM.match(line)
        if not m:
            if line.startswith("- ["):
                problems.append(f"line {n}: a box without a valid id (`- [ ] F-<section><n> ...`)")
            continue
        item = Item(m.group("id"), m.group("section"), m.group("mark") != " ", m.group("text"), n)
        if item.section != section:
            problems.append(f"line {n}: {item.id} sits under section {section or '?'}")
        if item.id in seen:
            problems.append(f"line {n}: {item.id} appears twice")
        seen.add(item.id)
        if item.done and "evidence:" not in item.text:
            problems.append(f"line {n}: {item.id} is ticked without `evidence:`")
        items.append(item)
    if not items:
        problems.append("no items found")
    return items, problems


def table(items: list[Item]) -> str:
    rows = ["section  done  open  open ids"]
    for section in sorted({i.section for i in items}):
        mine = [i for i in items if i.section == section]
        open_ids = " ".join(i.id for i in mine if not i.done)
        done, left = sum(i.done for i in mine), sum(not i.done for i in mine)
        rows.append(f"{section:<8} {done:>4}  {left:>4}  {open_ids}")
    rows.append(f"total    {sum(i.done for i in items):>4}  {sum(not i.done for i in items):>4}")
    return "\n".join(rows)


def main(argv: list[str]) -> int:
    items, problems = parse(DOC.read_text(encoding="utf-8"))
    print(table(items))
    if problems:
        print("\n".join(problems))
    if "--check" in argv:
        return 1 if problems else 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
