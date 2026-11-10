"""D-48 (b): every entry that starts a job, a model or the engine refuses with NS-W210 while an interrupted
update's repair record waits, whatever started the interpreter, and writes nothing.

The proof of block D started `python -m ns_studio.cli run` (the module, run by the environment's python,
not `studio.exe` and not the shim) on a dev install while the record waited, and it ran a job to
render_master: the old gate refused only a command that would start the engine, and on a dev install every
service is a fake. Each entry is started here exactly that way, as a real process.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from installer.stage2.web import venvrepair
from ns_common.config import save_install_state, save_runtime
from ns_common.models import InstallState, Runtime
from ns_common.paths import Paths

ROOT = Path(__file__).resolve().parents[1]
#: Written by the refusals themselves, by design (the engine's refusal is logged and left as one note).
TOLD = ("logs", "run/morning_note.json", "run/morning_note.lock", "run/morning_note.spool")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def pending_home(tmp_path: Path) -> tuple[Paths, Path]:
    """A dev install whose program is 0.2.0 and whose repair record waits for that program."""
    app = tmp_path / "app"
    app.mkdir()
    (app / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    (app / venvrepair.MARKER).write_text("fp-0.2.0", encoding="utf-8")
    paths = Paths(tmp_path / "home")
    save_install_state(paths, InstallState(version="0.2.0", app_path=str(app)))
    save_runtime(paths, Runtime.default("cpu-dev").model_copy(update={"dev_mode": True}))
    venvrepair.write(paths.home, app=app, for_app="fp-0.2.0", version="0.2.0", uv="uv", env={},
                     commands=[("studio", ["uv", "pip", "sync"]), ("supervisor", ["uv", "pip", "sync"])],
                     by="update")
    (tmp_path / "clip.mp4").write_bytes(b"not a video")
    (tmp_path / "job-args.json").write_text(json.dumps({"channel": "demo"}), encoding="utf-8")
    return paths, app


def snapshot(home: Path) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for p in home.rglob("*"):
        rel = p.relative_to(home).as_posix()
        if p.is_file() and not any(rel == t or rel.startswith(t + "/") for t in TOLD):
            st = p.stat()
            out[rel] = (st.st_size, st.st_mtime_ns)
    return out


def entries(tmp_path: Path) -> list[tuple[str, list[str]]]:
    """(name, python -m arguments) of every gated entry of the product (the report lists the others)."""
    clip, job_args = str(tmp_path / "clip.mp4"), str(tmp_path / "job-args.json")
    return [
        ("studio run", ["ns_studio.cli", "run", "--count", "1", "--fake-services"]),
        ("studio run --daily", ["ns_studio.cli", "run", "--daily"]),
        ("studio voice preview", ["ns_studio.cli", "voice", "preview", "narrator", "--fake-services"]),
        ("studio voice apply-pending", ["ns_studio.cli", "voice", "apply-pending", "--fake-services"]),
        ("studio calibrate", ["ns_studio.cli", "calibrate", "--channel", "demo", "--fake-services"]),
        ("studio series concepts", ["ns_studio.cli", "series", "concepts", "demo", "--fake-services"]),
        ("studio clip", ["ns_studio.cli", "clip", clip, "--channel", "demo", "--confirm-rights",
                         "--fake-services"]),
        ("clipper module", ["ns_studio.clipper.cli", clip, "--channel", "demo", "--confirm-rights"]),
        ("assistant module", ["ns_dashboard.assistant.cli", "ask", "what is waiting?", "--fake-services"]),
        ("dashboard job runner", ["ns_dashboard.studio_job", "run", "--args", job_args]),
        ("dashboard server", ["ns_dashboard.server", "--port", str(_free_port()), "--fake-services"]),
        ("ns tune", ["ns_supervisor.cli", "tune"]),
        ("ns verify", ["ns_supervisor.cli", "verify", "--quick"]),
        ("engine", ["ns_supervisor.app", "--headless", "--port", str(_free_port())]),
    ]


@pytest.mark.skipif(sys.platform != "win32", reason="the product's routes are Windows routes")
def test_every_job_or_engine_entry_refuses_through_python_m_and_writes_nothing(tmp_path: Path) -> None:
    paths, _app = pending_home(tmp_path)
    env = {**os.environ, "NIGHTSHIFT_HOME": str(paths.home), "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1",
           "NS_UNDER_TEST": "1"}
    failures: list[str] = []
    for name, args in entries(tmp_path):
        before = snapshot(paths.home)
        proc = subprocess.Popen(
            [sys.executable, "-m", *args], cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        )
        try:
            out, _ = proc.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            try:            # by its recorded pid, with the interpreter its venv launcher started
                family = [*psutil.Process(proc.pid).children(recursive=True), psutil.Process(proc.pid)]
            except psutil.Error:
                family = []
            for p in family:
                try:
                    p.kill()
                except psutil.Error:
                    pass
            out, _ = proc.communicate(timeout=30)
            failures.append(f"{name}: still running after 120 s (it did not refuse)")
            continue
        after = snapshot(paths.home)
        if "NS-W210" not in out or proc.returncode == 0:
            failures.append(f"{name}: exit {proc.returncode}, no NS-W210: {out.strip()[-300:]}")
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        if changed:
            failures.append(f"{name}: wrote {changed[:5]}")
        if "ns_dashboard.studio_job" in args and '"code": "NS-W210"' not in out:
            failures.append(f"{name}: no coded error event for the dashboard: {out.strip()[-200:]}")
    assert not failures, "\n".join(failures)
    assert venvrepair.record_path(paths.home).is_file(), "a refusal must leave the record for the repair"
