"""H-13: `dev/prove.py` runs proof scripts (`proofs/<slug>.py`) against an install and writes one
evidence file per box. Everything here uses fake homes, fake proof scripts under tmp_path, fake tool
scripts and stubs for the shell and the network: no real install, no network, no GPU, no FFmpeg, and no
process is started."""

from __future__ import annotations

import importlib
import io
import json
import sqlite3
import subprocess
import sys
import textwrap
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dev import prove, prove_ctx
from dev.prove_ctx import Ctx, InstallInfo, ProofFailed, ProofSkipped
from ns_studio.learn.status import ChannelStatus, format_status

TEMPLATE = """
PROOF = {{"id": "{id}", "boxes": {boxes}, "install": "{install}", "window": {window},
         "needs": {needs}{extra}}}

def run(ctx):
{body}
"""


def write_proof(
    pdir: Path,
    stem: str,
    *,
    id: str | None = None,  # noqa: A002
    boxes=("F-X1",),
    install="dev",
    window=False,
    needs=(),
    timeout_s: float | None = None,
    body="ctx.step('ran')",
) -> Path:
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "__init__.py").touch()
    path = pdir / f"{stem}.py"
    path.write_text(
        TEMPLATE.format(
            id=id or stem.replace("_", "-"),
            boxes=list(boxes),
            install=install,
            window=window,
            needs=list(needs),
            extra=f', "timeout_s": {timeout_s}' if timeout_s is not None else "",
            body=textwrap.indent(textwrap.dedent(body), "    "),
        ),
        encoding="utf-8",
    )
    return path


def make_home(root: Path, *, dev: bool = True, version: str = "9.9.9", cli: bool = True) -> Path:
    home = root / "home"
    (home / "config").mkdir(parents=True)
    (home / "app").mkdir()
    (home / "app" / "VERSION").write_text(version + "\n", encoding="utf-8")
    runtime = {"dev_mode": dev, "supervisor_port": 8099, "tts": {"port": 8091}}
    (home / "config" / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")
    if cli:
        for venv, exe in (("supervisor", "ns.exe"), ("studio", "studio.exe")):
            (home / "venvs" / venv / "Scripts").mkdir(parents=True)
            (home / "venvs" / venv / "Scripts" / exe).write_text("", encoding="utf-8")
    return home


class Tools:
    """Stands in for powershell + refs\\tools: records every call; `nsup` brings the fake engine up."""

    def __init__(self, tmp: Path, *, up: bool = False, nsup_rc: int = 0, busy: bool = False) -> None:
        self.dir = tmp / "tools"
        self.dir.mkdir()
        (self.dir / "nsup.ps1").write_text("", encoding="utf-8")
        (self.dir / "nsproc.ps1").write_text("", encoding="utf-8")
        self.up, self.nsup_rc, self.calls, self.busy = up, nsup_rc, [], busy
        self.reports: str | None = None  # the home the stub dashboard says it serves (default: the asked one)

    def ps(self, script: Path, args: list[str], timeout: float) -> tuple[int, str]:
        self.calls.append((script.name, list(args)))
        if script.name == "nsup.ps1":
            self.up = self.nsup_rc == 0
            return self.nsup_rc, "up (headless)" if self.nsup_rc == 0 else "ENGINE DID NOT START"
        if "-Down" in args:
            self.up = False
            return 0, "CLEAN: no Nightshift test processes"
        if self.up:
            return 0, "  4242     90 MB  python ns_supervisor\n  4343     80 MB  python ns_dashboard"
        if self.busy:  # someone else's engine, on another home
            return 0, "  5151    120 MB  python ns_supervisor (not ours)"
        return 0, "CLEAN: no Nightshift test processes"

    def probe(self, info: InstallInfo) -> bool:
        return self.up

    def names(self) -> list[str]:
        return [n + (" -Down" if "-Down" in a else "") for n, a in self.calls]


@pytest.fixture
def env(tmp_path: Path):
    lock = tmp_path / "NightShift" / "refs" / "departments" / "LAPTOP.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("ns-qa | proofs | 22:00\n", encoding="utf-8")  # the caller (qa) holds the laptop lock
    return SimpleNamespace(
        lock=lock,
        tmp=tmp_path,
        pdir=tmp_path / "proofs",
        home=make_home(tmp_path),
        tools=Tools(tmp_path),
        runs=tmp_path / "runs",
    )


def run_main(env, *args: str, tools: Tools | None = None, home: Path | None = None) -> int:
    t = tools or env.tools
    return prove.main(
        ["--home", str(home or env.home), "--runs", str(env.runs), "--proofs-dir", str(env.pdir)]
        + ["--tools", str(t.dir), "--lock-file", str(env.lock), "--dept", "qa", *args],
        runner_options={
            "commit": "abc123",
            "ps": t.ps,
            "probe": t.probe,
            "dashboard_home": lambda info: t.reports or str(home or env.home),
            "hard_grace_s": 0.2,
        },
    )


def evidence(env, box: str, version: str = "9.9.9") -> dict[str, Any]:
    return json.loads((env.runs / "proofs" / version / f"{box}.json").read_text(encoding="utf-8"))


def evidence_files(env, version: str = "9.9.9") -> list[str]:
    d = env.runs / "proofs" / version
    return sorted(p.name for p in d.glob("*.json")) if d.is_dir() else []


# --- discovery -----------------------------------------------------------------------------------------


def test_discovery_reads_declarations_and_skips_every_underscore_name(env):
    write_proof(env.pdir, "f_b7_review", boxes=["F-B7", "f-b8"], needs=["engine"], window=True, install="any")
    write_proof(env.pdir, "_hidden")
    (env.pdir / "_lib").mkdir()
    (env.pdir / "_lib" / "__init__.py").touch()
    (env.pdir / "_lib" / "stray.py").write_text("PROOF = {}\n", encoding="utf-8")
    (env.pdir / "_fakes").mkdir()
    (env.pdir / "_fixtures").mkdir()
    found = prove.discover(env.pdir)
    assert [p.stem for p in found] == ["f_b7_review"]  # __init__, _hidden and the folders are skipped
    p = found[0]
    assert (p.id, p.boxes, p.install, p.window, p.needs, p.timeout_s) == (
        "f-b7-review",
        ["F-B7", "F-B8"],
        "any",
        True,
        ["engine"],
        prove.DEFAULT_TIMEOUT_S,
    )
    assert p.valid


def test_a_missing_or_wrong_field_is_reported_not_a_crash(env):
    env.pdir.mkdir()
    files = {
        "no_install": 'PROOF = {"id": "no-inst", "boxes": ["F-X1"], "window": False}\ndef run(ctx): pass\n',
        "no_boxes": 'PROOF = {"id": "no-boxes", "install": "dev", "window": False}\ndef run(ctx): pass\n',
        "no_proof": "def run(ctx): pass\n",
        "not_literal": "PROOF = dict(id='x')\ndef run(ctx): pass\n",
        "no_run": 'PROOF = {"id": "no-run", "boxes": ["F-X1"], "install": "dev", "window": False}\n',
        "bad_values": 'PROOF = {"id": "Bad Id", "boxes": ["x"], "install": "cloud", "window": "yes",'
        ' "needs": ["gpu"], "timeout_s": -1, "extra": 1}\ndef run(ctx): pass\n',
        "syntax": "def run(:\n",
    }
    for stem, text in files.items():
        (env.pdir / f"{stem}.py").write_text(text, encoding="utf-8")
    write_proof(env.pdir, "first", id="same")
    write_proof(env.pdir, "second", id="same")
    by = {p.stem: p for p in prove.discover(env.pdir)}
    assert "missing field install" in by["no_install"].problems
    assert "missing field boxes" in by["no_boxes"].problems
    assert any("no module-level PROOF" in x for x in by["no_proof"].problems)
    assert any("not a literal" in x for x in by["not_literal"].problems)
    assert any("no `def run(ctx)`" in x for x in by["no_run"].problems)
    assert len(by["bad_values"].problems) >= 6
    assert any("cannot read" in x for x in by["syntax"].problems)
    assert by["first"].valid and any("also declared by first.py" in x for x in by["second"].problems)


def test_list_prints_id_boxes_install_window_needs_and_names_the_invalid(env, capsys):
    write_proof(env.pdir, "f_b7_review", boxes=["F-B7"], needs=["engine"], window=True, install="engine")
    (env.pdir / "broken.py").write_text("x = 1\n", encoding="utf-8")
    assert prove.main(["--list", "--proofs-dir", str(env.pdir)]) == 0  # --list needs no home
    head, row, *rest = capsys.readouterr().out.splitlines()
    assert head.split() == ["id", "boxes", "install", "window", "needs"]
    assert row.split() == ["f-b7-review", "F-B7", "engine", "yes", "engine"]
    assert any(line.startswith("INVALID broken.py") for line in rest)


def test_the_repo_proofs_are_valid_and_include_the_two_worked_proofs():
    found = {p.id: p for p in prove.discover()}
    assert all(p.problems == [] for p in found.values())
    cli, dash = found["f-b18-studio-status"], found["f-c1-status-events"]
    assert (cli.boxes, cli.needs, cli.window) == (["F-B18"], [], False)
    assert (dash.boxes, dash.needs, dash.window) == (["F-C1"], ["engine"], False)


# --- selection -----------------------------------------------------------------------------------------


def test_box_selects_only_the_proofs_that_declare_it_and_writes_evidence_per_box(env):
    write_proof(env.pdir, "one", boxes=["F-X1", "F-X2"])
    write_proof(env.pdir, "two", boxes=["F-Y1"])
    assert run_main(env, "--box", "f-x2") == 0
    assert evidence_files(env) == ["F-X1.json", "F-X2.json"]  # a proof of two boxes: one file for each
    assert evidence(env, "F-X1")["proof_id"] == "one"


def test_proof_selects_by_id_and_all_runs_every_proof_the_install_kind_fits(env):
    write_proof(env.pdir, "one", boxes=["F-X1"])
    write_proof(env.pdir, "two", boxes=["F-Y1"], install="any")
    write_proof(env.pdir, "engine_only", boxes=["F-Z1"], install="engine")
    assert run_main(env, "--proof", "one") == 0
    assert evidence_files(env) == ["F-X1.json"]
    assert run_main(env, "--all") == 0
    assert evidence_files(env) == ["F-X1.json", "F-Y1.json"]  # the engine-only proof does not fit a dev home


def test_a_proof_asked_for_by_name_on_the_wrong_install_kind_is_a_usage_error_and_keeps_old_evidence(
    env, capsys
):
    write_proof(env.pdir, "engine_only", boxes=["F-Z1"], install="engine", body="raise AssertionError('ran')")
    write_proof(env.pdir, "dev_only", boxes=["F-Z1"], install="dev")
    assert run_main(env, "--proof", "dev-only") == 0
    before = (env.runs / "proofs" / "9.9.9" / "F-Z1.json").read_text(encoding="utf-8")
    capsys.readouterr()
    calls = list(env.tools.calls)
    assert run_main(env, "--box", "F-Z1") == 2  # one of the two proofs of the box does not fit this home
    err = capsys.readouterr().err
    assert "engine-only" in err and "another install kind" in err
    assert (env.runs / "proofs" / "9.9.9" / "F-Z1.json").read_text(encoding="utf-8") == before
    assert env.tools.calls == calls  # nothing was started for the refused request
    assert run_main(env, "--all") == 0  # --all still skips what does not fit, quietly
    assert evidence(env, "F-Z1")["result"] == "pass"


def test_no_window_skips_window_proofs(env):
    write_proof(env.pdir, "windowed", boxes=["F-W1"], window=True, body="raise AssertionError('ran')")
    write_proof(env.pdir, "plain", boxes=["F-W2"])
    assert run_main(env, "--all", "--no-window") == 0
    assert evidence(env, "F-W1")["result"] == "skipped" and "--no-window" in evidence(env, "F-W1")["reason"]
    assert evidence(env, "F-W2")["result"] == "pass"


def test_usage_and_setup_errors_exit_2(env, capsys):
    write_proof(env.pdir, "one")
    assert run_main(env) == 2  # nothing asked for
    assert run_main(env, "--box", "F-NOPE1") == 2  # no proof declares it
    assert run_main(env, "--proof", "nope") == 2
    assert prove.main(["--all", "--proofs-dir", str(env.pdir)]) == 2  # no --home
    assert run_main(env, "--all", home=env.tmp / "missing") == 2
    bare = env.tmp / "bare"
    bare.mkdir()
    assert run_main(env, "--all", home=bare) == 2  # not an install (no runtime.json)
    (env.home / "app" / "VERSION").unlink()
    assert run_main(env, "--all") == 2  # cannot name the product version
    assert "VERSION" in capsys.readouterr().err
    assert evidence_files(env) == []


# --- results -------------------------------------------------------------------------------------------


def test_a_failing_proof_gives_exit_1_and_a_failed_evidence_file(env, capsys):
    write_proof(env.pdir, "good", boxes=["F-G1"])
    write_proof(env.pdir, "bad", boxes=["F-B1"], body="ctx.check(1 + 1 == 3, 'arithmetic holds', got=2)")
    assert run_main(env, "--all") == 1
    ev = evidence(env, "F-B1")
    assert ev["result"] == "fail" and ev["reason"] == "arithmetic holds"
    assert ev["steps"][-1]["ok"] is False and ev["steps"][-1]["observed"] == {"got": 2}
    assert "ProofFailed" in ev["traceback"]
    assert evidence(env, "F-G1")["result"] == "pass"  # the other proof still ran and passed
    assert "FAIL" in capsys.readouterr().out


def test_any_exception_in_a_proof_is_a_failure_with_its_traceback(env):
    write_proof(env.pdir, "boom", boxes=["F-E1"], body="{}['missing']")
    assert run_main(env, "--all") == 1
    ev = evidence(env, "F-E1")
    assert ev["result"] == "fail" and ev["reason"].startswith("KeyError")
    assert "Traceback" in ev["traceback"]


def test_a_skip_is_recorded_as_skipped_not_passed(env):
    write_proof(env.pdir, "needs_gpu", boxes=["F-S1"], body="ctx.skip('needs fake_gpu')")
    assert run_main(env, "--all") == 0
    ev = evidence(env, "F-S1")
    assert (ev["result"], ev["reason"]) == ("skipped", "needs fake_gpu")


def test_strict_makes_a_skip_fail_the_run(env, capsys):
    write_proof(env.pdir, "needs_gpu", boxes=["F-S1"], body="ctx.skip('needs fake_gpu')")
    write_proof(env.pdir, "plain", boxes=["F-S3"])
    assert run_main(env, "--all") == 0  # without --strict a skip does not fail the run, as before
    assert "skipped: a skip is not a pass" not in capsys.readouterr().out
    assert run_main(env, "--all", "--strict") == 3
    out = capsys.readouterr().out
    assert "1 skipped: a skip is not a pass" in out
    assert "needs-gpu: needs fake_gpu" in out  # which proof skipped, and why
    assert evidence(env, "F-S1")["result"] == "skipped"  # the evidence still says skipped, never passed
    assert evidence(env, "F-S3")["result"] == "pass"


def test_strict_counts_a_window_proof_skipped_by_no_window_and_a_failure_still_wins(env, capsys):
    write_proof(env.pdir, "windowed", boxes=["F-W1"], window=True)
    assert run_main(env, "--all", "--no-window") == 0
    capsys.readouterr()
    assert run_main(env, "--all", "--no-window", "--strict") == 3  # selected, and not run: not a pass
    assert "windowed: window proof skipped by --no-window" in capsys.readouterr().out
    write_proof(env.pdir, "bad", boxes=["F-B1"], body="ctx.check(False, 'broken')")
    assert run_main(env, "--all", "--no-window", "--strict") == 1  # a failure is 1, whatever else skipped


def test_strict_passes_when_nothing_skipped(env, capsys):
    write_proof(env.pdir, "plain", boxes=["F-S3"])
    assert run_main(env, "--all", "--strict") == 0
    assert "a skip is not a pass" not in capsys.readouterr().out


def test_a_skip_cannot_be_swallowed_by_a_proofs_except_exception(env):
    body = """
        try:
            ctx.skip('later')
        except Exception:
            pass
        ctx.step('reached after the skip')
    """
    write_proof(env.pdir, "sneaky", boxes=["F-S2"], body=body)
    run_main(env, "--all")
    ev = evidence(env, "F-S2")
    assert ev["result"] == "skipped"
    assert all(s["name"] != "reached after the skip" for s in ev["steps"])


def test_the_evidence_file_has_the_agreed_shape(env):
    body = """
        ctx.step('looked', value=3, path=ctx.home)
        ctx.check(True, 'it holds', a=1)
    """
    write_proof(env.pdir, "shape", boxes=["F-H1"], body=body)
    run_main(env, "--all")
    ev = evidence(env, "F-H1")
    for key in "proof_id box result reason steps version kind home started duration_s runner_commit".split():
        assert key in ev
    assert (ev["proof_id"], ev["box"], ev["result"], ev["version"], ev["kind"], ev["runner_commit"]) == (
        "shape",
        "F-H1",
        "pass",
        "9.9.9",
        "dev",
        "abc123",
    )
    assert ev["home"] == str(env.home) and ev["NIGHTSHIFT_HOME"] == str(env.home)
    assert isinstance(ev["duration_s"], float)
    assert [s["name"] for s in ev["steps"]] == ["looked", "it holds"]
    assert ev["steps"][0]["observed"] == {"value": 3, "path": str(env.home)}
    assert ev["steps"][1]["ok"] is True and ev["steps"][0]["ts"]
    assert (env.runs / "proofs" / "9.9.9" / "shape").is_dir()  # the proof's empty scratch folder


def test_the_version_comes_from_the_install_and_names_the_evidence_folder(env):
    (env.home / "app" / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    write_proof(env.pdir, "p", boxes=["F-V1"])
    run_main(env, "--all")
    assert evidence(env, "F-V1", "1.2.3")["version"] == "1.2.3"


def test_a_declaration_with_a_missing_field_fails_an_all_run_and_is_filed_not_crashed(env, capsys):
    write_proof(env.pdir, "fine", boxes=["F-F1"])
    (env.pdir / "half.py").write_text('PROOF = {"id": "half", "boxes": ["F-F2"]}\ndef run(ctx): pass\n')
    assert run_main(env, "--all") == 1
    assert evidence(env, "F-F1")["result"] == "pass"
    bad = evidence(env, "_invalid-half")
    assert bad["result"] == "fail" and "missing field install" in bad["reason"]
    assert "invalid PROOF declaration" in capsys.readouterr().out


def test_proofs_are_imported_by_package_name_and_can_use_the_underscore_helper_package(env):
    (env.pdir / "_lib").mkdir(parents=True)
    (env.pdir / "_lib" / "__init__.py").write_text("VALUE = 41\n", encoding="utf-8")
    body = """
        import sys
        from proofs._lib import VALUE
        ctx.check(VALUE + 1 == 42, 'helper imported', module=sys.modules['proofs.uses_lib'].__name__)
    """
    write_proof(env.pdir, "uses_lib", boxes=["F-L1"], body=body)
    assert run_main(env, "--all") == 0
    assert evidence(env, "F-L1")["steps"][0]["observed"] == {"module": "proofs.uses_lib"}
    assert not [m for m in sys.modules if m == "proofs" or m.startswith("proofs.")]  # nothing leaks out
    assert str(env.tmp) not in sys.path


# --- lifecycle -----------------------------------------------------------------------------------------


def test_the_runner_starts_the_engine_it_needs_and_brings_it_down_when_a_proof_raises(env):
    body = """
        ctx.step('engine up for the proof')
        raise RuntimeError('proof blew up')
    """
    write_proof(env.pdir, "needs_engine", boxes=["F-N1"], needs=["engine"], body=body)
    assert run_main(env, "--all") == 1
    names = env.tools.names()
    assert "nsup.ps1" in names and names[-1] == "nsproc.ps1 -Down"  # started, then stopped
    assert names.index("nsup.ps1") < names.index("nsproc.ps1 -Down")
    assert env.tools.up is False
    life = evidence(env, "F-N1")["lifecycle"]
    assert life["started_by_runner"] is True and life["pids_after_start"] == [4242, 4343]
    assert "nsproc_before" in life and "CLEAN" in life["nsproc_after"]


def test_the_runner_does_not_touch_an_engine_it_did_not_start(env):
    env.tools.up = True
    write_proof(env.pdir, "needs_engine", boxes=["F-N2"], needs=["engine"])
    assert run_main(env, "--all") == 0
    assert "nsup.ps1" not in env.tools.names() and "nsproc.ps1 -Down" not in env.tools.names()
    assert env.tools.up is True
    assert evidence(env, "F-N2")["lifecycle"]["started_by_runner"] is False


def test_no_engine_is_started_when_no_proof_needs_one(env):
    write_proof(env.pdir, "cli_only", boxes=["F-N3"])
    run_main(env, "--all")
    assert "nsup.ps1" not in env.tools.names() and "nsproc.ps1 -Down" not in env.tools.names()


def test_an_engine_that_does_not_start_fails_the_proofs_that_need_it_and_is_still_brought_down(env):
    sub = env.tmp / "x"
    sub.mkdir()
    bad = Tools(sub, nsup_rc=1)
    write_proof(
        env.pdir, "needs_engine", boxes=["F-N4"], needs=["engine"], body="raise AssertionError('ran')"
    )
    write_proof(env.pdir, "cli_only", boxes=["F-N5"])
    assert run_main(env, "--all", tools=bad) == 1
    ev = evidence(env, "F-N4")
    assert ev["result"] == "fail" and "engine did not start" in ev["reason"]
    assert evidence(env, "F-N5")["result"] == "pass"
    assert bad.names()[-1] == "nsproc.ps1 -Down"


def test_a_missing_nsup_tool_fails_engine_proofs_without_starting_anything(env):
    (env.tools.dir / "nsup.ps1").unlink()
    write_proof(env.pdir, "needs_engine", boxes=["F-N6"], needs=["engine"])
    assert run_main(env, "--all") == 1
    assert "nsup.ps1 not found" in evidence(env, "F-N6")["reason"]
    assert "nsproc.ps1 -Down" not in env.tools.names()


def test_spawned_helpers_are_killed_by_pid_even_when_the_proof_raises(env, monkeypatch):
    started, killed = [], []

    class FakeProc:
        def __init__(self, args, **kw):
            self.pid = 7000 + len(started)
            started.append((args, kw))

        def poll(self):
            return None

    monkeypatch.setattr(prove_ctx.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(prove_ctx, "kill_tree", lambda proc: killed.append(proc.pid))
    body = """
        ctx.spawn(['fake-proxy', '--port', '8899'], name='proxy')
        ctx.spawn(['watcher'])
        raise RuntimeError('after spawning')
    """
    write_proof(env.pdir, "spawner", boxes=["F-P1"], body=body)
    assert run_main(env, "--all") == 1
    assert killed == [7000, 7001]
    ev = evidence(env, "F-P1")
    assert [p["pid"] for p in ev["pids"]] == [7000, 7001] and ev["pids"][0]["name"] == "proxy"
    assert started[0][1]["env"]["NIGHTSHIFT_HOME"] == str(env.home)
    assert Path(started[0][1]["cwd"]).parts[-1] == "spawner"  # the proof's scratch folder
    assert ev["steps"][-1]["name"] == "runner stopped"


# --- the laptop lock -----------------------------------------------------------------------------------


def test_every_run_needs_the_callers_lock_wherever_the_home_is(env, capsys):
    home = make_home(env.tmp / "NightshiftDev3")  # a dev home that is not under installs\
    body = "ctx.step('env', mine=ctx._env.get('NS_LAPTOP_LOCK_MINE'))"
    write_proof(env.pdir, "p", boxes=["F-K1"], needs=["engine"], body=body)

    def go(dept: str) -> int:
        argv = ["--home", str(home), "--all", "--runs", str(env.runs), "--proofs-dir", str(env.pdir)]
        argv += ["--tools", str(env.tools.dir), "--lock-file", str(env.lock), "--dept", dept]
        opts = {"commit": "c", "ps": env.tools.ps, "probe": env.tools.probe}
        return prove.main(argv, runner_options={**opts, "dashboard_home": lambda info: str(home)})

    env.lock.unlink()
    assert go("qa") == 2  # no lock file: take it first
    assert "take" in capsys.readouterr().err
    env.lock.write_text("ns-packages | P10b render | 21:41\n", encoding="utf-8")
    assert go("qa") == 2  # somebody else's lock
    assert "held by 'ns-packages'" in capsys.readouterr().err
    assert evidence_files(env) == [] and env.tools.calls == []  # nothing was started, not even nsup
    env.lock.write_text("ns-qa | proofs | 22:00\n", encoding="utf-8")
    assert go("qa") == 0 and go("ns-qa") == 0  # the holder, with or without the prefix
    assert evidence(env, "F-K1")["steps"][0]["observed"] == {"mine": "1"}  # FFmpeg stays on its PATH


# --- boxes with several proofs, deadlines, interrupts ---------------------------------------------------


def test_two_proofs_of_one_box_share_its_file_and_a_failure_wins_in_either_order(env):
    fail = "ctx.check(False, 'broken', got=1)"
    write_proof(env.pdir, "a1", boxes=["F-X1"])  # passes, sorts first
    write_proof(env.pdir, "z1", boxes=["F-X1"], body=fail)  # fails, sorts last
    write_proof(env.pdir, "a2", boxes=["F-X2"], body=fail)  # fails, sorts first
    write_proof(env.pdir, "z2", boxes=["F-X2"])  # passes, sorts last
    assert run_main(env, "--all") == 1
    for box in ("F-X1", "F-X2"):
        ev = evidence(env, box)
        assert ev["result"] == "fail" and ev["reason"] == "broken"
        assert sorted(r["proof_id"] for r in ev["proofs"]) == [f"a{box[-1]}", f"z{box[-1]}"]
        assert {r["result"] for r in ev["proofs"]} == {"pass", "fail"}
    # running only the passing proof again does not wipe the failing proof's record
    assert run_main(env, "--proof", "a1") == 0
    ev = evidence(env, "F-X1")
    assert ev["result"] == "fail" and [r["proof_id"] for r in ev["proofs"]] == ["z1", "a1"]


def test_a_skip_never_replaces_an_earlier_result_of_the_same_proof(env):
    write_proof(env.pdir, "windowed", boxes=["F-W1"], window=True)
    assert run_main(env, "--all") == 0
    assert evidence(env, "F-W1")["result"] == "pass"
    assert run_main(env, "--all", "--no-window") == 0
    assert evidence(env, "F-W1")["result"] == "pass"  # the pass of this version stays on record


def test_a_proof_that_blocks_outside_ctx_fails_at_its_deadline_and_the_engine_still_comes_down(env):
    write_proof(
        env.pdir,
        "hang",
        boxes=["F-T1"],
        needs=["engine"],
        timeout_s=0.2,
        body="import time\ntime.sleep(2)",  # blocks without calling ctx, as a bare proc.wait() would
    )
    t0 = time.monotonic()
    assert run_main(env, "--all") == 1
    assert time.monotonic() - t0 < 1.5  # the runner did not wait for the sleep
    ev = evidence(env, "F-T1")
    assert ev["result"] == "fail" and "blocked outside ctx" in ev["reason"]
    assert env.tools.names()[-1] == "nsproc.ps1 -Down" and env.tools.up is False


def test_an_interrupt_keeps_the_evidence_of_finished_proofs_and_brings_the_engine_down(env, monkeypatch):
    write_proof(env.pdir, "first", boxes=["F-I1"], needs=["engine"])
    write_proof(env.pdir, "second", boxes=["F-I2"], needs=["engine"])
    real = prove.Runner._run_one

    def interrupt(self, proof, engine_error):
        if proof.id == "second":
            raise KeyboardInterrupt
        return real(self, proof, engine_error)

    monkeypatch.setattr(prove.Runner, "_run_one", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run_main(env, "--all")
    assert evidence(env, "F-I1")["result"] == "pass"
    assert evidence_files(env) == ["F-I1.json"]
    assert env.tools.names()[-1] == "nsproc.ps1 -Down" and env.tools.up is False


def test_the_engine_is_not_started_next_to_processes_the_runner_did_not_start(env, capsys):
    sub = env.tmp / "x"
    sub.mkdir()
    busy = Tools(sub, busy=True)
    write_proof(env.pdir, "needs_engine", boxes=["F-N7"], needs=["engine"])
    assert run_main(env, "--all", tools=busy) == 2
    assert "not start an engine next to them" in capsys.readouterr().err
    assert "nsup.ps1" not in busy.names() and "nsproc.ps1 -Down" not in busy.names()
    assert evidence_files(env) == []


# --- ctx -----------------------------------------------------------------------------------------------


def make_ctx(env, **kw) -> Ctx:
    scratch = env.tmp / "scratch"
    scratch.mkdir(exist_ok=True)
    info = prove_ctx.read_install(env.home)
    return Ctx(home=env.home, info=info, scratch=scratch, proof_id="t", env={"PATH": "x"}, **kw)


def test_read_install_gets_version_kind_and_ports_from_the_install(env):
    info = prove_ctx.read_install(env.home)
    assert (info.version, info.kind) == ("9.9.9", "dev")
    assert info.ports == {"dashboard": 8100, "supervisor": 8099, "tts": 8091}
    (env.home / "config" / "runtime.json").write_text(json.dumps({"supervisor_port": 9000}), encoding="utf-8")
    info = prove_ctx.read_install(env.home)
    assert (info.kind, info.ports["supervisor"]) == ("engine", 9000)


def test_cli_resolves_argv0_from_the_homes_own_venv_and_prints_nighthome_into_the_evidence(env, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, kw=kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="channel a [active]\n", stderr="")

    monkeypatch.setattr(prove_ctx.subprocess, "run", fake_run)
    ctx = make_ctx(env)
    done = ctx.cli(["studio", "status", "--channel", "a"], 30)
    assert done.stdout.startswith("channel a")
    studio = env.home / "venvs" / "studio" / "Scripts" / "studio.exe"
    assert seen["cmd"] == [str(studio), "status", "--channel", "a"]
    assert seen["kw"]["env"]["NIGHTSHIFT_HOME"] == str(env.home)
    ctx.cli(["ns", "doctor"], 30)
    assert seen["cmd"][0] == str(env.home / "venvs" / "supervisor" / "Scripts" / "ns.exe")
    step = ctx.steps[0]
    assert step["name"] == "cli studio status --channel a"
    assert step["observed"]["NIGHTSHIFT_HOME"] == str(env.home) and step["observed"]["exit_code"] == 0


@pytest.mark.parametrize(
    "argv", [["python", "-c", "1"], ["C:\\Windows\\cmd.exe"], ["ns.exe", "x"], [], ["ns/../x"]]
)
def test_cli_refuses_any_argv0_but_ns_and_studio(env, monkeypatch, argv):
    monkeypatch.setattr(prove_ctx.subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    with pytest.raises(ProofFailed) as err:
        make_ctx(env).cli(argv, 5)
    assert "ctx.cli" in str(err.value)


def test_cli_names_the_missing_program_when_the_venv_lacks_it(env):
    home = make_home(env.tmp / "other", cli=False)
    ctx = Ctx(home=home, info=prove_ctx.read_install(home), scratch=env.tmp, proof_id="t")
    with pytest.raises(ProofFailed, match="studio not found"):
        ctx.cli(["studio", "status"], 5)


def test_cli_timeout_is_a_failed_proof(env, monkeypatch):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(prove_ctx.subprocess, "run", slow)
    with pytest.raises(ProofFailed, match="did not finish"):
        make_ctx(env).cli(["ns", "doctor"], 5)


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status, self._body = status, body

    def read(self, n: int = -1) -> bytes:
        body, self._body = self._body, b""  # a second read finds the end of the answer
        return body

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        return None


def fake_opener(monkeypatch, answer):
    urls, handlers = [], []

    class Opener:
        def open(self, req, timeout=None):
            urls.append((req.get_method(), req.full_url, req.data, timeout))
            return answer(req)

    monkeypatch.setattr(prove_ctx.urllib.request, "build_opener", lambda *h: handlers.extend(h) or Opener())
    return urls, handlers


def test_api_talks_to_the_dashboard_port_without_a_proxy_and_parses_json_or_text(env, monkeypatch):
    def answer(req):
        return FakeResponse(200, b'{"ok": true}' if "json" in req.full_url else b"data: text")

    urls, handlers = fake_opener(monkeypatch, answer)
    ctx = make_ctx(env)
    assert ctx.api("GET", "/api/json") == (200, {"ok": True})
    assert ctx.api("POST", "/api/text", {"a": 1}) == (200, "data: text")
    assert urls[0][:2] == ("GET", "http://127.0.0.1:8100/api/json")
    assert urls[1][2] == b'{"a": 1}'
    assert any(getattr(h, "proxies", None) == {} for h in handlers)  # urllib.request.ProxyHandler({})
    assert [s["name"] for s in ctx.steps] == ["api GET /api/json", "api POST /api/text"]


def test_api_returns_an_error_status_and_fails_the_proof_when_nobody_answers(env, monkeypatch):
    def answer(req):
        if "gone" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b'{"error": "nope"}'))
        raise urllib.error.URLError("refused")

    fake_opener(monkeypatch, answer)
    ctx = make_ctx(env)
    assert ctx.api("GET", "/api/gone") == (404, {"error": "nope"})
    with pytest.raises(ProofFailed, match="no answer from the dashboard"):
        ctx.api("GET", "/api/other")


def test_api_fails_a_stream_that_keeps_sending_instead_of_reading_it_forever(env, monkeypatch):
    now = [0.0]

    class Endless(FakeResponse):
        def read1(self, n: int = -1) -> bytes:
            now[0] += 2  # a frame every 2 s keeps a per-read socket timeout from ever firing
            return b'data: {"kind": "status"}\n\n'

    fake_opener(monkeypatch, lambda req: Endless(200, b""))
    ctx = make_ctx(env, clock=lambda: now[0])
    with pytest.raises(ProofFailed, match="still sending after 5 s"):
        ctx.api("GET", "/api/events?limit=1", timeout=5)


def test_wait_polls_until_true_and_names_what_it_timed_out_on(env):
    now = [0.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        now[0] += s

    ctx = make_ctx(env, clock=lambda: now[0], sleep=sleep)
    calls = iter([None, 0, "ready"])
    assert ctx.wait(lambda: next(calls), 30, every=3, what="the job") == "ready"
    assert sleeps == [3, 3]
    with pytest.raises(ProofFailed, match="waiting for the queue to drain"):
        ctx.wait(lambda: False, 10, every=4, what="the queue to drain")


def test_a_proof_that_runs_past_timeout_s_fails_on_its_next_call(env):
    now = [0.0]
    ctx = make_ctx(env, clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s), timeout_s=10)
    with pytest.raises(ProofFailed, match="timed out after 10 s"):
        ctx.wait(lambda: False, 100, every=4, what="never")


def test_db_is_read_only_and_rows_answer_to_index_and_name(env):
    (env.home / "data").mkdir()
    con = sqlite3.connect(env.home / "data" / "studio.db")
    con.execute("create table outputs (channel text, status text)")
    con.execute("insert into outputs values ('a', 'pending')")
    con.commit()
    con.close()
    ctx = make_ctx(env)
    rows = ctx.db("select channel, status from outputs where channel = ?", ("a",))
    assert (rows[0][0], rows[0]["status"]) == ("a", "pending")
    with pytest.raises(sqlite3.OperationalError):
        ctx.db("insert into outputs values ('b', 'x')")


def test_check_and_fail_and_step_record_json_able_values(env):
    ctx = make_ctx(env)
    ctx.step("s", p=Path("x"), s={1, 2})
    with pytest.raises(ProofFailed):
        ctx.check(False, "no", obj=object)
    with pytest.raises(ProofFailed, match="stop here"):
        ctx.fail("stop here")
    json.dumps(ctx.steps)
    assert [s.get("ok") for s in ctx.steps] == [None, False, False]


def test_skip_ends_the_proof_with_the_reason(env):
    with pytest.raises(ProofSkipped, match="needs fake_gpu"):
        make_ctx(env).skip("needs fake_gpu")


def test_page_opens_edge_headed_at_the_dashboard_and_the_runner_closes_it(env):
    log = []

    class Page:
        def goto(self, url):
            log.append(("goto", url))

    class Browser:
        def new_page(self):
            return Page()

        def close(self):
            log.append("browser closed")

    class Chromium:
        def launch(self, **kw):
            log.append(("launch", kw))
            return Browser()

    class PW:
        chromium = Chromium()

        def stop(self):
            log.append("pw stopped")

    ctx = make_ctx(env, playwright_factory=lambda: SimpleNamespace(start=PW))
    ctx.page()
    assert log[:2] == [
        ("launch", {"channel": "msedge", "headless": False}),
        ("goto", "http://127.0.0.1:8100"),
    ]
    ctx.close()
    assert "browser closed" in log and "pw stopped" in log
    assert "browser" in ctx.left_on_screen and ctx.left_on_screen != "none"
    assert make_ctx(env).left_on_screen == "none"


def test_a_failing_helper_kill_does_not_stop_the_others(env, monkeypatch):
    killed = []

    def kill(proc):
        killed.append(proc.pid)
        if proc.pid == 1:
            raise OSError("access denied")

    monkeypatch.setattr(prove_ctx, "kill_tree", kill)
    ctx = make_ctx(env)
    ctx._procs = [SimpleNamespace(pid=1), SimpleNamespace(pid=2)]
    ctx.close()
    assert killed == [1, 2] and "kill failed" in json.dumps(ctx.steps)


# --- the two worked proofs, with stubs -----------------------------------------------------------------


def stub_cli(monkeypatch, out: str, code: int = 0, listing: str = "") -> list:
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        text = listing if cmd[1:3] == ["channel", "list"] else out
        return subprocess.CompletedProcess(cmd, code, stdout=text, stderr="")

    monkeypatch.setattr(prove_ctx.subprocess, "run", fake_run)
    return seen


def seed_channel(env, slug: str, rows: list[tuple[str, int]]) -> None:
    (env.home / "config" / "channels").mkdir(exist_ok=True)
    (env.home / "config" / "channels" / f"{slug}.yaml").write_text("slug: x\n", encoding="utf-8")
    (env.home / "data").mkdir(exist_ok=True)
    con = sqlite3.connect(env.home / "data" / "studio.db")
    con.execute("create table if not exists outputs (channel text, status text)")
    for status, n in rows:
        con.executemany("insert into outputs values (?, ?)", [(slug, status)] * n)
    con.commit()
    con.close()


def worked(name: str):
    importlib.invalidate_caches()
    return importlib.import_module(f"proofs.{name}")


def test_worked_cli_proof_passes_when_the_text_matches_the_files_and_rows(env, monkeypatch):
    seed_channel(env, "tales", [("approved", 2), ("pending", 1)])
    # the text is the product's own formatter's, so a wording change there breaks this test, not a real home
    st = ChannelStatus(slug="tales", status="active", counts={"approved": 2, "pending": 1})
    stub_cli(monkeypatch, "\n".join(format_status(st, "daily")) + "\n")
    ctx = make_ctx(env)
    worked("f_b18_studio_status").run(ctx)
    name = "channel tales: the printed outputs line equals the outputs table"
    assert any(s["name"] == name and s["ok"] for s in ctx.steps)
    assert ctx.steps[0]["name"] == "scope" and "launcher" in ctx.steps[0]["observed"]["not_covered"]


def test_worked_cli_proof_does_not_expect_a_profile_the_product_cannot_load(env, monkeypatch):
    seed_channel(env, "tales", [("approved", 1)])
    (env.home / "config" / "channels" / "broken.yaml").write_text("not: [valid\n", encoding="utf-8")
    st = ChannelStatus(slug="tales", status="active", counts={"approved": 1})
    listing = "tales\tactive\tshorts\tpairs=1\tTales\nbroken\tUNREADABLE\tbroken.yaml: bad\n"
    seen = stub_cli(monkeypatch, "\n".join(format_status(st)) + "\n", listing=listing)
    ctx = make_ctx(env)
    worked("f_b18_studio_status").run(ctx)  # `studio status` skips the broken file and prints the rest
    assert ["channel", "list"] in [c[1:3] for c in seen]
    listed = next(x for x in ctx.steps if x["name"] == "channel files on disk")
    assert listed["observed"]["unreadable_profiles"] == ["broken"]


def test_worked_cli_proof_fails_when_the_printed_counts_disagree_with_the_database(env, monkeypatch):
    seed_channel(env, "tales", [("approved", 2)])
    stub_cli(monkeypatch, "channel tales [active]\n  outputs: approved=5\n")
    with pytest.raises(ProofFailed, match="outputs line equals the outputs table"):
        worked("f_b18_studio_status").run(make_ctx(env))


def test_worked_cli_proof_on_a_home_without_channels_expects_the_no_channels_line(env, monkeypatch):
    stub_cli(monkeypatch, "no channels configured\n")
    worked("f_b18_studio_status").run(make_ctx(env))
    stub_cli(monkeypatch, "something else\n")
    with pytest.raises(ProofFailed, match="says so"):
        worked("f_b18_studio_status").run(make_ctx(env))


def test_worked_cli_proof_fails_on_a_nonzero_exit(env, monkeypatch):
    stub_cli(monkeypatch, "", code=1)
    with pytest.raises(ProofFailed, match="exits 0"):
        worked("f_b18_studio_status").run(make_ctx(env))


def dashboard_stub(monkeypatch, status: dict[str, Any], frame_kind: str = "status"):
    frame = "data: " + json.dumps({"kind": frame_kind, "payload": status}) + "\n\n"

    def answer(req):
        body = frame if "/api/events" in req.full_url else json.dumps(status)
        return FakeResponse(200, body.encode())

    return fake_opener(monkeypatch, answer)


def test_worked_dashboard_proof_passes_against_a_stub_dashboard(env, monkeypatch):
    seed_channel(env, "tales", [])
    status = {"version": "9.9.9", "engine": {"reachable": True}, "channels": [{"slug": "tales"}]}
    urls, _ = dashboard_stub(monkeypatch, status)
    stub_cli(monkeypatch, "", listing="tales\tactive\tshorts\tpairs=1\tTales\n")
    ctx = make_ctx(env)
    worked("f_c1_status_events").run(ctx)
    assert [u[1] for u in urls] == [
        "http://127.0.0.1:8100/api/status",
        "http://127.0.0.1:8100/api/events?limit=1",
        "http://127.0.0.1:8100/api/status",  # again: the frame's status is compared with it
    ]
    assert all(s.get("ok", True) for s in ctx.steps)


def test_worked_dashboard_proof_fails_on_a_wrong_version_a_dead_engine_or_a_ghost_channel(env, monkeypatch):
    good = {"version": "9.9.9", "engine": {"reachable": True}, "channels": []}
    for broken, match in (
        ({**good, "version": "0.0.1"}, "installed version"),
        ({**good, "engine": {"reachable": False}}, "engine is reachable"),
        ({**good, "channels": [{"slug": "ghost"}]}, "exactly the channels"),
    ):
        stub_cli(monkeypatch, "")
        dashboard_stub(monkeypatch, broken)
        with pytest.raises(ProofFailed, match=match):
            worked("f_c1_status_events").run(make_ctx(env))
    dashboard_stub(monkeypatch, good, frame_kind="job")
    with pytest.raises(ProofFailed, match="first frame is a status frame"):
        worked("f_c1_status_events").run(make_ctx(env))


def test_worked_dashboard_proof_fails_when_a_loadable_channel_is_missing_and_skips_an_unreadable_one(
    env, monkeypatch
):
    seed_channel(env, "tales", [])
    (env.home / "config" / "channels" / "broken.yaml").write_text("not: [valid\n", encoding="utf-8")
    listing = "tales\tactive\tshorts\tpairs=1\tTales\nbroken\tUNREADABLE\tbroken.yaml: bad\n"
    good = {"version": "9.9.9", "engine": {"reachable": True}, "channels": [{"slug": "tales"}]}
    stub_cli(monkeypatch, "", listing=listing)
    dashboard_stub(monkeypatch, good)
    worked("f_c1_status_events").run(make_ctx(env))  # the broken profile is not expected in the status
    dashboard_stub(monkeypatch, {**good, "channels": []})  # but the loadable one must be listed
    with pytest.raises(ProofFailed, match="exactly the channels"):
        worked("f_c1_status_events").run(make_ctx(env))


def test_the_worked_proofs_run_through_the_runner_with_stubs_and_leave_evidence(env, monkeypatch):
    """The real proofs/ folder, a fake home, stubs for the shell and HTTP: what QA sees, no install."""
    dashboard_stub(monkeypatch, {"version": "9.9.9", "engine": {"reachable": True}, "channels": []})
    stub_cli(monkeypatch, "no channels configured\n", listing="")
    env.pdir = prove.DEFAULT_PROOFS
    env.tools.up = True
    assert run_main(env, "--box", "F-B18", "F-C1") == 0
    assert evidence(env, "F-B18")["proof_id"] == "f-b18-studio-status"
    ev = evidence(env, "F-C1")
    assert (ev["result"], ev["proof_id"]) == ("pass", "f-c1-status-events")
    assert ev["steps"][-1]["ok"] is True


# --- review round: --dept, whose dashboard, shallow homes, page ------------------------------------------


def test_dept_is_required_to_run_and_a_missing_one_is_exit_2_with_a_plain_message(env, capsys):
    write_proof(env.pdir, "p", boxes=["F-D1"])
    argv = ["--home", str(env.home), "--all", "--runs", str(env.runs), "--proofs-dir", str(env.pdir)]
    argv += ["--tools", str(env.tools.dir), "--lock-file", str(env.lock)]  # QA holds the lock; no --dept
    opts = {"ps": env.tools.ps, "probe": env.tools.probe}
    assert prove.main(argv, runner_options=opts) == 2
    err = capsys.readouterr().err
    assert "--dept" in err and "required" in err
    assert evidence_files(env) == [] and env.tools.calls == []  # nothing started on QA's lock
    assert prove.main(["--list", "--proofs-dir", str(env.pdir)]) == 0  # --list needs neither home nor dept


def other_home(env) -> Path:
    return make_home(env.tmp / "other")


def test_a_proof_for_one_install_is_not_run_against_another_installs_dashboard_that_is_up(env, capsys):
    env.tools.up = True
    env.tools.reports = str(other_home(env))  # :8100 answers, but for installs\\dev, not for this home
    write_proof(
        env.pdir, "needs_engine", boxes=["F-O1"], needs=["engine"], body="raise AssertionError('ran')"
    )
    assert run_main(env, "--all") == 2
    err = capsys.readouterr().err
    assert "wrong install" in err and str(env.tools.reports) in err
    assert evidence_files(env) == []  # no pass (or any) evidence is written against the wrong install
    assert "nsproc.ps1 -Down" not in env.tools.names() and env.tools.up is True  # their engine is left alone


def test_a_dashboard_that_started_but_serves_another_home_is_refused_and_still_brought_down(env, capsys):
    env.tools.reports = str(other_home(env))
    write_proof(
        env.pdir, "needs_engine", boxes=["F-O2"], needs=["engine"], body="raise AssertionError('ran')"
    )
    assert run_main(env, "--all") == 2
    assert "wrong install" in capsys.readouterr().err
    assert env.tools.names()[-1] == "nsproc.ps1 -Down" and env.tools.up is False  # what the runner started
    assert evidence_files(env) == []


def test_a_dashboard_that_does_not_say_its_home_is_refused(env, capsys):
    env.tools.up = True
    write_proof(env.pdir, "needs_engine", boxes=["F-O3"], needs=["engine"])
    runs = prove.Runner(
        env.home,
        runs=env.runs,
        proofs_dir=env.pdir,
        tools=env.tools.dir,
        lock_path=env.lock,
        commit="c",
        ps=env.tools.ps,
        probe=env.tools.probe,
        dashboard_home=lambda info: None,
    )
    with pytest.raises(prove_ctx.SetupError, match="does not say which home"):
        runs.execute(prove.discover(env.pdir))


def test_the_default_dashboard_home_reads_disk_home_of_api_status(env, monkeypatch):
    home = "D:/NightShift/installs/dev"
    urls, _ = fake_opener(
        monkeypatch, lambda req: FakeResponse(200, json.dumps({"disk": {"home": home}}).encode())
    )
    info = prove_ctx.read_install(env.home)
    assert prove._default_dashboard_home(info) == home
    assert [u[1] for u in urls] == ["http://127.0.0.1:8100/api/status"]
    assert prove._same_folder("D:/NightShift/installs/DEV", "d:/nightshift/installs/dev")
    fake_opener(monkeypatch, lambda req: FakeResponse(200, b"{}"))
    assert prove._default_dashboard_home(info) is None  # a dashboard that does not say


def test_the_evidence_folder_default_does_not_crash_for_a_home_close_to_the_drive_root():
    assert prove.default_runs(Path("D:/NightShift/installs/dev")) == Path("D:/NightShift/runs").absolute()
    for shallow in (Path("D:/home"), Path("D:/")):
        assert prove.default_runs(shallow).name == "runs"  # a path, not an IndexError


def test_page_on_a_stopped_proof_opens_no_window(env):
    ctx = make_ctx(env, playwright_factory=lambda: pytest.fail("must not start"))
    ctx.abort()
    with pytest.raises(ProofFailed, match="stopped by the runner"):
        ctx.page()
