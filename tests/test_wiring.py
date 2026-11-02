"""Wiring checks (docs/design/system-gaps.md, "Unwired call" rows).

1. Every `ns_*` module imports (nothing heavy at module level, no dangling import).
2. Every dotted Python target the dashboard names in `ns_dashboard/adapters.py` and
   `ns_dashboard/assistant/tools.py` resolves to a real object. The targets are read from the source of
   those two files, not listed here: a `lazy("module", "attr")` call, an `import_module("module")` call, a
   `("module", "attr")` pair in a table, or a plain string such as `"ns_studio.orchestrator.run_job"`.
   Docstrings and messages are ignored. A new adapter that points at a function nobody wrote fails here.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("ns_common", "ns_studio", "ns_tts", "ns_supervisor", "ns_dashboard")
TARGET_FILES = ("ns_dashboard/adapters.py", "ns_dashboard/assistant/tools.py")
MODULE = re.compile(r"^ns_[a-z0-9_]+(\.[A-Za-z_][A-Za-z0-9_]*)*$")
DOTTED = re.compile(r"^ns_[a-z0-9_]+(\.[A-Za-z_][A-Za-z0-9_]*)+$")
COLON = re.compile(r"^(ns_[a-z0-9_]+(?:\.[A-Za-z_][A-Za-z0-9_]*)*):([A-Za-z_][A-Za-z0-9_.]*)$")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ---------------------------------------------------------------- 1. every module imports


def all_modules() -> list[str]:
    names: list[str] = []
    for pkg in PACKAGES:
        top = importlib.import_module(pkg)
        names.append(pkg)
        for info in pkgutil.walk_packages(top.__path__, prefix=f"{pkg}."):
            if info.name.rsplit(".", 1)[-1] != "__main__":
                names.append(info.name)
    return names


def test_every_ns_module_imports():
    failures: list[str] = []
    names = all_modules()
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as e:  # report every broken module at once
            failures.append(f"{name}: {type(e).__name__}: {e}")
    assert len(names) > 100, "package walk found suspiciously few modules"
    assert not failures, "modules that do not import:\n" + "\n".join(failures)


# ---------------------------------------------------------------- 2. dashboard targets resolve


def _docstring_nodes(tree: ast.AST) -> set[int]:
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            skip.add(id(node.value))  # bare strings: docstrings and comments-as-strings
    return skip


def _str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _call_name(call: ast.Call) -> str:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def collect_targets(source: str) -> set[tuple[str, str]]:
    """(module, attribute path) pairs named in `source`; attribute path is '' for a bare module."""
    tree = ast.parse(source)
    skip = _docstring_nodes(tree)
    found: set[tuple[str, str]] = set()
    covered: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) in ("lazy", "import_module") and node.args:
            mod = _str(node.args[0])
            attr = _str(node.args[1]) if len(node.args) > 1 else ""
            if mod and MODULE.match(mod) and attr is not None:
                found.add((mod, attr))
                covered.update(id(a) for a in node.args)
        elif isinstance(node, ast.Tuple | ast.List) and len(node.elts) == 2:
            mod, attr = _str(node.elts[0]), _str(node.elts[1])
            if mod and attr and MODULE.match(mod) and IDENT.match(attr):
                found.add((mod, attr))
                covered.update(id(e) for e in node.elts)
    for node in ast.walk(tree):
        text = _str(node)
        if text is None or id(node) in skip or id(node) in covered:
            continue
        m = COLON.match(text)
        if m:
            found.add((m.group(1), m.group(2)))
        elif DOTTED.match(text):
            found.add((text, ""))  # resolved below: longest importable module prefix + attributes
    return found


def resolve(module: str, attr: str) -> Any:
    """Import `module` and walk `attr`; with no `attr`, `module` may be `package.module.object`."""
    if attr:
        obj: Any = importlib.import_module(module)
        for part in attr.split("."):
            obj = getattr(obj, part)
        return obj
    parts = module.split(".")
    for cut in range(len(parts), 0, -1):
        name = ".".join(parts[:cut])
        try:
            obj = importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name and (name == e.name or name.startswith(e.name + ".")):
                continue  # `name` is not a module: it is an attribute of the one before
            raise
        for part in parts[cut:]:
            obj = getattr(obj, part)
        return obj
    raise ImportError(module)


def dashboard_targets() -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for rel in TARGET_FILES:
        for mod, attr in sorted(collect_targets((ROOT / rel).read_text(encoding="utf-8"))):
            out.append((rel, mod, attr))
    return out


TARGETS = dashboard_targets()


def test_the_dashboard_names_some_targets():
    assert any(rel.endswith("adapters.py") for rel, _, _ in TARGETS), "no adapter targets were found"


def _target_id(rel: str, module: str, attr: str) -> str:
    return f"{rel.split('/')[-1]}:{module}{'.' + attr if attr else ''}"


@pytest.mark.parametrize(
    ("rel", "module", "attr"), TARGETS, ids=[_target_id(*t) for t in TARGETS]
)
def test_dashboard_target_resolves(rel: str, module: str, attr: str):
    try:
        obj = resolve(module, attr)
    except (ImportError, AttributeError) as e:
        pytest.fail(f"{rel} points at {module}{'.' + attr if attr else ''}, which does not exist ({e})")
    assert obj is not None


# ---------------------------------------------------------------- the collector itself


def test_collector_reads_lazy_calls_tables_and_strings_but_not_docstrings():
    src = '''
"""ns_studio.never.imported is only prose."""
from x import lazy
def f():
    """Expects `ns_studio.orchestrator.nothing_here` (C7)."""
    a = lazy("ns_studio.orchestrator", "run_job")
    b = importlib.import_module("ns_supervisor.cli")
    c = ("ns_supervisor.verify", "run_verify")
    d = "ns_supervisor.doctor.build_bundle"
    e = "ns_supervisor.update:update_app"
    raise Error("ns_studio.cli is not installed yet")
'''
    assert collect_targets(src) == {
        ("ns_studio.orchestrator", "run_job"),
        ("ns_supervisor.cli", ""),
        ("ns_supervisor.verify", "run_verify"),
        ("ns_supervisor.doctor.build_bundle", ""),
        ("ns_supervisor.update", "update_app"),
    }
    assert resolve("ns_supervisor.doctor.build_bundle", "") is resolve("ns_supervisor.doctor", "build_bundle")
    assert isinstance(resolve("ns_supervisor.cli", ""), ModuleType)
    with pytest.raises(AttributeError):
        resolve("ns_studio.orchestrator", "run_job_with_progress_that_nobody_wrote")
    with pytest.raises(ImportError):
        resolve("ns_studio.no_such_module", "x")
