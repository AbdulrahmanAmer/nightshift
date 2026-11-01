import typer

from ns_common import __version__

app = typer.Typer(help="Nightshift Studio")


@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(f"studio {__version__}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
