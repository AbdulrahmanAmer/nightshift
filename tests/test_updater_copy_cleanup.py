r"""Pre-tag (final-engine row 2): the update worker deletes its own private venv copy after a relaunch.

The copy lives in `<home>\run\updater\venv-<stamp>` (150-300 MB). A process cannot delete its own
interpreter, so a detached PowerShell waits for the worker to exit and removes the folder.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_supervisor import update_job, updater_copy


def paths_in(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "ns")


def copy_python(paths: Paths, stamp: str = "20261103-030000") -> Path:
    exe = updater_copy.updater_root(paths) / f"venv-{stamp}" / "Scripts" / "python.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"x")
    return exe


def decode(argv: list[str]) -> str:
    assert argv[0] == "powershell.exe" and argv[argv.index("-EncodedCommand") - 1] == "Hidden"
    return base64.b64decode(argv[-1]).decode("utf-16-le")


# ---------------------------------------------------------------- which folder is mine


def test_own_copy_is_the_venv_folder_the_interpreter_runs_from(tmp_path: Path):
    paths = paths_in(tmp_path)
    exe = copy_python(paths)
    assert updater_copy.own_copy(paths, str(exe)) == updater_copy.updater_root(paths) / "venv-20261103-030000"


def test_a_worker_that_runs_from_the_real_venv_has_nothing_to_delete(tmp_path: Path):
    paths = paths_in(tmp_path)
    real = paths.venvs / "supervisor" / "Scripts" / "python.exe"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"x")
    assert updater_copy.own_copy(paths, str(real)) is None
    assert updater_copy.own_copy(paths, str(tmp_path / "elsewhere" / "python.exe")) is None
    # a folder under run\updater that is not a venv copy is not touched either
    other = updater_copy.updater_root(paths) / "notes" / "python.exe"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"x")
    assert updater_copy.own_copy(paths, str(other)) is None


# ---------------------------------------------------------------- the helper


def test_the_helper_waits_for_the_worker_then_removes_the_folder(tmp_path: Path):
    paths = paths_in(tmp_path)
    paths.home.mkdir(parents=True, exist_ok=True)
    exe = copy_python(paths)
    started: list[tuple[list[str], dict[str, Any]]] = []

    def spawn(argv: list[str], **kw: Any) -> int:
        started.append((argv, kw))
        return 4242

    folder = updater_copy.discard_own_copy(paths, spawn=spawn, pid=777, python=str(exe))
    assert folder == updater_copy.updater_root(paths) / "venv-20261103-030000"
    (argv, kw), = started
    script = decode(argv)
    assert "Wait-Process -Id 777" in script  # it must not run while the worker still holds python.exe
    assert str(folder) in script and "Remove-Item -LiteralPath $t -Recurse -Force" in script
    assert kw["cwd"] == paths.home


def test_a_quote_in_the_path_cannot_break_out_of_the_script():
    script = updater_copy.cleanup_script(Path("D:/José's ns/run/updater/venv-1"), 5)
    assert "José''s ns" in script


def test_nothing_is_started_when_the_worker_has_no_copy(tmp_path: Path):
    paths = paths_in(tmp_path)

    def spawn(argv: list[str], **kw: Any) -> int:
        raise AssertionError("no helper wanted")

    assert updater_copy.discard_own_copy(paths, spawn=spawn, python=str(tmp_path / "python.exe")) is None


def test_a_helper_that_cannot_start_is_not_fatal(tmp_path: Path):
    paths = paths_in(tmp_path)
    exe = copy_python(paths)

    def spawn(argv: list[str], **kw: Any) -> int:
        raise OSError("no")

    assert updater_copy.discard_own_copy(paths, spawn=spawn, python=str(exe)) is None


# ---------------------------------------------------------------- when the worker calls it


class Engine:
    """A `Relauncher` that comes back, or does not."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok

    def observe(self) -> None:
        return None

    def restart(self) -> None:
        if not self.ok:
            raise NsError("NS-E702", "the engine did not answer")


def test_cleanup_runs_after_the_done_state_is_written_and_the_engine_is_back(tmp_path: Path):
    paths = paths_in(tmp_path)
    seen: list[str] = []
    out = update_job.Outcome("Now on 0.1.1 (was 0.1.0).", "0.1.0", "0.1.1")
    code = update_job.run_worker(
        paths, lambda: out, relaunch=Engine(),
        cleanup=lambda: seen.append(update_job.read_status(paths)["state"]),
    )
    assert code == 0 and seen == ["done"]  # the page already has its answer when the helper is started


def test_cleanup_runs_after_a_rollback_whose_old_version_came_back(tmp_path: Path):
    paths = paths_in(tmp_path)
    seen: list[str] = []

    def boom() -> update_job.Outcome:
        raise NsError("NS-E702", "swap failed")

    code = update_job.run_worker(paths, boom, relaunch=Engine(), cleanup=lambda: seen.append("cleaned"))
    assert code == 1 and seen == ["cleaned"] and update_job.read_status(paths)["state"] == "failed"


def test_no_cleanup_when_the_engine_did_not_come_back(tmp_path: Path):
    """The copy may still be needed to look at what happened: the next update removes it."""
    paths = paths_in(tmp_path)
    seen: list[str] = []
    out = update_job.Outcome("Now on 0.1.1 (was 0.1.0).", "0.1.0", "0.1.1")
    run = update_job.run_worker
    assert run(paths, lambda: out, relaunch=Engine(ok=False), cleanup=lambda: seen.append("x")) == 1
    assert seen == []


def test_a_failing_cleanup_does_not_change_the_result(tmp_path: Path):
    paths = paths_in(tmp_path)

    def broken() -> None:
        raise RuntimeError("no powershell")

    out = update_job.Outcome("Now on 0.1.1 (was 0.1.0).", "0.1.0", "0.1.1")
    assert update_job.run_worker(paths, lambda: out, relaunch=Engine(), cleanup=broken) == 0
    assert update_job.read_status(paths)["state"] == "done"


def test_the_update_command_hands_the_worker_a_cleanup():
    src = (Path(__file__).resolve().parents[1] / "ns_supervisor" / "cli.py").read_text(encoding="utf-8")
    assert "cleanup=discard_copy" in src
    assert "updater_copy.discard_own_copy(paths, spawn=update_job.spawn_detached)" in src
