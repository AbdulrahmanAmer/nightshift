"""`studio` command line (component C7). `print` is allowed here: this module is the CLI entry point."""

import importlib.util
import json
import random
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError
from typer.core import TyperGroup

from ns_common import __version__
from ns_common.errors import NsError
from ns_common.keepawake import keep_awake
from ns_common.models import Channel, JobRunRequest, Voice
from ns_studio import dashboard_cmd, jobs, notifications, review, voice_setup
from ns_studio.calibrate import run as calibration
from ns_studio.channels import cadence, crud
from ns_studio.clipper import cli as clipper_cli
from ns_studio.context import JobContext
from ns_studio.daily import run_daily
from ns_studio.errors import UNEXPECTED, StudioError, describe_unexpected
from ns_studio.footage_keys import require_footage_key
from ns_studio.learn import importer, reweight, status
from ns_studio.orchestrator import OWNER_SCRIPT_FORMATS, plan_job, run_job
from ns_studio.policy.regen import run_job_guarded
from ns_studio.series import cli as series_cli

# Sub-apps whose `--fake-services` is a plain on/off flag; their arguments must not be rewritten.
FLAG_STYLE_COMMANDS: frozenset[str] = frozenset({"series", "ask"})


def normalize_fake_flag(args: list[str]) -> list[str]:
    """Rewrite a bare `--fake-services` to `--fake-services=all` so it can also take `=list`.

    Arguments of `studio series ...` and `studio ask ...` are left alone (their flag takes no value).
    """
    first = next((a for a in args if not a.startswith("-")), None)
    if first in FLAG_STYLE_COMMANDS:
        return args
    return ["--fake-services=all" if a == "--fake-services" else a for a in args]


class StudioGroup(TyperGroup):
    """Top-level group that accepts `--fake-services` with or without a value."""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        """Normalise the optional-value flag, then parse as usual."""
        return super().parse_args(ctx, normalize_fake_flag(list(args)))


app = typer.Typer(help="Nightshift Studio", no_args_is_help=False, cls=StudioGroup)
channel_app = typer.Typer(help="Manage channel profiles", no_args_is_help=True)
voice_app = typer.Typer(help="List, preview, design and clone voices", no_args_is_help=True)
app.add_typer(channel_app, name="channel")
app.add_typer(voice_app, name="voice")
app.add_typer(clipper_cli.app, name="clip")  # CR-C8-5
app.add_typer(series_cli.app, name="series")  # CR-C11-3


def _ask_unavailable(text: Annotated[str, typer.Argument(help="What to ask or tell Nightshift.")]) -> None:
    """Ask the Assistant (unavailable: the dashboard package or its dependencies are missing)."""
    typer.echo(
        "The Assistant needs the dashboard package and its dependencies (fastapi), which are not "
        "installed here. Install the dashboard extra, then try again.",
        err=True,
    )
    raise typer.Exit(1)


def mount_ask(parent: typer.Typer) -> bool:
    """Add `studio ask` (CR-C9-7); without the dashboard or fastapi it explains what is missing.

    Returns True when the real Assistant command was mounted.
    """
    if importlib.util.find_spec("fastapi") is not None:
        try:
            from ns_dashboard.assistant.cli import register
        except ImportError:
            pass
        else:
            register(parent)
            return True
    parent.command("ask")(_ask_unavailable)
    return False


ASK_AVAILABLE = mount_ask(app)

FakeOpt = Annotated[
    str | None,
    typer.Option(
        "--fake-services",
        help="In-process fakes: bare flag fakes all, or =llm,tts,whisper,embed,clips. "
        "With llm, jobs draw length_bucket 50 and pacing standard (fixtures are written for this budget).",
    ),
]


@contextmanager
def guarded() -> Iterator[None]:
    """Turn `NsError` / `StudioError` into a one-line message and exit code 1."""
    try:
        yield
    except (NsError, StudioError) as e:
        hint = e.definition.hint if isinstance(e, NsError) else e.hint
        typer.echo(f"error {e}", err=True)
        if hint:
            typer.echo(f"hint: {hint}", err=True)
        raise typer.Exit(1) from e
    except Exception as e:  # an uncoded bug or corrupt file: coded, logged, no traceback wall
        if type(e).__module__.split(".")[0] in ("click", "typer"):
            raise  # usage errors and Exit keep their own message and exit code
        err = StudioError(UNEXPECTED, describe_unexpected(e))
        typer.echo(f"error {err}", err=True)
        typer.echo(f"hint: {err.hint}", err=True)
        raise typer.Exit(1) from e


def fake_set(value: str | None) -> frozenset[str]:
    """Parse the `--fake-services` option value, mapping bad names to a usage error."""
    try:
        return jobs.parse_fake(value)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e


@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(f"studio {__version__}")


# ---------------------------------------------------------------- run
@app.command("run")
def run_cmd(
    daily: Annotated[bool, typer.Option("--daily", help="Cadence-planned nightly run.")] = False,
    channel: Annotated[str | None, typer.Option("--channel", help="Channel slug (default: all).")] = None,
    count: Annotated[int, typer.Option("--count", min=1, help="Jobs to create per channel.")] = 1,
    fmt: Annotated[str | None, typer.Option("--format", help="master_first | shorts_only | ...")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the stage plan and exit.")] = False,
    resume: Annotated[str | None, typer.Option("--resume", help="Resume the job with this id.")] = None,
    policy: Annotated[
        bool | None, typer.Option("--policy/--no-policy", help="Policy gates (default: on when available).")
    ] = None,
    script: Annotated[
        Path | None,
        typer.Option("--script", help="Text file with a story to narrate as one video, as written."),
    ] = None,
    title: Annotated[str | None, typer.Option("--title", help="Working title for --script.")] = None,
    fake_services: FakeOpt = None,
) -> None:
    """Create and run jobs through the stage pipeline."""
    fake = fake_set(fake_services)
    if fmt is not None and fmt not in jobs.FORMATS:
        raise typer.BadParameter(f"--format must be one of {', '.join(jobs.FORMATS)}")
    script_text = _script_text(script, title, count, daily=daily, resume=resume, fmt=fmt, channel=channel)
    with guarded():
        jobs.load_stage_modules()
        if dry_run and resume is None and not daily:
            jobs.prepare_plan(fmt or "shorts_only", policy)
            plan_fmt = fmt or "shorts_only"
            if script_text:
                plan_fmt = OWNER_SCRIPT_FORMATS[plan_fmt]
            names = [s.name for s in plan_job(plan_fmt)]
            typer.echo("plan: " + " -> ".join(names))
            return
        env = jobs.open_env(fake)
        if not dry_run:
            _setup_voice(env)
        if daily:
            _daily(env, dry_run, policy)
            return
        with keep_awake():  # the PC must not sleep under a run (`run_daily` holds its own)
            _run_jobs(
                env, fmt=fmt, channel=channel, count=count, dry_run=dry_run, resume=resume, policy=policy,
                script=script_text, title=title,
            )


def _script_text(
    script: Path | None, title: str | None, count: int, *, daily: bool, resume: str | None, fmt: str | None,
    channel: str | None,
) -> str | None:
    """The story of `--script <file>`, checked with `JobRunRequest` (the same rules as the dashboard).

    Usage errors (exit 2): a title without a script, a script with `--daily` or `--resume`, a count other
    than 1, a file that cannot be read, a text outside 200 to 30000 characters.
    """
    if script is None:
        if title is not None:
            raise typer.BadParameter("--title goes with --script")
        return None
    if daily or resume is not None:
        raise typer.BadParameter("--script cannot be combined with --daily or --resume")
    if fmt is not None and fmt not in OWNER_SCRIPT_FORMATS:
        raise typer.BadParameter("--script needs --format shorts_only or master_first")
    try:
        text = script.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise typer.BadParameter(f"cannot read {script}: {e}") from e
    try:
        req = JobRunRequest(
            channel=channel or "any", format=fmt, count=count, script=text, script_title=title
        )
    except ValidationError as e:
        raise typer.BadParameter(str(e.errors()[0]["msg"]).removeprefix("Value error, ")) from e
    return req.script


def _run_jobs(
    env: jobs.Env, *, fmt: str | None, channel: str | None, count: int, dry_run: bool, resume: str | None,
    policy: bool | None, script: str | None = None, title: str | None = None,
) -> None:
    """The `--resume` and plain branches of `studio run`."""
    if resume is not None:
        ctx = jobs.load_job(env, resume)
        jobs.prepare_plan(ctx.fmt, policy)
        if dry_run:
            typer.echo("plan: " + " -> ".join(run_job(ctx, dry_run=True)))
            return
        ran = run_job_guarded(ctx, resume=True)
        typer.echo(f"job {ctx.job_id} resumed: {len(ran)} stages visited")
        return
    rng = random.Random()
    channels = jobs.resolve_channels(env, channel)
    if script is not None and len(channels) != 1:
        raise typer.BadParameter("--script makes one video: name the channel with --channel")
    for ch in channels:
        job_fmt = fmt or ch.format
        jobs.prepare_plan(job_fmt, policy)
        for _ in range(count):
            require_footage_key(env.services)  # before any GPU work: `clips` would fail an hour in
            if script is not None:
                ctx = jobs.make_script_job(env, ch, job_fmt, script, title, rng=rng)
            else:
                ctx = jobs.make_job(env, ch, job_fmt, rng=rng)
            ran = run_job_guarded(ctx)
            typer.echo(f"job {ctx.job_id} done: {len(ran)} stages ({ch.slug}, {ctx.fmt})")


def _daily(env: jobs.Env, dry_run: bool, policy: bool | None) -> None:
    """The `--daily` branch of `studio run`."""
    now = datetime.now(UTC)
    for fmt_name in sorted({c.format for c in crud.all_channels(env.paths) if c.status == "active"}):
        jobs.prepare_plan(fmt_name, policy)
    if dry_run:
        due = cadence.plan_daily(env.paths, env.db, now)
        if not due:
            typer.echo("nothing due today")
        for dj in due:
            typer.echo(f"{dj.channel.slug}: {dj.count} x {dj.fmt} ({dj.plan.describe()})")
        return
    notifier = None if env.fake else notifications.default_notifier()
    try:
        report = run_daily(env, now=now, notifier=notifier)
    except (NsError, StudioError) as e:  # the night itself broke (not one job): say so before exiting
        notifications.send(env.paths, env.log, notifier, notifications.failure_message(e.code))
        raise
    if not report["channels"]:
        typer.echo("nothing due tonight: every channel already has this week's full video or today's Shorts")
    for c in report["channels"]:
        typer.echo(f"{c['channel']}: {c['jobs']} job(s), {c['plan']}")
    for j in report["jobs"]:
        typer.echo(f"job {j['job_id']} done: {j['stages']} stages ({j['channel']})")
    for f in report["failures"]:
        typer.echo(f"job {f['job_id']} FAILED {f['code']}: {f['error']}")
    typer.echo(f"backup: {report['backup']}")
    if report["failures"]:
        raise typer.Exit(1)




# ---------------------------------------------------------------- channel
def _parse_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


@channel_app.command("add")
def channel_add(
    slug: Annotated[str | None, typer.Option("--slug")] = None,
    name: Annotated[str | None, typer.Option("--name", help="Display name.")] = None,
    niche: Annotated[str | None, typer.Option("--niche")] = None,
    keywords: Annotated[str, typer.Option("--keywords", help="Comma-separated seed keywords.")] = "",
    voice_notes: Annotated[str, typer.Option("--voice-notes")] = "",
    speaker: Annotated[str, typer.Option("--speaker", help="Preset TTS speaker.")] = "Ryan",
    fmt: Annotated[str, typer.Option("--format")] = "master_first",
    from_yaml: Annotated[Path | None, typer.Option("--from-yaml", help="Load the profile from YAML.")] = None,
) -> None:
    """Add a channel (pool minimums and cross-channel distinctness are enforced)."""
    with guarded():
        env = jobs.open_env(light=True)
        if from_yaml is not None:
            ch = crud.load_channel_yaml(from_yaml)
        else:
            if not (slug and name and niche):
                raise typer.BadParameter("--slug, --name and --niche are required without --from-yaml")
            try:
                ch = Channel.model_validate(
                    {
                        "slug": slug,
                        "display_name": name,
                        "niche": niche,
                        "seed_keywords": [k.strip() for k in keywords.split(",") if k.strip()],
                        "voice_notes": voice_notes,
                        "voice": Voice(speaker=speaker).model_dump(),
                        "format": fmt,
                    }
                )
            except ValueError as e:
                raise StudioError("NS-E320", str(e).replace("\n", " ")) from e
        f = crud.add_channel(env.paths, env.db, ch)
        for w in crud.pool_warnings(ch.pools):
            typer.echo(f"warning: {w}")
        typer.echo(f"channel {ch.slug} added: {f}")


@channel_app.command("list")
def channel_list() -> None:
    """List configured channels."""
    with guarded():
        env = jobs.open_env(light=True)
        chans = crud.all_channels(env.paths)
        if not chans:
            typer.echo("no channels configured")
            return
        for c in chans:
            pairs = len(c.pools.hooks) * len(c.pools.structures)
            typer.echo(f"{c.slug}\t{c.status}\t{c.format}\tpairs={pairs}\t{c.display_name}")


@channel_app.command("edit")
def channel_edit(
    slug: Annotated[str, typer.Argument()],
    sets: Annotated[
        list[str] | None, typer.Option("--set", help="key=value (JSON value); dotted keys like pools.hooks.")
    ] = None,
) -> None:
    """Change channel fields; the result is re-validated before it is saved."""
    with guarded():
        updates: dict[str, Any] = {}
        for item in sets or []:
            if "=" not in item:
                raise typer.BadParameter(f"--set expects key=value, got {item!r}")
            key, raw = item.split("=", 1)
            updates[key.strip()] = _parse_value(raw)
        if not updates:
            raise typer.BadParameter("give at least one --set key=value")
        env = jobs.open_env(light=True)
        crud.edit_channel(env.paths, env.db, slug, updates)
        typer.echo(f"channel {slug} updated ({', '.join(updates)})")


@channel_app.command("validate")
def channel_validate(slug: Annotated[str | None, typer.Argument()] = None) -> None:
    """Check pool minimums and cross-channel distinctness for one or all channels."""
    with guarded():
        env = jobs.open_env(light=True)
        every = crud.all_channels(env.paths)
        targets = [crud.get_channel(env.paths, slug)] if slug else every
        failed = False
        for ch in targets:
            problems, warnings = crud.validate_channel(ch, [c for c in every if c.slug != ch.slug])
            for p in problems:
                typer.echo(f"{ch.slug}: PROBLEM {p}")
            for w in warnings:
                typer.echo(f"{ch.slug}: warning {w}")
            if not problems:
                typer.echo(f"{ch.slug}: ok")
            failed = failed or bool(problems)
        if failed:
            raise typer.Exit(1)


# ---------------------------------------------------------------- voice
@voice_app.command("list")
def voice_list(fake_services: FakeOpt = None) -> None:
    """List the voices known to the TTS service."""
    with guarded():
        env = jobs.open_env(fake_set(fake_services))
        owner = "studio:voice-list"
        env.services.acquirer.acquire("tts", owner)
        try:
            voices = env.services.tts.voices()
        finally:
            env.services.acquirer.release("tts", owner)
        for v in voices:
            typer.echo(f"{v.get('name', '?')}\t{v.get('mode', '?')}")


@voice_app.command("preview")
def voice_preview(
    name: Annotated[str, typer.Argument()],
    text: Annotated[str, typer.Option("--text")] = "This is how your channel will sound at night.",
    out: Annotated[Path | None, typer.Option("--out")] = None,
    speed: Annotated[float, typer.Option("--speed", min=0.7, max=1.3)] = 1.0,
    fake_services: FakeOpt = None,
) -> None:
    """Synthesize a short sample with voice NAME and write it to a WAV file."""
    with guarded():
        env = jobs.open_env(fake_set(fake_services))
        target = out or (env.paths.data / "voice_preview.wav")
        target.parent.mkdir(parents=True, exist_ok=True)
        owner = "studio:voice-preview"
        env.services.acquirer.acquire("tts", owner)
        try:
            seconds = env.services.tts.speak(text, name, speed, target)
        finally:
            env.services.acquirer.release("tts", owner)
        typer.echo(f"{target} ({seconds:.1f} s)")


@voice_app.command("design")
def voice_design(
    name: Annotated[str, typer.Argument(help="Library name of the new voice.")],
    description: Annotated[str, typer.Option("--description", help="The voice in plain English.")],
    language: Annotated[str, typer.Option("--language")] = "English",
    fake_services: FakeOpt = None,
) -> None:
    """Design a voice from a description (needs the VoiceDesign model) and store it in the library."""
    with guarded():
        env = jobs.open_env(fake_set(fake_services))
        folder = voice_setup.design_voice(env, name, description, language=language)
        typer.echo(f"voice {name} created: {folder}")


@voice_app.command("clone")
def voice_clone(
    name: Annotated[str, typer.Argument(help="Library name of the new voice.")],
    audio: Annotated[Path, typer.Option("--audio", help="A clean 3 to 30 s recording of one speaker.")],
    text: Annotated[str, typer.Option("--text", help="The words spoken in the recording.")],
    rights_confirmed: Annotated[
        bool, typer.Option("--rights-confirmed", help="I own or have the right to use this voice.")
    ] = False,
    language: Annotated[str, typer.Option("--language")] = "English",
    fake_services: FakeOpt = None,
) -> None:
    """Clone a voice from a recording and store it in the library."""
    with guarded():
        env = jobs.open_env(fake_set(fake_services))
        folder = voice_setup.clone_voice(
            env, name, audio, text, rights_confirmed=rights_confirmed, language=language
        )
        typer.echo(f"voice {name} created: {folder}")


@voice_app.command("apply-pending")
def voice_apply_pending(fake_services: FakeOpt = None) -> None:
    """Create the voice chosen during setup (queued in config/pending_voice.json), if it is not there yet."""
    with guarded():
        env = jobs.open_env(fake_set(fake_services))
        made = voice_setup.apply_pending(env)
        typer.echo(f"voice {made} created" if made else "no voice is waiting to be created")


def _setup_voice(env: jobs.Env) -> None:
    """Before a real run: create the voice setup queued. A failure is a warning here; the job that needs
    the voice then fails with its own message."""
    if env.fake:
        return
    try:
        made = voice_setup.apply_pending(env)
    except (NsError, StudioError) as e:
        env.log.warn("pending_voice_failed", str(e))
        typer.echo(
            f"warning: the voice chosen during setup is not created yet ({e}); "
            "try: studio voice apply-pending",
            err=True,
        )
        return
    if made:
        typer.echo(f"voice {made} created from your setup choice")


# ---------------------------------------------------------------- review, status, learn, pause
review_app = typer.Typer(help="Review queue: list, show, approve, reject", invoke_without_command=True)
app.add_typer(review_app, name="review")


@review_app.callback(invoke_without_command=True)
def review_list(
    ctx: typer.Context,
    channel: Annotated[str | None, typer.Option("--channel", help="Only this channel.")] = None,
) -> None:
    """List outputs waiting for review (with no sub-command)."""
    if ctx.invoked_subcommand is not None:
        return
    with guarded():
        env = jobs.open_env(light=True)
        rows = review.pending(env.db, channel)
        if not rows:
            typer.echo("nothing pending")
        for r in rows:
            typer.echo(f"{r['output_id']}\t{r['channel']}\t{r['kind']}\t{r['title']}")


@review_app.command("show")
def review_show(
    output_id: Annotated[str, typer.Argument()],
    play: Annotated[bool, typer.Option("--play", help="Open the video in the default player.")] = False,
) -> None:
    """Show story text, policy notes and disclosure advice of one output."""
    with guarded():
        env = jobs.open_env(light=True)
        for line in review.describe(env.paths, env.db, output_id):
            typer.echo(line)
        if play and not review.open_in_player(Path(review.get_output(env.db, output_id)["path"])):
            typer.echo("could not open a player for this file")


@review_app.command("approve")
def review_approve(
    output_id: Annotated[str, typer.Argument()],
    note: Annotated[str, typer.Option("--note")] = "",
) -> None:
    """Approve a pending output."""
    with guarded():
        env = jobs.open_env(light=True)
        review.approve(env.db, output_id, note)
        typer.echo(f"approved {output_id}")


@review_app.command("reject")
def review_reject(
    output_id: Annotated[str, typer.Argument()],
    reason: Annotated[str, typer.Option("--reason", help="Why (shown in 'studio status').")] = "",
) -> None:
    """Reject an output and move its files to rejected/."""
    with guarded():
        env = jobs.open_env(light=True)
        dest = review.reject(env.paths, env.db, output_id, reason)
        typer.echo(f"rejected {output_id}: {dest}")


@app.command("mark-uploaded")
def mark_uploaded_cmd(
    job_id: Annotated[str, typer.Argument()],
    item: Annotated[str, typer.Argument(help="long or s<k>")],
    url: Annotated[str, typer.Argument()],
) -> None:
    """Record the public URL of an approved output so analytics rows can be matched."""
    with guarded():
        env = jobs.open_env(light=True)
        out = review.mark_uploaded(env.db, job_id, item, url)
        typer.echo(f"{out} marked uploaded")


@app.command("pause")
def pause_cmd(
    slug: Annotated[str, typer.Argument()],
    reason: Annotated[str, typer.Option("--reason", help="The notice text.")] = "",
) -> None:
    """Pause a channel after a YouTube warning, strike or monetization notice."""
    with guarded():
        env = jobs.open_env(light=True)
        review.pause(env.paths, env.db, slug, reason)
        typer.echo(f"channel {slug} paused; nightly runs skip it")


@app.command("resume")
def resume_cmd(
    slug: Annotated[str, typer.Argument()],
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Resume a paused channel after reviewing its last 10 outputs."""
    with guarded():
        env = jobs.open_env(light=True)
        for r in review.last_outputs(env.db, slug):
            typer.echo(f"{r['output_id']}\t{r['status']}\t{r['title']}\t{r['review_note'] or ''}")
        typer.echo("Check these against spec sections 18.2-18.4; raise critic_bar or trim pools if needed.")
        if not yes and not typer.confirm(f"Resume {slug}?"):
            raise typer.Exit(1)
        review.resume(env.paths, env.db, slug)
        typer.echo(f"channel {slug} resumed")


@app.command("status")
def status_cmd(
    channel: Annotated[str | None, typer.Option("--channel")] = None,
    milestone: Annotated[bool, typer.Option("--milestone", help="Week-8 go/no-go (spec 18.12).")] = False,
) -> None:
    """Per-channel status, performers, rejections, incidents, or the go/no-go verdict."""
    with guarded():
        env = jobs.open_env(light=True)
        chans = [crud.get_channel(env.paths, channel)] if channel else crud.all_channels(env.paths)
        if not chans:
            typer.echo("no channels configured")
        for ch in chans:
            if milestone:
                lines = status.format_milestone(status.milestone(env.db, ch.slug))
            else:
                plan = cadence.plan_channel(env.db, ch, datetime.now(UTC))
                lines = status.format_status(status.channel_status(env.db, env.paths, ch), plan.describe())
            for line in lines:
                typer.echo(line)


@app.command("dashboard")
def dashboard_command(
    port: Annotated[
        int,
        typer.Option(
            "--port",
            min=1,
            max=65535,
            help="Address to probe and print. The dashboard itself always runs on port 8100 "
            "(the DASHBOARD_PORT constant), so change this only for a forwarded port.",
        ),
    ] = dashboard_cmd.DEFAULT_PORT,
    open_browser: Annotated[
        bool, typer.Option("--open", help="Open the dashboard in the default browser.")
    ] = False,
) -> None:
    """Start the engine if needed, make sure the dashboard is ready, and print its address."""
    with guarded():
        url = dashboard_cmd.ensure_dashboard(port)
        typer.echo(url)
        if open_browser and not dashboard_cmd.open_in_browser(url):
            typer.echo(f"Could not start a browser here. Open that address ({url}) in one yourself.")


@app.command("learn")
def learn_cmd(
    import_csv: Annotated[Path, typer.Option("--import", help="YouTube Studio CSV export.")],
    date: Annotated[str | None, typer.Option("--date", help="Export date YYYY-MM-DD.")] = None,
    column_map: Annotated[
        list[str] | None, typer.Option("--map", help="field=CSV column, e.g. viewed_pct=Stayed")
    ] = None,
) -> None:
    """Import analytics, match rows to outputs and reweight the variation pools."""
    with guarded():
        env = jobs.open_env(light=True)
        overrides: dict[str, str] = {}
        for item in column_map or []:
            if "=" not in item:
                raise typer.BadParameter(f"--map expects field=column, got {item!r}")
            k, v = item.split("=", 1)
            overrides[k.strip()] = v.strip()
        day = date or datetime.now(UTC).date().isoformat()
        rows = importer.read_csv(import_csv, overrides or None)
        result = importer.import_rows(env.db, rows, day)
        typer.echo(f"matched {len(result.matched)} of {len(rows)} rows")
        for r in result.unmatched:
            typer.echo(f"unmatched: {r.title or r.video_id or r.url}")
        for slug in sorted(set(result.matched.values())):
            ch = crud.get_channel(env.paths, slug)
            changes = reweight.reweight(env.db, ch)
            reweight.save_snapshot(env.paths, slug, changes)
            typer.echo(f"{slug}: {sum(len(c) for c in changes.values())} pool weights changed")


# ---------------------------------------------------------------- calibrate
def _calibration_job(env: jobs.Env, ch: Channel, rng: random.Random) -> JobContext:
    """A throw-away job context for one calibration story (its combo is not written to the ledger)."""
    return jobs.make_job(env, ch, "shorts_only", rng=rng, record=False)


@app.command("calibrate")
def calibrate_cmd(
    channel: Annotated[str, typer.Option("--channel", help="Channel slug.")],
    n: Annotated[int, typer.Option("--n", min=1, help="Stories to generate.")] = 20,
    score_csv: Annotated[Path | None, typer.Option("--score", help="Filled-in scores.csv.")] = None,
    fake_services: FakeOpt = None,
) -> None:
    """Generate stories for human scoring, or (with --score) correlate scores and set critic_bar."""
    with guarded():
        light = score_csv is not None
        env = jobs.open_env(light=True) if light else jobs.open_env(fake_set(fake_services))
        if score_csv is not None:
            rep = calibration.score(env.paths, env.db, channel, score_csv)
            typer.echo(f"scored stories: {rep.rows}")
            for item, rho in rep.per_item_spearman.items():
                typer.echo(f"spearman {item}: {rho:+.3f}")
            typer.echo(f"spearman overall: {rep.overall_spearman:+.3f}")
            typer.echo(f"critic mean bias: {rep.mean_bias:+.2f}")
            typer.echo(
                f"recommended critic_bar: {rep.recommended_bar:.1f} "
                f"(good pass {rep.good_pass_rate:.0%}, bad pass {rep.bad_pass_rate:.0%}, "
                f"{'meets' if rep.satisfied else 'does NOT meet'} the 80/20 target)"
            )
            typer.echo(f"refusal rate: {rep.refusal_rate:.1%}")
            if rep.two_model_recommended:
                if env.runtime.llama.story_model_path:
                    typer.echo("refusal rate is above 2 %: switch to two-model mode (spec decision D3)")
                else:
                    typer.echo(
                        "refusal rate is above 2 %, but no story model is configured "
                        "(llama.story_model_path is empty): two-model mode is not available on this install"
                    )
            return
        jobs.load_stage_modules()
        ch = crud.get_channel(env.paths, channel)
        rng = random.Random()
        owner = "studio:calibrate"
        acq = env.services.acquirer
        acq.acquire("llama", owner)
        try:
            acq.acquire("embed", owner)  # ideas embeds its premises; with embed stopped every story "refused"
            try:
                stories = calibration.generate(lambda _i: _calibration_job(env, ch, rng), n)
            finally:
                acq.release("embed", owner)
        finally:
            acq.release("llama", owner)
        out = calibration.write_generation(env.paths, env.db, channel, stories)
        refused = sum(1 for s in stories if s.refusal)
        typer.echo(f"wrote {len(stories)} stories to {out}")
        typer.echo(f"fill in {out / 'scores.csv'}, then: studio calibrate --channel {channel} --score FILE")
        typer.echo(f"refusal rate so far: {refused / len(stories):.1%}")


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
