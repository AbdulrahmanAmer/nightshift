"""A program Windows refuses to start (Smart App Control, Application Control, antivirus) is NS-E113.

Before this, WinError 4551 from ``Popen`` ended as NS-E202 ("a service failed") and, further on, as "the story
model did not pass" (NS-E203) or a quiet switch to the 7 GB PyTorch voice engine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from installer.stage2.runner import SubprocessRunner
from installer.stage2.steps import s08_smoke
from installer.stage2.testing import example_answers, make_test_context
from ns_common import avblock
from ns_common.avblock import blocked_hint, is_blocked_error, looks_blocked
from ns_common.errors import NsError
from ns_supervisor import procs
from ns_supervisor.services import ServiceDef
from ns_supervisor.testing import free_port, stub_def


def os_error(winerror: int, text: str = "blocked") -> OSError:
    err = OSError(22, text)
    err.winerror = winerror  # type: ignore[attr-defined]
    return err


@pytest.mark.parametrize("code", [225, 226, 1260, 4551])
def test_the_blocking_winerrors_are_recognised(code: int) -> None:
    assert is_blocked_error(os_error(code))
    assert code in avblock.BLOCK_WINERRORS


@pytest.mark.parametrize("code", [2, 5, 193, 1223, 740])
def test_other_winerrors_are_not(code: int) -> None:
    assert not is_blocked_error(os_error(code, "something else"))


@pytest.mark.parametrize(
    "text",
    [
        "[WinError 4551] An Application Control policy has blocked this file",
        "OSError: [WinError 225] Operation did not complete successfully because the file contains a virus "
        "or potentially unwanted software",
        "[WinError 226] The file was removed because it contained a virus",
        "[WinError 1260] This program is blocked by group policy",
        "error: os error 4551",
        "Smart App Control blocked llama-server.exe",
    ],
)
def test_the_wording_is_recognised(text: str) -> None:
    assert looks_blocked(text) and is_blocked_error(OSError(text))


def test_ordinary_failures_are_not_blocks() -> None:
    assert not looks_blocked("connection reset by peer")
    assert not looks_blocked("[WinError 193] %1 is not a valid Win32 application")
    assert not is_blocked_error(ValueError("virus"))


def test_the_hint_names_the_program_and_where_to_allow_it() -> None:
    hint = blocked_hint(r"C:\Users\Sam\Nightshift\bin\llama\llama-server.exe")
    assert "llama-server.exe" in hint and "Sam" not in hint
    assert blocked_hint("C:/x/qwen-tts.exe").startswith("Windows blocked qwen-tts.exe")
    assert "Allow it in Windows Security > App & browser control, then press Retry" in hint


def test_a_supervisor_start_blocked_by_smart_app_control_is_ns_e113(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def blocked(*args: Any, **kwargs: Any) -> None:
        raise os_error(4551, "An Application Control policy has blocked this file")

    monkeypatch.setattr(procs.subprocess, "Popen", blocked)
    d: ServiceDef = stub_def("llama", free_port())
    with pytest.raises(NsError) as ei:
        procs.start(d, tmp_path / "logs")
    assert ei.value.code == "NS-E113"
    assert "Windows Security > App & browser control" in ei.value.detail
    assert Path(d.cmd[0]).name in ei.value.detail


def test_a_supervisor_start_that_fails_otherwise_stays_ns_e202(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError(2, "no such file")

    monkeypatch.setattr(procs.subprocess, "Popen", missing)
    with pytest.raises(NsError) as ei:
        procs.start(stub_def("llama", free_port()), tmp_path / "logs")
    assert ei.value.code == "NS-E202"


def test_the_installer_runner_turns_a_blocked_launch_into_a_result_that_classifies_as_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import installer.stage2.runner as runner_mod

    def blocked(*args: Any, **kwargs: Any) -> None:
        raise os_error(4551, "An Application Control policy has blocked this file")

    monkeypatch.setattr(runner_mod.subprocess, "run", blocked)
    res = SubprocessRunner().run([r"D:\Nightshift\bin\uv\uv.exe", "--version"])
    assert res.returncode == 126 and not res.ok
    assert looks_blocked(res.text) and "uv.exe" in res.text


def test_the_installer_runner_still_raises_other_os_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import installer.stage2.runner as runner_mod

    def denied(*args: Any, **kwargs: Any) -> None:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(runner_mod.subprocess, "run", denied)
    with pytest.raises(PermissionError):
        SubprocessRunner().run(["x.exe"])


def test_a_blocked_check_in_the_verify_report_is_found_by_the_smoke_step() -> None:
    rows = [
        {"name": "supervisor.status", "passed": True, "log": None},
        {"name": "llama.start", "passed": False,
         "log": "NS-E113: Antivirus or another program blocked a file (Windows blocked llama-server.exe)"},
        {"name": "tts.start", "passed": False, "log": "NS-W301: engine fell back"},
    ]
    assert "llama.start" in s08_smoke.blocked_rows(rows)
    assert s08_smoke.blocked_rows(rows[:1]) == "" and s08_smoke.blocked_rows(None) == ""
    assert s08_smoke.blocked_rows([{"name": "x", "passed": True, "log": "virus scan clean"}]) == ""


def test_the_smoke_step_raises_ns_e113_for_a_blocked_program_before_ns_e203(tmp_path: Path) -> None:
    from installer.stage2.runner import RunResult

    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, simulate=True)
    report = (
        '{"checks": [{"name": "llama.start", "passed": false, "log": "NS-E113: [WinError 4551] '
        'An Application Control policy has blocked this file"}]}'
    )
    rows = s08_smoke.load_report_rows(ctx, RunResult(1, report, ""))
    assert rows is not None and s08_smoke.blocked_rows(rows).startswith("llama.start")
