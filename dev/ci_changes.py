"""Is this CI run a docs-only change, and the light path that tests one (H-11).

A pull request that changes documents only cannot change what the shards, test-collect, ui-build, bundle and
pyright check: none of them reads a document. What it can break is the tests that READ documents (the box
lists, the spec text, the inventory, the hygiene checks, the change-request status, the release wiring, the
Assistant's own docs search). Those are listed here, in ``DOC_TESTS``, and the light job ``test-docs`` runs
them; ``lint``, ``powershell-parse`` and ``scope-check`` run as always. Everything else, and any doubt, runs
the whole of CI.

What a document is (``is_document``), conservatively:

* anything under ``docs/``;
* a Markdown file at the top of the repository (``README.md``, ``CHANGELOG.md``, ``BUILD_SPEC.md``, ...);
* ``contracts/CHANGE_REQUESTS.md``.

A Markdown file anywhere else is code here: the product reads ``ns_studio/prompts/*.md``,
``ns_studio/exemplars/**/*.md`` and ``ns_dashboard/prompts/assistant.md`` at run time, the UI build's Tailwind
scanner reads every file under ``ns_dashboard/ui``, release.yml builds the release body from
``dev/release_notes.md``, and ``contracts/`` is the frozen interface.

Commands (``python dev/ci_changes.py <command> ...``)::

    decide --event NAME [--base SHA] [--head SHA] [--repo DIR] [--summary]
                        docs_only=true|false to $GITHUB_OUTPUT (the `changes` job), with the reason
    doc-tests           the test files that read documents, one per line
    run-doc-tests -- <pytest args>
                        run pytest on them (the `test-docs` job)
    gate --doc-report FILE [--summary]
                        the required check `test`: its docs path and its code path (env: see ``gate``)

Standard library only, so the CI jobs that call it need no install.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]

#: The test files that read documents: the docs-only path runs exactly these. ``tests/test_ci_changes.py``
#: fails when a test file that names a document is in neither this list nor ``NOT_DOC_READERS``.
DOC_TESTS: tuple[str, ...] = (
    "tests/test_assistant_eval.py",  # search_docs serves README.md and OPERATOR.md (docs_index)
    "tests/test_captions.py",  # BUILD_SPEC.md 7.6 and captions.drift_ms agree
    "tests/test_change_requests_status.py",  # contracts/CHANGE_REQUESTS.md, docs/ENGINEERING.md
    "tests/test_ci_changes.py",  # this list against what the tests name (reads the docs/ tree)
    "tests/test_ci_workflow.py",  # docs/REPO_SETTINGS.md names the required checks
    "tests/test_dashboard_assistant.py",  # search_docs over the real README.md and OPERATOR.md
    "tests/test_devfake_tool_cue.py",  # the assistant-tool rows of docs/VERIFICATION_INVENTORY.md
    "tests/test_finalization.py",  # docs/FINALIZATION.md, docs/FEATURE_STATUS.md (dev/finalization_status.py)
    "tests/test_hygiene.py",  # CHANGELOG.md and every tracked file
    "tests/test_installer_app_copy.py",  # git ls-files: every tracked file, documents included
    "tests/test_installer_driver_detection.py",  # OPERATOR.md names NS-W115
    "tests/test_installer_files.py",  # BUILD_SPEC.md, OPERATOR.md
    "tests/test_installer_first_run.py",  # OPERATOR.md
    "tests/test_release_wiring.py",  # CHANGELOG.md, docs/REPO_SETTINGS.md, docs/HANDOFF.md
    "tests/test_spec_installer_text.py",  # BUILD_SPEC.md, docs/FEATURE_STATUS.md, OPERATOR.md
    "tests/test_spec_runtime_text.py",  # BUILD_SPEC.md, docs/FEATURE_STATUS.md
    "tests/test_text_hygiene.py",  # control characters in every tracked text file (git ls-files)
    "tests/test_verification_inventory.py",  # docs/VERIFICATION_INVENTORY.md (dev/verification_inventory.py)
    "tests/test_webview2_pin.py",  # contracts/CHANGE_REQUESTS.md
)

#: Files that name a document without reading the repository's copy of it, each with the reason. A test file
#: here is not run on the docs-only path; a module here does not make the tests that import it doc readers.
NOT_DOC_READERS: dict[str, str] = {
    "dev/check_scope.py": "document paths are its scope rules; scope-check runs it on each pull request",
    "installer/stage2/downloader.py": "README.md is a name it skips in a GitHub tree listing",
    "installer/stage2/web/appcopy.py": "names the top-level entries it copies (docs, *.md); reads no text",
    "ns_supervisor/update.py": "reads CHANGELOG.md of the installed program, not of the repository",
    "tests/test_check_scope.py": "document paths are test data for the scope rules; it opens no document",
    "tests/test_installer_folder_rules.py": "repo/readme.md is a file it makes in a temporary folder",
    "tests/test_ns_update_release_notes.py": "writes its own CHANGELOG.md in a temporary folder, reads that",
    "tests/test_resolve_manifest.py": "README.md is an entry of a made-up GitHub tree listing",
    "tests/test_uv_shim.py": "README.md is a member of a made-up zip archive",
}

#: Folders that hold no Python the tests import (or too many files to walk for nothing).
SKIP_DIRS = frozenset(
    {".git", ".github", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "docs"}
)

#: name-status letters a docs-only change may carry: added, modified, deleted, renamed, copied. A type change
#: (T), an unmerged path (U), an unknown (X) or a broken pair (B) is doubt, and doubt runs everything.
DOC_STATUSES = frozenset("AMDRC")

SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


# ---- what a document is ---------------------------------------------------------------------------------
def normalize(path: str) -> str:
    text = path.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def is_document(path: str) -> bool:
    """True for docs/**, a top-level *.md and contracts/CHANGE_REQUESTS.md; False for everything else."""
    p = normalize(path)
    if not p or ".." in PurePosixPath(p).parts:
        return False
    if p.startswith("docs/") and len(p) > len("docs/"):
        return True
    if "/" not in p and p.endswith(".md"):
        return True
    return p == "contracts/CHANGE_REQUESTS.md"


# ---- the decision ----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Decision:
    docs_only: bool
    reason: str
    paths: tuple[str, ...] = ()


def parse_name_status(raw: str) -> list[tuple[str, tuple[str, ...]]]:
    """``git diff --name-status -z`` output as ``[(status, paths)]``; a rename or copy has two paths.

    Raises ValueError on anything it cannot read (the caller treats that as a code change).
    """
    fields = raw.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    entries: list[tuple[str, tuple[str, ...]]] = []
    i = 0
    while i < len(fields):
        status = fields[i]
        i += 1
        if not status or not status[0].isalpha():
            raise ValueError(f"no status letter in the diff listing at field {i}: {status!r}")
        n = 2 if status[0] in "RC" else 1
        paths = tuple(fields[i : i + n])
        i += n
        if len(paths) != n or not all(paths):
            raise ValueError(f"the diff listing ends inside the entry {status!r}")
        entries.append((status, paths))
    return entries


def _capped(items: Sequence[str], limit: int = 5) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


def classify(entries: Sequence[tuple[str, tuple[str, ...]]]) -> Decision:
    """Docs-only when every path of every entry is a document and every status is a plain one."""
    if not entries:
        return Decision(False, "empty diff: nothing to compare, so everything runs")
    paths = tuple(dict.fromkeys(normalize(p) for _, ps in entries for p in ps))
    odd = [f"{s} {' -> '.join(ps)}" for s, ps in entries if s[0] not in DOC_STATUSES]
    if odd:
        return Decision(False, f"a change git reports as {_capped(odd)}: everything runs", paths)
    code = [p for p in paths if not is_document(p)]
    if code:
        return Decision(False, f"{len(code)} changed path(s) are not documents: {_capped(code)}", paths)
    return Decision(True, f"docs-only change: {len(paths)} changed path(s), all documents", paths)


def git_diff(base: str, head: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", "diff", "--name-status", "-z", "--no-renames", base, head],
        cwd=cwd,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"git diff exited {proc.returncode}: {err[-1] if err else 'no message'}")
    return proc.stdout.decode("utf-8", "surrogateescape")


def decide(event: str, base: str, head: str, cwd: Path = ROOT) -> Decision:
    """The whole decision. Only a ``pull_request`` can be docs-only; any failure is a code change."""
    if event != "pull_request":
        return Decision(
            False, f"{event or 'an unnamed'} event: everything runs (only a pull request can skip)"
        )
    base, head = base.strip().lower(), head.strip().lower()
    for name, sha in (("base", base), ("head", head)):
        if not SHA_RE.match(sha) or set(sha) == {"0"}:
            return Decision(False, f"no base or head commit to compare ({name} is {sha!r}): everything runs")
    try:
        raw = git_diff(base, head, cwd)
        entries = parse_name_status(raw)
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        return Decision(False, f"the diff failed ({exc}): everything runs")
    return classify(entries)


# ---- the tests that read documents -----------------------------------------------------------------------
def documents(root: Path) -> set[str]:
    found = {"contracts/CHANGE_REQUESTS.md"}
    docs = root / "docs"
    if docs.is_dir():
        for dirpath, _dirnames, filenames in os.walk(docs):
            found.update((Path(dirpath) / f).relative_to(root).as_posix() for f in filenames)
    found.update(p.name for p in root.glob("*.md"))
    return found


def _strings(tree: ast.AST) -> list[str]:
    """Every string constant in the code, docstrings left out (a citation in prose reads nothing)."""
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]


def _imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


def _names_documents(strings: Iterable[str], doc_names: set[str]) -> list[str]:
    """The strings that point at a document: a docs/ path, a document's name, or a listing of them all."""
    hits = []
    for s in strings:
        t = normalize(s.strip())
        last = t.rsplit("/", 1)[-1].lower()
        if (
            t == "docs"
            or t.startswith("docs/")
            or t == "ls-files"
            or (t.lower().endswith(".md") and last in doc_names)
        ):
            hits.append(t)
    return hits


def _parse(path: Path, needles: Iterable[str]) -> ast.AST | None:
    """The syntax tree of a Python file, or None when it cannot be read or holds none of ``needles`` (a
    cheap text test first: every string the rules above can match contains one of them)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lowered = text.lower()
    if not any(n in lowered for n in needles):
        return None
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        return None


#: The text a file must contain for a string in it to name a document (lower case).
DOC_NEEDLES = ("docs", ".md", "ls-files")


def _python_files(root: Path, tests: bool) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        top = rel.parts[0] if rel.parts else ""
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        if (top == "tests") != tests:
            if not rel.parts:
                continue  # the root itself: walk on into its folders
            dirnames[:] = []
            continue
        out.extend(Path(dirpath) / f for f in sorted(filenames) if f.endswith(".py"))
    return out


def document_modules(root: Path, exempt: bool = True) -> dict[str, list[str]]:
    """Repository modules (outside tests/) that name a document, by path, with what they name.

    With ``exempt`` the modules in ``NOT_DOC_READERS`` are left out.
    """
    doc_names = {d.rsplit("/", 1)[-1].lower() for d in documents(root) if d.endswith(".md")}
    found: dict[str, list[str]] = {}
    for path in _python_files(root, tests=False):
        rel = path.relative_to(root).as_posix()
        if exempt and rel in NOT_DOC_READERS:
            continue
        tree = _parse(path, DOC_NEEDLES)
        hits = _names_documents(_strings(tree), doc_names) if tree is not None else []
        if hits:
            found[rel] = sorted(set(hits))
    return found


def _module_name(rel: str) -> str:
    name = rel[: -len(".py")].replace("/", ".")
    return name[: -len(".__init__")] if name.endswith(".__init__") else name


def document_readers(root: Path) -> dict[str, list[str]]:
    """Test files that name a document, by path, with why: a docs/ path or a document's name in a string
    (docstrings left out), ``git ls-files``, an import of a module that names a document, or that module's
    file name (a script loaded by path). A test that reaches a document only through a module that does not
    name one itself is not found: add it to ``DOC_TESTS`` by hand."""
    doc_names = {d.rsplit("/", 1)[-1].lower() for d in documents(root) if d.endswith(".md")}
    modules = document_modules(root)
    by_name = {_module_name(rel): rel for rel in modules}
    by_file = {rel.rsplit("/", 1)[-1]: rel for rel in modules}
    # a module is imported by a name that ends in its last part, or loaded by its file name
    needles = (
        *DOC_NEEDLES,
        *(name.rsplit(".", 1)[-1].lower() for name in by_name),
        *(f.lower() for f in by_file),
    )
    readers: dict[str, list[str]] = {}
    for path in _python_files(root, tests=True):
        if not (path.name.startswith("test_") or path.name.endswith("_test.py")):
            continue
        tree = _parse(path, needles)
        if tree is None:
            continue
        strings = _strings(tree)
        why = [f"names {h}" for h in sorted(set(_names_documents(strings, doc_names)))]
        why += [f"imports {name} ({by_name[name]})" for name in sorted(_imports(tree)) if name in by_name]
        why += [
            f"loads {by_file[last]} by path"
            for last in sorted({normalize(s).rsplit("/", 1)[-1] for s in strings})
            if last in by_file
        ]
        if why:
            readers[path.relative_to(root).as_posix()] = why
    return readers


# ---- the gate: the required check `test` -----------------------------------------------------------------
@dataclass(frozen=True)
class DocReport:
    ran: int
    skipped: int
    failed: int


def read_doc_report(path: Path) -> DocReport:
    cases = list(ET.parse(path).getroot().iter("testcase"))
    skipped = sum(1 for c in cases if c.find("skipped") is not None)
    failed = sum(1 for c in cases if c.find("failure") is not None or c.find("error") is not None)
    return DocReport(ran=len(cases) - skipped, skipped=skipped, failed=failed)


def gate(env: Mapping[str, str], doc_report: Path) -> tuple[list[str], list[str]]:
    """``(problems, summary lines)`` of the final `test` job.

    ``env``: CHANGES_RESULT (the `changes` job), DOCS_ONLY (its output: exactly ``true`` is the docs path,
    anything else the code path), DOC_TESTS_RESULT (`test-docs`), SHARD_RESULT (`test-shard`, all shards),
    COLLECT_RESULT (`test-collect`).

    The docs path passes only when the decision succeeded, `test-docs` succeeded and its report holds at
    least one test that ran and none that failed, and no shard or collection failed. The code path is the gate
    as it was before H-11: every shard and the collection must have succeeded (a skipped shard fails).
    Both paths also need the decision job itself to have succeeded.
    """
    changes = env.get("CHANGES_RESULT", "")
    docs_only = env.get("DOCS_ONLY", "") == "true"
    docs = env.get("DOC_TESTS_RESULT", "")
    shards = env.get("SHARD_RESULT", "")
    collect = env.get("COLLECT_RESULT", "")
    problems: list[str] = []
    summary: list[str] = []
    if changes != "success":
        problems.append(
            f"the changes job (the docs-only decision) did not succeed (result: {changes or 'none'}); "
            "everything ran as for code, but rerun the workflow so the decision itself is green"
        )
    if docs_only:
        print(f"docs path: changes {changes}, test-docs {docs}, test-shard {shards}, test-collect {collect}")
        if docs != "success":
            problems.append(
                f"test-docs (the tests that read documents) did not succeed (result: {docs or 'none'})"
            )
        for job, result in (("test-shard", shards), ("test-collect", collect)):
            if result not in ("skipped", "success"):
                problems.append(
                    f"{job} ran on a docs-only change and did not succeed (result: {result or 'none'})"
                )
        if not doc_report.is_file():
            problems.append(f"no report of the document tests at {doc_report.as_posix()}")
        else:
            try:
                rep = read_doc_report(doc_report)
            except (ET.ParseError, OSError) as exc:
                problems.append(f"the report of the document tests is not readable XML ({exc})")
            else:
                if rep.failed:
                    problems.append(f"the document tests: {rep.failed} failed")
                if rep.ran - rep.failed <= 0:
                    problems.append(
                        "no document test ran: a docs-only change needs the tests that read documents"
                    )
                if not problems:
                    summary.append(
                        f"docs-only change: {rep.ran} document tests ran, the code suite was not run"
                    )
                    summary.append("")
                    summary.append(
                        f"{len(DOC_TESTS)} test files read documents (`dev/ci_changes.py` DOC_TESTS); "
                        f"{rep.skipped} skipped. lint, powershell-parse and scope-check ran as always; "
                        "types and ui-build said not applicable; the shards, test-collect and bundle did "
                        "not run."
                    )
    else:
        print(f"code path: test-shard (all shards) {shards}, test-collect {collect}")
        if shards != "success":
            problems.append(
                f"the test shards did not all succeed (result: {shards or 'none'}); "
                "a failed, skipped or cancelled shard fails the required check"
            )
        if collect != "success":
            problems.append(f"test-collect did not succeed (result: {collect or 'none'})")
    return problems, summary


# ---- command line ----------------------------------------------------------------------------------------
def _append(env_name: str, text: str) -> None:
    target = os.environ.get(env_name)
    if target:
        with open(target, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(text)


def _cmd_decide(args: argparse.Namespace) -> int:
    d = decide(args.event, args.base, args.head, Path(args.repo))
    value = "true" if d.docs_only else "false"
    print(d.reason)
    for p in d.paths[:50]:
        print(f"  {p}")
    if len(d.paths) > 50:
        print(f"  ... and {len(d.paths) - 50} more")
    if args.summary:
        what = "docs-only change: the light path runs" if d.docs_only else "code change: everything runs"
        _append("GITHUB_STEP_SUMMARY", f"### changes\n\n{what}. {d.reason}\n")
    # last: the workflow reads this, and nothing after it may fail the job
    print(f"docs_only={value}")
    _append("GITHUB_OUTPUT", f"docs_only={value}\n")
    return 0


def _cmd_doc_tests(_args: argparse.Namespace) -> int:
    sys.stdout.write("".join(f"{f}\n" for f in DOC_TESTS))
    return 0


def _cmd_run_doc_tests(pytest_args: list[str]) -> int:
    gone = [f for f in DOC_TESTS if not (ROOT / f).is_file()]
    if gone:
        print(f"::error::listed in DOC_TESTS but not in the repository: {', '.join(gone)}")
        return 2
    print(f"{len(DOC_TESTS)} test files that read documents (dev/ci_changes.py DOC_TESTS)", flush=True)
    return subprocess.run(
        [sys.executable, "-m", "pytest", *pytest_args, *DOC_TESTS], cwd=ROOT, check=False
    ).returncode


def _cmd_gate(args: argparse.Namespace) -> int:
    problems, summary = gate(os.environ, Path(args.doc_report))
    for line in summary:
        if line:
            print(line)
    for p in problems:
        print(f"::error::{p}")
    if args.summary:
        _append("GITHUB_STEP_SUMMARY", "### test\n\n" + ("\n".join(summary) + "\n" if summary else ""))
        if problems:
            _append("GITHUB_STEP_SUMMARY", "".join(f"- {p}\n" for p in problems))
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    pytest_args: list[str] = []
    if "--" in args_in:
        cut = args_in.index("--")
        args_in, pytest_args = args_in[:cut], args_in[cut + 1 :]
    parser = argparse.ArgumentParser(prog="ci_changes", description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    dec = sub.add_parser("decide", help="docs_only=true|false for this run")
    dec.add_argument("--event", required=True, help="github.event_name")
    dec.add_argument("--base", default="", help="the pull request's base commit")
    dec.add_argument("--head", default="", help="the commit under test (github.sha)")
    dec.add_argument("--repo", default=str(ROOT), help="the checkout to diff in")
    dec.add_argument("--summary", action="store_true", help="append the decision to $GITHUB_STEP_SUMMARY")
    sub.add_parser("doc-tests", help="print the test files that read documents")
    sub.add_parser("run-doc-tests", help="run pytest on them (arguments after --)")
    gt = sub.add_parser("gate", help="the final `test` job: pass or fail the run's path")
    gt.add_argument("--doc-report", required=True, help="the JUnit report of test-docs")
    gt.add_argument("--summary", action="store_true", help="append the verdict to $GITHUB_STEP_SUMMARY")
    args = parser.parse_args(args_in)
    if args.command == "decide":
        return _cmd_decide(args)
    if args.command == "doc-tests":
        return _cmd_doc_tests(args)
    if args.command == "run-doc-tests":
        return _cmd_run_doc_tests(pytest_args)
    return _cmd_gate(args)


if __name__ == "__main__":
    raise SystemExit(main())
