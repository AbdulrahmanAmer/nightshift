"""``python -m installer.stage2.web``: the setup server (P1). Standard library only.

Exit codes: 0 done or closed, 10 a blocking check (headless), 11 answers.json unreadable, 12 a required tool
could not be installed, 13 Python could not be set up (stage 1), 14 the setup environment could not be built
or the server did not start, 130 stopped.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from installer.stage2.web import launch
from installer.stage2.web.controller import Config, Effects, SetupController
from installer.stage2.web.server import SetupServer
from installer.stage2.web.uninstall import UninstallApp

EXIT_OK = 0
EXIT_STOPPED = 130


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """The command line."""
    p = argparse.ArgumentParser(prog="python -m installer.stage2.web", description="Nightshift setup server")
    p.add_argument("--app-path", help="the extracted Nightshift folder (default: this checkout)")
    p.add_argument("--preflight", help="stage 1 hand-over JSON")
    p.add_argument("--answers", help="answers.json (prefilled, or the unattended answers with --headless)")
    p.add_argument("--home", help="install folder (default: NIGHTSHIFT_HOME, answers.json, stage 1's pick)")
    p.add_argument("--headless", action="store_true", help="no window: same steps, console output only")
    p.add_argument("--no-window", action="store_true", help="serve, but do not open a window (dev server)")
    p.add_argument("--uninstall", action="store_true", help="the uninstall window")
    p.add_argument("--simulate", action="store_true", help="CI mode: fake winget and uv, simulated downloads")
    p.add_argument("--fixture", help="serve one frozen screen state (design review); implies --simulate")
    p.add_argument("--port", type=int, default=0, help="listen on this port (default: a random free port)")
    p.add_argument("--halt-after", metavar="STEP", help="stop the wizard after this step (tests)")
    p.add_argument("--python", help="interpreter for the wizard in --simulate mode (default: this one)")
    return p.parse_args(list(argv) if argv is not None else None)


def say(text: str, level: str = "info") -> None:
    """One console line (the console window is the log)."""
    sys.stdout.write(text + "\n")      # the repo bans print() in installer code
    sys.stdout.flush()


def app_path_of(args: argparse.Namespace) -> Path:
    """The Nightshift folder."""
    return Path(args.app_path).resolve() if args.app_path else Path(__file__).resolve().parents[3]


ACTIVE_RUNS = ("running", "pausing", "paused", "waiting")


def watch_window(
    handle: launch.WindowHandle,
    controller: SetupController,
    fallback: Callable[[], None] | None = None,
) -> None:
    """Tell the console when the window closes while setup goes on, and replace a host that died.

    Exit 0 is a closed window. Any other exit while setup is unfinished (the host crashed, or exit 3 after a
    late WebView2 failure) opens the browser window instead, so setup never has a server and no window.
    """
    proc = handle.proc
    if proc is None:
        return

    def wait() -> None:
        code = proc.wait()
        run = controller.snapshot()["run"]
        if code != 0 and handle.host == "webview2" and run != "finished" and fallback is not None:
            say(f"{time.strftime('%H:%M:%S')}  The setup window stopped (exit code {code}); "
                "opening it in the browser instead.")
            fallback()
        elif run in ACTIVE_RUNS:
            say(f"{time.strftime('%H:%M:%S')}  The setup window was closed. Setup keeps running. "
                "Run INSTALL.cmd to open it again.")
        elif run == "stopped":
            say(f"{time.strftime('%H:%M:%S')}  The setup window was closed after Stop. "
                "Run INSTALL.cmd to open it again, or close this window to end setup.")

    threading.Thread(target=wait, name="window-watch", daemon=True).start()


def run_ui(args: argparse.Namespace, cfg: Config) -> int:
    """Serve the window and wait for it to say close-setup."""
    if cfg.fixture:
        from installer.stage2.web.fixtures import FixtureController

        controller: SetupController | FixtureController = FixtureController(cfg.fixture, cfg.app_path)
    else:
        fx = Effects(console=say)
        if cfg.simulate:
            from installer.stage2.web.fakes import fake_effects

            fx, _cmd = fake_effects(cfg.data_dir / "simulated")
            fx.console = say
        controller = SetupController(cfg, fx)
    holder: dict[str, launch.WindowHandle] = {}

    def reopen(skip_host: bool = False) -> None:
        handle = launch.open_window(server.url, app_path=cfg.app_path, skip_host=skip_host)
        holder["h"] = handle
        if isinstance(controller, SetupController):
            controller.set_host(handle.host)
            watch_window(handle, controller, lambda: reopen(skip_host=True))

    server = SetupServer(
        controller, static_dir=cfg.app_path / "installer" / "ui" / "static", port=cfg.port,
        version=controller.version, on_open_window=reopen,
    )
    folder = launch.data_dir()
    launch.write_server_files(folder, pid=server.pid, port=server.port, token=server.token,
                              app_path=str(cfg.app_path), url=server.url)
    server.start()
    say(f"Setup address: {server.url}")
    if not cfg.no_window:
        handle = launch.open_window(server.url, app_path=cfg.app_path)
        holder["h"] = handle
        if isinstance(controller, SetupController):
            controller.set_host(handle.host)
            watch_window(handle, controller, lambda: reopen(skip_host=True))
        launch.update_server_info(folder, host=handle.host, host_text=launch.host_label(handle))
        say("Setup window: " + launch.host_label(handle))
    try:
        while not controller.shutdown.wait(0.5):
            pass
    except KeyboardInterrupt:
        say("Setup paused. Run INSTALL.cmd again to carry on where it stopped.")
        code = EXIT_STOPPED
    else:
        code = _exit_code(controller)
    server.stop()
    launch.remove_server_files(folder)
    finished = isinstance(controller, SetupController) and controller.snapshot()["run"] == "finished"
    if code == EXIT_OK and finished:
        say("Setup is done. You can close this window.")
    return code


def _exit_code(controller: object) -> int:
    if isinstance(controller, SetupController):
        run = controller.snapshot()["run"]
        return EXIT_STOPPED if run == "stopped" else EXIT_OK
    return EXIT_OK


def run_uninstall(args: argparse.Namespace, cfg: Config) -> int:
    """The uninstall window."""
    app = UninstallApp(cfg.app_path, cfg.home, log=say)
    server = SetupServer(None, static_dir=cfg.app_path / "installer" / "ui" / "static", port=cfg.port,
                         uninstall=app)
    folder = launch.data_dir()
    launch.write_server_files(folder, pid=server.pid, port=server.port, token=server.token,
                              app_path=str(cfg.app_path), url=server.url)
    server.start()
    say(f"Setup address: {server.url}")
    if not cfg.no_window:
        handle = launch.open_window(server.url, app_path=cfg.app_path)
        say("Setup window: " + launch.host_label(handle))
    try:
        while not app.shutdown.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    server.stop()
    launch.remove_server_files(folder)
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args = parse_args(argv)
    app = app_path_of(args)
    cfg = Config(
        app_path=app,
        data_dir=launch.data_dir(),
        preflight=Path(args.preflight) if args.preflight else None,
        answers=Path(args.answers) if args.answers else None,
        home=Path(args.home) if args.home else None,
        headless=args.headless,
        simulate=bool(args.simulate or args.fixture),
        no_window=args.no_window,
        port=args.port,
        fixture=args.fixture,
        uninstall=args.uninstall,
        halt_after=args.halt_after,
    )
    if args.python:
        cfg.python = args.python
    if args.uninstall:
        return run_uninstall(args, cfg)
    if cfg.headless:
        try:
            controller = SetupController(cfg, _headless_effects(cfg))
        except FileNotFoundError as e:
            say(str(e))
            return 11
        return controller.run_headless()
    return run_ui(args, cfg)


def _headless_effects(cfg: Config) -> Effects:
    if cfg.simulate:
        from installer.stage2.web.fakes import fake_effects

        fx, _cmd = fake_effects(cfg.data_dir / "simulated")
        fx.console = say
        return fx
    return Effects(console=say)


if __name__ == "__main__":
    sys.exit(main())
