"""Look-before-you-ask checks of the Assistant's changing tools (component C9, defect D-27).

Every tool that changes something shows the owner a confirm card first. A card the owner can only confirm into
a refusal ("That did not work: 'schedule_at' must be a date and time...") is worse than no card, so each such
tool carries a `dry_run(ctx, args)` that makes the SAME checks the real run makes, through the service's own
`check_*` function (the real run calls it too, so the two cannot drift apart), and raises `ApiError` for what
is wrong. The loop turns that into a tool error that says in plain words what is missing, and shows no card.

A tool whose only arguments are enum or range values the JSON schema already checks has no `dry_run`; it is
listed in `tools.SCHEMA_ONLY` with the reason, and a test fails for a new changing tool that is in neither place.
"""

# Dense descriptor tables and long signatures: wrapping them hurts readability.
# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ns_dashboard.assistant.registry import ToolContext
from ns_dashboard.errors import ApiError
from ns_dashboard.formats import merge_dicts
from ns_studio.series.store import WorkshopError

DryRun = Callable[[ToolContext, dict[str, Any]], None]


def script_body(a: dict[str, Any]) -> dict[str, Any]:
    """The `JobRunRequest` fields of a `start_script_job` call."""
    return {
        "channel": a["channel"],
        "format": a.get("format"),
        "script": a["script"],
        "script_title": a.get("title"),
        "count": 1,
    }


def check_story(c: ToolContext, a: dict[str, Any]) -> None:
    """start_story_job: channel, type, count, footage key and start time, as `POST /api/jobs/run` checks them."""
    c.dash.jobs.check_run(a)


def check_script(c: ToolContext, a: dict[str, Any]) -> None:
    """start_script_job: the story-job checks plus the script (length at the channel's pace, banned phrases, headings)."""
    c.dash.jobs.check_run(script_body(a))


def check_clip(c: ToolContext, a: dict[str, Any]) -> None:
    """start_clip_job: channel, layout, lengths, start time and that the file or link can be used."""
    c.dash.jobs.check_clip({**a, "rights_confirmed": True})


def check_rerun(c: ToolContext, a: dict[str, Any]) -> None:
    """rerun_clips: a finished clip job and a layout the clipper can cut."""
    c.dash.jobs.check_clip_rerun(a)


def check_verify(c: ToolContext, _a: dict[str, Any]) -> None:
    """start_verify: refused while Nightshift shuts down."""
    c.dash.jobs.check_verify()


def check_resume(c: ToolContext, a: dict[str, Any]) -> None:
    """resume_job: a failed story job that left something to continue from."""
    c.dash.jobs.check_resume(str(a["job_id"]))


def check_cancel(c: ToolContext, a: dict[str, Any]) -> None:
    """cancel_job: a job that exists and has not ended."""
    c.dash.jobs.check_cancel(str(a["job_id"]))


def check_import(c: ToolContext, a: dict[str, Any]) -> None:
    """import_analytics: a non-empty .csv file that exists."""
    c.dash.jobs.check_csv_file(Path(str(a["path"])).expanduser())


def check_schedule(c: ToolContext, a: dict[str, Any]) -> None:
    """set_schedule: at least one of the two settings, and a time Windows can use."""
    body = {k: a[k] for k in ("schedule_time", "notifications") if k in a}
    if not body:
        raise ApiError(422, "invalid_options", "Give a schedule_time (HH:MM) or notifications (true/false).")
    c.dash.settings.check_settings(body)


def merged_channel(c: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    """The profile of channel `slug` with `changes` merged in (what edit_channel saves)."""
    current = c.dash.channels.load(a["slug"]).model_dump(mode="json")
    return merge_dicts(current, a["changes"])


def check_create_channel(c: ToolContext, a: dict[str, Any]) -> None:
    """create_channel: a free slug and a profile that passes the channel checks."""
    c.dash.channels.check_create(a["profile"])


def check_edit_channel(c: ToolContext, a: dict[str, Any]) -> None:
    """edit_channel: a channel that exists and a changed profile that passes the channel checks."""
    c.dash.channels.check_update(a["slug"], merged_channel(c, a))


def check_channel_exists(c: ToolContext, a: dict[str, Any]) -> None:
    """pause_channel, resume_channel: a channel that exists."""
    c.dash.channels.load(a["slug"])


def check_voice_design(c: ToolContext, a: dict[str, Any]) -> None:
    """design_voice: a free voice name and a description and test line of a usable length."""
    c.dash.voices.check_design(a)


def check_voice_clone(c: ToolContext, a: dict[str, Any]) -> None:
    """clone_voice: a free voice name, a test line, and a recording that exists (consent is the Yes itself)."""
    c.dash.voices.check_clone({**a, "consent": True})


def check_add_idea(c: ToolContext, a: dict[str, Any]) -> None:
    """add_idea: a channel that exists and a premise of one to three sentences."""
    c.dash.library.check_idea({**a, "source": "assistant"})


def check_idea_status(c: ToolContext, a: dict[str, Any]) -> None:
    """set_idea_status: an idea that exists and a status it can have."""
    c.dash.library.check_idea_status(int(a["idea_id"]), a.get("status"))


def check_footage(c: ToolContext, a: dict[str, Any]) -> None:
    """fetch_footage: search words, shape, source, and a stock-footage key to search with."""
    c.dash.library.check_footage(a.get("q"), a.get("orientation"), a.get("count"), a.get("source"))


def check_format_copy(c: ToolContext, a: dict[str, Any]) -> None:
    """duplicate_format: a format that exists, a free id and a copy that passes the format checks."""
    c.dash.library.check_format(None, a)


def check_format_edit(c: ToolContext, a: dict[str, Any]) -> None:
    """edit_format: a format that exists and a changed definition that passes the format checks."""
    c.dash.library.check_format(str(a["id"]), {"changes": a["changes"]})


def workshop_check(name: str) -> DryRun:
    """The dry run of Series Workshop tool `name`: `Workshop.check`, which reads the story files and calls no model."""

    def check(c: ToolContext, a: dict[str, Any]) -> None:
        from ns_dashboard import studio_bridge

        try:
            with studio_bridge.workshop_session(fake=studio_bridge.READ_ONLY) as ws:
                ws.check(name, a)
        except WorkshopError as e:
            raise ApiError(422, "invalid_options", str(e)) from e

    return check
