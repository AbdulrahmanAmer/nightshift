"""F-E7: after `ns update` the result carries the new version's CHANGELOG section (release notes)."""

import json
from pathlib import Path

import pytest

from ns_common.paths import Paths
from ns_dashboard.updater import classify_update, parse_update_output
from ns_supervisor import cli, update, update_job
from tests.test_ns_update import Env, make_app, make_zip
from tests.test_ns_update_git import FakeGit

CHANGELOG = """# Changelog

## [Unreleased]

- not shipped yet

## [0.2.0] - 2026-11-10

Intro line.

- Clipper: quiet clips reach -14 LUFS.
- Update: the card shows what changed.

## [0.1.0] - 2026-11-01

- first
"""


def test_release_notes_returns_the_section_of_that_version(tmp_path: Path) -> None:
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    text = update.release_notes(tmp_path, "0.2.0")
    assert text.startswith("## [0.2.0] - 2026-11-10")
    assert "- Clipper: quiet clips reach -14 LUFS." in text and "- Update: the card shows" in text
    assert "0.1.0" not in text and "not shipped yet" not in text


def test_release_notes_takes_the_last_section_and_ignores_a_v_prefix(tmp_path: Path) -> None:
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    assert update.release_notes(tmp_path, "v0.1.0").endswith("- first")


def test_release_notes_is_empty_without_a_changelog_or_section(tmp_path: Path) -> None:
    assert update.release_notes(tmp_path, "0.2.0") == ""
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    assert update.release_notes(tmp_path, "9.9.9") == ""
    assert update.release_notes(tmp_path, "") == ""


def test_a_zip_update_puts_the_new_changelog_section_in_the_result(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    z = make_zip(tmp_path, extra={"CHANGELOG.md": CHANGELOG})
    res = update.update_app(z, app, Env().env(), paths=Paths(tmp_path / "ns"))
    assert res.release_notes.startswith("## [0.2.0]") and "quiet clips" in res.release_notes
    assert "task re-registered" in res.notes          # `notes` keeps meaning what it did


def test_a_zip_without_a_changelog_gives_an_empty_string(tmp_path: Path) -> None:
    res = update.update_app(make_zip(tmp_path), make_app(tmp_path), Env().env(), paths=Paths(tmp_path / "ns"))
    assert res.release_notes == ""


def test_a_git_update_reads_the_changelog_of_the_tree_it_moved_to(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    (app / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    (app / "VERSION").write_text("0.2.0\n", encoding="utf-8")        # what the checkout would leave
    res = update.git_update(
        app, Env().env(), paths=Paths(tmp_path / "ns"), latest_tag=lambda: "v0.2.0", git=FakeGit()
    )
    assert res.release_notes.startswith("## [0.2.0]")


def test_the_worker_writes_release_notes_to_the_state_file(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "ns")
    out = update_job.Outcome("Now on 0.2.0 (was 0.1.0).", "0.1.0", "0.2.0", release_notes="## [0.2.0]\n- x")
    assert update_job.run_worker(paths, lambda: out) == 0
    assert update_job.read_status(paths)["release_notes"] == "## [0.2.0]\n- x"


def test_the_dashboard_state_carries_release_notes_only_when_there_are_some(tmp_path: Path) -> None:
    from ns_dashboard.updater import read_update_state

    paths = Paths(tmp_path / "ns")                       # the worker and the dashboard share one state file
    update_job.write_status(paths, "done", "Now on 0.2.0.", old="0.1.0", new="0.2.0")
    assert "release_notes" not in read_update_state(paths)         # same shape as before for old workers
    update_job.write_status(
        paths, "done", "Now on 0.2.0.", old="0.1.0", new="0.2.0", release_notes="## [0.2.0]"
    )
    assert read_update_state(paths)["release_notes"] == "## [0.2.0]"


def test_the_dashboard_keeps_release_notes_and_the_old_fields(tmp_path: Path) -> None:
    line = json.dumps({"updated": True, "old": "0.1.0", "new": "0.2.0", "release_notes": "## [0.2.0]\n- x"})
    res = classify_update(parse_update_output(0, f"Now on 0.2.0\n{line}\n", ""))
    assert res["release_notes"] == "## [0.2.0]\n- x"
    assert res["state"] == "updated" and (res["old"], res["new"]) == ("0.1.0", "0.2.0")


def test_the_cli_result_line_and_state_file_carry_the_release_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closed_port: int
) -> None:
    from tests.test_ns_update_github import pin_latest
    from tests.test_update_job import engine_down, invoke

    home = engine_down(tmp_path / "home", closed_port)
    pin_latest(monkeypatch, "99.0.0")
    monkeypatch.setattr(cli.releases, "download", lambda rel, dest, client=None: dest / rel.zip_name)
    done = update.UpdateResult("0.1.0", "99.0.0", [], "## [99.0.0]\n- big")
    monkeypatch.setattr(update, "update_app", lambda *a, **k: done)
    r = invoke(home, "update", "--github", "--worker")
    assert json.loads(r.output.strip().splitlines()[-1])["release_notes"] == "## [99.0.0]\n- big"
    assert update_job.read_status(Paths(home))["release_notes"] == "## [99.0.0]\n- big"


# ---------------------------------------------------------------------------------------------- D-53
# A candidate's changelog describes it under the heading of the release it previews (`## [0.1.15]` for
# 0.1.15-rc4), so an update TO a candidate showed no "What is new".
def test_a_candidate_reads_its_release_section(tmp_path: Path) -> None:
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    final = update.release_notes(tmp_path, "0.2.0")
    assert final.startswith("## [0.2.0] - 2026-11-10") and "quiet clips" in final
    for candidate in ("0.2.0-rc3", "0.2.0rc3", "v0.2.0-rc3", "0.2.0-RC3"):
        assert update.release_notes(tmp_path, candidate) == final, candidate
    # a release with no section of its own gets nothing: never [Unreleased], never an older section
    for other in ("0.3.0-rc1", "0.3.0", "0.2.1-rc1", "0.2-rc1", "rc3", "0.2.0-rcX"):
        assert update.release_notes(tmp_path, other) == "", other


def test_a_candidates_own_heading_comes_before_its_releases(tmp_path: Path) -> None:
    final_heading = "## [0.2.0] - 2026-11-10"
    rc3_section = "\n".join(["## [0.2.0-rc3] - 2026-11-09", "", "- only in rc3", "", final_heading])
    own = CHANGELOG.replace(final_heading, rc3_section)
    (tmp_path / "CHANGELOG.md").write_text(own, encoding="utf-8")
    assert update.release_notes(tmp_path, "0.2.0-rc3").startswith("## [0.2.0-rc3]")
    assert "- only in rc3" in update.release_notes(tmp_path, "0.2.0rc3")
    assert update.release_notes(tmp_path, "0.2.0-rc4").startswith("## [0.2.0] - ")  # rc4 has none of its own
    assert update.release_notes(tmp_path, "0.2.0").startswith("## [0.2.0] - ")


def test_a_zip_update_to_a_candidate_puts_its_release_section_in_the_result(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    z = make_zip(tmp_path, "0.2.0-rc1", extra={"CHANGELOG.md": CHANGELOG})
    res = update.update_app(z, app, Env().env(), paths=Paths(tmp_path / "ns"))
    assert res.release_notes.startswith("## [0.2.0] - 2026-11-10") and "quiet clips" in res.release_notes


def test_this_versions_changelog_has_notes_an_update_will_show() -> None:
    """The repository's own VERSION against its own CHANGELOG.md: whatever is released from this tree shows
    its notes after the update (a candidate: the section of the release it previews)."""
    root = Path(__file__).resolve().parents[1]
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    notes = update.release_notes(root, version)
    release = version.lower().split("-rc")[0].split("rc")[0]
    assert notes.startswith(f"## [{version}]") or notes.startswith(f"## [{release}]"), (version, notes[:80])
