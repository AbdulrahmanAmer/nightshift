"""`studio` command line (component C7). `print` is allowed here: this module is the CLI entry point."""

import json
import random
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

import typer

from ns_common import __version__
from ns_common.errors import NsError
from ns_common.models import Channel, Voice
from ns_studio import jobs
from ns_studio.channels import crud
from ns_studio.errors import StudioError
from ns_studio.orchestrator import plan_job, run_job

app = typer.Typer(help="Nightshift Studio", no_args_is_help=False)
channel_app = typer.Typer(help="Manage channel profiles", no_args_is_help=True)
voice_app = typer.Typer(help="List and preview voices", no_args_is_help=True)
app.add_typer(channel_app, name="channel")
app.add_typer(voice_app, name="voice")

FakeOpt = Annotated[
    str | None,
    typer.Option(
        "--fake-services",
        is_flag=False,
        flag_value="all",
        help="In-process fakes: bare flag fakes everything, or a list such as llm,tts,whisper,embed,clips.",
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
    channel: Annotated[str | None, typer.Option("--channel", help="Channel slug (default: all).")] = None,
    count: Annotated[int, typer.Option("--count", min=1, help="Jobs to create per channel.")] = 1,
    fmt: Annotated[str | None, typer.Option("--format", help="master_first | shorts_only | ...")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the stage plan and exit.")] = False,
    resume: Annotated[str | None, typer.Option("--resume", help="Resume the job with this id.")] = None,
    fake_services: FakeOpt = None,
) -> None:
    """Create and run jobs through the stage pipeline."""
    fake = fake_set(fake_services)
    if fmt is not None and fmt not in jobs.FORMATS:
        raise typer.BadParameter(f"--format must be one of {', '.join(jobs.FORMATS)}")
    with guarded():
        jobs.load_stage_modules()
        if dry_run and resume is None:
            names = [s.name for s in plan_job(fmt or "shorts_only")]
            typer.echo("plan: " + " -> ".join(names))
            return
        env = jobs.open_env(fake)
        if resume is not None:
            ctx = jobs.load_job(env, resume)
            if dry_run:
                typer.echo("plan: " + " -> ".join(run_job(ctx, dry_run=True)))
                return
            ran = run_job(ctx, resume=True)
            typer.echo(f"job {ctx.job_id} resumed: {len(ran)} stages visited")
            return
        rng = random.Random()
        for ch in jobs.resolve_channels(env, channel):
            for _ in range(count):
                ctx = jobs.make_job(env, ch, fmt or ch.format, rng=rng)
                ran = run_job(ctx)
                typer.echo(f"job {ctx.job_id} done: {len(ran)} stages ({ch.slug}, {ctx.fmt})")


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
        env = jobs.open_env(frozenset())
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
                raise StudioError("NS-E307", str(e).replace("\n", " ")) from e
        f = crud.add_channel(env.paths, env.db, ch)
        for w in crud.pool_warnings(ch.pools):
            typer.echo(f"warning: {w}")
        typer.echo(f"channel {ch.slug} added: {f}")


@channel_app.command("list")
def channel_list() -> None:
    """List configured channels."""
    with guarded():
        env = jobs.open_env(frozenset())
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
        env = jobs.open_env(frozenset())
        crud.edit_channel(env.paths, env.db, slug, updates)
        typer.echo(f"channel {slug} updated ({', '.join(updates)})")


@channel_app.command("validate")
def channel_validate(slug: Annotated[str | None, typer.Argument()] = None) -> None:
    """Check pool minimums and cross-channel distinctness for one or all channels."""
    with guarded():
        env = jobs.open_env(frozenset())
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


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
