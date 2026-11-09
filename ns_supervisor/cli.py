"""C2 `ns` console tool (spec 15.9). Thin wiring: each command delegates to its own module."""

import json
import os
from pathlib import Path
from typing import Annotated

import typer

from ns_common import __version__
from ns_common.config import load_runtime
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_supervisor import (
    backup,
    control,
    doctor,
    models_get,
    notify,
    relaunch,
    releases,
    schedule,
    tune,
    update,
    update_job,
    updater_copy,
    verify,
)
from ns_supervisor.client import SupervisorClient, ensure_running
from ns_supervisor.schedule_cfg import MODE_DISABLED, MODE_SWITCHED_OFF, read_run_time, read_schedule_mode
from ns_supervisor.services import venv_python

app = typer.Typer(help="Nightshift runtime tools", add_completion=False)
schedule_app = typer.Typer(help="The nightly scheduled task 'Nightshift Daily'", add_completion=False)
app.add_typer(schedule_app, name="schedule")
models_app = typer.Typer(help="Optional model downloads (VoiceDesign, the story model)", add_completion=False)
app.add_typer(models_app, name="models")


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
            if not control.wait_down(client, 45):
                typer.echo("The verify engine is slow to exit.", err=True)
    except NsError as e:
        typer.echo(f"Could not stop the verify engine: {e}", err=True)


@app.command("verify")
def cmd_verify(
    quick: Annotated[bool, typer.Option("--quick", help="Health, one completion, one voice only.")] = False,
    downloads: Annotated[
        bool,
        typer.Option(
            "--downloads",
            help="Only re-hash the downloaded models and programs against manifest.json (no engine).",
        ),
    ] = False,
) -> None:
    """Check every service and benchmark them; writes logs\\verify-report.json."""
    paths = Paths.from_env()
    if downloads:
        from installer.stage2 import verify_downloads

        raise typer.Exit(verify_downloads.run(paths, control.app_path(paths), typer.echo))
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
    if rt.dev_mode:
        typer.echo("Dev mode: the language model is a fake, so there is nothing to tune.")
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


def _stop_engine(paths: Paths) -> None:
    control.stop(paths, _client(paths))


def _sync(paths: Paths, app_dir: Path) -> None:
    update.resync_venvs(app_dir, paths)


def _reregister(paths: Paths, app_dir: Path) -> list[str]:
    """Point the nightly task at the updated program, keeping what the owner set (D-28).

    The task's state (switched on or off) and run time are read before it is replaced and restored after; the
    update log says what was kept. A task that `schedule.json` records as `disabled` (a dev install's task, or
    one the owner registered with `ns schedule install --disabled`) is not touched by an update, as before
    D-28. `ns schedule install` and the Settings run-time change DO register such a task enabled (setup's
    `ns schedule install --time T` relies on it for a real install made over a dev one); `--disabled` again
    keeps it off.
    """
    notes: list[str] = []
    mode = read_schedule_mode(paths)
    if mode in ("none", MODE_DISABLED):
        return notes
    try:
        schedule.install(paths, None, app_dir, keep_state=True, log=NsLogger("update", paths.logs))
    except schedule.ScheduleError as e:
        return [f"scheduled task NOT re-registered: {e}"]
    off = read_schedule_mode(paths) == MODE_SWITCHED_OFF
    when = read_run_time(paths, include_disabled=True)
    notes.append(
        f"scheduled task re-registered (still switched off, daily at {when})" if off
        else f"scheduled task re-registered (daily at {when})"
    )
    return notes


def _apply_update(
    paths: Paths,
    zip_path: Path | None,
    rollback: bool,
    github: bool,
    git: bool = False,
    channel: str = "stable",
) -> update_job.Outcome:
    """Do the update in this process (the worker). Prints progress; its last line is a JSON result."""
    app_dir = control.app_path(paths)
    env = update.UpdateEnv(
        stop=lambda: _stop_engine(paths),
        sync=lambda a: _sync(paths, a),
        verify=lambda a: update.verify_quick(a, venv_python(paths, "supervisor")),
        reregister=lambda a: _reregister(paths, a),
        settle=lambda: control.wait_down(_client(paths)),
    )
    if rollback:
        res = update.rollback_app(app_dir, env, paths=paths)
    elif git:
        res = update.git_update(app_dir, env, paths=paths, channel=channel)
    elif github:
        rel = releases.fetch_latest()
        current = update.current_version(app_dir)
        if not releases.is_newer(rel.version, current):
            msg = f"Already on the newest version ({current})."
            typer.echo(msg)
            typer.echo(json.dumps({"updated": False, "old": current, "new": current}))
            return update_job.Outcome(msg, current, current, updated=False)
        typer.echo(f"Downloading {rel.zip_name} ...")
        res = update.update_app(releases.download(rel, paths.data / "updates"), app_dir, env, paths=paths)
    else:
        assert zip_path is not None
        res = update.update_app(zip_path, app_dir, env, paths=paths)
    msg = f"Now on {res.new_version} (was {res.old_version})."
    typer.echo(msg)
    for n in res.notes:
        typer.echo(f"  {n}")
    result: dict[str, object] = {"updated": True, "old": res.old_version, "new": res.new_version}
    if res.release_notes:
        result["release_notes"] = res.release_notes
    typer.echo(json.dumps(result))
    return update_job.Outcome(msg, res.old_version, res.new_version, updated=True,
                              release_notes=res.release_notes)


@app.command("update")
def cmd_update(
    zip_path: Annotated[Path | None, typer.Argument(help="Nightshift-Update-<version>.zip")] = None,
    rollback: Annotated[bool, typer.Option("--rollback", help="Restore the previous version.")] = False,
    check: Annotated[bool, typer.Option("--check", help="Is a newer release on GitHub?")] = False,
    github: Annotated[
        bool, typer.Option("--github", help="Download the newest GitHub release, verify it and apply it.")
    ] = False,
    git: Annotated[
        bool, typer.Option("--git", help="Cloned-repo install: pull the newest release or main, re-sync.")
    ] = False,
    channel: Annotated[
        str, typer.Option("--channel", help="With --git: stable (newest release tag) or main.")
    ] = "stable",
    detach: Annotated[
        bool,
        typer.Option("--detach", help="Start the update in the background and return at once."),
    ] = False,
    worker: Annotated[bool, typer.Option("--worker", hidden=True)] = False,
    log: Annotated[Path | None, typer.Option("--log", hidden=True)] = None,
    home: Annotated[Path | None, typer.Option("--home", hidden=True)] = None,
    wait_pid: Annotated[int | None, typer.Option("--wait-pid", hidden=True)] = None,
) -> None:
    """Apply a release zip (verified, with automatic rollback), go back one version, or use GitHub.

    The update runs in a detached worker process, so stopping the engine (or closing the dashboard that
    started it) cannot kill it half way through the swap.
    """
    if sum([zip_path is not None, rollback, check, github, git]) != 1:
        raise _die("give exactly one of: a zip file, --rollback, --check, --github, --git", 2)
    if channel not in update.CHANNELS:
        raise _die("--channel must be one of: " + ", ".join(update.CHANNELS), 2)
    if channel != "stable" and not git:
        raise _die("--channel main works with --git only", 2)
    if home is not None:
        os.environ["NIGHTSHIFT_HOME"] = str(home)
    paths = Paths.from_env()
    if check:
        _update_check(update.current_version(control.app_path(paths)))
        return
    if worker:
        def discard_copy() -> None:
            """After a successful relaunch: have a helper delete the venv copy this worker runs from."""
            updater_copy.discard_own_copy(paths, spawn=update_job.spawn_detached)

        code = update_job.run_worker(
            paths, lambda: _apply_update(paths, zip_path, rollback, github, git, channel), log=log,
            wait_pid=wait_pid,
            old_version=update.current_version(control.app_path(paths)),
            relaunch=relaunch.default_relaunch(paths),
            cleanup=discard_copy,
        )
        raise typer.Exit(code)
    forwarded: list[str] = []
    if zip_path is not None:
        try:
            update.verify_package(zip_path.resolve())     # a bad zip is reported here, nothing is started
        except NsError as e:
            raise _die(e) from e
        forwarded.append(str(zip_path.resolve()))        # the worker starts in another folder
    if rollback:
        forwarded.append("--rollback")
    if github:
        forwarded.append("--github")
    if git:
        forwarded += ["--git", "--channel", channel]
    background = detach or update_job.under_engine()
    try:
        pid, log_file = update_job.launch(paths, forwarded, wait_pid=os.getpid() if background else None)
    except NsError as e:
        raise _die(e) from e
    if background:
        typer.echo(f"The update runs in the background; Nightshift will close. Progress: {log_file}")
        status = str(update_job.status_file(paths))
        typer.echo(json.dumps({"started": True, "log": str(log_file), "status": status}))
        return
    raise typer.Exit(update_job.follow(paths, pid, log_file, typer.echo))


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
    notes: list[str] = []
    try:
        f = backup.make_backup(Paths.from_env(), out, notes=notes)
    except NsError as e:  # NS-E120 (the keys copy could not be locked), NS-E119: code, message and hint
        raise _die(e) from e
    except OSError as e:  # a full disk, a folder that refuses files: a sentence, not a traceback
        raise _die(f"The backup could not be written: {e}") from e
    typer.echo(f"Backup written to {f}")
    for line in notes:
        typer.echo(line)


@app.command("restore")
def cmd_restore(
    zip_path: Annotated[Path, typer.Argument(help="nightshift-backup-<date>.zip")],
    all_files: Annotated[bool, typer.Option("--all", help="Also restore runtime.json.")] = False,
) -> None:
    """Restore a backup (what it overwrites is kept in data\\backup\\pre-restore-*)."""
    paths = Paths.from_env()
    if _client(paths).is_up():
        # the running engine holds data\studio.db open: the restore used to fail halfway (WinError 32) and
        # put everything back; now it says what to do before touching anything
        raise _die(NsError(
            "NS-E703",
            "Nightshift is running, so the database cannot be replaced. Stop it first (the 'Nightshift - "
            "Stop All' shortcut, or ns stop), then run the restore again. Nothing was changed.",
        ))
    try:
        lines = backup.restore_backup(paths, zip_path, include_machine=all_files)
    except NsError as e:
        raise _die(e) from e
    for ln in lines:
        typer.echo(ln)


def _schedule_call(
    fn_name: str, time: str | None = None, disabled: bool = False, enable: bool = False
) -> None:
    paths = Paths.from_env()
    try:
        if fn_name == "install":
            msg = schedule.install(
                paths, time, control.app_path(paths), disabled=disabled, keep_state=True, enable=enable
            )
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
    disabled: Annotated[
        bool,
        typer.Option(
            "--disabled",
            help="Register the task but switch it off (a dev install, or a pause). ns update leaves it off.",
        ),
    ] = False,
    enable: Annotated[
        bool,
        typer.Option("--enable", help="Switch the task on, even if you switched it off in Task Scheduler."),
    ] = False,
) -> None:
    """Register the daily task (wake the PC, run whether logged on or not, stop after 6 h).

    A task that already exists keeps its run time (without --time) and its state: a task you switched off in
    Task Scheduler stays off unless --enable. A task registered with --disabled is the exception: a plain
    `ns schedule install` registers it enabled (ns update leaves it off).
    """
    if enable and disabled:
        raise _die(NsError("NS-E204", "schedule install: --enable and --disabled cannot be used together"))
    _schedule_call("install", time, disabled, enable)


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
    """Show a Windows toast; without a desktop (a scheduled run) leave a morning note for the dashboard."""
    notify.toast_or_note(title, message)


@models_app.command("get")
def models_get_cmd(
    what: Annotated[str, typer.Argument(help="voicedesign | story_llm")],
    everything: Annotated[
        bool, typer.Option("--all", help="voicedesign: the GGUF talker and the PyTorch engine's weights.")
    ] = False,
) -> None:
    """Download an optional model (pinned in manifest.json, resumable, SHA256-checked).

    voicedesign follows the voice engine in runtime.json: the GGUF talker for qwentts, the Hugging Face
    weights for the PyTorch (reference) engine. --all fetches both.
    """
    if what not in models_get.KINDS:
        raise _die(f"unknown model {what!r}; choose one of: {', '.join(models_get.KINDS)}", 2)
    paths = Paths.from_env()
    try:
        result = models_get.get_model(what, paths, control.app_path(paths), typer.echo, everything=everything)
    except NsError as e:
        raise _die(e) from e
    typer.echo(json.dumps(result))


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
