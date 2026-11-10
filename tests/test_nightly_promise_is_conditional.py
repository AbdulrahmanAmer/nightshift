# ruff: noqa: E501
"""Every sentence that promises a nightly run says that one comes only while Automatic runs are on.

Since the nightly run is a switch in Settings, such a promise is untrue for an owner who switched it off,
and a card's words are fixed when they are written (review of block B's integration). This guard reads the
source of every place an owner text lives: the Python packages, the dashboard's pages, the PowerShell
scripts and the error table. A new sentence with the promise and without the condition fails here.

What counts as carrying the condition:
- Python: the statement that holds the sentence names `WHILE_NIGHTLY_ON` or `IF_NIGHTLY_ON`
  (`ns_studio.ownership`) or says "Automatic runs are on" itself. Docstrings and comments are not owner text.
- a page (`.ts`, `.tsx`): the expression that holds the sentence tests the state the dashboard has:
  `nightComes(...)` (`lib/format.ts`), `next_run` or `schedule_registered`.
- PowerShell and `contracts/errors.json`: the text says "Automatic runs are on".
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROMISES = (
    "next nightly run", "next scheduled run", "next night", "tonight's run",
    # the night's time decisions (card night-fits, `ns_studio/nightfit.py`): a continuation said without
    # "next nightly run" is a promise too
    "continues next", "night continues", "nightly run after", "goes back to it", "plans it again",
    "tries it once more", "make them again",
)
CONDITION = ("WHILE_NIGHTLY_ON", "IF_NIGHTLY_ON", "Automatic runs are on")
UI_CONDITION = ("nightComes(", "next_run", "schedule_registered")
PY_ROOTS = ("ns_studio", "ns_supervisor", "ns_dashboard", "installer")
UI_ROOT = "ns_dashboard/ui/src"
UI_SKIPPED = ("ns_dashboard/ui/src/lib/demo.ts",)   # the demo's canned answers, shown by no install
PS_ROOTS = ("installer", "launchers")
ERRORS = "contracts/errors.json"
NEAR = 6  # PowerShell: lines after the promise within which the same message must carry the condition

#: Promises that are true without the condition, each with the reason. (where, a piece of the sentence).
ALLOWED: dict[tuple[str, str], str] = {
    (ERRORS, "NS-W209"): (
        "said by a nightly run that started and found its program folder missing: the task ran, so "
        "Automatic runs were on"
    ),
    ("installer/launch.ps1", "the next night runs by itself"): (
        "the note of that same failed nightly start (NS-W209), written by the task's own launch script"
    ),
    (ERRORS, "NS-W118"): (
        "'the next start of Nightshift or the next nightly run': the start alone makes the sentence true"
    ),
    ("ns_studio/ownership.py", "the next start of Nightshift or the next nightly"): (
        "the same sentence as NS-W118, for a holder whose process is known: the start alone makes it true"
    ),
    ("ns_dashboard/assistant/prompt.py", "next nightly run: "): (
        "the Assistant's status line: the time of a run that is coming, added only while "
        "`engine.next_run()` has one; with Automatic runs off the line says so instead"
    ),
}


def _has(text: str) -> str | None:
    low = text.lower()
    return next((p for p in PROMISES if p in low), None)


def _files(root: str, *suffixes: str) -> list[Path]:
    found = []
    for path in sorted((ROOT / root).rglob("*")):
        rel = path.relative_to(ROOT).as_posix()
        if path.suffix in suffixes and "/node_modules/" not in rel and "/static/" not in rel:
            found.append(path)
    return found


def _docstrings(tree: ast.AST) -> set[int]:
    """`id` of every string constant that is a docstring."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                out.add(id(first.value))
    return out


def python_promises() -> list[tuple[str, int, str, str]]:
    """(file, line, the sentence's own text, the statement that holds it) for each promise in a string."""
    found = []
    for root in PY_ROOTS:
        for path in _files(root, ".py"):
            source = path.read_text(encoding="utf-8")
            if _has(source) is None:
                continue
            tree = ast.parse(source)
            skip = _docstrings(tree)
            lines = source.splitlines()

            def visit(node: ast.AST, stmt: ast.stmt | None, rel: str = path.relative_to(ROOT).as_posix(),
                      skip: set[int] = skip, lines: list[str] = lines) -> None:
                if isinstance(node, ast.stmt):
                    stmt = node
                if isinstance(node, ast.JoinedStr):     # an f-string: its literal parts, read as one text
                    text = "{".join(v.value for v in node.values if isinstance(v, ast.Constant))
                    if _has(text) and stmt is not None:
                        end = stmt.end_lineno or stmt.lineno
                        found.append((rel, node.lineno, text, "\n".join(lines[stmt.lineno - 1 : end])))
                    return
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if id(node) not in skip and _has(node.value) and stmt is not None:
                        end = stmt.end_lineno or stmt.lineno
                        found.append((rel, node.lineno, node.value, "\n".join(lines[stmt.lineno - 1 : end])))
                    return
                for child in ast.iter_child_nodes(node):
                    visit(child, stmt)

            visit(tree, None)
    return found


def _expression(line: str, at: int) -> str:
    """The innermost `{...}` of `line` around position `at` (a JSX expression); the whole line when the
    sentence is not inside braces (a plain statement)."""
    depth = 0
    start = -1
    for i in range(at - 1, -1, -1):
        if line[i] == "}":
            depth += 1
        elif line[i] == "{":
            if depth == 0:
                start = i
                break
            depth -= 1
    if start < 0:
        return line
    depth = 0
    for j in range(start, len(line)):
        if line[j] == "{":
            depth += 1
        elif line[j] == "}":
            depth -= 1
            if depth == 0:
                return line[start : j + 1]
    return line[start:]


def ui_promises() -> list[tuple[str, int, str, str]]:
    found = []
    for path in _files(UI_ROOT, ".ts", ".tsx"):
        rel = path.relative_to(ROOT).as_posix()
        if rel in UI_SKIPPED:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            if line.lstrip().startswith(("//", "/*", "*", "{/*")):
                continue
            for promise in PROMISES:
                for m in re.finditer(re.escape(promise), line, re.IGNORECASE):
                    found.append((rel, i + 1, line[max(0, m.start() - 60) : m.end() + 40], _expression(line, m.start())))
    return found


def powershell_promises() -> list[tuple[str, int, str, str]]:
    found = []
    for root in PS_ROOTS:
        for path in _files(root, ".ps1"):
            lines = path.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                if _has(line) and not line.lstrip().startswith("#"):
                    found.append((path.relative_to(ROOT).as_posix(), i + 1, line, "\n".join(lines[i : i + NEAR + 1])))
    return found


def error_promises() -> list[tuple[str, int, str, str]]:
    table = json.loads((ROOT / ERRORS).read_text(encoding="utf-8"))
    found = []
    for code, entry in table.items():
        text = f"{entry['message']} {entry['hint']}"
        if _has(text):
            found.append((ERRORS, 0, f"{code}: {text}", text))
    return found


def _allowed(rel: str, sentence: str) -> bool:
    return any(where == rel and piece in sentence for where, piece in ALLOWED)


def _ids(found: list[tuple[str, int, str, str]]) -> list[str]:
    return [f"{rel}:{line}" if line else text.split(":")[0] for rel, line, text, _ in found]


PY, UI, PS, ERR = python_promises(), ui_promises(), powershell_promises(), error_promises()


def test_there_are_promises_to_check() -> None:
    assert len([p for p in PY if p[0].startswith("ns_studio/")]) >= 7, "the sentences moved: point this guard at them"
    assert UI and ERR, "no promise found in the pages or the error table: the guard reads the wrong place"


@pytest.mark.parametrize(("rel", "line", "sentence", "statement"), PY, ids=_ids(PY))
def test_a_promised_nightly_run_names_the_switch(rel: str, line: int, sentence: str, statement: str) -> None:
    if _allowed(rel, sentence):
        return
    assert any(c in statement for c in CONDITION), f"{rel}:{line} promises a nightly run without the switch"


@pytest.mark.parametrize(("rel", "line", "sentence", "expression"), UI, ids=_ids(UI))
def test_a_page_promises_a_nightly_run_only_where_it_tests_the_state(
    rel: str, line: int, sentence: str, expression: str
) -> None:
    assert any(c in expression for c in UI_CONDITION), (
        f"{rel}:{line} says '{sentence.strip()}' in an expression that does not test whether a nightly run "
        "is coming (nightComes(status), next_run or schedule_registered)"
    )


@pytest.mark.parametrize(("rel", "line", "sentence", "near"), PS + ERR, ids=_ids(PS + ERR))
def test_a_script_or_an_error_text_promises_a_nightly_run_with_the_condition(
    rel: str, line: int, sentence: str, near: str
) -> None:
    if _allowed(rel, sentence):
        return
    assert "Automatic runs are on" in near, f"{rel}:{line or ''} promises a nightly run without the switch: {sentence}"


def test_every_allowed_promise_is_still_there() -> None:
    """An entry of the allow-list that matches nothing is a leftover: the list holds only what it excuses."""
    everything = PY + UI + PS + ERR
    for (where, piece), reason in ALLOWED.items():
        assert len(reason) > 20
        assert any(rel == where and piece in sentence for rel, _line, sentence, _ in everything), (where, piece)


def test_the_condition_names_the_place_to_change_it() -> None:
    from ns_studio.ownership import IF_NIGHTLY_ON, WHILE_NIGHTLY_ON

    for words in (WHILE_NIGHTLY_ON, IF_NIGHTLY_ON):
        assert "Automatic runs" in words and "Settings" in words
    # a text that cannot name the constant (the error table) says the condition in the constant's own words
    assert "Automatic runs are on" in IF_NIGHTLY_ON
    hint = json.loads((ROOT / ERRORS).read_text(encoding="utf-8"))["NS-E201"]["hint"]
    assert IF_NIGHTLY_ON.strip(" ()") in hint


def test_the_pages_decide_a_coming_night_in_one_place() -> None:
    """`nightComes` is the one test of "a nightly run is coming" the pages' promises use: Automatic runs on
    (an enabled task) and a run due (an active channel)."""
    source = (ROOT / UI_ROOT / "lib" / "format.ts").read_text(encoding="utf-8")
    found = re.search(r"export const nightComes = [^\n]+", source)
    assert found is not None, "lib/format.ts has no nightComes"
    assert "next_run" in found.group(0) and "schedule_registered" in found.group(0)
