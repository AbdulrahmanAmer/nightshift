"""C10: repo-wide hygiene rules (docs/ENGINEERING.md sections 1, 4 and 8).

Everything here reads source text or ASTs; no heavy package is ever imported.
"""

import ast
import json
import os
import re
import tomllib
from pathlib import Path

from ns_common import __version__
from ns_common.models import export_schemas

HEAVY = {"torch", "faster_whisper", "spacy", "cv2", "qwen_tts", "faster_qwen3_tts"}
SKIP_DIRS = {".git", ".venv", "venv", "dist", "build", "node_modules", "__pycache__", "stage"}
#: Modules that may print: CLI entry points, plus the NsLogger console sink (stderr by design).
PRINT_OK_NAMES = {"cli.py", "__main__.py"}
PRINT_OK_PATHS = {"ns_common/logging.py"}
SEMVER = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")


def _ns_dirs(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.is_dir() and p.name.startswith("ns_"))


def _py_files(root: Path, dirs: list[str]) -> list[Path]:
    out: list[Path] = []
    for d in dirs:
        base = root / d
        if base.is_dir():
            out.extend(sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts))
    return out


def _rel(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix()


def _walk_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        out.extend(Path(dirpath) / f for f in filenames if f.lower().endswith(suffixes))
    return sorted(out)


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _module_level(node: ast.AST):
    """Yield nodes that execute at import time (skips function bodies and TYPE_CHECKING blocks)."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        if isinstance(child, ast.If) and _is_type_checking(child.test):
            continue
        yield child
        yield from _module_level(child)


def _is_main_guard(test: ast.expr) -> bool:
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


# --- version ---------------------------------------------------------------------------------------


def test_version_files_agree(repo_root: Path):
    file_version = (repo_root / "VERSION").read_text(encoding="utf-8").strip()
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    assert SEMVER.match(file_version), file_version
    assert file_version == __version__ == pyproject["project"]["version"]


def test_changelog_has_unreleased_section(repo_root: Path):
    text = (repo_root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(r"^## \[Unreleased\]\s*$", text, re.MULTILINE)


# --- python source rules ---------------------------------------------------------------------------


def test_no_heavy_import_at_module_top_level(repo_root: Path):
    offenders: list[str] = []
    for path in _py_files(repo_root, _ns_dirs(repo_root)):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in _module_level(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            offenders += [
                f"{_rel(repo_root, path)}:{node.lineno} imports {n} at module level"
                for n in names
                if n.split(".")[0] in HEAVY
            ]
    assert not offenders, "heavy imports belong inside functions:\n" + "\n".join(offenders)


def test_no_shell_true(repo_root: Path):
    offenders: list[str] = []
    for path in _py_files(repo_root, [*_ns_dirs(repo_root), "installer", "launchers", "tools", "dev"]):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "shell":
                if isinstance(node.value, ast.Constant) and node.value.value is True:
                    offenders.append(f"{_rel(repo_root, path)}:{node.value.lineno}")
    assert not offenders, "shell=True is banned:\n" + "\n".join(offenders)


def test_no_print_outside_cli_modules(repo_root: Path):
    offenders: list[str] = []
    for path in _py_files(repo_root, _ns_dirs(repo_root)):
        rel = _rel(repo_root, path)
        if path.name in PRINT_OK_NAMES or rel in PRINT_OK_PATHS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        guarded: set[int] = set()
        for node in tree.body:
            if isinstance(node, ast.If) and _is_main_guard(node.test):
                guarded.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        for node in ast.walk(tree):
            is_print = (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print"
            )
            if is_print and node.lineno not in guarded:
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, "use NsLogger, print is for CLI modules only:\n" + "\n".join(offenders)


# --- PowerShell ------------------------------------------------------------------------------------


def _code_lines(text: str) -> list[str]:
    text = text.lstrip("﻿")
    text = re.sub(r"<#.*?#>", "", text, flags=re.DOTALL)
    lines = [ln.strip() for ln in text.splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("#")]


def _after_param_block(lines: list[str]) -> list[str]:
    """Drop a leading [CmdletBinding()] / param(...) block (PowerShell requires it to come first)."""
    i = 0
    while i < len(lines) and lines[i].lower().startswith("[cmdletbinding"):
        i += 1
    if i < len(lines) and lines[i].lower().startswith("param"):
        depth = 0
        while i < len(lines):
            depth += lines[i].count("(") - lines[i].count(")")
            i += 1
            if depth <= 0:
                break
    return lines[i:]


def test_powershell_scripts_start_strict(repo_root: Path):
    offenders: list[str] = []
    for path in _walk_files(repo_root, (".ps1",)):
        rel = _rel(repo_root, path)
        raw = path.read_bytes()
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            offenders.append(f"{rel}: not ASCII-only")
            text = raw.decode("utf-8", errors="replace")
        code = _after_param_block(_code_lines(text))
        first = code[0].lower() if len(code) > 0 else ""
        second = code[1].lower().replace('"', "'") if len(code) > 1 else ""
        if first != "set-strictmode -version latest":
            offenders.append(f"{rel}: first statement must be 'Set-StrictMode -Version Latest'")
        if second != "$erroractionpreference = 'stop'":
            offenders.append(f"{rel}: second statement must be \"$ErrorActionPreference = 'Stop'\"")
        if re.search(r"\b(invoke-expression|iex)\b", "\n".join(_code_lines(text)), re.IGNORECASE):
            offenders.append(f"{rel}: Invoke-Expression is banned")
    assert not offenders, "\n".join(offenders)


# --- contracts -------------------------------------------------------------------------------------


def test_error_codes_used_in_code_exist_in_registry(repo_root: Path):
    known = set(json.loads((repo_root / "contracts" / "errors.json").read_text(encoding="utf-8")))
    pattern = re.compile(r"NS-[EW]\d{3}")
    roots = [*_ns_dirs(repo_root), "installer", "launchers", "tools", "dev"]
    offenders: list[str] = []
    for d in roots:
        base = repo_root / d
        if not base.is_dir():
            continue
        for path in _walk_files(base, (".py", ".ps1", ".cmd")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for code in sorted(set(pattern.findall(text)) - known):
                offenders.append(f"{_rel(repo_root, path)} uses {code}")
    assert not offenders, "codes missing from contracts/errors.json:\n" + "\n".join(offenders)


def test_schema_files_match_exported_models(repo_root: Path, tmp_path: Path):
    exported = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in export_schemas(tmp_path)}
    schema_dir = repo_root / "contracts" / "schemas"
    on_disk = {
        p.name: json.loads(p.read_text(encoding="utf-8")) for p in sorted(schema_dir.glob("*.schema.json"))
    }
    assert set(on_disk) == set(exported), "schemas are exported from models, never hand-written"
    for name, schema in exported.items():
        assert on_disk[name] == schema, f"{name} drifted; run python -m ns_common.models"
