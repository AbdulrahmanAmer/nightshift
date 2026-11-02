"""C2 `ns` console tool (spec 15.9). Thin wiring: each command delegates to its own module."""

import json
from pathlib import Path
from typing import Annotated

import typer

from ns_common import __version__
from ns_common.config import load_runtime
from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_supervisor import backup, control, doctor, notify, releases, schedule, tune, update, verify
from ns_supervisor.client import SupervisorClient, ensure_running
from ns_supervisor.schedule_cfg import read_run_time

app = typer.Typer(help="Nightshift runtime tools", add_completion=False)
schedule_app = typer.Typer(help="The nightly scheduled task 'Nightshift Daily'", add_completion=False)
app.add_typer(schedule_app, name="schedule")


def _die(e: NsError | str, code: int = 1) -> typer.Exit:
    """Print an error (with its fix hint) and return the exit to raise."""
    if isinstance(e, NsError):
        typer.echo(f"{e}", err=True)
        typer.echo(f"Hint: {e.definition.hint}", err=True)
    else:
        typer.echo(e, err=True)
    return typer.Exit(code)


def _client(paths: Paths) -> SupervisorClient:
    return SupervisorClient.from_runtime(control.runtime_or_default(paths))


@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(f"ns {__version__}")


@app.command("stop")
def cmd_stop() -> None:
    """Stop the engine and every model process (what 'Nightshift - Stop All' runs)."""
    paths = Paths.from_env()
    for line in control.stop(paths, _client(paths)):
        typer.echo(line)


@app.command("doctor")
def cmd_doctor(
    bundle: Annotated[bool, typer.Option("--bundle", help="Write the diagnostics zip.")] = False,
    out_dir: Annotated[Path | None, typer.Option("--out-dir", hidden=True)] = None,
) -> None:
    """Check the install; with --bundle, collect logs and reports into a zip to send back."""
    paths = Paths.from_env()
    if bundle:
        out = doctor.build_bundle(paths, out_dir=out_dir, app=control.app_path(paths))
        typer.echo(f"Diagnostics written to {out}")
        return
    bad = 0
    for name, ok, detail in doctor.diagnose(paths, _client(paths)):
        typer.echo(f"[{'ok' if ok else 'FAIL'}] {name}: {detail}")
        bad += 0 if ok else 1
    if bad:
        raise typer.Exit(1)


def _stop_started_engine(client: SupervisorClient) -> None:
    """Shut down the headless engine `ns verify` started itself, so it does not linger (auto-exit is 30 min).

    A headless engine never starts the dashboard service; leaving it behind made the Dashboard shortcut
    find a running engine and open a page that was never served. An engine that was already up is left alone.
    """
    try:
        if client.is_up():
            client.shutdown(30)
    except NsError as e:
        typer.echo(f"Could not stop the verify engine: {e}", err=True)


@app.command("verify")
def cmd_verify(
    quick: Annotated[bool, typer.Option("--quick", help="Health, one completion, one voice only.")] = False,
) -> None:
    """Check every service and benchmark them; writes logs\\verify-report.json."""
    paths = Paths.from_env()
    rt = control.runtime_or_default(paths)
    client = _client(paths)
    started_here = not client.is_up()
    try:
        ensure_running(client, paths, headless=True)
    except NsError as e:
        typer.echo(f"Could not start the engine: {e}", err=True)
    try:
        report = verify.run_verify(paths, rt, client, quick=quick)
    finally:
        if started_here:
            _stop_started_engine(client)
    f = verify.write_report(paths, report)
    for line in verify.format_lines(report):
        typer.echo(line)
    typer.echo(f"Report: {f}")
    if not report.passed:
        raise typer.Exit(1)


@app.command("tune")
def cmd_tune(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the sweep matrix and exit.")] = False,
) -> None:
    """Find the fastest llama-server flags for this PC and save them to runtime.json."""
    paths = Paths.from_env()
    try:
        rt = load_runtime(paths)
    except NsError as e:
        raise _die(e) from e
    if dry_run:
        for line in tune.matrix_lines(rt):
            typer.echo(line)
        return
    if not rt.llama.model_path:
        raise _die("runtime.json has no llama model_path; run the installer first.")
    with tune.gpu_reservation(_client(paths)):
        outcome = tune.tune(paths, rt, tune.ProcessRunner(paths))
    for w in outcome.warnings:
        typer.echo(f"warning: {w}")
    if not outcome.ok or outcome.flags is None:
        raise _die("tune: no configuration loaded; runtime.json was left unchanged. See logs\\tune-*.json.")
    typer.echo(f"Best: {outcome.tok_s:.1f} tok/s with {outcome.flags.model_dump_json()}")
    typer.echo("Saved to runtime.json.")


@app.command("gate")
def cmd_gate(milestone: Annotated[str, typer.Argument(help="m0, m1, ...")]) -> None:
    """Run dev/gate_<milestone>.py of the installed app."""
    paths = Paths.from_env()
    code, msg = control.run_gate(milestone, control.app_path(paths))
    typer.echo(msg)
    if code != 0:
        raise typer.Exit(code)


def _stop_engine(paths: Paths) -> None:
    control.stop(paths, _client(paths))


def _sync(paths: Paths, app_dir: Path) -> None:
    update.resync_venvs(app_dir, paths)


def _reregister(paths: Paths, app_dir: Path) -> list[str]:
    notes: list[str] = []
    if read_run_time(paths) is not None:
        try:
            schedule.install(paths, None, app_dir)
            notes.append("scheduled task re-registered")
        except schedule.ScheduleError as e:
            notes.append(f"scheduled task NOT re-registered: {e}")
    return notes


@app.command("update")
def cmd_update(
    zip_path: Annotated[Path | None, typer.Argument(help="Nightshift-<version>.zip")] = None,
    rollback: Annotated[bool, typer.Option("--rollback", help="Restore the previous version.")] = False,
    check: Annotated[bool, typer.Option("--check", help="Is a newer release on GitHub?")] = False,
    github: Annotated[
        bool, typer.Option("--github", help="Download the newest GitHub release, verify it and apply it.")
    ] = False,
) -> None:
    """Apply a release zip (verified, with automatic rollback), go back one version, or use GitHub."""
    if sum([zip_path is not None, rollback, check, github]) != 1:
        raise _die("give exactly one of: a zip file, --rollback, --check, --github", 2)
    paths = Paths.from_env()
    app_dir = control.app_path(paths)
    if check:
        _update_check(update.current_version(app_dir))
        return
    env = update.UpdateEnv(
        stop=lambda: _stop_engine(paths),
        sync=lambda a: _sync(paths, a),
        verify=update.verify_quick,
        reregister=lambda a: _reregister(paths, a),
    )
    try:
        if rollback:
            res = update.rollback_app(app_dir, env, paths=paths)
        elif github:
            rel = releases.fetch_latest()
            if not releases.is_newer(rel.version, update.current_version(app_dir)):
                typer.echo(f"Already on the newest version ({update.current_version(app_dir)}).")
                typer.echo(json.dumps({"update_available": False, "latest": rel.version}))
                return
            typer.echo(f"Downloading {rel.zip_name} ...")
            res = update.update_app(releases.download(rel, paths.data / "updates"), app_dir, env, paths=paths)
        else:
            assert zip_path is not None
            res = update.update_app(zip_path, app_dir, env, paths=paths)
    except NsError as e:
        raise _die(e) from e
    typer.echo(f"Now on {res.new_version} (was {res.old_version}).")
    for n in res.notes:
        typer.echo(f"  {n}")


def _update_check(current: str) -> None:
    """Print the answer of `ns update --check`: a sentence, then one JSON line the dashboard parses."""
    try:
        result = releases.check(current)
    except NsError as e:
        raise _die(e) from e
    if result["update_available"]:
        typer.echo(f"Version {result['latest']} is available (you have {current}). Run: ns update --github")
    else:
        typer.echo(f"Up to date ({current}).")
    typer.echo(json.dumps(result))


@app.command("backup")
def cmd_backup(
    out: Annotated[Path | None, typer.Option("--out", help="Target zip (default: Desktop).")] = None,
) -> None:
    """Back up settings, voices, the database and exemplars (never models or videos)."""
    f = backup.make_backup(Paths.from_env(), out)
    typer.echo(f"Backup written to {f}")


@app.command("restore")
def cmd_restore(
    zip_path: Annotated[Path, typer.Argument(help="nightshift-backup-<date>.zip")],
    all_files: Annotated[bool, typer.Option("--all", help="Also restore runtime.json.")] = False,
) -> None:
    """Restore a backup (what it overwrites is kept in data\\backup\\pre-restore-*)."""
    try:
        lines = backup.restore_backup(Paths.from_env(), zip_path, include_machine=all_files)
    except NsError as e:
        raise _die(e) from e
    for ln in lines:
        typer.echo(ln)


def _schedule_call(fn_name: str, time: str | None = None) -> None:
    paths = Paths.from_env()
    try:
        if fn_name == "install":
            msg = schedule.install(paths, time, control.app_path(paths))
        elif fn_name == "remove":
            msg = schedule.remove(paths)
        else:
            msg = schedule.show(paths)
    except schedule.ScheduleError as e:
        raise _die(NsError("NS-E204", f"schedule {fn_name}: {e}")) from e
    typer.echo(msg)


@schedule_app.command("install")
def schedule_install(
    time: Annotated[str | None, typer.Option("--time", help="Local time HH:MM (default 03:00).")] = None,
) -> None:
    """Register the daily task (wake the PC, run whether logged on or not, stop after 6 h)."""
    _schedule_call("install", time)


@schedule_app.command("remove")
def schedule_remove() -> None:
    """Unregister the daily task."""
    _schedule_call("remove")


@schedule_app.command("show")
def schedule_show() -> None:
    """Show the task state and next run."""
    _schedule_call("show")


@app.command("notify")
def cmd_notify(title: str, message: str) -> None:
    """Show a Windows toast (does nothing where toasts are unavailable)."""
    notify.toast(title, message)


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
