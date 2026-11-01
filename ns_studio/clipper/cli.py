"""`studio clip` (component C8). `print` is allowed here: this module is a CLI entry point.

C7's `ns_studio/cli.py` mounts it with `app.add_typer(ns_studio.clipper.cli.app, name="clip")`
(CR-C8-5). It also runs standalone: `python -m ns_studio.clipper.cli <video> --channel <slug>`.
"""
from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer

from ns_common.config import load_channel
from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_studio.clipper.models import TEMPLATE_NAMES, ClipParams
from ns_studio.clipper.stages_clip import create_clip_job, resume_clip_job
from ns_studio.context import FAKEABLE
from ns_studio.orchestrator import run_job


def normalize_fake_flag(args: list[str]) -> list[str]:
    """Rewrite a bare `--fake-services` to `--fake-services=all` so the option can also take a list."""
    return ["--fake-services=all" if a == "--fake-services" else a for a in args]


app = typer.Typer(
    help="Clip a video you own into vertical Shorts.",
    no_args_is_help=False,
    context_settings={"allow_interspersed_args": True},
)


def parse_fake(value: str | None) -> frozenset[str]:
    """`None` -> no fakes, `all` -> every fakeable service, else a comma list such as `llm,whisper`."""
    if value is None or value == "":
        return frozenset()
    if value == "all":
        return FAKEABLE
    names = frozenset(v.strip() for v in value.split(",") if v.strip())
    unknown = names - FAKEABLE
    if unknown:
        raise typer.BadParameter(f"unknown service(s) {sorted(unknown)}; choose from {sorted(FAKEABLE)}")
    return names


@contextmanager
def guarded() -> Iterator[None]:
    """Turn `NsError` into a one-line message with its hint and exit code 1."""
    try:
        yield
    except NsError as e:
        typer.echo(f"error {e}", err=True)
        typer.echo(f"hint: {e.definition.hint}", err=True)
        raise typer.Exit(1) from e
    except FileNotFoundError as e:
        typer.echo(f"error file not found: {e.filename or e}", err=True)
        raise typer.Exit(1) from e


@app.callback(invoke_without_command=True)
def clip(
    video: Annotated[Path | None, typer.Argument(help="The video file to clip (you must own it).")] = None,
    channel: Annotated[str, typer.Option("--channel", help="Channel slug the clips belong to.")] = "",
    template: Annotated[
        str | None, typer.Option("--template", help="auto or " + ", ".join(TEMPLATE_NAMES))
    ] = None,
    max_clips: Annotated[int | None, typer.Option("--max-clips", help="Default 6.")] = None,
    min_s: Annotated[float | None, typer.Option("--min-s", help="Shortest clip (s), default 20.")] = None,
    max_s: Annotated[float | None, typer.Option("--max-s", help="Longest clip (s), default 60.")] = None,
    confirm_rights: Annotated[
        bool, typer.Option("--confirm-rights", help="Confirm you own or are licensed to use the video.")
    ] = False,
    reselect: Annotated[
        str | None, typer.Option("--reselect", help="Job id: redo selection and later stages only.")
    ] = None,
    fake_services: Annotated[
        str | None, typer.Option("--fake-services", help="In-process fakes: bare = all, or =llm,whisper.")
    ] = None,
) -> None:
    """Import, transcribe, select, lay out, render and file clips of VIDEO."""
    fake = parse_fake(fake_services)
    paths = Paths.from_env()
    with guarded():
        if reselect:
            _reselect(paths, reselect, channel, fake, template, max_clips, min_s, max_s)
            return
        if video is None or not channel:
            raise typer.BadParameter("give a VIDEO and --channel <slug> (or --reselect <job_id>)")
        if not confirm_rights and not typer.confirm("I own or am licensed to use this video"):
            typer.echo("Rights not confirmed; nothing was done.", err=True)
            raise typer.Exit(1)
        ch = load_channel(paths, channel)
        params = ClipParams(
            source=str(video.resolve()),
            channel=channel,
            template=template or "auto",
            max_clips=max_clips or 6,
            min_s=min_s or 20.0,
            max_s=max_s or 60.0,
            rights_confirmed=True,
        )
        try:
            params.validate()
        except ValueError as e:
            raise typer.BadParameter(str(e)) from e
        ctx = create_clip_job(paths, ch, params, fake=fake)
        typer.echo(f"job {ctx.job_id}")
        ran = run_job(ctx)
        typer.echo(f"done: {', '.join(ran)}")
        typer.echo(f"clips: {ctx.file('clips')}")


def _reselect(
    paths: Paths,
    job_id: str,
    channel: str,
    fake: frozenset[str],
    template: str | None,
    max_clips: int | None,
    min_s: float | None,
    max_s: float | None,
) -> None:
    old = ClipParams.load(paths.job_dir(job_id))
    new = replace(
        old,
        template=template or old.template,
        max_clips=max_clips or old.max_clips,
        min_s=min_s or old.min_s,
        max_s=max_s or old.max_s,
    )
    try:
        new.validate()
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e
    ctx = resume_clip_job(paths, job_id, load_channel(paths, channel or old.channel), fake=fake)
    new.save(ctx.job_dir)
    ran = run_job(ctx)
    typer.echo(f"re-ran selection for {job_id}: {', '.join(ran)}")


def main() -> None:
    """Console entry point for standalone use."""
    app(args=normalize_fake_flag(sys.argv[1:]))


if __name__ == "__main__":
    main()
