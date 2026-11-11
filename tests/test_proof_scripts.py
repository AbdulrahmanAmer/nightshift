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
from typing import Any

import pytest

from dev import prove
from tests.proofhome import ProofHome, proof_home  # noqa: F401  (`proof_home` is the fixture, used by name)

#: proof id -> {"passes": the test that runs it to a pass, "fails": the tests that put one fault it exists to
#: catch into the home and assert it FAILS at the named check}. A proof needs both.
COVERED = {
    "f-b7-review-reject": {
        "passes": "test_f_b7_passes_on_the_fake_services_home",
        "fails": ["test_f_b7_fails_when_a_rejected_output_leaves_a_file_in_the_outbox"],
    },
    "f-b13-b14-footage-429": {
        "passes": "test_f_b13_b14_passes_against_the_fake_footage_endpoint",
        "fails": [
            "test_f_b13_fails_when_a_fetched_clip_is_not_the_fakes_bytes",
            "test_f_b13_fails_when_library_does_not_list_a_fetched_clip",
            "test_f_b14_fails_when_a_fetch_ignores_retry_after",
            "test_f_b14_fails_when_an_endless_429_does_not_end_in_ns_e504",
        ],
    },
    "f-b18-studio-status": {
        "passes": "test_f_b18_passes_on_the_fake_services_home",
        "fails": ["test_f_b18_fails_when_status_prints_a_wrong_outputs_count"],
    },
    "f-c1-status-events": {
        "passes": "test_f_c1_passes_on_the_fake_services_home",
        "fails": ["test_f_c1_fails_when_an_events_frame_differs_from_the_status"],
    },
    "f-h47-closed-code-window": {
        "passes": "test_fh47_proof_runs_against_a_fake_home",
        "fails": ["test_fh47_proof_fails_when_a_closed_console_keeps_the_card"],
    },
    "vram-busy": {
        "passes": "test_vram_busy_proof_runs_against_a_fake_home",
        "fails": ["test_vram_busy_proof_fails_when_a_load_is_not_checked"],
    },
}


def assert_passed(result: prove.Result, *, at_least_checks: int = 3) -> None:
    """The runner's verdict is a pass (not a skip) and every recorded check held."""
    assert result.result == "pass", f"{result.proof.id}: {result.result}: {result.reason}"
    checks = [s for s in result.steps if "ok" in s]
    assert len(checks) >= at_least_checks, f"{result.proof.id} checked only {len(checks)} things"
    assert all(s["ok"] for s in checks), [s["name"] for s in checks if not s["ok"]]


def step_names(result: prove.Result) -> list[str]:
    return [str(s["name"]) for s in result.steps]


def assert_fails_at(result: prove.Result, check: str) -> None:
    """The runner's verdict is a FAIL, and the failing check is the named one (its text), not some other
    failure (a crash, a skip, an earlier check)."""
    assert result.result == "fail", f"{result.proof.id} did not fail: {result.result} {result.reason}"
    assert result.reason.startswith(check), f"failed at {result.reason!r}, wanted {check!r}"
    failed = [s["name"] for s in result.steps if s.get("ok") is False]
    assert failed and str(failed[-1]).startswith(check), failed


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
    assert_fails_at(broken, "the files MOVED to rejected/")
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


def allow_footage_proof(proof_home: ProofHome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FFmpeg for the fake endpoint, and `ctx.spawn` allowed for this test only (the fixture is per test)."""
    ffmpeg_for_the_fake(tmp_path, monkeypatch)
    proof_home.allow_spawn = True


def test_f_b13_b14_passes_against_the_fake_footage_endpoint(
    proof_home: ProofHome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    allow_footage_proof(proof_home, tmp_path, monkeypatch)
    result = proof_home.run("f_b13_b14_footage_429")
    assert_passed(result, at_least_checks=30)
    names = step_names(result)
    assert any("Library > Footage (GET /api/library/footage) lists the fetched" in n for n in names)
    assert any(n.startswith("endless 429: the fetch fails with the coded NS-E504") for n in names)
    assert "cleanup: the proof's keys, clip rows and clip files are gone" in names
    assert proof_home.state.db.one("select count(*) as n from clips")["n"] == 0
    # the fake endpoint was a real child with its pid recorded, and the runner stopped it
    assert [p["name"] for p in result.extra["pids"]] == ["footage-fake"]
    assert any(s["name"] == "runner stopped" for s in result.steps)


def test_f_b13_fails_when_a_fetched_clip_is_not_the_fakes_bytes(
    proof_home: ProofHome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_studio.media import clip_sources

    real = clip_sources._HttpSource._fetch_file

    def corrupt(self, url: str, dest: Path) -> Path:  # the download lands, but not as the endpoint sent it
        out = real(self, url, dest)
        out.write_bytes(out.read_bytes() + b"corrupted in transit")
        return out

    monkeypatch.setattr(clip_sources._HttpSource, "_fetch_file", corrupt)
    allow_footage_proof(proof_home, tmp_path, monkeypatch)
    result = proof_home.run("f_b13_b14_footage_429")
    assert_fails_at(result, "pexels clip ")
    assert "the fake's bytes" in result.reason


def test_f_b13_fails_when_library_does_not_list_a_fetched_clip(
    proof_home: ProofHome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_dashboard.svc_library import LibraryService

    monkeypatch.setattr(LibraryService, "footage", lambda self, q=None, limit=60: [])
    allow_footage_proof(proof_home, tmp_path, monkeypatch)
    result = proof_home.run("f_b13_b14_footage_429")
    assert_fails_at(result, "Library > Footage (GET /api/library/footage) lists the fetched Pexels clips")


def test_f_b14_fails_when_a_fetch_ignores_retry_after(
    proof_home: ProofHome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_studio.media import clip_sources

    monkeypatch.setattr(clip_sources, "retry_wait_s", lambda resp, wall_now: 0.0)  # a 429 is retried at once
    allow_footage_proof(proof_home, tmp_path, monkeypatch)
    result = proof_home.run("f_b13_b14_footage_429")
    assert_fails_at(result, "the fetch waited out 3 Retry-After periods")


def test_f_b14_fails_when_an_endless_429_does_not_end_in_ns_e504(
    proof_home: ProofHome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_common.errors import NsError
    from ns_studio.media import clip_sources

    real = clip_sources._HttpSource._get

    def other_code(self, *args, **kwargs):  # giving up on a 429 is reported as the key, not NS-E504
        try:
            return real(self, *args, **kwargs)
        except NsError as err:
            if err.code == "NS-E504":
                raise NsError("NS-E501", str(err)) from err
            raise

    monkeypatch.setattr(clip_sources._HttpSource, "_get", other_code)
    allow_footage_proof(proof_home, tmp_path, monkeypatch)
    result = proof_home.run("f_b13_b14_footage_429")
    assert_fails_at(result, "endless 429: the fetch fails with the coded NS-E504")


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
    assert_passed(result, at_least_checks=9)
    names = step_names(result)
    assert "the frame carries the same status object" in names
    assert "the frame's status equals GET /api/status (version, schedule flag, channels)" in names


def test_f_b18_fails_when_status_prints_a_wrong_outputs_count(
    proof_home: ProofHome, monkeypatch: pytest.MonkeyPatch
):
    from ns_dashboard.fakes import seed_output
    from ns_studio.learn import status

    seed_output(proof_home.state.db, proof_home.paths, kind="short", k=1, title="Floor 13")
    real = status.format_status

    def off_by_one(st, cadence_line=""):
        return [line.replace("outputs: pending=1", "outputs: pending=2") for line in real(st, cadence_line)]

    monkeypatch.setattr(status, "format_status", off_by_one)
    result = proof_home.run("f_b18_studio_status")
    assert_fails_at(result, "channel after-dark: the printed outputs line equals the outputs table")


def test_f_c1_fails_when_an_events_frame_differs_from_the_status(
    proof_home: ProofHome, monkeypatch: pytest.MonkeyPatch
):
    from ns_dashboard import svc_engine

    real = svc_engine.iter_sse

    def other_status(sub, status_fn, **kw):  # the stream's status loses the channels the status route has
        return real(sub, lambda: {**status_fn(), "channels": []}, **kw)

    monkeypatch.setattr(svc_engine, "iter_sse", other_status)
    result = proof_home.run("f_c1_status_events")
    assert_fails_at(result, "the frame's status equals GET /api/status")


# ------------------------------------------------------------------------------- F-H47 on the simulated card
class RealEngine:
    """The REAL engine (`Supervisor` and `create_app`, the real holder probe and monitor) over the proof home,
    made an engine-test install with `fake_gpu` (H-10), on its own free port. The services are stub processes
    with the gpu flags of the real service table; the proof home's fake engine is stopped first."""

    def __init__(self, ph: ProofHome) -> None:
        from ns_common.config import save_runtime
        from ns_common.models import Runtime
        from ns_supervisor.app import create_app
        from ns_supervisor.core import Supervisor
        from ns_supervisor.services import SERVICES, build_service
        from ns_supervisor.testing import fast_config, free_port, stub_def
        from tests.proofhome import _Server

        rt = Runtime.default("cpu-dev").model_copy(update={"fake_footage": True, "fake_gpu": True})
        rt.tts.engine = "fake"
        save_runtime(ph.paths, rt)
        ports = {n: free_port() for n in SERVICES}

        def resolve(name: str, alias: str | None) -> Any:
            real = build_service(rt, ph.paths, name, alias)
            return stub_def(name, ports[name], gpu=real.gpu, variant=real.variant,
                            model_alias=real.model_alias, persistent=real.persistent)  # fmt: skip

        self.sup = Supervisor(ph.paths, rt, config=fast_config(idle_unload_s=60), resolver=resolve)
        self.server = _Server(create_app(self.sup))
        self.url = f"http://127.0.0.1:{self.server.port}"

    def stop(self) -> None:
        self.server.stop()  # its lifespan stops the services and closes the job


@pytest.fixture
def fh47_home(proof_home: ProofHome) -> ProofHome:
    proof_home.supervisor.stop()
    proof_home.supervisor = RealEngine(proof_home)  # type: ignore[assignment]  # stopped by proof_home.close
    proof_home.allow_spawn = True  # the console stand-in: a real child, ended by its pid
    return proof_home


def test_fh47_proof_runs_against_a_fake_home(fh47_home: ProofHome):
    result = fh47_home.run("f_h47_closed_code_window")
    assert_passed(result, at_least_checks=10)
    names = step_names(result)
    assert "the card is free within 5 s of the console closing" in names
    assert "the next nightly job gets the card at once (wait false)" in names


def test_fh47_proof_fails_when_a_closed_console_keeps_the_card(
    fh47_home: ProofHome, monkeypatch: pytest.MonkeyPatch
):
    from ns_supervisor.core import Supervisor

    async def never(self: Supervisor) -> None:  # the F-H47 defect: nobody looks at the holder's process
        return None

    monkeypatch.setattr(Supervisor, "_release_gone_holder", never)
    result = fh47_home.run("f_h47_closed_code_window")
    assert_fails_at(result, "the card is free within 5 s of the console closing")


# ------------------------------------------------------------------------------ vram-busy, the simulated card
def test_vram_busy_proof_runs_against_a_fake_home(fh47_home: ProofHome):
    result = fh47_home.run("vram_busy")
    assert_passed(result, at_least_checks=8)
    names = step_names(result)
    assert "the Dashboard's start is refused at once with NS-E208 naming game.exe" in names
    assert "the nightly's load goes ahead once the memory is free" in names
    assert "the nightly gives up at its bound with NS-E208, saying how long it waited" in names


def test_vram_busy_proof_fails_when_a_load_is_not_checked(
    fh47_home: ProofHome, monkeypatch: pytest.MonkeyPatch
):
    from ns_supervisor import vram

    monkeypatch.setattr(vram, "check", lambda *a, **k: None)  # the defect: nothing reads the card first
    result = fh47_home.run("vram_busy")
    assert_fails_at(result, "the Dashboard's start is refused at once with NS-E208 naming game.exe")


# ------------------------------------------------------------------------------------------ the rule


def uncovered(covered: dict[str, dict[str, Any]]) -> list[str]:
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
    for pid, row in covered.items():
        stem = found[pid].stem if pid in found else ""
        if not row.get("fails"):
            problems.append(f"{pid} has no negative case: a test that asserts FAIL with its fault")
        for kind, names in (("passes", [row.get("passes")]), ("fails", row.get("fails") or [])):
            for name in names:
                test = getattr(me, str(name), None)
                if not (inspect.isfunction(test) and str(name).startswith("test_")):
                    problems.append(f"{pid}: {name} is not a test in this file")
                    continue
                source = inspect.getsource(test)
                if kind == "passes" and stem and f'run("{stem}")' not in source:
                    problems.append(f"{pid}: {name} does not run {stem} through proof_home.run")
                if kind == "fails" and "assert_fails_at(" not in source:
                    problems.append(f"{pid}: {name} does not assert the proof FAILS at a named check")
    return problems


def test_every_proof_script_has_a_fake_home_test():
    assert len(prove.discover()) >= 4  # the four proofs of D-43; a folder that lost them must not pass
    assert uncovered(COVERED) == []


def test_the_rule_names_a_proof_without_a_test_without_a_negative_and_a_test_that_runs_nothing():
    c1 = COVERED["f-c1-status-events"]
    without = {k: v for k, v in COVERED.items() if k != "f-b7-review-reject"}
    assert any("f-b7-review-reject has no test" in p for p in uncovered(without))
    names_nothing = {**COVERED, "f-c1-status-events": {**c1, "passes": "test_x"}}
    assert any("test_x is not a test" in p for p in uncovered(names_nothing))
    no_negative = {**COVERED, "f-c1-status-events": {"passes": c1["passes"], "fails": []}}
    assert any("f-c1-status-events has no negative case" in p for p in uncovered(no_negative))
    only_passing = {**COVERED, "f-c1-status-events": {**c1, "fails": [c1["passes"]]}}
    assert any("does not assert the proof FAILS" in p for p in uncovered(only_passing))
    gone = {**COVERED, "f-gone": {"passes": "t", "fails": ["t"]}}
    assert any("not a proof any more" in p for p in uncovered(gone))
