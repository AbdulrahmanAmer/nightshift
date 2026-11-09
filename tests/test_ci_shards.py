"""H-3: the CI test job is sharded by test file (dev/ci_shards.py).

The rules these tests pin: a file always lands in the same shard (also when other files are added or
removed), the shards together are exactly the unsharded collection with nothing missing and nothing twice,
the split is balanced by measured seconds, and the proof step fails loudly when it is not so.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from dev import ci_shards as cs

ROOT = Path(__file__).resolve().parents[1]
SHARDS = 4

# 15 minutes for the slowest shard, setup (checkout, python, FFmpeg, pip) included.
SLOWEST_SHARD_LIMIT_S = 15 * 60
SETUP_ALLOWANCE_S = 180


def synthetic_weights() -> cs.Weights:
    files = {f"tests/test_w{i:02d}.py": float(30 - i) for i in range(20)}
    return cs.Weights(files=files, default_seconds=3.0, extra_seconds={1: 10.0})


def synthetic_node_ids(files: list[str]) -> list[str]:
    """Node ids in the shapes pytest prints: plain, in a class, parametrized (with '::' and spaces inside)."""
    ids: list[str] = []
    for f in files:
        ids += [
            f"{f}::test_plain",
            f"{f}::TestGroup::test_in_class",
            f"{f}::test_param[a::b]",
            f"{f}::test_param[with space-1]",
        ]
    return ids


# ---- stable placement -------------------------------------------------------------------------------
def test_a_file_missing_from_the_weights_goes_to_a_hash_shard_that_does_not_depend_on_python_hash_seed():
    # sha256-based, so the same on every machine and every run (these values are pinned on purpose)
    assert cs.hash_shard("tests/test_brand_new.py", 4) == cs.hash_shard("tests/test_brand_new.py", 4)
    seen = {cs.hash_shard(f"tests/test_new_{i}.py", 4) for i in range(60)}
    assert seen == {1, 2, 3, 4}  # spreads over every shard
    code = (
        "from dev import ci_shards as c;"
        "print(c.hash_shard('tests/test_brand_new.py', 4), c.hash_shard('tests/test_other.py', 3))"
    )
    outs = {
        subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env={**os.environ, "PYTHONHASHSEED": seed},
            check=True,
        ).stdout.strip()
        for seed in ("0", "1", "12345")
    }
    assert len(outs) == 1, outs


def test_every_file_lands_in_exactly_one_shard():
    w = synthetic_weights()
    files = [*w.files, "tests/test_unweighted_a.py", "tests/test_unweighted_b.py"]
    groups = cs.split(files, SHARDS, w)
    assert len(groups) == SHARDS
    flat = [f for g in groups for f in g]
    assert sorted(flat) == sorted(files)
    assert len(flat) == len(set(flat))


def test_the_same_file_lands_in_the_same_shard_when_other_files_are_added_or_removed():
    w = synthetic_weights()
    base = [*w.files, "tests/test_unweighted_a.py"]
    before = {f: cs.shard_of(f, SHARDS, w) for f in base}
    extra = [f"tests/test_added_{i}.py" for i in range(40)] + ["tests/test_aaa.py", "tests/test_zzz.py"]
    after_add = {f: s for s, g in enumerate(cs.split([*base, *extra], SHARDS, w), start=1) for f in g}
    for f, shard in before.items():
        assert after_add[f] == shard, f"{f} moved from shard {shard} to {after_add[f]} when files were added"
    # removing files (also weighted ones) moves nobody either
    kept = [f for i, f in enumerate(base) if i % 3]
    after_remove = {f: s for s, g in enumerate(cs.split(kept, SHARDS, w), start=1) for f in g}
    for f, shard in after_remove.items():
        assert before[f] == shard, f"{f} moved from shard {before[f]} to {shard} when files were removed"


def test_the_shard_count_is_the_only_thing_that_may_move_a_file():
    w = synthetic_weights()
    for n in (1, 2, 3, 4, 5):
        groups = cs.split(list(w.files), n, w)
        assert len(groups) == n
        assert sum(len(g) for g in groups) == len(w.files)
    assert cs.split(list(w.files), 1, w)[0] == sorted(w.files)


def test_weighted_files_are_balanced_by_their_seconds():
    w = cs.Weights(
        files={f"tests/test_{i}.py": s for i, s in enumerate([100, 90, 80, 70, 60, 50, 40, 30, 20])},
        default_seconds=3.0,
    )
    groups = cs.split(list(w.files), 3, w)
    loads = cs.predicted_seconds(groups, w)
    assert max(loads) - min(loads) <= 20
    assert sum(loads) == pytest.approx(540)


def test_the_first_shard_is_given_less_because_it_also_runs_the_real_network_step():
    w = cs.Weights(
        files={f"tests/test_{i}.py": 10.0 for i in range(12)}, default_seconds=3.0, extra_seconds={1: 40.0}
    )
    loads = cs.predicted_seconds(cs.split(list(w.files), 3, w), w)
    assert loads[0] <= loads[1] and loads[0] <= loads[2]
    assert loads[0] == pytest.approx(40 + 10)  # 40 s of network step + one 10 s file


def test_paths_are_normalised_so_a_windows_spelling_hits_the_same_shard():
    w = synthetic_weights()
    assert cs.shard_of("tests\\test_w03.py", 4, w) == cs.shard_of("tests/test_w03.py", 4, w)
    assert cs.shard_of("./tests/test_w03.py", 4, w) == cs.shard_of("tests/test_w03.py", 4, w)


# ---- the committed weights --------------------------------------------------------------------------
def discovered_repo_files() -> list[str]:
    return cs.discover_test_files(ROOT / "tests")


def test_the_committed_weights_file_is_well_formed():
    raw = json.loads((ROOT / "dev" / "ci_shard_weights.json").read_text(encoding="utf-8"))
    assert set(raw) >= {"files", "default_seconds", "shard_extra_seconds"}
    assert raw["default_seconds"] > 0
    assert raw["files"], "the weights file lists no test file"
    for name, seconds in raw["files"].items():
        assert name.startswith("tests/") and name.endswith(".py") and "\\" not in name, name
        assert isinstance(seconds, (int, float)) and seconds > 0, name


def test_the_slowest_shard_is_under_fifteen_minutes_by_the_measured_weights():
    w = cs.load_weights()
    files = discovered_repo_files()
    groups = cs.split(files, SHARDS, w)
    loads = cs.predicted_seconds(groups, w)
    unweighted = [f for f in files if f not in w.files]
    print()  # shown by `pytest -s` and on a failure; the CI `plan` step prints it on every run
    print(cs.render_plan(groups, w))
    print(f"{len(unweighted)} of {len(files)} test files are not in dev/ci_shard_weights.json (hash-placed)")
    assert len(groups) == SHARDS and all(groups), "every shard must have files"
    assert max(loads) + SETUP_ALLOWANCE_S < SLOWEST_SHARD_LIMIT_S, loads
    # balanced: the slowest is within 15% of the mean (LPT over measured seconds)
    assert max(loads) <= 1.15 * (sum(loads) / len(loads)), loads


def test_the_shards_together_are_every_test_file_of_the_repo():
    w = cs.load_weights()
    files = discovered_repo_files()
    assert len(files) > 100
    groups = cs.split(files, SHARDS, w)
    flat = [f for g in groups for f in g]
    assert sorted(flat) == sorted(files) and len(flat) == len(set(flat))


# ---- discovery and parsing --------------------------------------------------------------------------
def test_discovery_finds_test_files_the_way_pytest_does(tmp_path: Path):
    t = tmp_path / "tests"
    (t / "sub").mkdir(parents=True)
    (t / "__pycache__").mkdir()
    for name in ("test_a.py", "b_test.py", "sub/test_c.py", "conftest.py", "helpers.py", "online.py"):
        (t / name).write_text("", encoding="utf-8")
    (t / "__pycache__" / "test_d.py").write_text("", encoding="utf-8")
    (t / "notes.txt").write_text("", encoding="utf-8")
    found = cs.discover_test_files(t, root=tmp_path)
    assert found == ["tests/b_test.py", "tests/sub/test_c.py", "tests/test_a.py"]


PYTEST_OUTPUT = """\
tests/test_a.py::test_one
tests/test_a.py::TestX::test_two
tests/test_b.py::test_param[a::b]
tests/test_b.py::test_param[x y-1]

3777/3800 tests collected (23 deselected) in 3.21s
"""


def test_collected_node_ids_are_read_from_pytest_output():
    ids, errors = cs.parse_collected(PYTEST_OUTPUT)
    assert ids == [
        "tests/test_a.py::test_one",
        "tests/test_a.py::TestX::test_two",
        "tests/test_b.py::test_param[a::b]",
        "tests/test_b.py::test_param[x y-1]",
    ]
    assert errors == []


def test_collection_errors_are_reported_not_swallowed():
    text = PYTEST_OUTPUT + "ERROR tests/test_c.py - ImportError: no module\n1 error in 0.5s\n"
    ids, errors = cs.parse_collected(text)
    assert len(ids) == 4
    assert errors and "tests/test_c.py" in errors[0]


def test_the_per_file_counts_format_is_refused():
    # `-qq` (the pyproject addopts `-q` plus `-q`) prints "file: count" lines, not node ids
    ids, errors = cs.parse_collected("tests/test_a.py: 5\ntests/test_b.py: 7\n\n12 tests collected in 1s\n")
    assert ids == []
    assert errors == []


# ---- the completeness proof -------------------------------------------------------------------------
def test_the_union_of_the_shard_selections_is_the_collection_by_node_id():
    w = synthetic_weights()
    files = [
        *w.files,
        "tests/test_unweighted_a.py",
        "tests/test_unweighted_b.py",
        "tests/test_unweighted_c.py",
    ]
    ids = synthetic_node_ids(files)
    selections = [cs.select_node_ids(ids, s, SHARDS, w) for s in range(1, SHARDS + 1)]
    assert cs.check_partition(ids, selections) == []
    assert sorted(i for sel in selections for i in sel) == sorted(ids)
    # a whole file is always together in one shard
    for f in files:
        owners = {s for s, sel in enumerate(selections) if any(i.startswith(f + "::") for i in sel)}
        assert len(owners) == 1, f


def test_a_test_in_no_shard_fails_the_proof_loudly():
    w = synthetic_weights()
    ids = synthetic_node_ids(list(w.files))
    selections = [cs.select_node_ids(ids, s, SHARDS, w) for s in range(1, SHARDS + 1)]
    dropped = selections[2].pop()
    problems = cs.check_partition(ids, selections)
    assert any("in no shard" in p and dropped in p for p in problems), problems


def test_a_test_in_two_shards_fails_the_proof_loudly():
    w = synthetic_weights()
    ids = synthetic_node_ids(list(w.files))
    selections = [cs.select_node_ids(ids, s, SHARDS, w) for s in range(1, SHARDS + 1)]
    twice = selections[0][0]
    selections[1].append(twice)
    problems = cs.check_partition(ids, selections)
    assert any("in two shards" in p and twice in p for p in problems), problems


def test_a_selected_test_that_is_not_in_the_collection_fails_the_proof():
    w = synthetic_weights()
    ids = synthetic_node_ids(list(w.files))
    selections = [cs.select_node_ids(ids, s, SHARDS, w) for s in range(1, SHARDS + 1)]
    selections[0].append("tests/test_ghost.py::test_never_collected")
    problems = cs.check_partition(ids, selections)
    assert any("not in the unsharded collection" in p and "test_ghost" in p for p in problems), problems


# ---- the execution proof: what the shards really ran ------------------------------------------------
def write_junit(
    path: Path, cases: list[tuple[str, str, float]], skipped_collection: Sequence[str] = ()
) -> None:
    rows = "".join(f'<testcase classname="{c}" name="{n}" time="{t}"/>' for c, n, t in cases)
    rows += "".join(
        f'<testcase classname="" name="{m}"><skipped message="collection skipped">x</skipped></testcase>'
        for m in skipped_collection
    )
    path.write_text(
        f'<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">{rows}'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )


def junit_cases(ids: list[str]) -> list[tuple[str, str, float]]:
    """What pytest's JUnit writer makes of node ids (classname: dotted module and classes; name: the test)."""
    out = []
    for i in ids:
        path, _, rest = i.partition("::")
        dotted = path[:-3].replace("/", ".")
        parts = rest.split("[")[0].split("::")
        name = parts[-1] + (("[" + rest.split("[", 1)[1]) if "[" in rest else "")
        classname = ".".join([dotted, *parts[:-1]])
        out.append((classname, name, 0.5))
    return out


def make_reports(tmp_path: Path, ids: list[str], w: cs.Weights, shards: int = SHARDS) -> Path:
    rep = tmp_path / "reports"
    rep.mkdir()
    for s in range(1, shards + 1):
        write_junit(rep / f"report-shard-{s}.xml", junit_cases(cs.select_node_ids(ids, s, shards, w)))
    return rep


def test_verify_passes_when_the_shards_ran_exactly_the_collection(tmp_path: Path):
    w = synthetic_weights()
    ids = synthetic_node_ids([*w.files, "tests/test_unweighted_a.py"])
    rep = make_reports(tmp_path, ids, w)
    verdict = cs.verify(ids, SHARDS, w, rep)
    assert verdict.problems == []
    assert [r.shard for r in verdict.rows] == [1, 2, 3, 4]
    assert sum(r.tests for r in verdict.rows) == len(ids)
    assert sum(r.ran_seconds for r in verdict.rows) == pytest.approx(0.5 * len(ids))


def test_verify_fails_when_a_shard_produced_no_report(tmp_path: Path):
    w = synthetic_weights()
    ids = synthetic_node_ids(list(w.files))
    rep = make_reports(tmp_path, ids, w)
    (rep / "report-shard-3.xml").unlink()
    problems = cs.verify(ids, SHARDS, w, rep).problems
    assert any("shard 3" in p and "no JUnit report" in p for p in problems), problems


def test_verify_fails_when_a_shard_ran_fewer_tests_than_it_was_given(tmp_path: Path):
    w = synthetic_weights()
    ids = synthetic_node_ids(list(w.files))
    rep = make_reports(tmp_path, ids, w)
    victim = cs.select_node_ids(ids, 2, SHARDS, w)
    cases = junit_cases(victim)[:-3]
    write_junit(rep / "report-shard-2.xml", cases)
    problems = cs.verify(ids, SHARDS, w, rep).problems
    for dropped in victim[-3:]:
        assert any("in no shard" in p and dropped in p for p in problems), (dropped, problems)


def test_verify_fails_when_a_file_ran_in_two_shards(tmp_path: Path):
    w = synthetic_weights()
    ids = synthetic_node_ids(list(w.files))
    rep = make_reports(tmp_path, ids, w)
    stray = cs.select_node_ids(ids, 1, SHARDS, w)[:2]
    other = junit_cases(cs.select_node_ids(ids, 4, SHARDS, w)) + junit_cases(stray)
    write_junit(rep / "report-shard-4.xml", other)
    problems = cs.verify(ids, SHARDS, w, rep).problems
    assert any("belongs to shard 1" in p and "ran in shard 4" in p for p in problems), problems
    for twice in stray:
        assert any("ran twice" in p and twice in p for p in problems), (twice, problems)


def test_verify_fails_on_a_test_from_a_file_that_was_never_collected(tmp_path: Path):
    w = synthetic_weights()
    ids = synthetic_node_ids(list(w.files))
    rep = make_reports(tmp_path, ids, w)
    cases = junit_cases(cs.select_node_ids(ids, 1, SHARDS, w)) + [("tests.test_ghost", "test_x", 0.1)]
    write_junit(rep / "report-shard-1.xml", cases)
    problems = cs.verify(ids, SHARDS, w, rep).problems
    assert any("test_ghost" in p for p in problems), problems


def test_verify_ignores_a_collection_skip_but_fails_a_collection_error(tmp_path: Path):
    w = synthetic_weights()
    ids = synthetic_node_ids(list(w.files))
    rep = make_reports(tmp_path, ids, w)
    cases = junit_cases(cs.select_node_ids(ids, 1, SHARDS, w))
    write_junit(rep / "report-shard-1.xml", cases, skipped_collection=["tests/test_skipped_module"])
    assert cs.verify(ids, SHARDS, w, rep).problems == []
    (rep / "report-shard-1.xml").write_text(
        '<testsuites><testsuite><testcase classname="" name="tests.test_broken">'
        '<error message="collection failure">tb</error></testcase></testsuite></testsuites>',
        encoding="utf-8",
    )
    problems = cs.verify(ids, SHARDS, w, rep).problems
    assert any("collection failure" in p for p in problems), problems


def test_verify_fails_on_an_empty_collection(tmp_path: Path):
    problems = cs.verify([], SHARDS, synthetic_weights(), tmp_path).problems
    assert any("collected no tests" in p for p in problems), problems


# ---- the proof can fail: deliberately broken splits (H-4) ---------------------------------------------
#
# The unsharded run (test-collect) is `ids`. Each broken split below is what the four shard reports would
# look like if the split lost a file, ran a file twice or left a shard empty. The proof must name the ids.
def collected_ids() -> tuple[cs.Weights, list[str], list[str]]:
    w = synthetic_weights()
    files = [*w.files, "tests/test_unweighted_a.py", "tests/test_unweighted_b.py"]
    return w, files, synthetic_node_ids(files)


def ids_of_file(ids: list[str], file: str) -> list[str]:
    return [i for i in ids if cs.node_file(i) == file]


def test_the_node_id_comparison_passes_on_an_exact_split_and_names_every_kind_of_difference():
    ids = synthetic_node_ids(["tests/test_a.py", "tests/test_b.py"])
    exact = {1: ids[:4], 2: ids[4:]}
    assert cs.compare_node_ids(ids, exact) == []
    assert any("in no shard" in p for p in cs.compare_node_ids(ids, {1: ids[:4], 2: ids[4:-1]}))
    assert any("ran twice" in p for p in cs.compare_node_ids(ids, {1: ids[:4], 2: [*ids[4:], ids[0]]}))
    assert any("ran twice" in p for p in cs.compare_node_ids(ids, {1: [*ids[:4], ids[0]], 2: ids[4:]}))
    ghost = cs.compare_node_ids(ids, {1: ids[:4], 2: [*ids[4:], "tests/test_a.py::test_other"]})
    assert any("not in the unsharded collection" in p and "test_other" in p for p in ghost)


def test_the_node_id_comparison_does_not_need_the_helper_split_at_all():
    """Same count of tests, different ids: the old per-file count passed this, the id comparison must not."""
    ids = ["tests/test_a.py::test_one", "tests/test_a.py::test_two"]
    swapped = {1: ["tests/test_a.py::test_one", "tests/test_a.py::test_renamed"]}
    problems = cs.compare_node_ids(ids, swapped)
    assert any("in no shard" in p and "test_two" in p for p in problems), problems
    assert any("not in the unsharded collection" in p and "test_renamed" in p for p in problems), problems


def test_verify_fails_when_one_file_is_dropped_from_every_shard(tmp_path: Path):
    w, files, ids = collected_ids()
    rep = make_reports(tmp_path, ids, w)
    victim_file = files[3]
    owner = cs.shard_of(victim_file, SHARDS, w)
    kept = [i for i in cs.select_node_ids(ids, owner, SHARDS, w) if cs.node_file(i) != victim_file]
    write_junit(rep / f"report-shard-{owner}.xml", junit_cases(kept))
    dropped = ids_of_file(ids, victim_file)
    assert len(dropped) == 4
    problems = cs.verify(ids, SHARDS, w, rep).problems
    for node_id in dropped:
        assert any("in no shard" in p and node_id in p for p in problems), (node_id, problems)


def test_verify_fails_when_one_file_is_in_two_shards(tmp_path: Path):
    w, files, ids = collected_ids()
    rep = make_reports(tmp_path, ids, w)
    victim_file = files[5]
    owner = cs.shard_of(victim_file, SHARDS, w)
    other = owner % SHARDS + 1
    doubled = junit_cases(cs.select_node_ids(ids, other, SHARDS, w) + ids_of_file(ids, victim_file))
    write_junit(rep / f"report-shard-{other}.xml", doubled)
    problems = cs.verify(ids, SHARDS, w, rep).problems
    for node_id in ids_of_file(ids, victim_file):
        assert any("ran twice" in p and node_id in p for p in problems), (node_id, problems)


def test_verify_fails_when_one_shard_collects_nothing(tmp_path: Path):
    w, _files, ids = collected_ids()
    rep = make_reports(tmp_path, ids, w)
    write_junit(rep / "report-shard-3.xml", [])
    problems = cs.verify(ids, SHARDS, w, rep).problems
    assert any("shard 3 collected nothing" in p for p in problems), problems
    for node_id in cs.select_node_ids(ids, 3, SHARDS, w)[:3]:
        assert any("in no shard" in p and node_id in p for p in problems), (node_id, problems)


@pytest.mark.parametrize("break_it", ["drop-file", "file-twice", "empty-shard"])
def test_the_verify_command_exits_1_on_a_broken_split_and_0_on_the_whole_one(
    break_it: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    w = cs.load_weights()
    files = discovered_repo_files()
    ids = synthetic_node_ids(files)
    collected = tmp_path / "collected.txt"
    collected.write_text("\n".join(ids) + "\n", encoding="utf-8")
    rep = make_reports(tmp_path, ids, w)
    args = ["verify", "--of", "4", "--collected", str(collected), "--reports", str(rep)]
    assert cs.main(args) == 0
    capsys.readouterr()

    shard_ids = [cs.select_node_ids(ids, s, SHARDS, w) for s in range(1, SHARDS + 1)]
    if break_it == "drop-file":
        gone = cs.node_file(shard_ids[0][0])
        write_junit(
            rep / "report-shard-1.xml",
            junit_cases([i for i in shard_ids[0] if cs.node_file(i) != gone]),
        )
    elif break_it == "file-twice":
        write_junit(rep / "report-shard-2.xml", junit_cases([*shard_ids[1], *shard_ids[0][:4]]))
    else:
        write_junit(rep / "report-shard-4.xml", [])
    assert cs.main(args) == 1
    out = capsys.readouterr().out
    assert "::error::" in out
    assert "ok:" not in out


# ---- the command line -------------------------------------------------------------------------------
def run_cli(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(ROOT / "dev" / "ci_shards.py"), *args], capture_output=True, cwd=cwd, check=False
    )


def test_files_prints_one_path_per_line_with_unix_line_endings():
    # bash splits `$(...)` on whitespace but keeps a trailing \r on the name: Windows text mode would break it
    proc = run_cli("files", "--shard", "1", "--of", "4")
    assert proc.returncode == 0, proc.stderr
    assert b"\r" not in proc.stdout
    lines = proc.stdout.decode().splitlines()
    assert lines and all(line.startswith("tests/") and line.endswith(".py") for line in lines)


def test_the_four_shards_of_the_command_line_cover_the_repo_once():
    seen: list[str] = []
    for s in range(1, 5):
        proc = run_cli("files", "--shard", str(s), "--of", "4")
        assert proc.returncode == 0, proc.stderr
        seen += proc.stdout.decode().splitlines()
    assert sorted(seen) == sorted(discovered_repo_files())
    assert len(seen) == len(set(seen))


def test_run_hands_the_shard_files_to_pytest_after_the_given_arguments(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    calls: list[list[str]] = []

    class Done:
        returncode = 7

    monkeypatch.setattr(cs.subprocess, "run", lambda argv, **kw: (calls.append(list(argv)), Done())[1])
    rc = cs.main(["run", "--shard", "2", "--of", "4", "--", "-q", "-m", "not network", "--junitxml=r.xml"])
    assert rc == 7  # pytest's own exit code goes through
    (argv,) = calls
    assert argv[:3] == [sys.executable, "-m", "pytest"]
    assert argv[3:7] == ["-q", "-m", "not network", "--junitxml=r.xml"]
    files = argv[7:]
    assert files == cs.split(discovered_repo_files(), 4, cs.load_weights())[1]
    assert "shard 2/4" in capsys.readouterr().out


def test_run_refuses_to_start_pytest_on_a_shard_with_no_files(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    # pytest with no file arguments would run the WHOLE suite in that shard
    calls: list[object] = []
    monkeypatch.setattr(cs.subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(cs, "discover_test_files", lambda *a, **k: [])
    assert cs.main(["run", "--shard", "1", "--of", "4", "--", "-q"]) == 2
    assert calls == []
    assert "no test files" in capsys.readouterr().out


def test_plan_fails_when_the_slowest_shard_is_over_the_limit(capsys: pytest.CaptureFixture[str]):
    assert cs.main(["plan", "--of", "4", "--limit", "100000"]) == 0
    assert cs.main(["plan", "--of", "4", "--limit", "10"]) == 1
    assert "shard 1" in capsys.readouterr().out


def test_shard_numbers_outside_the_range_are_a_usage_error(capsys: pytest.CaptureFixture[str]):
    assert cs.main(["files", "--shard", "5", "--of", "4"]) == 2
    assert cs.main(["files", "--shard", "0", "--of", "4"]) == 2


def test_verify_command_reads_the_collection_and_the_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    w = cs.load_weights()
    files = discovered_repo_files()[:40]
    ids = synthetic_node_ids(files)
    collected = tmp_path / "collected.txt"
    collected.write_text("\n".join(ids) + "\n\n40 tests collected in 1s\n", encoding="utf-8")
    rep = make_reports(tmp_path, ids, w)
    args = ["verify", "--of", "4", "--collected", str(collected), "--reports", str(rep), "--summary"]
    assert cs.main(args) == 0
    out = capsys.readouterr().out
    assert "no test is in no shard or in two" in out
    assert "| shard |" in summary.read_text(encoding="utf-8")
    (rep / "report-shard-2.xml").unlink()
    assert cs.main(args) == 1
    assert "::error::" in capsys.readouterr().out


# ---- refreshing the weights from real reports -------------------------------------------------------
def test_weights_are_the_median_seconds_per_file_across_reports(tmp_path: Path):
    known = ["tests/test_a.py", "tests/test_b.py"]
    reports = []
    for n, (ta, tb) in enumerate([(10.0, 2.0), (30.0, 2.0), (20.0, 4.0)]):
        p = tmp_path / f"r{n}.xml"
        write_junit(
            p,
            [
                ("tests.test_a", "t1", ta / 2),
                ("tests.test_a.TestK", "t2", ta / 2),
                ("tests.test_b", "t1", tb),
                ("tests.test_gone", "t1", 99.0),
            ],
        )
        reports.append(p)
    weights = cs.build_weights(reports, known)
    assert weights == {"tests/test_a.py": 20.0, "tests/test_b.py": 2.0}  # test_gone is not a file any more


def test_the_weights_command_keeps_the_extras_and_sorts_the_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    out = tmp_path / "weights.json"
    out.write_text(
        json.dumps({"default_seconds": 9, "shard_extra_seconds": {"1": 30}, "files": {}}), encoding="utf-8"
    )
    report = tmp_path / "r.xml"
    write_junit(report, [("tests.test_ci_shards", "t", 5.0), ("tests.test_ci_outage_report", "t", 1.0)])
    assert cs.main(["weights", str(report), "--write", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert list(data["files"]) == sorted(data["files"])
    assert data["files"]["tests/test_ci_shards.py"] == 5.0
    assert data["shard_extra_seconds"] == {"1": 30}
    assert out.read_text(encoding="utf-8").endswith("\n") and "\r" not in out.read_text(encoding="utf-8")
