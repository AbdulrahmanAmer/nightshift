"""C1: every uv call uses a cache on the install drive, and a uv failure shows uv's own last lines."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from installer.stage2 import errors as install_errors
from installer.stage2.runner import RunResult
from installer.stage2.simulate import FakeRunner
from installer.stage2.steps import s04_venvs
from installer.stage2.steps.s04_venvs import S04Venvs
from installer.stage2.testing import example_answers, make_test_context
from ns_common import uv as ns_uv
from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_supervisor import update


def ctx_for(tmp_path: Path, runner: FakeRunner):
    return make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, runner=runner)


def fail_with(text: str, code: int = 2) -> RunResult:
    return RunResult(code, "", text)


# ---- the cache -----------------------------------------------------------------------------------
def test_uv_env_puts_the_cache_under_the_home_folder(tmp_path: Path):
    env = ns_uv.uv_env(tmp_path / "home")
    assert env["UV_LINK_MODE"] == "copy"
    assert env["UV_CACHE_DIR"] == str(tmp_path / "home" / "cache" / "uv")
    assert (tmp_path / "home" / "cache" / "uv").is_dir()


def test_uv_env_leaves_the_cache_out_when_the_folder_cannot_be_made(tmp_path: Path):
    blocker = tmp_path / "home"
    blocker.write_text("a file where the home folder should be", encoding="utf-8")
    assert ns_uv.uv_env(blocker) == {"UV_LINK_MODE": "copy"}


def test_every_uv_call_of_s04_carries_the_cache_dir(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop")
    ctx = ctx_for(tmp_path, runner)
    S04Venvs().run(ctx)
    calls = [c for c in runner.calls if c.tag.startswith("uv ")]
    assert len(calls) >= 6
    cache = str(ctx.paths.home / "cache" / "uv")
    assert all(c.env.get("UV_CACHE_DIR") == cache and c.env.get("UV_LINK_MODE") == "copy" for c in calls)


def test_the_update_resync_uses_the_same_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    seen: list[dict] = []

    def fake_run(argv, **kw):
        seen.append(kw.get("env") or {})
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    rc, _ = update._run(["uv", "pip", "list"], ns_uv.uv_env(tmp_path / "home"))
    assert rc == 0 and seen[0]["UV_CACHE_DIR"] == str(tmp_path / "home" / "cache" / "uv")


def test_the_resync_default_runner_passes_the_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    paths = Paths(tmp_path / "home")
    (paths.venvs / "tts" / "Scripts").mkdir(parents=True)
    (paths.venvs / "tts" / "Scripts" / "python.exe").write_bytes(b"")
    envs: list[dict] = []

    def fake_run(argv, **kw):
        envs.append(kw.get("env") or {})
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    update.resync_venvs(tmp_path, paths, uv="uv", free=lambda _venv: [])
    assert envs and all(e.get("UV_CACHE_DIR") == str(paths.home / "cache" / "uv") for e in envs)


# ---- the detail ----------------------------------------------------------------------------------
def test_a_failed_uv_call_carries_the_last_fifteen_lines(tmp_path: Path):
    noise = "\n".join(f"line {i}" for i in range(1, 41))
    runner = FakeRunner(
        tmp_path / "desktop", results={"uv pip install studio app": fail_with(noise + "\nerror: boom")}
    )
    ctx = ctx_for(tmp_path, runner)
    with pytest.raises(NsError) as ei:
        S04Venvs().run(ctx)
    err = ei.value
    assert err.code == "NS-E105" and "uv said:" in err.detail and "exit 2" in err.detail
    assert "error: boom" in err.detail and "line 40" in err.detail
    assert "line 26" not in err.detail and "line 27" in err.detail      # 15 lines: boom + line 27..40


def test_the_uv_output_is_in_the_log_too(tmp_path: Path):
    runner = FakeRunner(tmp_path / "desktop", results={"uv venv studio": fail_with("error: nope")})
    ctx = ctx_for(tmp_path, runner)
    with pytest.raises(NsError):
        S04Venvs().run(ctx)
    logs = "".join(p.read_text(encoding="utf-8") for p in ctx.paths.logs.glob("installer-*.jsonl"))
    assert "error: nope" in logs


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("error: failed to write cache\n  Caused by: There is not enough space on the disk. (os error 112)",
         "NS-E104"),
        ("error: No space left on device (os error 28)", "NS-E104"),
        ("error: [WinError 4551] An Application Control policy has blocked this file", "NS-E113"),
        ("error: Failed to fetch https://pypi.org/simple/numpy/", "NS-E105"),
    ],
)
def test_uv_errors_are_classified(text: str, code: str):
    err = install_errors.uv_error("installing x failed", fail_with(text))
    assert err.code == code and text.splitlines()[-1].strip() in err.detail


def test_a_uv_call_with_no_output_still_names_the_exit_code():
    err = install_errors.uv_error("uv venv studio failed", RunResult(3))
    assert err.code == "NS-E105" and err.detail == "uv venv studio failed (exit 3)"


def test_the_bootstrap_classifier_knows_a_full_disk():
    from installer.stage2.web.bootstrap import classify

    assert classify("os error 112", "NS-E105") == "NS-E104"
    assert classify("os error 112", "NS-E114") == "NS-E114"
    assert classify("virus", "NS-E105") == "NS-E113"


# ---- C: free space when the cache cannot be on the install drive -----------------------------------
def test_s04_checks_the_system_drive_only_when_the_cache_cannot_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = ctx_for(tmp_path, FakeRunner(tmp_path / "desktop"))
    ctx.free_gb_fn = lambda _p: 1.0
    s04_venvs.check_cache_space(ctx)                         # the cache is on the install drive: no check
    monkeypatch.setattr(s04_venvs, "uv_env", lambda home: {"UV_LINK_MODE": "copy"})
    with pytest.raises(NsError) as ei:
        s04_venvs.check_cache_space(ctx)
    assert ei.value.code == "NS-E104" and "1 GB free" in ei.value.detail
    ctx.free_gb_fn = lambda _p: 50.0
    s04_venvs.check_cache_space(ctx)
