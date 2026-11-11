"""`studio series ...` command line (component C11; spec 23.3 step 7).

A typer sub-app that mirrors the Assistant tools. C7 mounts it with `app.add_typer(series_app, name="series")`
(CR-C11-3). `--fake-services` swaps every service for its in-process fake (no GPU needed).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated, Any

import typer
from pydantic import BaseModel

from ns_common.commandline import render
from ns_common.errors import NsError
from ns_studio.context import FAKEABLE
from ns_studio.errors import hint_of
from ns_studio.gate import job_gate
from ns_studio.series.store import MAX_MINUTES, MIN_MINUTES, WorkshopError
from ns_studio.series.workshop import Workshop, default_workshop

app = typer.Typer(help="Series Workshop: develop multi-part stories as files, then produce them.")

Slug = Annotated[str, typer.Argument(help="Story slug (see `studio series list`).")]
Fake = Annotated[bool, typer.Option("--fake-services", help="Use in-process fakes for every service.")]


def _workshop(fake: bool) -> Workshop:
    return default_workshop(FAKEABLE if fake else ())


def _show(value: Any) -> None:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    typer.echo(value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False))


def _run(fake: bool, action: Callable[[Workshop], Any], *, gated: bool = True) -> None:
    """Run `action` on a workshop, print its result, and turn coded errors into exit code 1. `gated`: the
    command may run a model or queue a job, so it passes the start gate first (D-48, `ns_studio.gate`)."""
    try:
        if gated:
            job_gate()
        _show(action(_workshop(fake)))
    except NsError as e:
        typer.echo(f"error {e.code}: {e.definition.message} {e.detail}".strip(), err=True)
        typer.echo(f"hint: {hint_of(e)}", err=True)
        raise typer.Exit(1) from e
    except WorkshopError as e:
        typer.echo(f"error: {render(str(e))}", err=True)    # its command as this owner types it (FN-3)
        raise typer.Exit(1) from e


@app.command()
def start(
    channel: Annotated[str, typer.Argument(help="Channel slug.")],
    premise: Annotated[str | None, typer.Option(help="Your starting idea (optional).")] = None,
    fake_services: Fake = False,
) -> None:
    """Create a story folder with a concept skeleton and print its slug."""
    _run(fake_services, lambda ws: ws.start(channel, premise))


@app.command()
def concepts(
    slug: Slug,
    n: Annotated[int, typer.Option(help="How many drafts (1-6).")] = 3,
    fake_services: Fake = False,
) -> None:
    """Write concept drafts concept-A.md, concept-B.md, ..."""
    _run(fake_services, lambda ws: ws.concepts(slug, n))


@app.command()
def regenerate(
    slug: Slug,
    letter: Annotated[str, typer.Argument(help="Draft letter.")],
    feedback: Annotated[str, typer.Option(help="What should change.")],
    fake_services: Fake = False,
) -> None:
    """Rewrite one concept draft with your notes."""
    _run(fake_services, lambda ws: ws.regenerate(slug, letter, feedback))


@app.command()
def choose(
    slug: Slug, letter: Annotated[str, typer.Argument(help="Draft letter.")], fake_services: Fake = False
) -> None:
    """Promote a draft to concept.md."""
    _run(fake_services, lambda ws: ws.choose(slug, letter))


@app.command("map")
def map_cmd(
    slug: Slug,
    parts: Annotated[int, typer.Option(help="Number of parts.")],
    minutes: Annotated[int, typer.Option(help=f"Minutes per part, {MIN_MINUTES}-{MAX_MINUTES}.")] = 15,
    fake_services: Fake = False,
) -> None:
    """Write series.md, the part map."""
    _run(fake_services, lambda ws: ws.map_parts(slug, parts, minutes))


@app.command()
def write(
    slug: Slug, n: Annotated[int, typer.Argument(help="Part number.")], fake_services: Fake = False
) -> None:
    """Write script.md, the Shorts files and notes.md of one part."""
    _run(fake_services, lambda ws: ws.write_part(slug, n))


@app.command()
def revise(
    slug: Slug,
    n: Annotated[int, typer.Argument(help="Part number.")],
    feedback: Annotated[str, typer.Option(help="Notes to apply.")],
    fake_services: Fake = False,
) -> None:
    """Apply your notes to a part's script and nothing else."""
    _run(fake_services, lambda ws: ws.revise_part(slug, n, feedback))


@app.command()
def approve(
    slug: Slug,
    part: Annotated[str, typer.Argument(help="Part number or 'all'.")] = "all",
    fake_services: Fake = False,
) -> None:
    """Approve written parts for production."""
    _run(fake_services, lambda ws: ws.approve(slug, part))


@app.command()
def design(
    slug: Slug,
    fmt: Annotated[str | None, typer.Option("--format", help="Format id.")] = None,
    visual_treatment: Annotated[str | None, typer.Option(help="Visual treatment preset.")] = None,
    caption_style: Annotated[str | None, typer.Option(help="Caption style preset.")] = None,
    voice: Annotated[str | None, typer.Option(help="Preset voice speaker.")] = None,
    thumbnail_style: Annotated[str | None, typer.Option(help="Thumbnail style.")] = None,
    cadence_days: Annotated[int | None, typer.Option(help="Days between parts.")] = None,
    fake_services: Fake = False,
) -> None:
    """Save design.yaml (shows the allowed choices when no option is given)."""
    fields = {
        "format": fmt,
        "visual_treatment": visual_treatment,
        "caption_style": caption_style,
        "voice": voice,
        "thumbnail_style": thumbnail_style,
        "publish_cadence_days": cadence_days,
    }
    if all(v is None for v in fields.values()):
        _run(fake_services, lambda ws: ws.design_choices(slug))
    else:
        _run(fake_services, lambda ws: ws.design(slug, **fields))


@app.command()
def produce(
    slug: Slug,
    part: Annotated[str, typer.Argument(help="Part number or 'all'.")] = "all",
    fake_services: Fake = False,
) -> None:
    """Queue one master-first job per approved part."""
    _run(fake_services, lambda ws: ws.produce(slug, part))


@app.command()
def status(slug: Slug, fake_services: Fake = False) -> None:
    """Show the story status, parts and the suggested next step."""
    _run(fake_services, lambda ws: ws.status(slug), gated=False)


@app.command("list")
def list_cmd(fake_services: Fake = False) -> None:
    """List every workshop story."""
    _run(fake_services, lambda ws: ws.list_stories(), gated=False)
