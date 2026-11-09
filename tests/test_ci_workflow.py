"""H-3, H-11: the shape of the sharded test job and of the docs-only path in ci.yml and release.yml.

Branch protection requires the check names lint, types, test, ui-build, powershell-parse and scope-check;
the shards, the collection job, the docs-only decision and the rules of the real-network step (H-1b) must not
drift apart from what dev/ci_shards.py, dev/ci_changes.py and the weights file say. GitHub Actions cannot run
here, so the `if:` wiring is evaluated below by a small evaluator of the expression syntax the workflow uses,
for each kind of run (a docs-only pull request, a code pull request, a push, the nightly run, a failed
decision).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from dev import ci_changes as cc
from dev import ci_shards as cs

ROOT = Path(__file__).resolve().parents[1]
SHARDS = cs.SHARDS
SETUP_ALLOWANCE_S = 180  # checkout, python, FFmpeg, pip: about 90 s measured, 180 s allowed
# How many test files without a measured weight any ONE shard must be able to take before the timeout guard
# fails. A pull request that adds tests does not edit the weights (they are refreshed from CI's own timings),
# and several such pull requests merge between two refreshes.
NEW_FILE_ROOM = 8

#: The checks branch protection requires (docs/REPO_SETTINGS.md); the names are job ids and stay as they are.
REQUIRED = ("lint", "types", "test", "ui-build", "powershell-parse", "scope-check")

DOCS_ONLY = "needs.changes.outputs.docs_only"


def load(rel: str) -> dict[str, Any]:
    return yaml.safe_load((ROOT / ".github" / "workflows" / rel).read_text(encoding="utf-8"))


def triggers(wf: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML reads the key `on` as the boolean True (YAML 1.1)
    return wf["on"] if "on" in wf else wf[True]


def steps_text(job: dict[str, Any]) -> str:
    return "\n".join(str(s.get("run", "")) for s in job["steps"])


def step(job: dict[str, Any], needle: str) -> dict[str, Any]:
    found = [s for s in job["steps"] if needle in s.get("name", "") or needle in str(s.get("run", ""))]
    assert found, f"no step with {needle!r}"
    return found[0]


def needs_of(job: dict[str, Any]) -> list[str]:
    raw = job.get("needs", [])
    return [raw] if isinstance(raw, str) else list(raw)


@pytest.fixture(scope="module")
def ci() -> dict[str, Any]:
    return load("ci.yml")


@pytest.fixture(scope="module")
def release() -> dict[str, Any]:
    return load("release.yml")


# ---- the required names -----------------------------------------------------------------------------
def test_every_required_check_is_still_a_job_with_that_name(ci: dict[str, Any]):
    for name in REQUIRED:
        assert name in ci["jobs"], name
        assert ci["jobs"][name].get("name", name) == name, f"{name} must keep its check name"
    assert "bundle" in ci["jobs"]


def test_the_shards_and_the_helper_jobs_do_not_take_a_required_name(
    ci: dict[str, Any], release: dict[str, Any]
):
    for wf in (ci, release):
        names = [wf["jobs"]["test-shard"]["name"], wf["jobs"]["test-collect"].get("name", "test-collect")]
        assert not set(names) & set(REQUIRED)
        assert wf["jobs"]["test-shard"]["name"] == f"test-shard (${{{{ matrix.shard }}}}/{SHARDS})"
    for helper in ("changes", "test-docs"):
        assert ci["jobs"][helper].get("name", helper) not in REQUIRED


def test_docs_name_the_same_required_checks():
    text = (ROOT / "docs" / "REPO_SETTINGS.md").read_text(encoding="utf-8")
    assert "`lint`, `types`, `test`, `ui-build`, `powershell-parse`, `scope-check`" in text
    assert f"test-shard (1/{SHARDS})" in text and f"({SHARDS}/{SHARDS})" in text
    assert "docs-only" in text and "dev/ci_changes.py" in text


# ---- the final job ----------------------------------------------------------------------------------
def test_the_final_test_job_in_ci_needs_every_path_always_runs_and_gates_with_the_script(ci: dict[str, Any]):
    job = ci["jobs"]["test"]
    assert set(needs_of(job)) == {"changes", "test-shard", "test-collect", "test-docs"}
    assert job["if"] == "always()", "a skipped required check counts as passed: the final job must always run"
    assert "strategy" not in job
    names = [s.get("name", s.get("uses", "")) for s in job["steps"]]
    gate = step(job, "dev/ci_changes.py gate")
    at = job["steps"].index(gate)
    # checkout and python come first, and like the gate they run whatever happened before, unless the run was
    # cancelled (then the last step fails the job: see the cancelled-run test below)
    assert [s.get("uses", "").split("@")[0] for s in job["steps"][:2]] == [
        "actions/checkout",
        "actions/setup-python",
    ], names
    for s in job["steps"][: at + 1]:
        assert str(s.get("if", "")).startswith("${{ !cancelled()"), s
    assert gate["if"] == "${{ !cancelled() }}" and "continue-on-error" not in gate
    assert gate["run"] == "python dev/ci_changes.py gate --doc-report docs-report/report-docs.xml --summary"
    env = gate["env"]
    assert env == {
        "CHANGES_RESULT": "${{ needs.changes.result }}",
        "DOCS_ONLY": "${{ needs.changes.outputs.docs_only }}",
        "DOC_TESTS_RESULT": "${{ needs.test-docs.result }}",
        "SHARD_RESULT": "${{ needs.test-shard.result }}",
        "COLLECT_RESULT": "${{ needs.test-collect.result }}",
    }
    # the document report is fetched before the gate on the docs path only; a missing one is the gate's call
    fetch = step(job, "Download the report of the document tests")
    assert job["steps"].index(fetch) < at
    assert fetch["with"]["name"] == "junit-report-docs" and fetch["with"]["path"] == "docs-report"
    assert (
        fetch["if"] == f"${{{{ !cancelled() && {DOCS_ONLY} == 'true' }}}}"
        and fetch["continue-on-error"] is True
    )
    # every later step but the last is the code path's report: it runs after a failed gate (so the report is
    # complete), but not when cancelled and not on the docs path, where there are no shard reports to read
    later, last = job["steps"][at + 1 : -1], job["steps"][-1]
    assert later and all(s["if"] == f"${{{{ !cancelled() && {DOCS_ONLY} != 'true' }}}}" for s in later)
    assert last["if"] == "cancelled()" and "exit 1" in last["run"]


def test_a_cancelled_run_costs_one_step_of_the_test_job_and_never_shows_it_green(ci: dict[str, Any]):
    """A newer push cancels the older run (concurrency). The job itself must still run (`always()`): a skipped
    required check counts as passed. Its steps must not (no checkout, no Python, no gate), except the last,
    which fails; and on a run that was not cancelled that last step must not run."""
    job = ci["jobs"]["test"]
    assert job["if"] == "always()"
    ctx = {"github.event_name": "pull_request", DOCS_ONLY: "true", "matrix.shard": "1"}
    for docs_only in ("true", "false", ""):
        ctx[DOCS_ONLY] = docs_only
        cancelled = {"always()": True, "cancelled()": True, "success()": False, "failure()": False}
        running = [s for s in job["steps"] if evaluate(str(s.get("if", "success()")), ctx, cancelled)]
        assert running == [job["steps"][-1]], [s.get("name", s.get("uses")) for s in running]
        (only,) = running
        assert only["run"].rstrip().endswith("exit 1") and "continue-on-error" not in only
        # a run that was not cancelled, whatever failed before: every step but the last may run
        for success in (True, False):
            normal = {"always()": True, "cancelled()": False, "success()": success, "failure()": not success}
            assert not evaluate(str(job["steps"][-1]["if"]), ctx, normal)
            assert evaluate(str(step(job, "dev/ci_changes.py gate")["if"]), ctx, normal)


def test_the_final_test_job_in_release_yml_still_gates_every_shard(release: dict[str, Any]):
    job = release["jobs"]["test"]
    assert set(needs_of(job)) == {"test-shard", "test-collect"}
    assert job["if"] == "always()"
    first = job["steps"][0]
    assert "needs.test-shard.result" in str(first["env"]) and "needs.test-collect.result" in str(first["env"])
    script = first["run"]
    assert '"$SHARD_RESULT" != "success"' in script and '"$COLLECT_RESULT" != "success"' in script
    assert 'exit "$status"' in script, "the first step must fail the job unless every shard succeeded"
    later = [s for s in job["steps"][1:] if "if" in s]
    assert later and all(s["if"] == "${{ !cancelled() }}" for s in later)


def test_the_old_single_job_is_gone_the_final_job_runs_no_pytest(ci: dict[str, Any], release: dict[str, Any]):
    for wf in (ci, release):
        assert "pytest" not in steps_text(wf["jobs"]["test"])


def test_the_final_job_proves_nothing_fell_between_the_shards(ci: dict[str, Any], release: dict[str, Any]):
    for wf in (ci, release):
        job = wf["jobs"]["test"]
        verify = step(job, "dev/ci_shards.py verify")["run"]
        assert f"--of {SHARDS}" in verify and "--collected collected/collected.txt" in verify
        assert "--reports reports" in verify
        assert "node ids" in step(job, "dev/ci_shards.py verify")["name"]
        names = [s.get("with", {}).get("name") or s.get("with", {}).get("pattern") for s in job["steps"]]
        assert "junit-report-shard-*" in names and "collected-tests" in names
        assert step(job, "ci_slowest.py")["run"].startswith("python dev/ci_slowest.py --top 20 --summary")


def test_the_plan_step_reports_the_files_with_no_weight_in_the_job_summary(ci: dict[str, Any]):
    run = step(ci["jobs"]["test"], "dev/ci_shards.py plan")["run"]
    assert run == f"python dev/ci_shards.py plan --of {SHARDS} --limit 720 --summary"


def test_the_outage_report_counts_across_all_shards_with_its_rules_intact(ci: dict[str, Any]):
    job = ci["jobs"]["test"]
    assert step(job, "ci_outage_report.py")["run"] == "python dev/ci_outage_report.py reports/*.xml"
    expr = job["env"]["NS_REQUIRE_ONLINE"]
    assert "github.event_name == 'schedule'" in expr and "refs/tags/" in expr
    assert expr == ci["jobs"]["test-shard"]["env"]["NS_REQUIRE_ONLINE"]


# ---- the shards -------------------------------------------------------------------------------------
def test_the_unit_tests_are_a_matrix_of_shards_that_do_not_stop_each_other(ci: dict[str, Any]):
    job = ci["jobs"]["test-shard"]
    assert job["strategy"]["fail-fast"] is False
    assert job["strategy"]["matrix"]["shard"] == list(range(1, SHARDS + 1))
    run = step(job, "dev/ci_shards.py run")["run"]
    assert f"--shard ${{{{ matrix.shard }}}} --of {SHARDS} --" in run
    assert '-m "not network"' in run
    assert "--junitxml=report-shard-${{ matrix.shard }}.xml" in run
    upload = step(job, "Upload JUnit report")
    assert upload["with"]["name"] == "junit-report-shard-${{ matrix.shard }}"
    assert upload["if"] == "always()"
    assert "report-shard-${{ matrix.shard }}.xml" in upload["with"]["path"]
    assert "report-network.xml" in upload["with"]["path"]


def test_the_shard_count_is_one_number_that_the_workflows_and_the_helper_agree_on():
    for rel in ("ci.yml", "release.yml"):
        text = (ROOT / ".github" / "workflows" / rel).read_text(encoding="utf-8")
        counts = {int(n) for n in re.findall(r"--of (\d+)", text)}
        counts |= {int(n) for n in re.findall(r"matrix\.shard \}\}/(\d+)\)", text)}
        # comments and step names that spell the shards out, e.g. "test-shard (1/N) ... (N/N)"
        counts |= {int(n) for n in re.findall(r"\((?:\d+|N)/(\d+)\)", text)}
        wf = yaml.safe_load(text)
        counts.add(len(wf["jobs"]["test-shard"]["strategy"]["matrix"]["shard"]))
        assert counts == {SHARDS}, (rel, counts)
        assert "all four" not in text and "four shards" not in text


def test_the_shard_timeout_is_twice_the_predicted_time_and_far_under_the_old_forty_minutes(
    ci: dict[str, Any], release: dict[str, Any]
):
    weights = cs.load_weights()
    groups = cs.split(cs.discover_test_files(), SHARDS, weights)
    loads = cs.predicted_seconds(groups, weights)
    for wf in (ci, release):
        minutes = wf["jobs"]["test-shard"]["timeout-minutes"]
        over = [
            cs.over_budget_message(
                s,
                SHARDS,
                load,
                SETUP_ALLOWANCE_S,
                f"the {minutes}-minute shard timeout needs twice the prediction",
                [f for f in groups[s - 1] if f not in weights.files],
                weights.default_seconds,
            )
            for s, load in enumerate(loads, start=1)
            if minutes * 60 < 2 * (load + SETUP_ALLOWANCE_S)
        ]
        assert not over, "\n".join(over)
        assert minutes < 40
        for other in ("test-collect", "test"):
            assert 10 <= wf["jobs"][other]["timeout-minutes"] <= 20


def test_every_shard_has_room_for_new_test_files_before_the_guard_fails(
    ci: dict[str, Any], release: dict[str, Any]
):
    """The guard above failed on `main` three times in a day with 21 s or less to spare: a merged pull
    request brought one unweighted test file and every open pull request went red. Room is by design."""
    weights = cs.load_weights()
    loads = cs.predicted_seconds(cs.split(cs.discover_test_files(), SHARDS, weights), weights)
    need = NEW_FILE_ROOM * weights.default_seconds
    for wf in (ci, release):
        budget = wf["jobs"]["test-shard"]["timeout-minutes"] * 60 / 2 - SETUP_ALLOWANCE_S
        tight = {s: round(budget - load) for s, load in enumerate(loads, start=1) if budget - load < need}
        assert not tight, (
            f"shards with less than {need:.0f} s of room ({NEW_FILE_ROOM} unweighted test files) before the"
            f" timeout guard fails, seconds left per shard: {tight}. Refresh the weights"
            f" ({cs.REFRESH_COMMAND}), make the slowest test files cheaper, or add a shard; do not wait"
            " for the guard itself to go red."
        )


def test_the_guard_message_names_the_shard_its_prediction_and_what_to_run():
    # every file of the shard is measured: the table may be stale, so a refresh from main comes first
    msg = cs.over_budget_message(3, SHARDS, 612.4, SETUP_ALLOWANCE_S, "the 25-minute shard timeout")
    assert f"shard 3/{SHARDS}" in msg and "612 s" in msg and "792 s" in msg
    assert cs.REFRESH_COMMAND in msg and cs.REFRESH_COMMAND == "python dev/ci_shards.py refresh --run latest"
    assert msg.index(cs.REFRESH_COMMAND) < msg.index("raise SHARDS")
    assert "SHARDS in dev/ci_shards.py" in msg and f"python dev/ci_shards.py plan --of {SHARDS + 1}" in msg


def test_the_guard_message_says_a_refresh_cannot_help_a_new_test_file_and_what_does():
    """The common case: the pull request's own new test file has no weight. A refresh re-measures main's files
    only, so the message must not send the builder there first; it names the files and the three remedies."""
    new = ["tests/test_brand_new_a.py", "tests/test_brand_new_b.py"]
    msg = cs.over_budget_message(4, SHARDS, 575.0, SETUP_ALLOWANCE_S, "the 25-minute shard timeout", new, 9.4)
    assert f"shard 4/{SHARDS}" in msg and "2 of its test files have no measured weight" in msg
    assert all(f in msg for f in new) and "9.4 s each" in msg
    assert f"`{cs.REFRESH_COMMAND}` does not help them" in msg
    assert "until the first refresh after it merges" in msg
    # (1) measure from this pull request's own run, (2) split or rename, (3) more shards: each with a command
    one, two, three = msg.index("(1)"), msg.index("(2)"), msg.index("(3)")
    assert one < two < three
    assert "gh run list --workflow ci --branch" in msg[one:two]
    assert "python dev/ci_shards.py refresh --run <run id>" in msg[one:two]
    assert (
        "split or rename" in msg[two:three]
        and f"python dev/ci_shards.py plan --of {SHARDS}`" in msg[two:three]
    )
    assert "raise SHARDS" in msg[three:] and f"python dev/ci_shards.py plan --of {SHARDS + 1}" in msg[three:]
    # the preview it prints runs (refresh --run <id> is run against a fake gh in tests/test_ci_shards.py)
    assert cs.main(["plan", "--of", str(SHARDS + 1)]) == 0
    many = [f"tests/test_n{i:02d}.py" for i in range(cs.LIST_LIMIT + 3)]
    assert "and 3 more" in cs.over_budget_message(1, SHARDS, 600.0, 0, "over", many, 9.4)


def test_the_real_network_step_runs_in_shard_one_only_and_keeps_its_rules(ci: dict[str, Any]):
    job = ci["jobs"]["test-shard"]
    net = step(job, "Real-network download tests")
    assert net["if"] == "matrix.shard == 1"
    assert net["timeout-minutes"] == 12 and net["shell"] == "bash"
    run = net["run"]
    # H-1b: an outage that outlasts the retries is a warning and a skip on push and PR runs, a failure on the
    # nightly run and tag runs (NS_REQUIRE_ONLINE); a skip for any other reason fails the step
    for needle in (
        "timeout 300 python -m pytest -o addopts= -v -s -m network tests/test_downloader_network.py",
        "--junitxml=report-network.xml",
        "python dev/ci_outage_report.py report-network.xml || true",
        'if [ "$rc" -ne 0 ]; then exit "$rc"; fi',
        'if node is not None and "upstream outage" in',
        'assert tests >= 4, "the real-network tests must run in CI"',
        'assert skipped == outage, "the real-network tests must run in CI, not be skipped',
    ):
        assert needle in run, needle
    assert job["env"]["NS_NETWORK_TESTS"] == "1"
    assert "-m network" not in steps_text({"steps": [s for s in job["steps"] if s is not net]})
    # the unit step still runs when the network step failed (both results are wanted), not when cancelled
    assert step(job, "Unit tests")["if"] == "${{ !cancelled() }}"


# ---- push versus pull_request -----------------------------------------------------------------------
def test_push_runs_for_main_and_tags_only_and_a_pull_request_is_tested_by_its_own_run(ci: dict[str, Any]):
    on = triggers(ci)
    assert on["push"]["branches"] == ["main"]
    assert on["push"]["tags"], "tag pushes must still run CI (NS_REQUIRE_ONLINE=1)"
    assert "pull_request" in on
    assert on["schedule"] == [{"cron": "23 4 * * *"}]
    # no merge_group, workflow_dispatch or pull_request_target: if one is added, dev/ci_changes.py decide
    # treats it as code (only `pull_request` can be docs-only), and this test asks to look again
    assert set(on) == {"push", "pull_request", "schedule"}


def test_a_newer_push_still_cancels_the_older_run_of_the_same_pull_request(ci: dict[str, Any]):
    # H-12: only pull request runs are cancelled (the rest is tested at the end of this file)
    group = ci["concurrency"]["group"]
    assert "github.event_name" in group and "github.head_ref" in group


def test_scope_check_lint_and_powershell_parse_run_as_before(ci: dict[str, Any]):
    assert ci["jobs"]["scope-check"]["if"] == "github.event_name == 'pull_request'"
    for name in ("lint", "powershell-parse"):
        assert ci["jobs"][name]["if"] == "github.event_name != 'schedule'", name
    for name in ("lint", "powershell-parse", "scope-check"):
        assert "needs" not in ci["jobs"][name], f"{name} must not wait for or depend on the decision"


def test_the_nightly_run_still_runs_the_whole_test_job(ci: dict[str, Any]):
    for name in ("test-shard", "test-collect", "test"):
        assert "schedule" not in str(ci["jobs"][name].get("if", "")), name


# ---- H-11: the docs-only path -------------------------------------------------------------------------
def test_the_changes_job_decides_first_on_every_event_with_the_script(ci: dict[str, Any]):
    job = ci["jobs"]["changes"]
    assert "if" not in job and "needs" not in job, "the decision runs on every event"
    assert job["outputs"] == {"docs_only": "${{ steps.decide.outputs.docs_only }}"}
    checkout = job["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@") and checkout["with"]["fetch-depth"] == 0
    decide = job["steps"][-1]  # last: nothing after it can fail the job once the output is written
    assert decide["id"] == "decide"
    assert decide["env"] == {
        "EVENT_NAME": "${{ github.event_name }}",
        "BASE_SHA": "${{ github.event.pull_request.base.sha }}",
        "HEAD_SHA": "${{ github.sha }}",
    }
    assert decide["run"] == (
        'python3 dev/ci_changes.py decide --event "$EVENT_NAME" --base "$BASE_SHA" '
        '--head "$HEAD_SHA" --summary'
    )
    assert job["timeout-minutes"] <= 5


@pytest.mark.parametrize("name", ["test-shard", "test-collect", "bundle"])
def test_the_jobs_that_cannot_see_a_document_skip_on_a_docs_only_change(ci: dict[str, Any], name: str):
    job = ci["jobs"][name]
    assert "changes" in needs_of(job)
    cond = job["if"]
    assert f"{DOCS_ONLY} != 'true'" in cond, cond
    assert "!cancelled()" in cond, "without a status function a failed decision would skip the job"


@pytest.mark.parametrize("name", ["types", "ui-build"])
def test_types_and_ui_build_say_not_applicable_on_a_docs_only_change_and_run_in_full_otherwise(
    ci: dict[str, Any], name: str
):
    job = ci["jobs"][name]
    assert needs_of(job) == ["changes"]
    assert job["if"] == "${{ !cancelled() && github.event_name != 'schedule' }}"
    assert job["runs-on"] == f"${{{{ {DOCS_ONLY} == 'true' && 'ubuntu-latest' || 'windows-latest' }}}}"
    first, rest = job["steps"][0], job["steps"][1:]
    assert first["if"] == f"{DOCS_ONLY} == 'true'"
    assert "docs-only change: not applicable" in first["run"] and "GITHUB_STEP_SUMMARY" in first["run"]
    # every other step is the real check; an upload-on-failure step can only follow a failed real step
    assert rest and all(s.get("if") in (f"{DOCS_ONLY} != 'true'", "failure()") for s in rest), name
    assert any(s.get("if") == f"{DOCS_ONLY} != 'true'" and "run" in s for s in rest), name


def test_the_light_job_runs_the_document_tests_on_a_docs_only_change_only(ci: dict[str, Any]):
    job = ci["jobs"]["test-docs"]
    assert needs_of(job) == ["changes"]
    assert job["if"] == f"${{{{ !cancelled() && {DOCS_ONLY} == 'true' }}}}"
    run = step(job, "dev/ci_changes.py run-doc-tests")["run"]
    assert (
        run == 'python dev/ci_changes.py run-doc-tests -- -q -rs -m "not network" --junitxml=report-docs.xml'
    )
    upload = step(job, "Upload the report of the document tests")
    assert upload["if"] == "always()" and upload["with"]["name"] == "junit-report-docs"
    assert upload["with"]["path"] == "report-docs.xml"
    assert 'pip install -e ".[dev]"' in steps_text(job)
    assert job["timeout-minutes"] <= 15


# A small evaluator of the expression syntax ci.yml uses in `if:` and `runs-on:` (GitHub's rules: `&&` and
# `||` return one of their operands, strings compare without case, a job-level `if:` with no status function
# is `success() && (...)`). An expression it does not know fails the test: extend it, do not guess.
TOKEN = re.compile(r"\s*(&&|\|\||==|!=|!|\(|\)|'(?:[^']|'')*'|\d+|[A-Za-z_][A-Za-z0-9_.\-]*)")
STATUS = ("always()", "cancelled()", "success()", "failure()")


def tokens(expr: str) -> list[str]:
    text = expr.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2]
    out, pos = [], 0
    while pos < len(text.rstrip()):
        m = TOKEN.match(text, pos)
        assert m, f"cannot read {text[pos:]!r} in {expr!r}"
        out.append(m.group(1))
        pos = m.end()
    return out


def evaluate(expr: str, ctx: dict[str, Any], status: dict[str, bool]) -> Any:
    toks = tokens(expr)
    pos = 0

    def peek() -> str | None:
        return toks[pos] if pos < len(toks) else None

    def take() -> str:
        nonlocal pos
        pos += 1
        return toks[pos - 1]

    def primary() -> Any:
        t = take()
        if t == "(":
            v = alternative()
            assert take() == ")"
            return v
        if t.startswith("'"):
            return t[1:-1].replace("''", "'")
        if t.isdigit():
            return t  # compared as text, like the context values here
        if peek() == "(":
            take()
            assert take() == ")", f"only functions without arguments are known: {t}"
            assert f"{t}()" in STATUS, f"unknown function {t}"
            return status[f"{t}()"]
        assert t in ctx, f"unknown context {t} in {expr!r}"
        return ctx[t]

    def negation() -> Any:
        if peek() == "!":
            take()
            return not negation()
        left = primary()
        if peek() in ("==", "!="):
            op, right = take(), primary()
            same = str(left).lower() == str(right).lower()
            return same if op == "==" else not same
        return left

    def conjunction() -> Any:
        v = negation()
        while peek() == "&&":
            take()
            right = negation()
            v = right if v else v
        return v

    def alternative() -> Any:
        v = conjunction()
        while peek() == "||":
            take()
            right = conjunction()
            v = v if v else right
        return v

    result = alternative()
    assert pos == len(toks), f"trailing tokens in {expr!r}"
    return result


def simulate(wf: dict[str, Any], event: str, decision: str | None) -> dict[str, dict[str, Any]]:
    """Which jobs and steps run for one kind of run. ``decision``: what `changes` says, None if it failed."""
    jobs = wf["jobs"]
    results: dict[str, str] = {}
    outputs = {"docs_only": decision or ""}
    seen: dict[str, dict[str, Any]] = {}
    pending = list(jobs)
    while pending:
        name = next(n for n in pending if all(d in results for d in needs_of(jobs[n])))
        pending.remove(name)
        job, deps = jobs[name], needs_of(jobs[name])
        ctx = {
            "github.event_name": event,
            "needs.changes.outputs.docs_only": outputs["docs_only"] if "changes" in deps else "",
            "matrix.shard": "1",
        }
        dep_results = [results[d] for d in deps]
        status = {
            "always()": True,
            "cancelled()": False,
            "success()": all(r == "success" for r in dep_results),
            "failure()": any(r == "failure" for r in dep_results),
        }
        # inside a job every step succeeds in this simulation: success() is about the earlier steps there
        step_status = {"always()": True, "cancelled()": False, "success()": True, "failure()": False}
        cond = str(job.get("if", ""))
        if not any(s in cond for s in STATUS):
            cond = f"success() && ({cond.removeprefix('${{').removesuffix('}}')})" if cond else "success()"
        runs = bool(evaluate(cond, ctx, status))
        if not runs:
            results[name] = "skipped"
            seen[name] = {"runs": False}
            continue
        results[name] = "failure" if (name == "changes" and decision is None) else "success"
        steps = [s for s in job.get("steps", []) if evaluate(str(s.get("if", "success()")), ctx, step_status)]
        runs_on = job["runs-on"]
        seen[name] = {
            "runs": True,
            "runs-on": evaluate(runs_on, ctx, status) if "${{" in runs_on else runs_on,
            "steps": steps,
        }
    seen["_results"] = {"results": results, "docs_only": outputs["docs_only"]}
    return seen


def ran(seen: dict[str, dict[str, Any]]) -> set[str]:
    return {n for n, v in seen.items() if not n.startswith("_") and v["runs"]}


def gate_exit(seen: dict[str, dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    results = seen["_results"]["results"]
    env = {
        "CHANGES_RESULT": results["changes"],
        "DOCS_ONLY": seen["_results"]["docs_only"],
        "DOC_TESTS_RESULT": results["test-docs"],
        "SHARD_RESULT": results["test-shard"],
        "COLLECT_RESULT": results["test-collect"],
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    report = tmp_path / "report-docs.xml"
    report.write_text(
        '<testsuites><testsuite><testcase classname="tests.test_a" name="t" time="0.1"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    return cc.main(["gate", "--doc-report", str(report)])


ALL_JOBS = {
    "changes",
    "lint",
    "types",
    "test-shard",
    "test-collect",
    "test-docs",
    "test",
    "ui-build",
    "powershell-parse",
    "bundle",
    "scope-check",
}


def test_a_docs_only_pull_request_runs_the_light_path_and_every_required_check_reports(
    ci: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    seen = simulate(ci, "pull_request", "true")
    assert ran(seen) == {
        "changes",
        "lint",
        "types",
        "ui-build",
        "powershell-parse",
        "scope-check",
        "test-docs",
        "test",
    }
    assert set(REQUIRED) <= ran(seen), "a required check that is skipped would count as passed"
    for name in ("types", "ui-build"):
        assert seen[name]["runs-on"] == "ubuntu-latest"
        (only,) = seen[name]["steps"]
        assert "docs-only change: not applicable" in only["run"]
    assert gate_exit(seen, tmp_path, monkeypatch) == 0


def test_a_code_pull_request_runs_everything_as_before(
    ci: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    seen = simulate(ci, "pull_request", "false")
    assert ran(seen) == ALL_JOBS - {"test-docs"}
    for name in ("types", "ui-build"):
        assert seen[name]["runs-on"] == "windows-latest"
        assert not any("not applicable" in str(s.get("run", "")) for s in seen[name]["steps"])
        real = [s for s in ci["jobs"][name]["steps"][1:] if s.get("if") != "failure()"]
        assert seen[name]["steps"] == real
    gate_steps = [s.get("name", s.get("uses")) for s in seen["test"]["steps"]]
    assert "Download the report of the document tests" not in gate_steps
    assert "The node ids the shards ran are exactly the node ids collected unsharded" in gate_steps
    assert gate_exit(seen, tmp_path, monkeypatch) == 0


def test_a_push_to_main_or_a_tag_runs_everything(
    ci: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # decide says false for every push (tests/test_ci_changes.py); scope-check is for pull requests only
    seen = simulate(ci, "push", "false")
    assert ran(seen) == ALL_JOBS - {"test-docs", "scope-check"}
    assert gate_exit(seen, tmp_path, monkeypatch) == 0


def test_the_nightly_run_runs_the_whole_test_job_and_nothing_else(ci: dict[str, Any]):
    seen = simulate(ci, "schedule", "false")
    assert ran(seen) == {"changes", "test-shard", "test-collect", "test"}


def test_a_failed_decision_runs_everything_and_fails_the_gate(
    ci: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    seen = simulate(ci, "pull_request", None)
    assert ran(seen) == ALL_JOBS - {"test-docs"}, (
        "a failed decision must not skip a required check or a shard"
    )
    for name in ("types", "ui-build"):
        assert seen[name]["runs-on"] == "windows-latest"
    assert gate_exit(seen, tmp_path, monkeypatch) == 1


def test_the_evaluator_reads_github_rules():
    ctx = {"github.event_name": "push", "needs.changes.outputs.docs_only": ""}
    status = {"always()": True, "cancelled()": False, "success()": False, "failure()": True}
    assert evaluate("${{ !cancelled() && needs.changes.outputs.docs_only != 'true' }}", ctx, status) is True
    assert evaluate("needs.changes.outputs.docs_only == 'true' && 'a' || 'b'", ctx, status) == "b"
    assert evaluate("github.event_name == 'PUSH'", ctx, status) is True  # strings compare without case
    with pytest.raises(AssertionError):
        evaluate("startsWith(github.ref, 'refs/tags/')", ctx, status)


# ---- release.yml ------------------------------------------------------------------------------------
def test_the_tag_run_in_release_yml_is_a_full_run_of_everything_with_the_same_proof(release: dict[str, Any]):
    run = step(release["jobs"]["test-shard"], "dev/ci_shards.py run")["run"]
    assert "-m" not in run.split(" -- ", 1)[1].split(), "the tag run has no marker filter"
    collect = step(release["jobs"]["test-collect"], "--collect-only")["run"]
    assert "not network" not in collect
    assert release["jobs"]["release"]["needs"] == "test"
    assert release["jobs"]["test"]["if"] == "always()"
    assert release["jobs"]["test-shard"]["strategy"]["matrix"]["shard"] == list(range(1, SHARDS + 1))
    assert "changes" not in release["jobs"] and "docs_only" not in str(release), "a tag run has no docs path"


def shard_disagreements(ci: dict[str, Any], release: dict[str, Any]) -> list[str]:
    """Where release.yml and ci.yml (and SHARDS) disagree on the shards or the completeness proof."""
    a, b = ci["jobs"], release["jobs"]
    out: list[str] = []
    for key in ("name", "timeout-minutes", "strategy"):
        if a["test-shard"].get(key) != b["test-shard"].get(key):
            out.append(f"test-shard {key}: {a['test-shard'].get(key)!r} vs {b['test-shard'].get(key)!r}")
    for name, wf in (("ci.yml", a), ("release.yml", b)):
        if wf["test-shard"]["strategy"]["matrix"]["shard"] != list(range(1, SHARDS + 1)):
            out.append(f"{name}: the matrix is not 1..{SHARDS}")
        run = step(wf["test-shard"], "dev/ci_shards.py run")["run"].split(" -- ", 1)[0]
        if run != f"python dev/ci_shards.py run --shard ${{{{ matrix.shard }}}} --of {SHARDS}":
            out.append(f"{name}: the shard command is {run!r}")
        verify = step(wf["test"], "dev/ci_shards.py verify")
        want = (
            f"python dev/ci_shards.py verify --of {SHARDS} --collected collected/collected.txt "
            "--reports reports --summary"
        )
        if verify["run"] != want or "node ids" not in verify.get("name", ""):
            out.append(f"{name}: the completeness proof is {verify['run']!r}")
        fetched = {
            s.get("with", {}).get("name") or s.get("with", {}).get("pattern") for s in wf["test"]["steps"]
        }
        if not {"junit-report-shard-*", "collected-tests"} <= fetched:
            out.append(f"{name}: the final job does not fetch every shard report and the collection")
        upload = step(wf["test-shard"], "Upload JUnit report")
        if upload["with"]["name"] != "junit-report-shard-${{ matrix.shard }}" or upload["if"] != "always()":
            out.append(f"{name}: the shard report upload changed")
        if "--collect-only" not in steps_text(wf["test-collect"]):
            out.append(f"{name}: test-collect collects nothing")
    return out


def test_release_yml_and_ci_yml_run_the_same_shards_and_the_same_completeness_proof(
    ci: dict[str, Any], release: dict[str, Any]
):
    """A tag run must not disagree with ci.yml: the same SHARDS, matrix, check names, timeout, per-shard
    command (but for ci's marker filter) and the same node-id proof in the final job."""
    assert shard_disagreements(ci, release) == []


@pytest.mark.parametrize("other", [SHARDS - 1, SHARDS + 1])
def test_the_agreement_check_fails_when_release_yml_says_another_shard_count(ci: dict[str, Any], other: int):
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    broken = text.replace(f"/{SHARDS})", f"/{other})").replace(f"--of {SHARDS}", f"--of {other}")
    broken = broken.replace(
        f"shard: [{', '.join(map(str, range(1, SHARDS + 1)))}]",
        f"shard: [{', '.join(map(str, range(1, other + 1)))}]",
    )
    assert broken != text
    problems = shard_disagreements(ci, yaml.safe_load(broken))
    assert any("test-shard name" in p for p in problems), problems
    assert any("the matrix is not" in p for p in problems) and any(
        "completeness proof" in p for p in problems
    )


# ---- the helper files the workflows call exist ------------------------------------------------------
def test_every_script_the_workflows_call_exists():
    for rel in (
        "dev/ci_changes.py",
        "dev/ci_shards.py",
        "dev/ci_slowest.py",
        "dev/ci_outage_report.py",
        "dev/ci_shard_weights.json",
    ):
        assert (ROOT / rel).is_file(), rel


# ---- H-12: a push to main is never cancelled, a pull request run is --------------------------------------
def _concurrency_for(
    ci: dict[str, Any], event: str, ref: str, head_ref: str = "", sha: str = ""
) -> tuple[str, bool]:
    """Evaluate ci.yml's concurrency group and cancel-in-progress for one event.

    The two expressions use only `github.<field>`, `format()`, `==`, `&&`, `||` and quoted strings, which
    Python reads the same way (GitHub's `&&`/`||` also return an operand), so they are evaluated here and
    not pattern-matched."""
    from types import SimpleNamespace

    # `ref` is the short name; GitHub's github.ref is refs/tags/<name> for a v* tag and refs/heads/<name> else
    full = f"refs/tags/{ref}" if re.match(r"v\d", ref) else f"refs/heads/{ref}"
    github = SimpleNamespace(event_name=event, ref_name=ref, ref=full, head_ref=head_ref, sha=sha)

    def value(expr: str) -> Any:
        py = expr.replace("&&", " and ").replace("||", " or ")
        # format('{0}-{1}', a, b) is GitHub's format(); Python's str.format reads the same template
        return eval(py, {"__builtins__": {}}, {"github": github, "format": lambda t, *a: t.format(*a)})  # noqa: S307

    conc = ci["concurrency"]
    group = re.sub(r"\$\{\{(.*?)\}\}", lambda m: str(value(m.group(1).strip())), conc["group"])
    cancel = conc["cancel-in-progress"]
    if isinstance(cancel, str):
        cancel = value(re.fullmatch(r"\$\{\{(.*)\}\}", cancel.strip()).group(1).strip())  # type: ignore[union-attr]
    return group, cancel


def test_a_push_to_main_is_never_cancelled_but_a_superseded_pull_request_run_is(ci: dict[str, Any]):
    assert ci["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    for event, ref in (("push", "main"), ("push", "v0.1.15-rc1"), ("schedule", "main")):
        assert _concurrency_for(ci, event, ref, sha="a" * 40)[1] is False, (event, ref)
    pr = _concurrency_for(ci, "pull_request", "190/merge", head_ref="orchestrator/h11-docs-fast-path")
    assert pr[1] is True


def test_every_push_to_main_has_its_own_concurrency_group(ci: dict[str, Any]):
    # cancel-in-progress: false alone is not enough: GitHub keeps one running and ONE pending run per group
    # and cancels an older pending run when a newer one arrives. Six pushes in a row would still lose the
    # middle ones. So a push has the commit in its group name, and no two pushes share a group.
    one = _concurrency_for(ci, "push", "main", sha="1" * 40)[0]
    two = _concurrency_for(ci, "push", "main", sha="2" * 40)[0]
    assert one != two
    # a pull request keeps one group per branch, so a newer push to it replaces the older run
    pr1 = _concurrency_for(ci, "pull_request", "190/merge", head_ref="feat/x", sha="1" * 40)[0]
    pr2 = _concurrency_for(ci, "pull_request", "190/merge", head_ref="feat/x", sha="2" * 40)[0]
    other = _concurrency_for(ci, "pull_request", "191/merge", head_ref="feat/y", sha="3" * 40)[0]
    assert pr1 == pr2 and pr1 != other


def test_a_tag_run_never_shares_a_group_with_the_main_push_or_another_tag_on_the_same_commit(
    ci: dict[str, Any],
):
    # a release tag points at the commit the main push already ran on, and an rc and the final can be tagged
    # on one commit. With only the sha in the group they would share a group, and the second would queue
    # behind the first, and a third would replace the pending one. Each ref gets its own group.
    sha = "c" * 40
    refs = ("main", "v0.1.15-rc1", "v0.1.15-rc2", "v0.1.15")
    groups = {_concurrency_for(ci, "push", ref, sha=sha)[0] for ref in refs}
    assert len(groups) == 4, groups


def test_the_ci_concurrency_group_cannot_cancel_another_workflow(ci: dict[str, Any], release: dict[str, Any]):
    # group names are shared by every workflow of the repository: ci.yml's carry the `ci-` prefix and
    # release.yml's and build-qwentts.yml's start with their own names, so a run of one never touches another
    ci_groups = {
        _concurrency_for(ci, event, "main", head_ref="main", sha="f" * 40)[0]
        for event in ("push", "pull_request", "schedule")
    }
    assert all(g.startswith("ci-") for g in ci_groups), ci_groups
    for other in ("release.yml", "build-qwentts.yml"):
        group = str(load(other)["concurrency"]["group"])
        assert not group.startswith("ci-"), (other, group)
