"""`studio ask "<text>"`: the same Assistant loop on the command line (component C9, spec 20.3a).

C7 mounts it with `from ns_dashboard.assistant.cli import register; register(app)` (CR-C9-7); it also runs
standalone: `python -m ns_dashboard.assistant.cli ask "what is waiting for review?"`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated, Any

import typer

from ns_common.paths import Paths

EchoFn = Callable[[str], None]
ConfirmFn = Callable[[str], bool]


def run_ask(
    dash: Any,
    text: str,
    *,
    conversation: str | None = None,
    yes: bool = False,
    echo: EchoFn = typer.echo,
    confirm_fn: ConfirmFn | None = None,
) -> int:
    """Run one question through the Assistant, printing events; returns 0, or 1 when an error event occurred.

    Confirm cards are asked on the terminal (`yes=True` accepts them all)."""
    ask_yes: ConfirmFn = confirm_fn or (lambda q: typer.confirm(q, default=False))
    status = 0
    for ev in dash.assistant.chat(text, conversation):
        kind, payload = ev["kind"], ev["payload"]
        if kind == "message":
            echo(payload["text"])
        elif kind == "tool_call":
            echo(f"  [{payload['tool']}] {json.dumps(payload['args'], ensure_ascii=False)}")
        elif kind == "confirm":
            echo(f"  Needs your OK: {payload['text']}")
            if yes or ask_yes("Do it?"):
                out = dash.assistant.confirm(payload["action_id"])
                echo(f"  {out['message']}")
            else:
                dash.assistant.dismiss(payload["action_id"])
                echo("  Okay, not doing it.")
        elif kind == "busy":
            echo(payload["message"])
        elif kind == "error":
            echo(f"Error: {payload['message']}")
            status = 1
    return status


def build_dashboard(fake_services: bool = False) -> Any:
    """Service layer for command-line use (real adapters, or fakes with an echoing model)."""
    from ns_dashboard.assistant.llm import EchoToolLlm, StaticProvider
    from ns_dashboard.handlers import Dashboard, build_state

    state = build_state(Paths.from_env(), fake_services=fake_services)
    provider = StaticProvider(EchoToolLlm()) if fake_services else None
    return Dashboard(state, llm_provider=provider)


def ask(
    text: Annotated[str, typer.Argument(help="What to ask or tell Nightshift.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Accept every confirm card.")] = False,
    conversation: Annotated[
        str | None, typer.Option("--conversation", "-c", help="Continue a conversation.")
    ] = None,
    fake_services: Annotated[bool, typer.Option("--fake-services", help="No GPU: use fakes.")] = False,
) -> None:
    """Ask the Assistant (it uses the same tools as the dashboard; changes need your OK)."""
    dash = build_dashboard(fake_services)
    raise typer.Exit(run_ask(dash, text, conversation=conversation, yes=yes))


def register(parent: typer.Typer) -> None:
    """Add `ask` to another Typer app (`studio`)."""
    parent.command("ask")(ask)


app = typer.Typer(help="Nightshift Assistant")


@app.callback()
def _root() -> None:
    """Nightshift Assistant commands."""


register(app)

if __name__ == "__main__":
    app()
