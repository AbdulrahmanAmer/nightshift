"""The fake-services home every proof script is run against in CI (D-43).

`proof_home` makes a dev-install-shaped home in a temporary folder with the product's own code (`Paths`,
`save_runtime`, `seed_channel`), puts the REAL dashboard app over it (`ns_dashboard.server.create_app` with
its fake services, listening on its own free loopback port), and runs a proof script through the REAL
runner (`dev.prove.Runner.execute`), so the result is the one `dev/prove.py --all` would count:

- `ctx.api` is the real `Ctx.api` (urllib over a real socket) against that app;
- `ctx.db` is the real read-only query on the home's `data/studio.db`;
- `ctx.cli` runs the `ns` and `studio` commands through typer's `CliRunner` in this process, with
  NIGHTSHIFT_HOME set (the real venv programs do not exist here);
- `ctx.spawn` is refused unless the test allows it. When allowed it starts a real child for the fake footage
  endpoint (a recorded pid, killed by the runner's `ctx.close()`), and answers the proof's second dashboard
  in-process: the same app state with the live key check, listening on its own free port;
- the job worker is driven from `ctx.wait`'s sleep (`worker.run_next()`), so a started job runs to its end on
  the dashboard's fake job service, which writes its outputs with the product's own outbox layout
  (`ns_dashboard/fakes.py:fake_outbox_path` -> `outbox_target`: the title is in the path, never the job id).

Nothing here reaches the Task Scheduler, the Desktop, the registry, the keyring (the dashboard gets
`FakeKeyring`) or the user's environment; no default product port (8099, 8100, 8080) is used.
"""

from __future__ import annotations

import dataclasses
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from typer.testing import CliRunner

import ns_common
from dev import prove
from dev.prove_ctx import Ctx, InstallInfo
from ns_common.config import save_runtime
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_dashboard.assistant.llm import ScriptedToolLlm, StaticProvider
from ns_dashboard.fakes import FakeSupervisor, seed_channel
from ns_dashboard.handlers import build_state
from ns_dashboard.server import create_app
from ns_dashboard.state import AppState
from ns_dashboard.supervisor_link import SupervisorLink

#: the variables a proof sets to point the product at its fake footage endpoint; the in-process dashboard
#: gets exactly these (a child process would have inherited them)
PROXY_VARIABLES = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE")


class _Server:
    """An ASGI app on its own free loopback port, in a thread; `stop` ends it and joins the thread."""

    def __init__(self, app: Any) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = int(self.sock.getsockname()[1])
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        self.thread = threading.Thread(
            target=self.server.run,
            kwargs={"sockets": [self.sock]},
            name=f"proofhome-{self.port}",
            daemon=True,
        )
        self.thread.start()
        end = time.monotonic() + 30
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > end:
                raise RuntimeError("the in-process dashboard did not start")
            time.sleep(0.02)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(30)
        self.sock.close()


class _InProcess:
    """What `ctx.spawn` returns for a helper that runs inside this process (the proof only polls it)."""

    returncode: int | None = None
    pid = 0

    def poll(self) -> int | None:
        return self.returncode


class HomeCtx(Ctx):
    """The real `Ctx`, with `cli` and `spawn` answered by the fake-services home (see the module doc)."""

    def __init__(self, owner: ProofHome, **kw: Any) -> None:
        kw["sleep"] = owner.sleep
        super().__init__(**kw)
        self.owner = owner

    def cli(self, argv: list[str], timeout: float = 120) -> subprocess.CompletedProcess[str]:
        from ns_studio.cli import app as studio_app
        from ns_supervisor.cli import app as ns_app

        programs = {"ns": ns_app, "studio": studio_app}
        if not argv or argv[0] not in programs:
            raise AssertionError(f"ctx.cli runs only ns or studio, not {argv[:1]}")
        result = CliRunner().invoke(programs[argv[0]], [str(a) for a in argv[1:]])
        try:
            err = result.stderr
        except ValueError:  # click before 8.2 mixes stderr into stdout unless asked not to
            err = ""
        self.step(f"cli {' '.join(argv)}", exit_code=result.exit_code, in_process=True)
        return subprocess.CompletedProcess(argv, result.exit_code, result.stdout, err)

    def spawn(self, args: list[str], env: dict[str, str] | None = None, name: str | None = None) -> Any:
        if not self.owner.allow_spawn:
            raise AssertionError(f"ctx.spawn is refused by the fake-services home: {args[:2]}")
        script = Path(str(args[1])).name if len(args) > 1 else ""
        if script == "footage.py" and str(args[0]) == sys.executable:
            return super().spawn(args, env, name)  # the fake endpoint: a real child, pid recorded
        if script == "dashboard_no_worker.py":
            return self.owner.start_proof_dashboard(self, args, env or {})
        raise AssertionError(f"ctx.spawn is refused by the fake-services home: {args[:2]}")

    def close(self) -> None:
        super().close()
        self.owner.stop_proof_dashboard()


class ProofHome:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        self.home = tmp_path / "home"
        self.allow_spawn = False
        self.paths = Paths(self.home)
        self.paths.ensure()
        save_runtime(self.paths, Runtime.default("cpu-dev").model_copy(update={"dev_mode": True}))
        (self.home / "app").mkdir(exist_ok=True)
        (self.home / "app" / "VERSION").write_text(ns_common.__version__ + "\n", encoding="utf-8")
        # the home's supervisor venv python: the proof only asks that it exists, `spawn` answers the launch
        python = self.home / "venvs" / "supervisor" / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True)
        python.write_text("", encoding="utf-8")
        static = tmp_path / "static"
        static.mkdir()
        (static / "index.html").write_text("<!doctype html><title>Nightshift</title>", encoding="utf-8")
        (static / "app.js").write_text("// ui", encoding="utf-8")
        self.static = static
        self.supervisor = FakeSupervisor().start()
        self.state: AppState = build_state(
            self.paths, fake_services=True, link=SupervisorLink(self.supervisor.url, timeout_s=2.0)
        )
        seed_channel(self.state.db, self.paths, "after-dark", format="master_first")
        self.server = _Server(self._app(self.state))
        self.proof_server: _Server | None = None

    # ---------------------------------------------------------------------------------- the apps
    def _app(self, state: AppState) -> Any:
        provider = StaticProvider(ScriptedToolLlm([]))
        return create_app(
            state=state, start_worker=False, static_dir=self.static, heartbeat_s=0.01, llm_provider=provider
        )

    def start_proof_dashboard(self, ctx: Ctx, args: list[str], env: dict[str, str]) -> _InProcess:
        """`proofs/_fakes/dashboard_no_worker.py` in this process: the same app state (so the same keys,
        `clips` table and cache), with the live key check the real dashboard has, on its own free port; the
        port goes to the file named by argv[1], as the script does."""
        from ns_dashboard.svc_settings import live_key_check

        for key in PROXY_VARIABLES:
            if key in env:
                self.monkeypatch.setenv(key, env[key])
        app = self._app(self.state)
        app.state.dashboard.settings.key_check = live_key_check
        self.proof_server = _Server(app)
        port_file = Path(args[2])
        tmp = port_file.with_name(port_file.name + ".tmp")
        tmp.write_text(str(self.proof_server.port), encoding="ascii")
        tmp.replace(port_file)
        ctx.step("spawn dashboard-proof (in this process)", port=self.proof_server.port)
        return _InProcess()

    def stop_proof_dashboard(self) -> None:
        if self.proof_server is not None:
            self.proof_server.stop()
            self.proof_server = None

    def sleep(self, seconds: float) -> None:
        """What `ctx.wait` does between two looks: run the queued jobs (the worker thread is not started),
        then wait a moment."""
        while self.state.worker.run_next():
            pass
        time.sleep(min(seconds, 0.2))

    # ---------------------------------------------------------------------------------- running a proof
    def run(self, stem: str) -> prove.Result:
        """`proofs/<stem>.py` through the real runner against this home; the runner's own `Result`."""
        found = [p for p in prove.discover() if p.stem == stem]
        assert len(found) == 1 and found[0].valid, (stem, [p.problems for p in found])
        home = self

        def make_ctx(**kw: Any) -> HomeCtx:
            info: InstallInfo = kw["info"]
            kw["info"] = dataclasses.replace(
                info,
                ports={**info.ports, "dashboard": home.server.port, "supervisor": home.supervisor_port},
            )
            return HomeCtx(home, **kw)

        self.monkeypatch.setattr(prove, "Ctx", make_ctx)
        runner = prove.Runner(
            self.home,
            runs=self.tmp / "runs",
            tools=self.tmp / "tools",
            lock_path=self.tmp / "LAPTOP.lock",
            commit="proofhome",
            ps=lambda script, args, timeout: (0, "CLEAN: the fake-services home starts nothing"),
            probe=lambda info: True,
            dashboard_home=lambda info: str(self.home),
            hard_grace_s=5,
        )
        return runner.execute(found)[0]

    @property
    def supervisor_port(self) -> int:
        return int(self.supervisor.url.rsplit(":", 1)[1])

    def close(self) -> None:
        self.stop_proof_dashboard()
        self.server.stop()  # its lifespan closes the databases
        self.supervisor.stop()


@pytest.fixture
def proof_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ProofHome]:
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path / "home"))
    ph = ProofHome(tmp_path, monkeypatch)
    try:
        yield ph
    finally:
        ph.close()

