# ruff: noqa: E501
"""Card FN-2: `run/morning_note.json` has one owner, and this test fails when a second one appears.

The owner is `ns_supervisor/notify.py` (and, for the hours when no Python exists, the one function
`Add-NsMorningNote` of `installer/launch.ps1`). Everything else leaves a note through `notify.leave_note`, reads
through `notify.read_notes` and removes through `notify.dismiss`; a caller that must say something once passes
`once=` instead of reading the notes first (its check and its write are then one step under the owner's lock).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCT = ("ns_common", "ns_studio", "ns_supervisor", "ns_dashboard", "ns_tts", "installer", "dev", "tools")
OWNER = "ns_supervisor/notify.py"
STUB = "installer/launch.ps1"
STUB_FUNCTION = "Add-NsMorningNote"
FILE_NAME = "morning_note.json"
#: The owner's own names for the file, its lock and its limit: nobody else has a use for them.
OWNER_NAMES = frozenset({"NOTE_FILE", "NOTE_LOCK", "NOTE_KEEP", "NOTES_FULL", "note_path", "clear_notes"})
#: Callers that used to read the notes to decide "already said" and then write: they pass `once=` now.
ONCE_CALLERS = (
    "ns_supervisor/schedule.py", "ns_supervisor/app.py", "ns_supervisor/update_check.py",
    "ns_supervisor/verify.py", "ns_studio/voice_setup.py",
)
SKIP_DIRS = frozenset({"node_modules", "__pycache__", ".git", "static", ".venv"})


def _files(root: Path, suffix: str, tops: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for top in tops:
        base = root / top
        if base.is_dir():
            out += [p for p in base.rglob(f"*{suffix}") if not SKIP_DIRS.intersection(p.relative_to(root).parts)]
    return sorted(out)


def _docstrings(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def python_offences(root: Path) -> list[str]:
    """`file:line: why` for every Python site outside the owner that names the notes file or the owner's names."""
    out: set[tuple[str, int, str]] = set()
    for path in _files(root, ".py", PRODUCT):
        rel = path.relative_to(root).as_posix()
        if rel == OWNER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        docs = _docstrings(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and FILE_NAME in node.value and id(node) not in docs:
                out.add((rel, node.lineno, f"names {FILE_NAME} (only {OWNER} touches that file)"))
            name = (
                node.id if isinstance(node, ast.Name)
                else node.attr if isinstance(node, ast.Attribute)
                else node.name if isinstance(node, ast.alias)
                else None
            )
            if name in OWNER_NAMES:
                out.add((rel, getattr(node, "lineno", 0), f"uses {name} (the notes file's own names stay inside {OWNER})"))
            if rel in ONCE_CALLERS and isinstance(node, ast.Attribute) and node.attr == "read_notes":
                out.add((rel, node.lineno, "reads the notes to decide before it writes (pass once= to leave_note)"))
    return [f"{rel}:{line}: {why}" for rel, line, why in sorted(out)]


def powershell_offences(root: Path) -> list[str]:
    """`file:line: why` for every PowerShell line (comments left out) that names the notes file outside the
    launch script's one function."""
    out: list[str] = []
    for path in _files(root, ".ps1", (*PRODUCT, "launchers")) + sorted(root.glob("*.ps1")):
        rel = path.relative_to(root).as_posix()
        inside = ""
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            start = re.match(r"function\s+([\w-]+)", line)
            if start:
                inside = start.group(1)
            elif line.startswith("}"):
                inside = ""
            code = line.split("#", 1)[0]
            if FILE_NAME in code and not (rel == STUB and inside == STUB_FUNCTION):
                out.append(f"{rel}:{number}: names {FILE_NAME} outside {STUB}:{STUB_FUNCTION}")
    return out


def test_no_writer_of_the_notes_file_outside_its_owner() -> None:
    found = python_offences(ROOT) + powershell_offences(ROOT)
    assert found == [], "\n".join(["the morning notes have one owner:", *found])
    # the scan looked at the owner's callers at all (a renamed folder would make it pass on nothing)
    assert (ROOT / OWNER).is_file() and all((ROOT / rel).is_file() for rel in (*ONCE_CALLERS, STUB))
    stub = (ROOT / STUB).read_text(encoding="ascii")
    assert f"function {STUB_FUNCTION} " in stub and FILE_NAME in stub


def test_the_scan_finds_a_new_writer(tmp_path: Path) -> None:
    """The test of the test: a second writer, in each form it could take, is named with its file and line."""
    (tmp_path / "ns_studio").mkdir()
    (tmp_path / "ns_supervisor").mkdir()
    (tmp_path / "installer").mkdir()
    (tmp_path / "ns_studio" / "tidy.py").write_text(
        '"""Tidies run/morning_note.json (a docstring may name it)."""\n'
        "from pathlib import Path\n"
        "\n"
        "def tidy(home):\n"
        '    Path(home, "run/morning_note.json").unlink()\n'
        '    return f"{home}/run/morning_note.json"  # log event morning_note_failed is not the file\n',
        encoding="utf-8",
    )
    (tmp_path / "ns_supervisor" / "other.py").write_text(
        "from ns_supervisor import notify\n"
        "from ns_supervisor.notify import note_path\n"
        "\n"
        "def wipe(paths, log):\n"
        '    log.warn("morning_note_failed", "x")\n'
        "    notify.note_path(paths).unlink()\n"
        "    return notify.NOTE_FILE\n",
        encoding="utf-8",
    )
    (tmp_path / "ns_supervisor" / "schedule.py").write_text(
        "from ns_supervisor import notify\n"
        "\n"
        "def say(paths, text):\n"
        "    if not notify.read_notes(paths):\n"
        '        notify.leave_note(paths, "Nightshift", text)\n',
        encoding="utf-8",
    )
    (tmp_path / "installer" / "launch.ps1").write_text(
        "# morning_note.json in a comment is not a use\n"
        "function Add-NsMorningNote {\n"
        "    $f = Join-Path $run 'morning_note.json'\n"
        "}\n"
        "function Restore-AfterMiss {\n"
        "    Remove-Item (Join-Path $run 'morning_note.json')\n"
        "}\n",
        encoding="ascii",
    )
    (tmp_path / "installer" / "uninstall.ps1").write_text("Remove-Item \"$h\\run\\morning_note.json\"\n", encoding="ascii")
    py = python_offences(tmp_path)
    assert [o.split(": ")[0] for o in py] == [
        "ns_studio/tidy.py:5", "ns_studio/tidy.py:6",
        "ns_supervisor/other.py:2", "ns_supervisor/other.py:6", "ns_supervisor/other.py:7",
        "ns_supervisor/schedule.py:4",
    ], py
    ps = powershell_offences(tmp_path)
    assert [o.split(": ")[0] for o in ps] == ["installer/launch.ps1:6", "installer/uninstall.ps1:1"], ps
