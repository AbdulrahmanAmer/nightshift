"""Split the CI test job into shards by test file, and prove that nothing fell between them (H-3).

The suite is about 3,900 tests in 250 files and takes about 28 minutes on one runner. CI runs it as a matrix
of shards instead; each shard runs whole test files, so the order inside a file never changes and a file's
tests stay together.

Placement is deterministic and does not depend on which other files exist:

* a file listed in ``dev/ci_shard_weights.json`` (measured seconds per file) is placed by longest-first
  bin-packing over EVERY file in that table, so the answer is a pure function of the table and the shard
  count, not of the files currently in ``tests/``;
* a file that is not in the table (a new test file) goes to ``sha256(path) % shards``, so adding a test file
  needs no change here and never moves another file. Until the table is refreshed it counts as
  ``default_seconds`` of the table: the larger of the mean and the median of the measured files, never a
  flattering guess (the suite is skewed: half the files take under two seconds, a few take minutes).

Only a change of the shard count, or of the table (``refresh`` below), moves a file.

``SHARDS`` is the shard count: the test-shard matrices in .github/workflows/ci.yml and release.yml must say
the same (tests/test_ci_workflow.py checks every place).

Commands (``python dev/ci_shards.py <command> ...``)::

    files   --shard N --of M                 the test files of one shard, one per line
    run     --shard N --of M -- <pytest args>  run pytest on the files of one shard
    plan    --of M [--limit SECONDS] [--summary]
                                             predicted seconds per shard; exit 1 over the limit; the summary
                                             names the files with no measured weight
    verify  --of M --collected FILE --reports DIR [--summary]
                                             the completeness proof: the node ids the shards reported are
                                             exactly the node ids the unsharded run collected
    refresh [--run ID|latest ...] [--junit DIR ...] [--write FILE]
                                             re-measure dev/ci_shard_weights.json from the JUnit reports of a
                                             CI run (downloaded with gh; `latest` is the last green main push
                                             run) or of report folders
    weights REPORT.xml ... [--write FILE]    the same table from report files given one by one

Standard library only, so the CI jobs that call it need no install.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
WEIGHTS_PATH = ROOT / "dev" / "ci_shard_weights.json"
TESTS_DIR = ROOT / "tests"

#: The number of test shards (H-3b). The workflows' test-shard matrices, check names and ``--of`` say the
#: same number (tests/test_ci_workflow.py fails on any other). Twelve, not eight: block E's GPU kit cases run
#: for about 21 minutes (151 cases that each make a whole install home; CI run 43038567089, where they were
#: one unweighted file and their shard reached the 30-minute limit), so eight shards were predicted at 773 s
#: against the guard's 720 s, and ten at 628 s with 92 s of room where tests/test_ci_workflow.py asks for
#: 122 (eight unweighted files); twelve are predicted at 516 s. (Eight, not six, came with block D: six
#: were predicted at 758 s, eight at 569 s, and the guard also wants room for new unweighted test files in
#: every shard, about 82 s. Before: five shards were predicted at
#: 558 to 567 s against 570 s, six left about 100 s. Real timings for the slowest files took that room, so the
#: shard timeout is 30 minutes and tests/test_ci_workflow.py asks for room for several unweighted files, not
#: for none.)
SHARDS = 12

#: What to run when a shard is predicted over its budget (printed by the guards).
REFRESH_COMMAND = "python dev/ci_shards.py refresh --run latest"

#: How many problems of one kind are listed before "... and N more" (the log stays readable).
LIST_LIMIT = 25

#: A file whose tests ran in no shard (all of them `network`, run in shard 1's own step) costs this much in
#: the unit step; its real seconds are part of ``shard_extra_seconds`` for shard 1.
NETWORK_ONLY_SECONDS = 0.1


@dataclass(frozen=True)
class Weights:
    """Measured seconds per test file, the weight of a file that is not listed, and fixed work per shard."""

    files: dict[str, float]
    default_seconds: float = 3.0
    #: shard number -> seconds of work besides its test files (shard 1 also runs the real-network step)
    extra_seconds: dict[int, float] = field(default_factory=dict)


def default_seconds(files: Mapping[str, float]) -> float:
    """The weight of a test file the table does not list: the larger of the mean and the median of the
    measured files. The median alone flatters a new file (most files take a second or two, the few heavy ones
    take minutes); the mean is what a file adds to a shard on average."""
    values = list(files.values())
    if not values:
        return 1.0
    # rounded up to a tenth of a second: never below the mean or the median it stands for
    return math.ceil(max(statistics.mean(values), statistics.median(values)) * 10) / 10


def load_weights(path: Path = WEIGHTS_PATH) -> Weights:
    raw = json.loads(path.read_text(encoding="utf-8"))
    files = {normalize(k): float(v) for k, v in raw["files"].items()}
    return Weights(
        files=files,
        default_seconds=default_seconds(files),
        extra_seconds={int(k): float(v) for k, v in raw.get("shard_extra_seconds", {}).items()},
    )


def raise_shards_text(shards: int) -> str:
    """The last remedy of every guard message: one more shard, with the command that previews it."""
    return (
        f"raise SHARDS in dev/ci_shards.py and, to the same number, the test-shard matrices, check names and "
        f"`--of` in .github/workflows/ci.yml and release.yml (tests/test_ci_workflow.py names every place); "
        f"preview it first with `python dev/ci_shards.py plan --of {shards + 1}`"
    )


def over_budget_message(
    shard: int,
    shards: int,
    predicted_s: float,
    setup_s: float,
    why: str,
    unweighted: Sequence[str] = (),
    default_s: float | None = None,
) -> str:
    """What a guard says when a shard is predicted over its budget: which shard, by how much, what to run.

    ``unweighted`` are the shard's test files with no measured weight. They decide the first remedy: a refresh
    re-measures only the files of the last green main run, so it cannot help a file a pull request adds (that
    file counts at the default until the first refresh after it merges).
    """
    total = predicted_s + setup_s
    amount = (
        f"{predicted_s:.0f} s of tests + {setup_s:.0f} s of setup = {total:.0f} s"
        if setup_s
        else f"{predicted_s:.0f} s of tests"
    )
    head = f"shard {shard}/{shards} is predicted at {amount}: {why}."
    if not unweighted:
        return (
            f"{head} Every file in it has a measured weight, so the table may be stale: refresh the weights "
            f"from the last green main run with `{REFRESH_COMMAND}` (needs gh) and commit "
            f"dev/ci_shard_weights.json. If the refreshed table still predicts it over, "
            f"{raise_shards_text(shards)}."
        )
    each = f"{default_s:.1f} s" if default_s is not None else "the default weight"
    names = ", ".join(unweighted[:LIST_LIMIT]) + (
        f" and {len(unweighted) - LIST_LIMIT} more" if len(unweighted) > LIST_LIMIT else ""
    )
    return (
        f"{head} {len(unweighted)} of its test files have no measured weight and count as {each} each: "
        f"{names}. `{REFRESH_COMMAND}` does not help them: it re-measures the files of the last green main "
        "run, and a test file this pull request adds is in no main run; it counts at the default until the "
        "first refresh after it merges. What helps: (1) measure them from this pull request's own CI run "
        "(the shards upload their JUnit reports even when a test fails): `gh run list --workflow ci --branch "
        "<this branch> --limit 1` for the run id, then `python dev/ci_shards.py refresh --run <run id>` and "
        "commit dev/ci_shard_weights.json; (2) split or rename the new file: a file with no weight goes to "
        f"the shard sha256(path) % {shards} names, so a new name moves it, and each part of a split counts "
        f"{each} until it is measured; check with `python dev/ci_shards.py plan --of {shards}`; "
        f"(3) {raise_shards_text(shards)}."
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


def _node_id_of_case(classname: str, name: str, modules: dict[str, str]) -> str | None:
    """The pytest node id a JUnit ``<testcase>`` stands for, or None when its module is not a collected file.

    pytest writes ``classname`` as the dotted module plus the classes and ``name`` as the test (parameters
    included, even when they contain ``::`` or ``/``), so ``tests.test_a.TestX`` + ``test_y[1]`` is the
    node id ``tests/test_a.py::TestX::test_y[1]``.
    """
    parts = classname.split(".")
    for k in range(len(parts), 0, -1):
        module = modules.get(".".join(parts[:k]))
        if module:
            return "::".join([module, *parts[k:], name])
    return None


def compare_node_ids(collected: Sequence[str], ran: Mapping[int, Sequence[str]]) -> list[str]:
    """Problems if the node ids the shards ran are not exactly the unsharded collection.

    ``collected`` is the node ids of the unsharded run (test-collect); ``ran`` maps a shard number to the node
    ids that shard actually reported. The comparison is by node id and counts occurrences, so it fails on:

    * a collected id that no shard ran (a dropped file, a shard that stopped or collected nothing);
    * an id that ran more than once (a file in two shards, or twice in one);
    * an id that ran but is not in the unsharded collection.

    It does not depend on how the files were split: a different splitter, a hand-edited weights table or a
    shard that ran something else all show up here.
    """
    expected = Counter(collected)
    got: Counter[str] = Counter()
    shards_of: dict[str, list[int]] = {}
    for shard, ids in sorted(ran.items()):
        for node_id in ids:
            got[node_id] += 1
            shards_of.setdefault(node_id, []).append(shard)

    missing = [i for i in dict.fromkeys(collected) if got[i] == 0]
    twice = [
        f"{i} (ran {got[i]} times, expected {expected[i]}; shards {' and '.join(map(str, shards_of[i]))})"
        for i in got
        if i in expected and got[i] > expected[i]
    ]
    short = [f"{i} (ran {got[i]} times, expected {expected[i]})" for i in got if 0 < got[i] < expected[i]]
    ghosts = [f"{i} (shard {shards_of[i][0]})" for i in got if i not in expected]
    problems = [f"in no shard (collected, never ran): {m}" for m in _capped(missing)]
    problems += [f"ran twice: {m}" for m in _capped(twice)]
    problems += [f"ran fewer times than collected: {m}" for m in _capped(short)]
    problems += [f"ran but not in the unsharded collection: {m}" for m in _capped(ghosts)]
    return problems


def verify(collected: Sequence[str], shards: int, weights: Weights, reports: Path) -> Verdict:
    """The completeness proof, from the unsharded collection and the JUnit reports of the shards.

    1. each shard wrote ``report-shard-N.xml`` and it holds tests (a shard that collected nothing fails);
    2. the SET of node ids the shards reported is exactly the set the unsharded run collected: an id that
       no shard ran, or that ran twice (in two shards or in one), or that ran without being collected, fails
       (``compare_node_ids``). This is the proof; it does not use the helper's own split;
    3. every test ran in the shard the helper assigned its file to (a plan the workflow did not follow
       fails), and the helper's own selections are a clean partition (``check_partition``).
    """
    problems: list[str] = []
    if not collected:
        return Verdict(["the unsharded collection collected no tests"], [])
    selections = [select_node_ids(collected, s, shards, weights) for s in range(1, shards + 1)]
    problems += check_partition(collected, selections)

    collected_files = sorted({node_file(i) for i in collected})
    modules = _module_index(collected_files)
    table = weighted_table(weights, shards)

    ran: dict[int, list[str]] = {}  # shard -> node ids reported
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
        ids: list[str] = []
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
            seconds += float(case.get("time") or 0)
            node_id = _node_id_of_case(classname, name, modules)
            if node_id is None:
                strangers.append(f"{classname}::{name}")
            else:
                ids.append(node_id)
        problems += [f"ran in shard {shard} but not collected: {s}" for s in _capped(strangers)]
        if not ids and not strangers:
            problems.append(f"shard {shard} collected nothing: {report.name} reports no tests")
        ran[shard] = ids
        rows.append(
            ShardRow(
                shard, len(per_shard_files[shard - 1]), len(ids) + len(strangers), loads[shard - 1], seconds
            )
        )

    problems += compare_node_ids(collected, ran)

    wrong: dict[tuple[str, int, int], int] = {}  # (file, owner, shard it ran in) -> tests
    for shard, ids in ran.items():
        for node_id in ids:
            file = node_file(node_id)
            owner = table.get(file) or hash_shard(file, shards)
            if owner != shard:
                key = (file, owner, shard)
                wrong[key] = wrong.get(key, 0) + 1
    for (file, owner, shard), count in sorted(wrong.items()):
        problems.append(f"{file} belongs to shard {owner} but ran in shard {shard} ({count} tests)")
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
            f"refresh the table with `{REFRESH_COMMAND}` when the shards drift apart."
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


def render_plan_summary(groups: Sequence[Sequence[str]], weights: Weights, unweighted: Sequence[str]) -> str:
    """The job-summary section of `plan`: predicted seconds per shard, the files with no measured weight."""
    loads = predicted_seconds(groups, weights)
    lines = [
        "### Shard plan",
        "",
        "| shard | files | predicted s | files with no measured weight |",
        "|---|---|---|---|",
    ]
    for s, (group, load) in enumerate(zip(groups, loads, strict=True), start=1):
        missing = sum(1 for f in group if f not in weights.files)
        lines.append(f"| {s}/{len(groups)} | {len(group)} | {load:.0f} | {missing} |")
    lines.append("")
    if unweighted:
        lines.append(
            f"{len(unweighted)} test file(s) have no measured weight in `dev/ci_shard_weights.json` and "
            f"count as {weights.default_seconds:.1f} s each (the larger of the mean and the median of the "
            f"measured files) until the table is refreshed with `{REFRESH_COMMAND}`:"
        )
        lines += [f"- `{f}`" for f in _capped(list(unweighted))]
    else:
        lines.append(
            "Every test file has a measured weight (a new one would count as "
            f"{weights.default_seconds:.1f} s, the larger of the mean and the median of the measured files)."
        )
    return "\n".join(lines) + "\n"


def _cmd_plan(args: argparse.Namespace) -> int:
    weights = load_weights()
    files = discover_test_files()
    groups = split(files, args.shards, weights)
    print(render_plan(groups, weights))
    unweighted = [f for f in files if f not in weights.files]
    print(
        f"{len(unweighted)} of {len(files)} test files are not in the weights table (placed by hash, "
        f"{weights.default_seconds:.1f} s each)"
    )
    for f in _capped(unweighted):
        print(f"  no measured weight: {f}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if args.summary and summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(render_plan_summary(groups, weights, unweighted))
    loads = predicted_seconds(groups, weights)
    if args.limit is None:
        return 0
    over = [(s, load) for s, load in enumerate(loads, start=1) if load > args.limit]
    for s, load in over:
        why = f"over the limit of {args.limit:.0f} s"
        new = [f for f in groups[s - 1] if f not in weights.files]
        msg = over_budget_message(s, args.shards, load, 0, why, new, weights.default_seconds)
        print(f"::error::{msg}")
    return 1 if over else 0


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
        print(
            "ok: the node ids the shards ran are exactly the node ids collected unsharded: "
            "no test is in no shard or in two, and each ran in the shard it was given"
        )
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if args.summary and summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(render_summary(verdict, args.shards, unweighted))
    return 1 if problems else 0


def weights_table(
    shard_reports: Sequence[Path],
    network_reports: Sequence[Path],
    source: str,
    previous: Mapping[str, object],
) -> dict[str, object]:
    """The content of dev/ci_shard_weights.json from the JUnit reports of the shards (and the network
    step)."""
    known = discover_test_files()
    files = build_weights(shard_reports, known)
    # all of a file's tests are `network`: the unit step deselects them, shard 1's own step runs them
    for name in build_weights(network_reports, known):
        files.setdefault(name, NETWORK_ONLY_SECONDS)
    return {
        "about": (
            "Seconds per test file for dev/ci_shards.py: the median over the JUnit reports of real CI "
            "runs. A file not listed is placed by hash and counts as the larger of the mean and the median "
            "of these. "
            f"Refresh: {REFRESH_COMMAND} (or --run <id>, or --junit <folder of report-shard-*.xml>)"
        ),
        "measured": {"source": source, "files": len(files)},
        "shard_extra_seconds": previous.get("shard_extra_seconds", {"1": 15}),
        "files": dict(sorted(files.items())),
    }


def _write_table(data: Mapping[str, object], target: Path | None) -> None:
    text = json.dumps(data, indent=1) + "\n"
    if target is None:
        sys.stdout.write(text)
        return
    target.write_text(text, encoding="utf-8", newline="\n")
    files = data["files"]
    assert isinstance(files, dict)
    print(f"{len(files)} files -> {target}")
    not_measured = [f for f in discover_test_files() if f not in files]
    if not_measured:
        print(f"{len(not_measured)} test file(s) not measured (they will count as the default):")
        for f in _capped(not_measured):
            print(f"  {f}")


def _previous(target: Path | None) -> dict[str, object]:
    if target is None or not target.is_file():
        return {}
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _cmd_weights(args: argparse.Namespace) -> int:
    reports = [Path(r) for r in args.reports if Path(r).is_file()]
    if not reports:
        print("no readable report given")
        return 2
    target = Path(args.write) if args.write else None
    shard = [r for r in reports if r.name != "report-network.xml"]
    network = [r for r in reports if r.name == "report-network.xml"]
    source = "JUnit reports: " + ", ".join(r.name for r in reports)
    _write_table(weights_table(shard, network, source, _previous(target)), target)
    return 0


def _gh(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run the GitHub command line from the repository (it finds the repo from the checkout's remote)."""
    proc = subprocess.run(["gh", *argv], cwd=ROOT, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(argv[:2])} exited {proc.returncode}: {(proc.stderr or '').strip()}")
    return proc


def _last_green_main_run() -> str:
    proc = _gh(
        [
            "run",
            "list",
            "--workflow",
            "ci",
            "--branch",
            "main",
            "--event",
            "push",
            "--status",
            "success",
            "--limit",
            "1",
            "--json",
            "databaseId",
        ]
    )
    runs = json.loads(proc.stdout or "[]")
    if not runs:
        raise RuntimeError("gh found no successful ci run of a push to main")
    return str(runs[0]["databaseId"])


def _cmd_refresh(args: argparse.Namespace) -> int:
    target = Path(args.write)
    sources: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="ci-shards-") as tmp:
            folders: list[Path] = []
            for run in args.run:
                run_id = _last_green_main_run() if run == "latest" else run
                folder = Path(tmp) / f"run-{run_id}"
                _gh(["run", "download", run_id, "--pattern", "junit-report-shard-*", "--dir", str(folder)])
                folders.append(folder)
                sources.append(f"run {run_id}")
            for given in args.junit:
                folders.append(Path(given))
                sources.append(f"JUnit reports in {given}")
            shard = sorted(r for f in folders if f.is_dir() for r in f.rglob("report-shard-*.xml"))
            network = sorted(r for f in folders if f.is_dir() for r in f.rglob("report-network.xml"))
            if not shard:
                where = ", ".join(sources) or "nothing given (use --run or --junit)"
                print(f"no JUnit report (report-shard-*.xml) in {where}; {target} is unchanged")
                return 2
            data = weights_table(shard, network, "; ".join(sources), _previous(target))
            _write_table(data, target)
    except FileNotFoundError as exc:
        print(f"the gh command line is not installed or not on PATH ({exc}); {target} is unchanged")
        return 2
    except (RuntimeError, ValueError, KeyError) as exc:
        print(f"{exc}; {target} is unchanged")
        return 2
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
        "--limit", type=float, default=None, help="fail when a shard is predicted over this many seconds"
    )
    plan.add_argument("--summary", action="store_true", help="append the plan to $GITHUB_STEP_SUMMARY")
    ver = sub.add_parser("verify", help="prove that the shards are exactly the unsharded collection")
    _shard_args(ver, with_shard=False)
    ver.add_argument("--collected", required=True, help="output of pytest --collect-only -q -o addopts=")
    ver.add_argument("--reports", required=True, help="folder with report-shard-N.xml")
    ver.add_argument("--summary", action="store_true", help="append a table to $GITHUB_STEP_SUMMARY")
    ref = sub.add_parser("refresh", help="re-measure the weights from a CI run's JUnit reports")
    ref.add_argument("--run", action="append", default=[], help="a CI run id, or `latest` (repeatable)")
    ref.add_argument(
        "--junit", action="append", default=[], help="a folder of report-shard-*.xml (repeatable)"
    )
    ref.add_argument("--write", default=str(WEIGHTS_PATH), help="the JSON file to write")
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
    if args.command == "refresh":
        return _cmd_refresh(args)
    return _cmd_weights(args)


if __name__ == "__main__":
    raise SystemExit(main())
