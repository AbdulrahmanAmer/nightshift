"""The GPU kit's cases (tests/gpukit_cases.py) are collected from several files so that CI's by-file shards
can spread them (dev/ci_shards.py). A case that is in no file's list would silently never run, and one in two
lists would run twice: this file fails for both, and for a fixture a list leaves out."""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent
CASES = TESTS / "gpukit_cases.py"


def _cases() -> tuple[list[str], list[str]]:
    """`(test functions, fixtures)` defined at the top level of the cases module."""
    tests: list[str] = []
    fixtures: list[str] = []
    for node in ast.parse(CASES.read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.FunctionDef):
            assert not (isinstance(node, ast.ClassDef) and node.name.startswith("Test")), (
                f"{node.name}: a test class in gpukit_cases.py is not collected by the part files"
            )
            continue
        if any(ast.unparse(d).startswith("pytest.fixture") for d in node.decorator_list):
            fixtures.append(node.name)
        elif node.name.startswith("test_"):
            tests.append(node.name)
    return tests, fixtures


def _parts() -> dict[str, list[str]]:
    """What each part file imports from the cases module, by file name."""
    out: dict[str, list[str]] = {}
    for path in sorted(TESTS.glob("test_gpu_kit*.py")):
        if path.name == Path(__file__).name:
            continue
        names: list[str] = []
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ImportFrom) and node.module == "tests.gpukit_cases":
                names.extend(alias.asname or alias.name for alias in node.names)
            else:  # a part file holds no test of its own: every case lives in the cases module
                assert not isinstance(node, (ast.FunctionDef, ast.ClassDef)), f"{path.name}: {node.name}"
        out[path.name] = names
    return out


def test_every_kit_case_is_collected_from_exactly_one_file() -> None:
    tests, fixtures = _cases()
    parts = _parts()
    assert len(parts) >= 2 and tests and fixtures, (sorted(parts), len(tests), fixtures)
    listed = [name for names in parts.values() for name in names if name.startswith("test_")]
    twice = sorted({name for name in listed if listed.count(name) > 1})
    assert not twice, f"collected from two files (it would run twice): {twice}"
    assert sorted(listed) == sorted(tests), {
        "in no part file (it would never run)": sorted(set(tests) - set(listed)),
        "listed but not in gpukit_cases.py": sorted(set(listed) - set(tests)),
    }
    for name, names in parts.items():  # a test asks for a fixture by name: it must be in the file's namespace
        assert set(fixtures) <= set(names), (name, sorted(set(fixtures) - set(names)))


def test_the_cases_module_is_not_collected_by_itself() -> None:
    """Its name must not match pytest's `test_*.py`, or every case would also run from it (and its asserts are
    rewritten because tests/conftest.py registers it)."""
    assert not CASES.name.startswith("test_") and not CASES.name.endswith("_test.py")
    conftest = (TESTS / "conftest.py").read_text(encoding="utf-8")
    assert 'pytest.register_assert_rewrite("tests.gpukit_cases")' in conftest
