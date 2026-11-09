"""H-3: the shape of the sharded test job in ci.yml and release.yml.

Branch protection requires the check name `test`; the shards, the collection job and the rules of the
real-network step (H-1b) must not drift apart from what dev/ci_shards.py and the weights file say.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from dev import ci_shards as cs

ROOT = Path(__file__).resolve().parents[1]
SHARDS = 4
SETUP_ALLOWANCE_S = 180  # checkout, python, FFmpeg, pip: about 90 s measured, 180 s allowed

#: The checks branch protection requires (docs/REPO_SETTINGS.md); the names are job ids and stay as they are.
REQUIRED = ("lint", "types", "test", "ui-build", "powershell-parse", "scope-check")


def load(rel: str) -> dict[str, Any]:
    return yaml.safe_load((ROOT / ".github" / "workflows" / rel).read_text(encoding="utf-8"))


def triggers(wf: dict[str, Any]) -> dict[str, Any]:
    # PyYAML reads the key `on` as the boolean True (YAML 1.1)
    return wf["on"] if "on" in wf else wf[True]


def steps_text(job: dict[str, Any]) -> str:
    return "\n".join(str(s.get("run", "")) for s in job["steps"])


def step(job: dict[str, Any], needle: str) -> dict[str, Any]:
    found = [s for s in job["steps"] if needle in s.get("name", "") or needle in str(s.get("run", ""))]
    assert found, f"no step with {needle!r}"
    return found[0]


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


def test_the_shards_do_not_take_a_required_name(ci: dict[str, Any], release: dict[str, Any]):
    for wf in (ci, release):
        names = [wf["jobs"]["test-shard"]["name"], wf["jobs"]["test-collect"].get("name", "test-collect")]
        assert not set(names) & set(REQUIRED)
        assert wf["jobs"]["test-shard"]["name"] == f"test-shard (${{{{ matrix.shard }}}}/{SHARDS})"


def test_docs_name_the_same_required_checks():
    text = (ROOT / "docs" / "REPO_SETTINGS.md").read_text(encoding="utf-8")
    assert "`lint`, `types`, `test`, `ui-build`, `powershell-parse`, `scope-check`" in text
    assert "test-shard" in text


# ---- the final job ----------------------------------------------------------------------------------
def test_the_final_test_job_needs_every_shard_and_always_runs(ci: dict[str, Any], release: dict[str, Any]):
    for wf in (ci, release):
        job = wf["jobs"]["test"]
        assert set(job["needs"]) == {"test-shard", "test-collect"}
        assert job["if"] == "always()", (
            "a skipped required check counts as passed: the final job must always run"
        )
        assert "strategy" not in job
        first = job["steps"][0]
        assert "needs.test-shard.result" in str(first["env"]) and "needs.test-collect.result" in str(
            first["env"]
        )
        script = first["run"]
        assert '"$SHARD_RESULT" != "success"' in script and '"$COLLECT_RESULT" != "success"' in script
        assert 'exit "$status"' in script, "the first step must fail the job unless every shard succeeded"
        # the later steps still run after it failed (so the report is complete), but not when cancelled
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


def test_the_shard_count_is_the_same_everywhere():
    counts = set()
    for rel in ("ci.yml", "release.yml"):
        text = (ROOT / ".github" / "workflows" / rel).read_text(encoding="utf-8")
        counts |= {int(n) for n in re.findall(r"--of (\d+)", text)}
        counts |= {int(n) for n in re.findall(r"matrix\.shard \}\}/(\d+)\)", text)}
        wf = yaml.safe_load(text)
        counts.add(len(wf["jobs"]["test-shard"]["strategy"]["matrix"]["shard"]))
    assert counts == {SHARDS}


def test_the_shard_timeout_is_twice_the_predicted_time_and_far_under_the_old_forty_minutes(
    ci: dict[str, Any], release: dict[str, Any]
):
    weights = cs.load_weights()
    groups = cs.split(cs.discover_test_files(), SHARDS, weights)
    predicted = max(cs.predicted_seconds(groups, weights)) + SETUP_ALLOWANCE_S
    for wf in (ci, release):
        minutes = wf["jobs"]["test-shard"]["timeout-minutes"]
        assert minutes * 60 >= 2 * predicted, f"{minutes} min is under twice the predicted {predicted:.0f} s"
        assert minutes < 40
        for other in ("test-collect", "test"):
            assert 10 <= wf["jobs"][other]["timeout-minutes"] <= 20


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
    assert set(on) == {"push", "pull_request", "schedule"}


def test_a_newer_push_still_cancels_the_older_run_of_the_same_pull_request(ci: dict[str, Any]):
    # H-12: only pull request runs are cancelled (the rest is tested at the end of this file)
    group = ci["concurrency"]["group"]
    assert "github.event_name" in group and "github.head_ref" in group


def test_scope_check_still_runs_on_pull_requests_and_only_there(ci: dict[str, Any]):
    assert ci["jobs"]["scope-check"]["if"] == "github.event_name == 'pull_request'"
    for name in ("lint", "types", "ui-build", "powershell-parse", "bundle"):
        assert ci["jobs"][name]["if"] == "github.event_name != 'schedule'", name


def test_the_nightly_run_still_runs_the_whole_test_job(ci: dict[str, Any]):
    for name in ("test-shard", "test-collect", "test"):
        assert "schedule" not in str(ci["jobs"][name].get("if", "")), name


# ---- release.yml ------------------------------------------------------------------------------------
def test_the_tag_run_in_release_yml_is_a_full_run_of_everything_with_the_same_proof(release: dict[str, Any]):
    run = step(release["jobs"]["test-shard"], "dev/ci_shards.py run")["run"]
    assert "-m" not in run.split(" -- ", 1)[1].split(), "the tag run has no marker filter"
    collect = step(release["jobs"]["test-collect"], "--collect-only")["run"]
    assert "not network" not in collect
    assert release["jobs"]["release"]["needs"] == "test"
    assert release["jobs"]["test"]["if"] == "always()"
    assert release["jobs"]["test-shard"]["strategy"]["matrix"]["shard"] == list(range(1, SHARDS + 1))


# ---- the helper files the workflows call exist ------------------------------------------------------
def test_every_script_the_workflows_call_exists():
    for rel in (
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
