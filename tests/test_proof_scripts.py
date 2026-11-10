"""D-43: every proof script under `proofs/` is run to its end, in CI, against a fake-services home.

The home (`tests/proofhome.py:proof_home`) is made by the product's own code and the proofs run through
the real runner (`dev.prove.Runner`), so what these tests see is what `dev/prove.py --all --strict` counts.
A proof whose own code is wrong, or that believes in a file layout the product does not make (D-43: F-B7
looked for the job id in the outbox path; F-B13/F-B14 indexed a tuple like a dict), fails here before a
release candidate.

`COVERED` is the rule: a proof without a test in this file fails
`test_every_proof_script_has_a_fake_home_test`. To add a proof, add a test that runs it through
`proof_home.run("<file stem>")` and a row in `COVERED`.
"""
# ruff: noqa: F811  (a fixture imported by name is redefined by the test argument that asks for it)

from __future__ import annotations

import inspect
import os
import shutil
import sys
import types
from pathlib import Path

import pytest

from dev import prove
from tests.proofhome import ProofHome, proof_home  # noqa: F401  (`proof_home` is the fixture, used by name)

#: proof id -> the test in this file that runs it against the fake-services home
COVERED = {
    "f-b7-review-reject": "test_f_b7_passes_on_the_fake_services_home",
    "f-b13-b14-footage-429": "test_f_b13_b14_passes_against_the_fake_footage_endpoint",
    "f-b18-studio-status": "test_f_b18_passes_on_the_fake_services_home",
    "f-c1-status-events": "test_f_c1_passes_on_the_fake_services_home",
}


def assert_passed(result: prove.Result, *, at_least_checks: int = 3) -> None:
    """The runner's verdict is a pass (not a skip) and every recorded check held."""
    assert result.result == "pass", f"{result.proof.id}: {result.result}: {result.reason}"
    checks = [s for s in result.steps if "ok" in s]
    assert len(checks) >= at_least_checks, f"{result.proof.id} checked only {len(checks)} things"
    assert all(s["ok"] for s in checks), [s["name"] for s in checks if not s["ok"]]


def step_names(result: prove.Result) -> list[str]:
    return [str(s["name"]) for s in result.steps]


# --------------------------------------------------------------------------------------------- F-B7


def test_f_b7_passes_on_the_fake_services_home(proof_home: ProofHome):
    first = proof_home.run("f_b7_review_reject")
    assert_passed(first, at_least_checks=15)
    names = step_names(first)
    assert "before review: the output's file is in outbox/" in names
    assert any(n.startswith("the files MOVED to rejected/") for n in names)
    assert any(n.startswith("the files are back in outbox/") for n in names)
    # a story output: the title is in its path, never the job id (the layout F-B7 once got wrong)
    row = proof_home.state.db.one("select path, job_id from outputs where channel = 'proof-review'")
    assert "Fake story for proof-review" in row["path"] and row["job_id"] not in row["path"]
    # second run on the same home: the proof channel exists and was paused again, the proof still passes
    second = proof_home.run("f_b7_review_reject")
    assert_passed(second, at_least_checks=15)
    assert "proof channel resumed for this run" in step_names(second)
    mine = proof_home.state.db.one("select count(*) as n from outputs where channel = 'proof-review'")
    assert mine["n"] == 2


def test_f_b7_fails_when_a_rejected_output_leaves_a_file_in_the_outbox(
    proof_home: ProofHome, monkeypatch: pytest.MonkeyPatch
):
    from ns_studio import review

    def copy_not_move(src: str, dst: str) -> str:
        return str(shutil.copy2(src, dst))

    with monkeypatch.context() as patched:  # the reject route COPIES the files (`review.reject`'s move)
        patched.setattr(review, "shutil", types.SimpleNamespace(move=copy_not_move))
        broken = proof_home.run("f_b7_review_reject")
    assert broken.result == "fail", f"a copy instead of a move passed: {broken.result}"
    assert "the files MOVED to rejected/" in broken.reason, broken.reason
    # failed, then retried: with the route as the product has it, the same proof on the same home passes
    assert_passed(proof_home.run("f_b7_review_reject"), at_least_checks=15)


# ----------------------------------------------------------------------------------- F-B13 and F-B14

STAND_IN_FFMPEG = "import sys\nopen(sys.argv[-1], 'wb').write(b'stand-in clip: payload only')\n"


def ffmpeg_for_the_fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Where the fake footage endpoint makes its two clips: the FFmpeg CI installs; on the laptop (FFmpeg is
    off PATH there, `nspytest.ps1`) a stand-in of the same name that writes payload bytes (the product's
    download of a clip does not decode it, and the proof compares bytes). Returns which one is used."""
    if shutil.which("ffmpeg"):
        return "real"
    assert not os.environ.get("CI"), "CI installs FFmpeg (the pinned build): it must be on PATH here"
    folder = tmp_path / "standin"
    folder.mkdir()
    script = folder / "standin.py"
    script.write_text(STAND_IN_FFMPEG, encoding="utf-8")
    (folder / "ffmpeg.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="ascii")
    monkeypatch.setenv("PATH", str(folder) + os.pathsep + os.environ.get("PATH", ""))
    return "stand-in"


def test_f_b13_b14_passes_against_the_fake_footage_endpoint(
    proof_home: ProofHome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    ffmpeg_for_the_fake(tmp_path, monkeypatch)
    proof_home.allow_spawn = True
    try:
        result = proof_home.run("f_b13_b14_footage_429")
    finally:
        proof_home.allow_spawn = False
    assert_passed(result, at_least_checks=30)
    names = step_names(result)
    assert any("Library > Footage (GET /api/library/footage) lists the fetched" in n for n in names)
    assert any(n.startswith("endless 429: the fetch fails with the coded NS-E501") for n in names)
    assert "cleanup: the proof's keys, clip rows and clip files are gone" in names
    assert proof_home.state.db.one("select count(*) as n from clips")["n"] == 0
    # the fake endpoint was a real child with its pid recorded, and the runner stopped it
    assert [p["name"] for p in result.extra["pids"]] == ["footage-fake"]
    assert any(s["name"] == "runner stopped" for s in result.steps)


def has_ffmpeg(folder: Path) -> bool:
    return any((folder / f"ffmpeg{ext}").exists() for ext in (".exe", ".cmd", ".bat", ""))


def test_the_footage_proof_finds_ffmpeg_where_the_product_does(
    proof_home: ProofHome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """PATH without FFmpeg (the repo test venv's PATH, QA's case) and FFmpeg named the way the product finds
    it (`NS_FFMPEG`): the proof goes on to start its fake endpoint instead of skipping. Spawn is refused here,
    so it ends there, at the refusal, not at 'FFmpeg is not on PATH'."""
    without = [d for d in os.environ["PATH"].split(os.pathsep) if d and not has_ffmpeg(Path(d))]
    monkeypatch.setenv("PATH", os.pathsep.join(without))  # every other tool (git, openssl) stays findable
    assert shutil.which("ffmpeg") is None
    stand_in = tmp_path / "somewhere" / "ffmpeg.exe"
    stand_in.parent.mkdir()
    stand_in.write_bytes(b"")
    monkeypatch.setenv("NS_FFMPEG", str(stand_in))
    result = proof_home.run("f_b13_b14_footage_429")
    assert "FFmpeg is not on PATH" not in result.reason, result.reason
    assert result.result == "fail" and "ctx.spawn is refused" in result.reason, (result.result, result.reason)


def test_ffmpeg_path_looks_where_the_product_looks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from proofs._lib import ffmpeg_path

    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("NS_FFMPEG", raising=False)
    other = tmp_path / "another-home"  # NIGHTSHIFT_HOME names a different home: the argument wins
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(other))
    home = tmp_path / "the-install"
    assert ffmpeg_path(home) is None
    exe = home / "bin" / "ffmpeg" / "ffmpeg.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert ffmpeg_path(home) == str(exe)
    assert os.environ["NIGHTSHIFT_HOME"] == str(other)  # the lookup puts the variable back
    named = tmp_path / "named" / "ffmpeg.exe"
    named.parent.mkdir()
    named.write_bytes(b"")
    monkeypatch.setenv("NS_FFMPEG", str(named))
    assert ffmpeg_path(home) == str(named)  # the override comes first, as in ns_common.ffmpeg


# ------------------------------------------------------------------------------- the worked proofs


def test_f_b18_passes_on_the_fake_services_home(proof_home: ProofHome):
    result = proof_home.run("f_b18_studio_status")
    assert_passed(result)
    names = step_names(result)
    assert any(n.startswith("channel after-dark: the printed outputs line equals") for n in names)


def test_f_c1_passes_on_the_fake_services_home(proof_home: ProofHome):
    result = proof_home.run("f_c1_status_events")
    assert_passed(result, at_least_checks=7)
    assert "the frame carries the same status object" in step_names(result)


# ------------------------------------------------------------------------------------------ the rule


def uncovered(covered: dict[str, str]) -> list[str]:
    """What is wrong with a COVERED table, one sentence each (empty: nothing)."""
    found = {p.id: p for p in prove.discover()}
    problems = [f"proof {pid} is invalid: {'; '.join(p.problems)}" for pid, p in found.items() if p.problems]
    for pid in sorted(set(found) - set(covered)):
        problems.append(
            f"proof {pid} has no test that runs it against the fake-services home: add a test to "
            "tests/test_proof_scripts.py that calls proof_home.run('<file stem>') and a row in COVERED"
        )
    for pid in sorted(set(covered) - set(found)):
        problems.append(f"COVERED names {pid}, which is not a proof any more")
    me = sys.modules[__name__]
    for pid, name in covered.items():
        test = getattr(me, name, None)
        if not (inspect.isfunction(test) and name.startswith("test_")):
            problems.append(f"{pid}: {name} is not a test in this file")
        elif pid in found and f'run("{found[pid].stem}")' not in inspect.getsource(test):
            problems.append(f"{pid}: {name} does not run {found[pid].stem} through proof_home.run")
    return problems


def test_every_proof_script_has_a_fake_home_test():
    assert len(prove.discover()) >= 4  # the four proofs of D-43; a folder that lost them must not pass
    assert uncovered(COVERED) == []


def test_the_rule_names_a_proof_without_a_test_and_a_test_that_runs_nothing():
    without = {k: v for k, v in COVERED.items() if k != "f-b7-review-reject"}
    assert any("f-b7-review-reject has no test" in p for p in uncovered(without))
    names_nothing = {**COVERED, "f-c1-status-events": "test_every_proof_script_has_a_fake_home_test"}
    assert any("does not run f_c1_status_events" in p for p in uncovered(names_nothing))
    assert any("no longer" in p or "not a proof" in p for p in uncovered({**COVERED, "f-gone": "test_x"}))
