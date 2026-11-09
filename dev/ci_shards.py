"""Split the CI test job into shards by test file, and prove that nothing fell between them (H-3).

The suite is about 3,900 tests in 250 files and takes about 28 minutes on one runner. CI runs it as a matrix
of shards instead; each shard runs whole test files, so the order inside a file never changes and a file's
tests stay together.

Placement is deterministic and does not depend on which other files exist:

* a file listed in ``dev/ci_shard_weights.json`` (measured seconds per file) is placed by longest-first
  bin-packing over EVERY file in that table, so the answer is a pure function of the table and the shard
  count, not of the files currently in ``tests/``;
* a file that is not in the table (a new test file) goes to ``sha256(path) % shards``, so adding a test file
  needs no change here and never moves another file.

Only a change of the shard count, or of the table (``weights`` below), moves a file.

Commands (``python dev/ci_shards.py <command> ...``)::

    files   --shard N --of M                 the test files of one shard, one per line
    run     --shard N --of M -- <pytest args>  run pytest on the files of one shard
    plan    --of M [--limit SECONDS]         predicted seconds per shard; exit 1 over the limit
    verify  --of M --collected FILE --reports DIR [--summary]
                                             the completeness proof (see ``verify``)
    weights REPORT.xml ... [--write FILE]    median seconds per test file from JUnit reports

Standard library only, so the CI jobs that call it need no install.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
WEIGHTS_PATH = ROOT / "dev" / "ci_shard_weights.json"
TESTS_DIR = ROOT / "tests"

#: How many problems of one kind are listed before "... and N more" (the log stays readable).
LIST_LIMIT = 25


@dataclass(frozen=True)
class Weights:
    """Measured seconds per test file, the weight of a file that is not listed, and fixed work per shard."""

    files: dict[str, float]
    default_seconds: float = 3.0
    #: shard number -> seconds of work besides its test files (shard 1 also runs the real-network step)
    extra_seconds: dict[int, float] = field(default_factory=dict)


def load_weights(path: Path = WEIGHTS_PATH) -> Weights:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Weights(
        files={normalize(k): float(v) for k, v in raw["files"].items()},
        default_seconds=float(raw["default_seconds"]),
        extra_seconds={int(k): float(v) for k, v in raw.get("shard_extra_seconds", {}).items()},
    )


def normalize(path: str) -> str:
    """``tests\\test_a.py`` and ``./tests/test_a.py`` are the same file as ``tests/test_a.py``."""
    text = path.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return PurePosixPath(text).as_posix()


def node_file(node_id: str) -> str:
    """The test file of a node id (everything before the first ``::``)."""
    return normalize(node_id.split("::", 1)[0])


# ---- placement ----------------------------------------------------------------------------------------
def hash_shard(path: str, shards: int) -> int:
    """Shard (1-based) of a file that is not in the weights table. sha256, not ``hash()``, which is salted."""
    digest = hashlib.sha256(normalize(path).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % shards + 1


def weighted_table(weights: Weights, shards: int) -> dict[str, int]:
    """Longest-first bin-packing of every file in the table.

    Ties break on the path, and on equal load the later shard gets the file (shard 1 carries the real-network
    step).
    """
    loads = [weights.extra_seconds.get(s, 0.0) for s in range(1, shards + 1)]
    table: dict[str, int] = {}
    for path, seconds in sorted(weights.files.items(), key=lambda kv: (-kv[1], kv[0])):
        index = min(range(shards), key=lambda i: (loads[i], -i))  # a tie goes to the later shard
        loads[index] += seconds
        table[path] = index + 1
    return table


def shard_of(path: str, shards: int, weights: Weights) -> int:
    """The shard (1-based) a test file belongs to."""
    name = normalize(path)
    placed = weighted_table(weights, shards).get(name)
    return placed if placed is not None else hash_shard(name, shards)


def split(files: Iterable[str], shards: int, weights: Weights) -> list[list[str]]:
    """``shards`` sorted lists of files; index 0 is shard 1. Every file is in exactly one."""
    table = weighted_table(weights, shards)
    groups: list[list[str]] = [[] for _ in range(shards)]
    for f in sorted({normalize(f) for f in files}):
        groups[(table.get(f) or hash_shard(f, shards)) - 1].append(f)
    return groups


def predicted_seconds(groups: Sequence[Sequence[str]], weights: Weights) -> list[float]:
    """Predicted test seconds per shard (the shard's fixed extra included, setup not)."""
    return [
        weights.extra_seconds.get(s, 0.0) + sum(weights.files.get(f, weights.default_seconds) for f in group)
        for s, group in enumerate(groups, start=1)
    ]


def render_plan(groups: Sequence[Sequence[str]], weights: Weights) -> str:
    loads = predicted_seconds(groups, weights)
    lines = []
    for s, (group, load) in enumerate(zip(groups, loads, strict=True), start=1):
        unweighted = sum(1 for f in group if f not in weights.files)
        note = f", {unweighted} not in the weights table" if unweighted else ""
        head = f"shard {s}/{len(groups)}: {len(group):3d} files"
        lines.append(f"{head}, predicted {load:6.0f} s ({load / 60:4.1f} min){note}")
    lines.append(
        f"slowest shard: {max(loads):.0f} s ({max(loads) / 60:.1f} min), mean {sum(loads) / len(loads):.0f} s"
    )
    return "\n".join(lines)


def discover_test_files(tests_dir: Path = TESTS_DIR, root: Path = ROOT) -> list[str]:
    """Test files the way pytest's default ``python_files`` finds them, as paths relative to ``root``."""
    found = set()
    for pattern in ("test_*.py", "*_test.py"):
        for p in tests_dir.rglob(pattern):
            if "__pycache__" not in p.parts and p.is_file():
                found.add(p.relative_to(root).as_posix())
    return sorted(found)


# ---- the collection -------------------------------------------------------------------------------------
NODE_ID_RE = re.compile(r"^[^\s:][^:\n]*\.py::\S")


def parse_collected(text: str) -> tuple[list[str], list[str]]:
    """``(node ids, collection error lines)`` from ``pytest --collect-only -q -o addopts=`` output.

    A node id line starts with a path ending in ``.py::``. The count lines (``-qq``), the blank line and the
    ``N tests collected`` summary are ignored; an ``ERROR <path>`` line is a test file that did not import.
    """
    ids: list[str] = []
    errors: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if NODE_ID_RE.match(line):
            ids.append(line)
        elif line.startswith("ERROR "):
            errors.append(line)
    return ids, errors


def select_node_ids(node_ids: Iterable[str], shard: int, shards: int, weights: Weights) -> list[str]:
    """The node ids that shard ``shard`` (1-based) runs, in collection order."""
    table = weighted_table(weights, shards)
    return [i for i in node_ids if (table.get(node_file(i)) or hash_shard(node_file(i), shards)) == shard]


def _capped(items: Sequence[str]) -> list[str]:
    shown = list(items[:LIST_LIMIT])
    if len(items) > LIST_LIMIT:
        shown.append(f"... and {len(items) - LIST_LIMIT} more")
    return shown


def check_partition(collected: Sequence[str], selections: Sequence[Sequence[str]]) -> list[str]:
    """Problems if the selections are not exactly the collection: a test in no shard, in two, or unknown."""
    problems: list[str] = []
    owners: dict[str, list[int]] = {}
    for shard, selection in enumerate(selections, start=1):
        for node_id in selection:
            owners.setdefault(node_id, []).append(shard)
    collected_set = set(collected)
    missing = [i for i in dict.fromkeys(collected) if i not in owners]
    twice = [f"{i} (shards {' and '.join(map(str, o))})" for i, o in owners.items() if len(o) > 1]
    ghosts = [i for i in owners if i not in collected_set]
    problems += [f"in no shard: {m}" for m in _capped(missing)]
    problems += [f"in two shards: {m}" for m in _capped(twice)]
    problems += [f"selected but not in the unsharded collection: {m}" for m in _capped(ghosts)]
    return problems


# ---- what the shards really ran -----------------------------------------------------------------------
@dataclass(frozen=True)
class ShardRow:
    shard: int
    files: int
    tests: int
    predicted_seconds: float
    ran_seconds: float


@dataclass(frozen=True)
class Verdict:
    problems: list[str]
    rows: list[ShardRow]


def _module_index(files: Iterable[str]) -> dict[str, str]:
    """``tests.test_a`` -> ``tests/test_a.py``: JUnit names a test by its dotted module and classes."""
    return {f[: -len(".py")].replace("/", "."): f for f in files if f.endswith(".py")}


def _file_of_classname(classname: str, modules: dict[str, str]) -> str | None:
    parts = classname.split(".")
    for k in range(len(parts), 0, -1):
        hit = modules.get(".".join(parts[:k]))
        if hit:
            return hit
    return None


def _read_junit(path: Path) -> list[ET.Element]:
    return list(ET.parse(path).getroot().iter("testcase"))


def verify(collected: Sequence[str], shards: int, weights: Weights, reports: Path) -> Verdict:
    """The completeness proof, from the unsharded collection and the JUnit reports of the shards.

    1. the helper's shard selections, taken from the collected node ids, are exactly the collection: no test
       in no shard, none in two (``check_partition``);
    2. each shard wrote ``report-shard-N.xml``, and for every test file the number of tests it reports is the
       number collected, in the shard the helper assigned and in no other. A test file the shards never ran, a
       file that ran in two shards, a shard that stopped early and a collection failure each name themselves.
    """
    problems: list[str] = []
    if not collected:
        return Verdict(["the unsharded collection collected no tests"], [])
    selections = [select_node_ids(collected, s, shards, weights) for s in range(1, shards + 1)]
    problems += check_partition(collected, selections)

    collected_per_file: dict[str, int] = {}
    for node_id in collected:
        collected_per_file[node_file(node_id)] = collected_per_file.get(node_file(node_id), 0) + 1
    modules = _module_index(collected_per_file)
    table = weighted_table(weights, shards)

    ran: dict[tuple[str, int], int] = {}  # (file, shard) -> tests reported
    rows: list[ShardRow] = []
    per_shard_files = [sorted({node_file(i) for i in sel}) for sel in selections]
    loads = predicted_seconds(per_shard_files, weights)
    for shard in range(1, shards + 1):
        report = reports / f"report-shard-{shard}.xml"
        if not report.is_file():
            problems.append(f"shard {shard} produced no JUnit report ({report.name})")
            rows.append(ShardRow(shard, len(per_shard_files[shard - 1]), 0, loads[shard - 1], 0.0))
            continue
        try:
            cases = _read_junit(report)
        except ET.ParseError as exc:
            problems.append(f"shard {shard}: {report.name} is not readable XML ({exc})")
            rows.append(ShardRow(shard, len(per_shard_files[shard - 1]), 0, loads[shard - 1], 0.0))
            continue
        tests = 0
        seconds = 0.0
        strangers: list[str] = []
        for case in cases:
            classname, name = case.get("classname", ""), case.get("name", "")
            error, skipped = case.find("error"), case.find("skipped")
            if error is not None and error.get("message", "") == "collection failure":
                problems.append(f"shard {shard}: collection failure in {classname or name}")
                continue
            if skipped is not None and skipped.get("message", "") == "collection skipped":
                continue  # a whole module skipped itself (importorskip): it has no tests to count
            file = _file_of_classname(classname, modules)
            tests += 1
            seconds += float(case.get("time") or 0)
            if file is None:
                strangers.append(f"{classname}::{name}")
            else:
                ran[(file, shard)] = ran.get((file, shard), 0) + 1
        problems += [f"ran in shard {shard} but not collected: {s}" for s in _capped(strangers)]
        rows.append(ShardRow(shard, len(per_shard_files[shard - 1]), tests, loads[shard - 1], seconds))

    for file, count in sorted(collected_per_file.items()):
        owner = table.get(file) or hash_shard(file, shards)
        elsewhere = sorted(s for (f, s) in ran if f == file and s != owner)
        for s in elsewhere:
            problems.append(f"{file} belongs to shard {owner} but ran in shard {s} ({ran[(file, s)]} tests)")
        got = ran.get((file, owner), 0)
        report_exists = (reports / f"report-shard-{owner}.xml").is_file()
        if report_exists and got != count:
            problems.append(f"{file} (shard {owner}): collected {count} tests, ran {got}")
    return Verdict(problems, rows)


def render_summary(verdict: Verdict, shards: int, unweighted: int) -> str:
    lines = [
        "### Test shards",
        "",
        "| shard | files | tests | predicted s | ran s |",
        "|---|---|---|---|---|",
    ]
    for r in verdict.rows:
        cells = (
            f"{r.shard}/{shards}",
            r.files,
            r.tests,
            f"{r.predicted_seconds:.0f}",
            f"{r.ran_seconds:.0f}",
        )
        lines.append("| " + " | ".join(str(c) for c in cells) + " |")
    lines.append("")
    if unweighted:
        lines.append(
            f"{unweighted} test file(s) are not in `dev/ci_shard_weights.json` and were placed by hash; "
            "refresh the table with `python dev/ci_shards.py weights report-shard-*.xml --write "
            "dev/ci_shard_weights.json` when the shards drift apart."
        )
        lines.append("")
    if verdict.problems:
        lines.append("**The shards are not the whole suite:**")
        lines += [f"- {p}" for p in verdict.problems]
    else:
        lines.append("Every collected test ran in exactly one shard.")
    return "\n".join(lines) + "\n"


# ---- weights from real reports ------------------------------------------------------------------------
def build_weights(reports: Iterable[Path], known_files: Iterable[str]) -> dict[str, float]:
    """Median seconds per test file over the reports; files that are not in ``known_files`` are dropped."""
    known = list(known_files)
    modules = _module_index(known)
    samples: dict[str, list[float]] = {}
    for report in reports:
        per_file: dict[str, float] = {}
        for case in _read_junit(report):
            file = _file_of_classname(case.get("classname", ""), modules)
            if file is not None:
                per_file[file] = per_file.get(file, 0.0) + float(case.get("time") or 0)
        for file, seconds in per_file.items():
            samples.setdefault(file, []).append(seconds)
    return {f: max(round(statistics.median(v), 1), 0.1) for f, v in sorted(samples.items())}


# ---- command line -------------------------------------------------------------------------------------
def _shard_args(p: argparse.ArgumentParser, with_shard: bool = True) -> None:
    if with_shard:
        p.add_argument("--shard", type=int, required=True, help="this shard, 1-based")
    p.add_argument("--of", type=int, required=True, dest="shards", help="how many shards in all")


def _files_of(shard: int, shards: int) -> list[str]:
    return split(discover_test_files(), shards, load_weights())[shard - 1]


def _check_range(shard: int | None, shards: int) -> str | None:
    if shards < 1:
        return "--of must be at least 1"
    if shard is not None and not 1 <= shard <= shards:
        return f"--shard must be between 1 and {shards}"
    return None


def _write_stdout_lf(lines: Iterable[str]) -> None:
    """Bytes, with plain LF: on Windows text mode would write CRLF, and bash keeps the CR on each name."""
    sys.stdout.flush()
    sys.stdout.buffer.write(("".join(f"{line}\n" for line in lines)).encode("utf-8"))
    sys.stdout.buffer.flush()


def _cmd_files(args: argparse.Namespace) -> int:
    _write_stdout_lf(_files_of(args.shard, args.shards))
    return 0


def _cmd_run(args: argparse.Namespace, pytest_args: list[str]) -> int:
    weights = load_weights()
    files = split(discover_test_files(), args.shards, weights)[args.shard - 1]
    if not files:
        print(
            f"shard {args.shard}/{args.shards} has no test files; refusing to start pytest (it would run all)"
        )
        return 2
    unweighted = sum(1 for f in files if f not in weights.files)
    predicted = sum(weights.files.get(f, weights.default_seconds) for f in files)
    print(
        f"shard {args.shard}/{args.shards}: {len(files)} test files "
        f"({unweighted} not in the weights table), predicted {predicted:.0f} s of tests",
        flush=True,
    )
    return subprocess.run(
        [sys.executable, "-m", "pytest", *pytest_args, *files], cwd=ROOT, check=False
    ).returncode


def _cmd_plan(args: argparse.Namespace) -> int:
    weights = load_weights()
    files = discover_test_files()
    groups = split(files, args.shards, weights)
    print(render_plan(groups, weights))
    unweighted = [f for f in files if f not in weights.files]
    print(f"{len(unweighted)} of {len(files)} test files are not in the weights table (placed by hash)")
    worst = max(predicted_seconds(groups, weights))
    if args.limit is not None and worst > args.limit:
        print(
            f"::error::the slowest shard is predicted at {worst:.0f} s, over the limit of {args.limit:.0f} s"
        )
        return 1
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    weights = load_weights()
    text = Path(args.collected).read_text(encoding="utf-8", errors="replace")
    collected, errors = parse_collected(text)
    verdict = verify(collected, args.shards, weights, Path(args.reports))
    problems = [*(f"collection: {e}" for e in errors), *verdict.problems]
    verdict = Verdict(problems, verdict.rows)
    unweighted = len({node_file(i) for i in collected} - set(weights.files))
    n_files = len({node_file(i) for i in collected})
    print(f"collected {len(collected)} tests in {n_files} files; {args.shards} shards")
    for r in verdict.rows:
        print(
            f"shard {r.shard}/{args.shards}: {r.files} files, {r.tests} tests ran, "
            f"{r.ran_seconds:.0f} s (predicted {r.predicted_seconds:.0f} s)"
        )
    if problems:
        for p in problems:
            print(f"::error::{p}")
        print("::error::the shards are not the whole suite: a test is in no shard or in two, or was not run")
    else:
        print("ok: no test is in no shard or in two; every collected test ran in the shard it was given")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if args.summary and summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(render_summary(verdict, args.shards, unweighted))
    return 1 if problems else 0


def _cmd_weights(args: argparse.Namespace) -> int:
    reports = [Path(r) for r in args.reports if Path(r).is_file()]
    if not reports:
        print("no readable report given")
        return 2
    computed = build_weights(reports, discover_test_files())
    target = Path(args.write) if args.write else None
    previous = json.loads(target.read_text(encoding="utf-8")) if target and target.is_file() else {}
    data = {
        "about": (
            "Seconds per test file for dev/ci_shards.py (median over JUnit reports of real CI runs). "
            "A file not listed is placed by hash. Refresh: python dev/ci_shards.py weights "
            "report-shard-*.xml --write dev/ci_shard_weights.json"
        ),
        "default_seconds": previous.get("default_seconds") or round(statistics.mean(computed.values()), 1),
        "shard_extra_seconds": previous.get("shard_extra_seconds", {"1": 15}),
        "files": computed,
    }
    text = json.dumps(data, indent=1) + "\n"
    if target:
        target.write_text(text, encoding="utf-8", newline="\n")
        print(f"{len(computed)} files -> {target}")
    else:
        sys.stdout.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    pytest_args: list[str] = []
    if "--" in args_in:
        cut = args_in.index("--")
        args_in, pytest_args = args_in[:cut], args_in[cut + 1 :]
    parser = argparse.ArgumentParser(prog="ci_shards", description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    _shard_args(sub.add_parser("files", help="print the test files of one shard"))
    _shard_args(sub.add_parser("run", help="run pytest on the files of one shard (arguments after --)"))
    plan = sub.add_parser("plan", help="print the predicted seconds per shard")
    _shard_args(plan, with_shard=False)
    plan.add_argument(
        "--limit", type=float, default=None, help="fail when the slowest shard is predicted over this"
    )
    ver = sub.add_parser("verify", help="prove that the shards are exactly the unsharded collection")
    _shard_args(ver, with_shard=False)
    ver.add_argument("--collected", required=True, help="output of pytest --collect-only -q -o addopts=")
    ver.add_argument("--reports", required=True, help="folder with report-shard-N.xml")
    ver.add_argument("--summary", action="store_true", help="append a table to $GITHUB_STEP_SUMMARY")
    wts = sub.add_parser("weights", help="median seconds per test file from JUnit reports")
    wts.add_argument("reports", nargs="+")
    wts.add_argument("--write", default=None, help="write this JSON file (default: print)")
    args = parser.parse_args(args_in)

    bad = _check_range(getattr(args, "shard", None), getattr(args, "shards", 1))
    if bad:
        print(bad)
        return 2
    if args.command == "files":
        return _cmd_files(args)
    if args.command == "run":
        return _cmd_run(args, pytest_args)
    if args.command == "plan":
        return _cmd_plan(args)
    if args.command == "verify":
        return _cmd_verify(args)
    return _cmd_weights(args)


if __name__ == "__main__":
    raise SystemExit(main())
