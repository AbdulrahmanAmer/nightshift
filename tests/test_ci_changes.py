"""H-11: a pull request that changes only documents does not run the whole suite (dev/ci_changes.py).

Three things are pinned here:

* the decision (``decide``): only a pull request whose tested tree differs from its base in documents alone is
  docs-only; every other event, an empty diff, a failed diff and a missing base run everything;
* the list of tests that read documents (``DOC_TESTS``): a test file that names a document and is not on it
  (or on the reviewed exemptions) fails here, so the light job cannot silently miss one;
* the gate of the required check ``test`` (``gate``): on the docs path it passes only when the document tests
  ran and passed, and says how many; on the code path a failed, skipped or cancelled shard fails it, as
  before.
"""

from __future__ import annotations

import json
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from dev import ci_changes as cc

ROOT = Path(__file__).resolve().parents[1]


# ---- what a document is ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    [
        "docs/FINALIZATION.md",
        "docs/superpowers/plans/m0-c4-tts-service.md",
        "docs/design-tool/designer.js",  # docs/** is documents, whatever the extension
        "README.md",
        "CHANGELOG.md",
        "BUILD_SPEC.md",
        "OPERATOR.md",
        "contracts/CHANGE_REQUESTS.md",
        "docs\\HANDOFF.md",
        "./docs/HANDOFF.md",
    ],
)
def test_these_paths_are_documents(path: str):
    assert cc.is_document(path), path


@pytest.mark.parametrize(
    "path",
    [
        # Markdown the product or the release reads at run time is code, not a document
        "ns_studio/prompts/hook.md",
        "ns_studio/exemplars/horror/doorbell.md",
        "ns_dashboard/prompts/assistant.md",
        "ns_dashboard/ui/README.md",  # Tailwind scans every file of the UI project
        "dev/release_notes.md",  # release.yml builds the release body from it
        "contracts/CONTRACTS.md",  # the frozen interface: protected path
        "ns_studio/presets/fonts/README.md",
        "tests/fixtures/notes.md",
        ".github/workflows/ci.yml",
        "pyproject.toml",
        "ns_x/app.py",
        "docs",  # a file named docs at the top is not the docs folder
        "Docs/guide.md",  # git paths are case-sensitive: not the docs folder
        "docs/../ns_studio/orchestrator.py",
        "README.MD.py",
        "",
    ],
)
def test_these_paths_are_code(path: str):
    assert not cc.is_document(path), path


def test_every_markdown_file_the_product_ships_inside_a_package_is_code():
    found = [
        p.relative_to(ROOT).as_posix() for p in ROOT.glob("ns_*/**/*.md") if "node_modules" not in p.parts
    ]
    assert any(f.startswith("ns_studio/prompts/") for f in found), "the prompt files moved: update this test"
    for rel in found:
        assert not cc.is_document(rel), rel


# ---- the decision on a real repository ---------------------------------------------------------------------
def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def write(repo: Path, rel: str, text: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    for key, value in (
        ("user.email", "ci@example.invalid"),
        ("user.name", "ci"),
        ("core.autocrlf", "false"),
        ("commit.gpgsign", "false"),
    ):
        git(r, "config", key, value)
    write(r, "docs/guide.md", "guide\n")
    write(r, "README.md", "readme\n")
    write(r, "contracts/CHANGE_REQUESTS.md", "requests\n")
    write(r, "ns_x/app.py", "x = 1\n")
    write(r, "ns_x/old.py", "y = 2\n")
    write(r, "ns_x/prompts/hook.md", "a prompt\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    return r


def commit(repo: Path, message: str = "change") -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def test_a_pull_request_that_changes_only_documents_is_docs_only(repo: Path):
    base = head(repo)
    write(repo, "docs/guide.md", "guide, ticked\n")
    write(repo, "docs/new/notes.md", "new\n")
    write(repo, "README.md", "readme, reworded\n")
    write(repo, "contracts/CHANGE_REQUESTS.md", "requests, closed\n")
    tip = commit(repo)
    d = cc.decide("pull_request", base, tip, cwd=repo)
    assert d.docs_only is True, d.reason
    assert sorted(d.paths) == [
        "README.md",
        "contracts/CHANGE_REQUESTS.md",
        "docs/guide.md",
        "docs/new/notes.md",
    ]


def test_documents_plus_one_code_file_is_a_code_change(repo: Path):
    base = head(repo)
    write(repo, "docs/guide.md", "guide, ticked\n")
    write(repo, "ns_x/app.py", "x = 2\n")
    d = cc.decide("pull_request", base, commit(repo), cwd=repo)
    assert d.docs_only is False
    assert "ns_x/app.py" in d.reason


def test_a_markdown_prompt_the_product_reads_is_a_code_change(repo: Path):
    base = head(repo)
    write(repo, "ns_x/prompts/hook.md", "a different prompt\n")
    d = cc.decide("pull_request", base, commit(repo), cwd=repo)
    assert d.docs_only is False
    assert "ns_x/prompts/hook.md" in d.reason


def test_a_rename_from_docs_into_code_is_a_code_change(repo: Path):
    base = head(repo)
    git(repo, "mv", "docs/guide.md", "ns_x/guide.py")
    d = cc.decide("pull_request", base, commit(repo), cwd=repo)
    assert d.docs_only is False
    assert "ns_x/guide.py" in d.reason


def test_a_deleted_code_file_is_a_code_change(repo: Path):
    base = head(repo)
    write(repo, "docs/guide.md", "guide, ticked\n")
    git(repo, "rm", "-q", "ns_x/old.py")
    d = cc.decide("pull_request", base, commit(repo), cwd=repo)
    assert d.docs_only is False
    assert "ns_x/old.py" in d.reason


@pytest.mark.parametrize(
    "machinery",
    ["dev/ci_changes.py", "dev/ci_shards.py", "dev/ci_shard_weights.json", ".github/workflows/ci.yml"],
)
def test_a_change_to_the_ci_machinery_runs_everything_even_if_is_document_were_wrong(
    repo: Path, monkeypatch: pytest.MonkeyPatch, machinery: str
):
    """The pull request's own copy of this script decides. Even if a careless edit made is_document say
    yes to everything, a pull request that touches the CI machinery still takes the full path."""
    base = head(repo)
    write(repo, "docs/guide.md", "guide, ticked\n")
    write(repo, machinery, "changed\n")
    tip = commit(repo)
    monkeypatch.setattr(cc, "is_document", lambda path: True)
    d = cc.decide("pull_request", base, tip, cwd=repo)
    assert d.docs_only is False
    assert "CI machinery" in d.reason and machinery in d.reason


def test_the_ci_machinery_is_found_under_either_name_of_a_rename():
    assert cc.touches_ci([("R100", ("dev/ci_changes.py", "docs/old.md"))]) == ["dev/ci_changes.py"]
    assert cc.touches_ci([("A", (".github/actions/x/action.yml",))]) == [".github/actions/x/action.yml"]
    assert cc.touches_ci([("M", ("docs/dev/ci_changes.py",)), ("M", ("README.md",))]) == []


def test_an_empty_diff_runs_everything(repo: Path):
    base = head(repo)
    tip = commit(repo, "nothing changed")
    d = cc.decide("pull_request", base, tip, cwd=repo)
    assert d.docs_only is False
    assert "empty" in d.reason


def test_a_failed_diff_runs_everything(repo: Path):
    unknown = "1" * 40  # a well-formed commit id this repository does not have (a force push dropped it)
    d = cc.decide("pull_request", unknown, head(repo), cwd=repo)
    assert d.docs_only is False
    assert "diff failed" in d.reason


def test_a_diff_outside_a_repository_runs_everything(tmp_path: Path):
    d = cc.decide("pull_request", "a" * 40, "b" * 40, cwd=tmp_path)
    assert d.docs_only is False
    assert "diff failed" in d.reason


@pytest.mark.parametrize("base", ["", "0" * 40, "not-a-sha", "abc123"])
def test_a_missing_or_malformed_base_runs_everything(repo: Path, base: str):
    d = cc.decide("pull_request", base, head(repo), cwd=repo)
    assert d.docs_only is False
    assert "no base" in d.reason


@pytest.mark.parametrize(
    "event", ["push", "schedule", "merge_group", "workflow_dispatch", "pull_request_target", ""]
)
def test_every_event_but_a_pull_request_runs_everything(tmp_path: Path, event: str):
    # push to main and tags always run everything; no git is needed to say so (tmp_path is no repository)
    d = cc.decide(event, "a" * 40, "b" * 40, cwd=tmp_path)
    assert d.docs_only is False
    assert "everything runs" in d.reason


def test_the_name_status_listing_is_read_with_renames_and_copies():
    raw = (
        "M\0docs/a.md\0R100\0docs/b.md\0docs/c.md\0C075\0README.md\0docs/d.md\0D\0docs/e.md\0A\0docs/f g.md\0"
    )
    entries = cc.parse_name_status(raw)
    assert entries == [
        ("M", ("docs/a.md",)),
        ("R100", ("docs/b.md", "docs/c.md")),
        ("C075", ("README.md", "docs/d.md")),
        ("D", ("docs/e.md",)),
        ("A", ("docs/f g.md",)),
    ]
    assert cc.classify(entries).docs_only is True
    # a rename whose new name is code is a code change, whichever way git reports it
    assert cc.classify(cc.parse_name_status("R100\0docs/b.md\0ns_x/b.py\0")).docs_only is False
    assert cc.classify(cc.parse_name_status("R100\0ns_x/b.py\0docs/b.md\0")).docs_only is False


@pytest.mark.parametrize(
    "raw",
    [
        "R100\0docs/a.md\0",  # a rename with one path
        "\0docs/a.md\0",  # no status letter
        "M",  # no path
    ],
)
def test_a_malformed_listing_is_refused(raw: str):
    with pytest.raises(ValueError):
        cc.parse_name_status(raw)


@pytest.mark.parametrize("status", ["T", "U", "X", "B"])
def test_an_odd_status_letter_is_a_code_change_even_on_a_document(status: str):
    # T (a file became a link), U (unmerged), X (unknown), B (broken pair): doubt runs everything
    assert cc.classify([(status, ("docs/a.md",))]).docs_only is False


# ---- the command the `changes` job runs -----------------------------------------------------------------
def test_decide_writes_docs_only_for_the_workflow_to_read(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    out, summary = tmp_path / "output.txt", tmp_path / "summary.md"
    out.write_text("earlier=1\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    base = head(repo)
    write(repo, "docs/guide.md", "guide, ticked\n")
    tip = commit(repo)
    args = [
        "decide",
        "--event",
        "pull_request",
        "--base",
        base,
        "--head",
        tip,
        "--repo",
        str(repo),
        "--summary",
    ]
    assert cc.main(args) == 0
    assert out.read_text(encoding="utf-8") == "earlier=1\ndocs_only=true\n"
    assert "docs_only=true" in capsys.readouterr().out
    assert "docs-only" in summary.read_text(encoding="utf-8")

    write(repo, "ns_x/app.py", "x = 3\n")
    tip = commit(repo)
    args[args.index("--head") + 1] = tip
    assert cc.main(args) == 0
    assert out.read_text(encoding="utf-8").endswith("docs_only=false\n")


def test_decide_on_a_push_needs_no_base_and_says_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    out = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert cc.main(["decide", "--event", "push", "--base", "", "--head", "b" * 40]) == 0
    assert out.read_text(encoding="utf-8") == "docs_only=false\n"


# ---- the list of tests that read documents ----------------------------------------------------------------
def test_every_listed_document_test_exists_and_is_a_test_file():
    assert cc.DOC_TESTS == tuple(sorted(set(cc.DOC_TESTS))), "keep DOC_TESTS sorted and without repeats"
    for rel in cc.DOC_TESTS:
        assert (ROOT / rel).is_file(), rel
        assert rel.startswith("tests/test_") and rel.endswith(".py"), rel


@pytest.mark.parametrize(
    "rel",
    [
        "tests/test_finalization.py",
        "tests/test_spec_installer_text.py",
        "tests/test_spec_runtime_text.py",
        "tests/test_verification_inventory.py",
        "tests/test_hygiene.py",
        "tests/test_text_hygiene.py",
        "tests/test_change_requests_status.py",
        "tests/test_release_wiring.py",
        "tests/test_assistant_eval.py",  # the Assistant's search_docs reads README.md and OPERATOR.md
    ],
)
def test_the_document_tests_the_order_names_are_on_the_list(rel: str):
    assert rel in cc.DOC_TESTS


@pytest.fixture(scope="module")
def repo_readers() -> dict[str, list[str]]:
    return cc.document_readers(ROOT)


def test_every_test_file_that_reads_a_document_is_on_the_list(repo_readers: dict[str, list[str]]):
    readers = repo_readers
    assert "tests/test_finalization.py" in readers, "the scanner must still see the finalization test"
    assert "tests/test_verification_inventory.py" in readers and "tests/test_assistant_eval.py" in readers
    missing = {f: why for f, why in readers.items() if f not in cc.DOC_TESTS and f not in cc.NOT_DOC_READERS}
    assert not missing, (
        "these test files name a document but are not in DOC_TESTS (dev/ci_changes.py), so a docs-only "
        "pull request would not run them. Add each to DOC_TESTS, or, if it only names a document without "
        f"reading the repository's copy, to NOT_DOC_READERS with the reason: {json.dumps(missing, indent=1)}"
    )


def test_every_exemption_is_still_needed_and_says_why(repo_readers: dict[str, list[str]]):
    flagged_tests = repo_readers
    flagged_modules = cc.document_modules(ROOT, exempt=False)
    for rel, why in cc.NOT_DOC_READERS.items():
        assert why and len(why) > 20, rel
        assert (ROOT / rel).is_file(), rel
        assert rel in flagged_tests or rel in flagged_modules, f"{rel} no longer names a document: drop it"
        assert rel not in cc.DOC_TESTS, rel


def make_tree(root: Path) -> None:
    write(root, "docs/GUIDE.md", "guide\n")
    write(root, "README.md", "readme\n")
    write(root, "pkg/__init__.py", "")
    write(root, "pkg/reader.py", 'from pathlib import Path\nTEXT = Path("README.md")\n')
    write(root, "pkg/plain.py", "X = 1\n")
    write(
        root,
        "tests/test_reads.py",
        'from pathlib import Path\nROOT = Path(".")\n'
        'def test_x():\n    (ROOT / "docs" / "GUIDE.md").read_text()\n',
    )
    write(root, "tests/test_names_a_root_doc.py", 'NAME = "README.md"\n')
    write(root, "tests/test_lists_the_repo.py", 'ARGS = ["git", "ls-files", "-z"]\n')
    write(root, "tests/test_imports_reader.py", "from pkg import reader\n")
    write(root, "tests/test_imports_reader_dotted.py", "import pkg.reader\n")
    write(root, "tests/test_loads_reader_by_path.py", 'SCRIPT = "pkg/reader.py"\n')
    write(root, "tests/test_docstring_only.py", '"""Cites docs/GUIDE.md and README.md in prose only."""\n')
    write(root, "tests/test_plain.py", "from pkg import plain\nURL = 'https://example.com/api/docs/'\n")


def test_the_scanner_finds_each_way_a_new_test_can_read_a_document(tmp_path: Path):
    make_tree(tmp_path)
    readers = cc.document_readers(tmp_path)
    assert set(readers) == {
        "tests/test_reads.py",
        "tests/test_names_a_root_doc.py",
        "tests/test_lists_the_repo.py",
        "tests/test_imports_reader.py",
        "tests/test_imports_reader_dotted.py",
        "tests/test_loads_reader_by_path.py",
    }, readers
    assert any("pkg/reader.py" in why for why in readers["tests/test_imports_reader.py"])


def make_transitive_tree(root: Path) -> None:
    """Tests that reach a document only through the repository's own modules, and one call-time reader."""
    write(root, "docs/GUIDE.md", "guide\n")
    write(root, "README.md", "readme\n")
    write(root, "pkg/__init__.py", "")
    write(root, "pkg/reader.py", 'from pathlib import Path\nTEXT = Path("README.md")\n')
    write(root, "pkg/middle.py", "from pkg import reader\n")
    write(root, "pkg/sub/__init__.py", "")
    write(root, "pkg/sub/relative.py", "from .. import middle\n")
    write(root, "pkg/runner.py", 'ARGS = ["python", "-m", "pkg.middle"]\n')
    write(root, "pkg/lazy.py", 'def search():\n    return open("README.md").read()\n')
    write(root, "pkg/hub.py", "from pkg import lazy\n")
    write(root, "tests/helper_mod.py", "from pkg import middle\n")
    write(root, "tests/sub/conftest.py", 'GUIDE = "docs/GUIDE.md"\n')
    write(root, "tests/test_via_middle.py", "import pkg.middle\n")
    write(root, "tests/test_via_relative.py", "from pkg.sub import relative\n")
    write(root, "tests/test_runs_module.py", 'ARGS = ["python", "-m", "pkg.middle"]\n')
    write(root, "tests/test_via_runner.py", "from pkg import runner\n")
    write(root, "tests/test_runs_script.py", 'ARGS = ["python", "pkg/middle.py"]\n')
    write(root, "tests/test_via_helper.py", "from tests import helper_mod\n")
    write(root, "tests/sub/test_under_conftest.py", "X = 1\n")
    write(root, "tests/test_hub_only.py", "from pkg import hub\n")
    write(root, "tests/test_hub_and_search.py", "from pkg import hub\nfrom pkg.lazy import search\n")
    # a test file that names a document in its own tests, and one that imports a helper from it
    write(
        root,
        "tests/test_names_doc.py",
        'from pathlib import Path\nNAME = "README.md"\ndef helper():\n    return 1\n',
    )
    write(root, "tests/test_imports_a_test.py", "from tests.test_names_doc import helper\n")
    write(root, "tests/test_plain.py", "import json\nURL = 'https://example.com/api/docs/'\n")


CALL_TIME = {"pkg/lazy.py": (("search",), "reads README.md only when search() is called; pkg.hub imports it")}


def test_the_scanner_follows_the_repository_s_own_imports_paths_and_module_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    make_transitive_tree(tmp_path)
    monkeypatch.setattr(cc, "CALL_TIME_READERS", CALL_TIME)
    readers = cc.document_readers(tmp_path)
    assert set(readers) == {
        "tests/test_via_middle.py",  # import -> a module that imports the reader
        "tests/test_via_relative.py",  # a relative import on the way
        "tests/test_runs_module.py",  # python -m pkg.middle
        "tests/test_via_runner.py",  # imports a module that runs pkg.middle as -m
        "tests/test_runs_script.py",  # runs pkg/middle.py by path
        "tests/test_via_helper.py",  # a helper under tests/
        "tests/sub/test_under_conftest.py",  # the conftest.py pytest loads names a document
        "tests/test_hub_and_search.py",  # calls the call-time reader's entry point
        "tests/test_names_doc.py",  # names one itself
    }, readers
    assert any(
        "pkg/reader.py <- pkg/middle.py <- tests/test_via_middle.py" in w
        for w in readers["tests/test_via_middle.py"]
    )
    assert any(w.startswith("calls search of pkg/lazy.py") for w in readers["tests/test_hub_and_search.py"])
    # not readers: reaching a call-time reader without calling it, importing a helper from a test file
    # whose own tests name a document, and a URL that merely contains /docs/
    for rel in ("tests/test_hub_only.py", "tests/test_imports_a_test.py", "tests/test_plain.py"):
        assert rel not in readers, (rel, readers.get(rel))


def test_without_the_call_time_exception_a_plain_import_of_the_reader_counts(tmp_path: Path):
    make_transitive_tree(tmp_path)
    readers = cc.document_readers(tmp_path)  # the repository's CALL_TIME_READERS do not name pkg/lazy.py
    assert "tests/test_hub_only.py" in readers


def test_the_list_check_fails_for_a_test_that_reads_docs_through_another_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    make_transitive_tree(tmp_path)
    monkeypatch.setattr(cc, "CALL_TIME_READERS", CALL_TIME)
    monkeypatch.setattr(cc, "DOC_TESTS", ("tests/test_names_doc.py",))
    readers = cc.document_readers(tmp_path)
    missing = [f for f in readers if f not in cc.DOC_TESTS and f not in cc.NOT_DOC_READERS]
    assert "tests/test_via_middle.py" in missing and "tests/test_runs_module.py" in missing
    assert "tests/test_names_doc.py" not in missing


def test_the_scanner_prints_no_warning_for_a_file_python_warns_about(tmp_path: Path):
    make_tree(tmp_path)
    write(tmp_path, "pkg/escapes.py", 'PATTERN = "\\d+ files"\n')  # an invalid escape: a SyntaxWarning
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        cc.document_readers(tmp_path)
        cc.document_modules(tmp_path)
    assert [str(w.message) for w in seen] == []


def test_every_call_time_reader_still_names_a_document_and_says_why(repo_readers: dict[str, list[str]]):
    modules = cc.document_modules(ROOT, exempt=False)
    for rel, (entries, why) in cc.CALL_TIME_READERS.items():
        assert (ROOT / rel).is_file() and rel in modules, f"{rel} no longer names a document: drop it"
        assert entries and len(why) > 20, rel
    # the dashboard reaches docs_index through orientation and the tool registry; only the tests that search
    # are readers (both on the list), the other dashboard tests are not
    assert "tests/test_dashboard_api.py" not in repo_readers
    for rel in ("tests/test_dashboard_assistant.py", "tests/test_assistant_eval.py"):
        assert any("ns_dashboard/assistant/docs_index.py" in w for w in repo_readers[rel]), rel


def test_the_list_check_fails_for_a_new_test_that_reads_docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The completeness test above, run against a tree with one unlisted reader: it must fail."""
    make_tree(tmp_path)
    monkeypatch.setattr(cc, "DOC_TESTS", ("tests/test_reads.py",))
    readers = cc.document_readers(tmp_path)
    missing = [f for f in readers if f not in cc.DOC_TESTS and f not in cc.NOT_DOC_READERS]
    assert "tests/test_names_a_root_doc.py" in missing and "tests/test_reads.py" not in missing


def test_doc_tests_prints_the_list_one_per_line(capsys: pytest.CaptureFixture[str]):
    assert cc.main(["doc-tests"]) == 0
    assert capsys.readouterr().out.splitlines() == list(cc.DOC_TESTS)


def test_run_doc_tests_hands_the_list_to_pytest_after_the_given_arguments(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    calls: list[list[str]] = []

    class Done:
        returncode = 3

    monkeypatch.setattr(cc.subprocess, "run", lambda argv, **kw: (calls.append(list(argv)), Done())[1])
    assert cc.main(["run-doc-tests", "--", "-q", "--junitxml=report-docs.xml"]) == 3
    (argv,) = calls
    assert argv[:3] == [sys.executable, "-m", "pytest"]
    assert argv[3:5] == ["-q", "--junitxml=report-docs.xml"]
    assert argv[5:] == list(cc.DOC_TESTS)
    assert f"{len(cc.DOC_TESTS)} test files that read documents" in capsys.readouterr().out


def test_run_doc_tests_refuses_when_a_listed_file_is_gone(monkeypatch: pytest.MonkeyPatch):
    calls: list[object] = []
    monkeypatch.setattr(cc.subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(cc, "DOC_TESTS", ("tests/test_finalization.py", "tests/test_gone_for_good.py"))
    assert cc.main(["run-doc-tests", "--", "-q"]) == 2
    assert calls == []


# ---- the gate: the final `test` job ------------------------------------------------------------------------
def junit(path: Path, passed: int, skipped: int = 0, failed: int = 0) -> Path:
    rows = [f'<testcase classname="tests.test_a" name="t{i}" time="0.1"/>' for i in range(passed)]
    rows += [
        f'<testcase classname="tests.test_a" name="s{i}" time="0"><skipped message="x"/></testcase>'
        for i in range(skipped)
    ]
    rows += [
        f'<testcase classname="tests.test_a" name="f{i}" time="0"><failure message="boom"/></testcase>'
        for i in range(failed)
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">{"".join(rows)}'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    return path


DOCS_PATH = {
    "CHANGES_RESULT": "success",
    "DOCS_ONLY": "true",
    "DOC_TESTS_RESULT": "success",
    "SHARD_RESULT": "skipped",
    "COLLECT_RESULT": "skipped",
}
CODE_PATH = {
    "CHANGES_RESULT": "success",
    "DOCS_ONLY": "false",
    "DOC_TESTS_RESULT": "skipped",
    "SHARD_RESULT": "success",
    "COLLECT_RESULT": "success",
}


def run_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str], report: Path | None
) -> tuple[int, str]:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    for key in ("CHANGES_RESULT", "DOCS_ONLY", "DOC_TESTS_RESULT", "SHARD_RESULT", "COLLECT_RESULT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    target = report if report is not None else tmp_path / "absent" / "report-docs.xml"
    rc = cc.main(["gate", "--doc-report", str(target), "--summary"])
    text = summary.read_text(encoding="utf-8") if summary.is_file() else ""
    return rc, text


def test_the_docs_path_passes_when_the_document_tests_ran_and_says_how_many(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    report = junit(tmp_path / "docs" / "report-docs.xml", passed=12, skipped=2)
    rc, summary = run_gate(tmp_path, monkeypatch, DOCS_PATH, report)
    out = capsys.readouterr().out
    assert rc == 0, out
    line = "docs-only change: 12 document tests ran, the code suite was not run"
    assert line in summary and line in out
    assert "2 skipped" in summary


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        ({"DOC_TESTS_RESULT": "failure"}, "test-docs"),
        ({"DOC_TESTS_RESULT": "cancelled"}, "test-docs"),
        ({"DOC_TESTS_RESULT": "skipped"}, "test-docs"),
        ({"CHANGES_RESULT": "failure"}, "changes"),
        ({"SHARD_RESULT": "failure"}, "test-shard"),
        ({"COLLECT_RESULT": "cancelled"}, "test-collect"),
    ],
)
def test_the_docs_path_fails_when_any_of_its_jobs_did_not_succeed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: dict[str, str],
    needle: str,
):
    report = junit(tmp_path / "docs" / "report-docs.xml", passed=12)
    rc, summary = run_gate(tmp_path, monkeypatch, {**DOCS_PATH, **change}, report)
    out = capsys.readouterr().out
    assert rc == 1
    assert "::error::" in out and needle in out
    assert "document tests ran, the code suite was not run" not in summary


def test_the_docs_path_fails_without_a_report_or_with_no_test_that_ran(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    rc, _ = run_gate(tmp_path, monkeypatch, DOCS_PATH, None)
    assert rc == 1 and "no report" in capsys.readouterr().out
    report = junit(tmp_path / "docs" / "report-docs.xml", passed=0, skipped=5)
    rc, _ = run_gate(tmp_path, monkeypatch, DOCS_PATH, report)
    assert rc == 1 and "no document test ran" in capsys.readouterr().out
    report = junit(tmp_path / "docs" / "report-docs.xml", passed=9, failed=1)
    rc, _ = run_gate(tmp_path, monkeypatch, DOCS_PATH, report)
    assert rc == 1 and "1 failed" in capsys.readouterr().out
    bad = tmp_path / "docs" / "report-docs.xml"
    bad.write_text("<testsuites><testsuite>", encoding="utf-8")
    rc, _ = run_gate(tmp_path, monkeypatch, DOCS_PATH, bad)
    assert rc == 1 and "not readable" in capsys.readouterr().out


def test_the_code_path_passes_when_every_shard_and_the_collection_succeeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    rc, summary = run_gate(tmp_path, monkeypatch, CODE_PATH, None)
    assert rc == 0, capsys.readouterr().out
    # nothing to say on a passing code path (the shard steps write their own summary): not even a heading
    assert summary == ""


@pytest.mark.parametrize("shard", ["skipped", "failure", "cancelled", ""])
def test_on_a_code_change_a_skipped_failed_or_cancelled_shard_still_fails_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], shard: str
):
    rc, _ = run_gate(tmp_path, monkeypatch, {**CODE_PATH, "SHARD_RESULT": shard}, None)
    out = capsys.readouterr().out
    assert rc == 1
    assert "a failed, skipped or cancelled shard fails the required check" in out


def test_on_a_code_change_a_failed_collection_fails_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    rc, _ = run_gate(tmp_path, monkeypatch, {**CODE_PATH, "COLLECT_RESULT": "skipped"}, None)
    assert rc == 1 and "test-collect" in capsys.readouterr().out


def test_a_failed_decision_takes_the_code_path_and_fails_the_gate_even_when_the_shards_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    # the changes job failed: its output is empty, so every job ran as for code; the gate still says it failed
    env = {**CODE_PATH, "CHANGES_RESULT": "failure", "DOCS_ONLY": ""}
    rc, _ = run_gate(tmp_path, monkeypatch, env, None)
    out = capsys.readouterr().out
    assert rc == 1 and "changes" in out


@pytest.mark.parametrize("value", ["", "false", "True", "yes", "1", " true"])
def test_anything_but_the_exact_word_true_is_the_code_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], value: str
):
    # the shards were skipped: on the code path that fails, whatever the document tests did
    env = {**DOCS_PATH, "DOCS_ONLY": value}
    report = junit(tmp_path / "docs" / "report-docs.xml", passed=12)
    rc, summary = run_gate(tmp_path, monkeypatch, env, report)
    assert rc == 1
    assert "test-shard" in capsys.readouterr().out
    assert "docs-only change" not in summary
