"""Say, in the CI log, which online checks an upstream outage skipped or failed (H-1).

``python dev/ci_outage_report.py report.xml [more.xml ...]`` reads pytest JUnit files. A test whose skip
reason or failure message contains ``upstream outage`` (tests/online.py) is counted: the online pin checks
and the real-network download tests skip for an outage (and fail with it only where ``NS_REQUIRE_ONLINE`` is
set). The count goes to the log as a ``::warning::`` (or an ``::error::`` for a failure) and to the job
summary, so a run that skipped them is visible.

Exit status: 0, except when ``NS_REQUIRE_ONLINE`` is set (the nightly run and every tag run) and any test was
skipped or failed for an outage: then 1, so these checks cannot be skipped every day without anyone noticing.
Missing files are ignored (a step that was killed writes none). Standard library only.
"""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

MARK = "upstream outage"


def scan(paths: list[Path]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """``(skipped, failed)`` as lists of ``(test id, reason)`` for the tests an outage took out."""
    skipped: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        for case in root.iter("testcase"):
            name = f"{case.get('classname', '')}::{case.get('name', '')}".strip(":")
            for tag, bucket in (("skipped", skipped), ("failure", failed), ("error", failed)):
                node = case.find(tag)
                if node is None:
                    continue
                text = f"{node.get('message', '')} {node.text or ''}"
                if MARK in text:
                    reason = text[text.index(MARK) :].strip().splitlines()[0][:300]
                    bucket.append((name, reason))
    return skipped, failed


def report(paths: list[Path], required: bool, out=sys.stdout) -> int:
    skipped, failed = scan(paths)
    lines: list[str] = []
    if skipped:
        names = ", ".join(n.rsplit("::", 1)[-1] for n, _ in skipped)
        lines.append(f"::warning::{len(skipped)} online check(s) skipped for an upstream outage: {names}")
        lines += [f"  skipped {n}: {r}" for n, r in skipped]
    if failed:
        lines.append(
            f"::error::{len(failed)} real-network test(s) failed for an upstream outage, not a wrong pin"
        )
        lines += [f"  failed {n}: {r}" for n, r in failed]
    if not skipped and not failed:
        lines.append("no online check was skipped or failed for an upstream outage")
    if required and skipped:
        lines.append("::error::NS_REQUIRE_ONLINE is set: online checks may not be skipped for an outage")
    for line in lines:
        print(line, file=out)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and (skipped or failed):
        with open(summary, "a", encoding="utf-8") as fh:
            bullets = "\n".join(f"- {line.lstrip(':').strip()}" for line in lines)
            fh.write(f"### Upstream outage\n\n{bullets}\n")
    return 1 if required and (skipped or failed) else 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    return report([Path(a) for a in args], bool(os.environ.get("NS_REQUIRE_ONLINE")))


if __name__ == "__main__":
    raise SystemExit(main())
