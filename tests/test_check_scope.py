"""C10: unit tests for dev/check_scope.py (the PR scope guard)."""

import pytest

from dev import check_scope as cs


def paths(branch: str, files: list[str]) -> list[str]:
    return [v.path for v in cs.check(branch, files)]


def test_component_of_parses_branch_names():
    assert cs.component_of("feat/c4-tts") == 4
    assert cs.component_of("feat/c10-delivery") == 10
    assert cs.component_of("feat/dashboard") is None
    assert cs.component_of("fix/c4-x") is None


@pytest.mark.parametrize(
    ("branch", "file"),
    [
        ("feat/c1-installer", "installer/stage1.ps1"),
        ("feat/c1-installer", "INSTALL.cmd"),
        ("feat/c1-installer", "OPERATOR.md"),
        ("feat/c2-runtime", "ns_supervisor/supervisor.py"),
        ("feat/c4-tts", "ns_tts/server.py"),
        ("feat/c5-text", "ns_studio/text/prompts.py"),
        ("feat/c5-text", "ns_studio/prompts/ideas.md"),
        ("feat/c6-media", "ns_studio/media/render.py"),
        ("feat/c6-media", "ns_studio/presets/caption_styles.yaml"),
        ("feat/c7-core", "ns_studio/orchestrator.py"),
        ("feat/c7-core", "ns_studio/channels/crud.py"),
        ("feat/c8-clipper", "ns_studio/clipper/select.py"),
        ("feat/c8-clipper", "ns_studio/presets/clip_templates.yaml"),
        ("feat/c9-dashboard", "ns_dashboard/api/jobs.py"),
        ("feat/c10-delivery", ".github/workflows/ci.yml"),
        ("feat/c10-delivery", "pyproject.toml"),
        ("feat/c10-delivery", "dev/make_bundle.ps1"),
    ],
)
def test_owned_paths_are_accepted(branch: str, file: str):
    assert paths(branch, [file]) == []


@pytest.mark.parametrize(
    ("branch", "file"),
    [
        ("feat/c4-tts", "ns_studio/stages.py"),
        ("feat/c5-text", "ns_studio/media/render.py"),
        ("feat/c6-media", "ns_studio/presets/clip_templates.yaml"),
        ("feat/c7-core", "ns_studio/clipper/select.py"),
        ("feat/c1-installer", ".github/workflows/ci.yml"),
        ("feat/c9-dashboard", "ns_supervisor/cli.py"),
        ("feat/c2-runtime", "README.md"),
    ],
)
def test_foreign_paths_are_rejected(branch: str, file: str):
    assert paths(branch, [file]) == [file]


@pytest.mark.parametrize(
    "file", ["ns_common/paths.py", "contracts/errors.json", "manifest.json", "tests/conftest.py"]
)
def test_protected_paths_rejected_for_builders(file: str):
    for branch in ("feat/c4-tts", "feat/c10-delivery", "fix/something"):
        assert paths(branch, [file]) == [file]


def test_pyproject_is_open_to_c10_only():
    assert paths("feat/c10-delivery", ["pyproject.toml"]) == []
    assert paths("feat/c4-tts", ["pyproject.toml"]) == ["pyproject.toml"]


def test_orchestrator_branches_may_edit_everything():
    files = ["ns_common/paths.py", "contracts/errors.json", "ns_tts/server.py", "manifest.json"]
    assert paths("feat/c3-contracts", files) == []
    assert paths("orchestrator/amend", files) == []
    assert paths("feat/release-readiness", files) == []


def test_main_is_unchecked():
    assert paths("main", ["ns_common/paths.py"]) == []


def test_change_requests_file_is_open_to_everyone():
    assert paths("feat/c5-text", ["contracts/CHANGE_REQUESTS.md"]) == []
    assert paths("feat/c5-text", ["contracts/CONTRACTS.md"]) == ["contracts/CONTRACTS.md"]


def test_tests_are_shared_but_conftest_is_not():
    assert paths("feat/c6-media", ["tests/test_render.py", "tests/fixtures/x/y.json"]) == []
    assert paths("feat/c6-media", ["tests/conftest.py"]) == ["tests/conftest.py"]


def test_plan_files_belong_to_their_component():
    plan = "docs/superpowers/plans/m0-c4-tts-service.md"
    assert paths("feat/c4-tts", [plan]) == []
    assert paths("feat/c5-text", [plan]) == [plan]


def test_unscoped_branch_only_gets_protected_rule():
    assert paths("fix/typo", ["README.md", "ns_tts/server.py"]) == []
    assert paths("fix/typo", ["ns_common/paths.py"]) == ["ns_common/paths.py"]


def test_unknown_component_number_is_a_violation():
    assert paths("feat/c42-x", ["README.md"]) == ["README.md"]


def test_every_ownership_entry_is_well_formed():
    for comp, patterns in cs.OWNERSHIP.items():
        assert 1 <= comp <= 10
        assert patterns, comp
        for p in patterns:
            assert not p.startswith("/")
            assert "\\" not in p


def test_main_cli_exit_codes():
    assert cs.main(["--branch", "feat/c4-tts", "--files", "ns_tts/a.py"]) == 0
    assert cs.main(["--branch", "feat/c4-tts", "--files", "ns_common/a.py"]) == 1
