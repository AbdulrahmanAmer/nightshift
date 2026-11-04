# ruff: noqa: E501
"""The offline Assistant evaluation: the partner's 20 questions on the scripted fake model, in ordinary pytest.

For every case: topics and tools offered (router), what `search_docs` may serve (retrieval scope), the token
budget of every request, and the confirm gate. See `ns_dashboard/assistant/eval.py` and
`tests/assistant_eval/cases.yaml`. The real model's choices are checked on the partner PC, not here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ns_dashboard.assistant import docs_index, prompt
from ns_dashboard.assistant.eval import load_cases, render_markdown, run_case
from tests.assistant_eval import fixture_home

CASES = Path(__file__).parent / "assistant_eval" / "cases.yaml"


@pytest.fixture
def home(tmp_home: Path) -> fixture_home.FixtureHome:
    return fixture_home.build(tmp_home)


def test_the_cases_file_holds_the_twenty_questions(home: fixture_home.FixtureHome):
    cases = load_cases(CASES, home.ids, fixture_home.SECRET)
    assert [c.id for c in cases] == list(range(1, 21))
    assert all(c.gold and c.topics for c in cases)
    assert fixture_home.SECRET in next(c for c in cases if c.id == 3).question
    assert next(c for c in cases if c.id == 5).context["output_id"] == home.ids["review_id"]


@pytest.mark.parametrize("ctx", [16384, 32768])
def test_all_twenty_cases_pass_offline(home: fixture_home.FixtureHome, ctx: int):
    results = [run_case(home.dash, c, ctx_tokens=ctx) for c in load_cases(CASES, home.ids, fixture_home.SECRET)]
    failed = [r for r in results if not r.ok]
    assert not failed, "\n" + render_markdown(results)
    biggest = max(r.stats["total"] for r in results)
    assert biggest <= ctx
    assert max(len(r.offered) for r in results) <= 18


def test_the_report_names_every_section(home: fixture_home.FixtureHome):
    cases = load_cases(CASES, home.ids, fixture_home.SECRET)
    result = run_case(home.dash, cases[0])
    assert set(result.stats) == {"system", "tools", "context_block", "history", "total"}
    table = render_markdown([result])
    assert "| 1 |" in table and "diagnose" in table


def test_retrieval_scope_excludes_the_build_documents(home: fixture_home.FixtureHome):
    from ns_dashboard.assistant import orientation
    from ns_dashboard.assistant.registry import ToolContext

    index = orientation.docs(ToolContext(home.dash))
    files = {c.file for c in index.chunks}
    assert files <= docs_index.ALLOWED_SOURCES, files
    assert docs_index.CAPABILITIES_FILE in files and "contracts/errors.json" in files
    for hit in index.search("builders may not edit contracts BUILD_SPEC search_library", 10):
        assert hit["file"] in docs_index.ALLOWED_SOURCES
    # the generated list answers "can you...?" from the registry and the never-list
    names = {c.heading for c in index.chunks if c.file == docs_index.CAPABILITIES_FILE}
    assert {"diagnose", "fetch_footage", "cannot: approve", "cannot: reject", "upload to YouTube"} <= names


def test_budget_numbers_are_printed_for_the_record(home: fixture_home.FixtureHome):
    """Not an assertion about the model: it keeps the measured sections of the 16k plan in the test log."""
    results = [run_case(home.dash, c) for c in load_cases(CASES, home.ids, fixture_home.SECRET)]
    worst = {k: max(r.stats[k] for r in results) for k in ("system", "tools", "context_block", "history", "total")}
    plan = prompt.BUDGET_16K
    assert worst["system"] <= plan.system, worst
    assert worst["tools"] <= plan.tools, worst
    assert worst["context_block"] <= plan.context_block, worst
