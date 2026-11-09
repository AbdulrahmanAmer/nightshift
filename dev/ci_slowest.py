"""List the slowest tests of the shards' JUnit reports, for the job summary of the final ``test`` job (H-3).

``python dev/ci_slowest.py [--top 20] [--summary] reports/*.xml`` prints a table of the slowest tests across
all reports (the time pytest records per test: setup, call and teardown). With ``--summary`` the same table is
appended to ``$GITHUB_STEP_SUMMARY`` so a test that grows slow shows up on every run. Always exits 0: it is
information, and a missing or unreadable report is skipped. Standard library only.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Row:
    seconds: float
    test: str
    source: str


def _source(path: Path) -> str:
    """``report-shard-2.xml`` -> ``shard 2``; ``report-network.xml`` -> ``network``."""
    m = re.fullmatch(r"report-(shard)-(\d+)", path.stem)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return path.stem.removeprefix("report-")


def read_cases(paths: list[Path]) -> list[Row]:
    rows: list[Row] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        source = _source(path)
        for case in root.iter("testcase"):
            name = f"{case.get('classname', '')}::{case.get('name', '')}".strip(":")
            rows.append(Row(float(case.get("time") or 0), name, source))
    return rows


def slowest(paths: list[Path], top: int = 20) -> list[Row]:
    """The ``top`` slowest tests over all reports, slowest first (ties by name, so the order is stable)."""
    return sorted(read_cases(paths), key=lambda r: (-r.seconds, r.test, r.source))[:top]


def render(rows: list[Row], paths: list[Path]) -> str:
    cases = read_cases(paths)
    reports = sum(1 for p in paths if p.is_file())
    total = sum(c.seconds for c in cases)
    lines = [
        f"### {len(rows)} slowest tests",
        "",
        "| # | seconds | test | report |",
        "|---|---|---|---|",
    ]
    for rank, r in enumerate(rows, start=1):
        lines.append(f"| {rank} | {r.seconds:.1f} | `{r.test.replace('|', chr(92) + '|')}` | {r.source} |")
    lines += ["", f"{len(cases)} tests in {reports} report(s), {total:.0f} s of test time in all."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ci_slowest", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("reports", nargs="*")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--summary", action="store_true", help="append the table to $GITHUB_STEP_SUMMARY")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    paths = [Path(r) for r in args.reports]
    rows = slowest(paths, args.top)
    if not rows:
        print("no test timings found in the given reports")
        return 0
    text = render(rows, paths)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if args.summary and summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
