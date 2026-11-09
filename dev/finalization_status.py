"""Count the boxes of docs/FINALIZATION.md and docs/FEATURE_STATUS.md (section H) per section; `--check`
fails on a ticked box without evidence.

    python dev/finalization_status.py          # a table: section, done, open, and the open ids
    python dev/finalization_status.py --check  # exit 1 when a ticked box has no `evidence:` or an id repeats

A tick may cite a proof script as its evidence: `evidence: proof:<proof id> (runs/proofs/<ver>/F-B7.json)`.
`--check` then also fails when no `proofs/*.py` declares that id, or it does not declare the box (H-13).
It checks only that the id exists: it does not read the evidence file, which lives under `runs/` and not in
the repository. That the proof passed on the named version is for Review to see in the evidence that the
tick PR cites.

Every item is one line `- [ ] F-<section><n> <text>` or `- [x] ... evidence: <pointer>`; the section letter
must match the heading it sits under. `tests/test_finalization.py` runs the check in CI.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs" / "FINALIZATION.md"
#: docs/FEATURE_STATUS.md carries section H (a box per partial or missing spec row); both files are one list.
DOCS = (DOC, DOC.with_name("FEATURE_STATUS.md"))
ITEM = re.compile(r"^- \[(?P<mark>[ xX])\] (?P<id>F-(?P<section>[A-Z])\d+) (?P<text>.+)$")
HEADING = re.compile(r"^## (?P<section>[A-Z])\. ")
#: `proof:<id>` inside a ticked box's evidence names a proof script under proofs/ (see dev/prove.py)
PROOF_REF = re.compile(r"(?<![\w-])proof:(?P<id>[a-z0-9][a-z0-9_-]*)")


@dataclass(frozen=True)
class Item:
    id: str
    section: str
    done: bool
    text: str
    line: int


def known_proofs() -> dict[str, list[str]]:
    """Valid proof scripts under proofs/: proof id -> its box ids (read with ast, nothing runs)."""
    try:
        from dev import prove
    except ImportError:  # run as a script: dev/ is on the path, the repo root is not
        import prove  # type: ignore[import-not-found,no-redef]
    return {p.id: p.boxes for p in prove.discover() if p.valid}


def parse(text: str, proofs: dict[str, list[str]] | None = None) -> tuple[list[Item], list[str]]:
    """Items in order plus the problems found (section mismatch, duplicate id, done without evidence, a
    ticked box citing a proof id that `proofs` (id -> boxes) does not know or that lacks this box)."""
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
        if item.done and proofs is not None and "evidence:" in item.text:
            for ref in PROOF_REF.finditer(item.text.split("evidence:", 1)[1]):
                declared = proofs.get(ref.group("id"))
                if declared is None:
                    problems.append(
                        f"line {n}: {item.id} cites proof {ref.group('id')}: no proofs/*.py declares it"
                    )
                elif item.id not in declared:
                    problems.append(f"line {n}: proof {ref.group('id')} does not declare box {item.id}")
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


def parse_all(docs: tuple[Path, ...] = DOCS) -> tuple[list[Item], list[str]]:
    """Items and problems of every list file that exists."""
    items: list[Item] = []
    problems: list[str] = []
    proofs = known_proofs()
    for doc in docs:
        if doc.exists():
            i, p = parse(doc.read_text(encoding="utf-8"), proofs)
            items += i
            problems += [f"{doc.name}: {x}" for x in p]
    return items, problems


def main(argv: list[str]) -> int:
    items, problems = parse_all()
    print(table(items))
    if problems:
        print("\n".join(problems))
    if "--check" in argv:
        return 1 if problems else 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
