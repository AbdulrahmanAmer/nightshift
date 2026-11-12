"""The tool table: every `/api` route either declares a tool descriptor here or is listed in NO_TOOL with
the reason (component C9, spec 20.3a).

`build_registry()` returns the `Registry`; a hygiene test compares the table with the app's real routes, so a
new route without a descriptor (or a reason) fails CI.
"""
# Dense descriptor tables and long signatures: wrapping them hurts readability.
# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ns_common.models import SCRIPT_MAX_CHARS, SCRIPT_MIN_CHARS
from ns_dashboard.assistant import composite, drafts, dryrun, orientation
from ns_dashboard.assistant.registry import TOPICS, Registry, ToolCall, ToolContext, ToolDef, ToolSpec, obj
from ns_dashboard.common import offered_formats
from ns_dashboard.errors import ApiError

Route = tuple[str, str]

#: Changing tools without a `dry_run`: every argument they take is an enum or a range the JSON schema already
#: checks AND nothing about the system's state can make the run refuse (the reason says why). Empty today:
#: warmup_engine and shutdown_engine need an engine that answers, and make_diagnostics needs the doctor. A changing
#: tool is in `dryrun` or here, never neither (`tests/test_assistant_validate_before_card.py`).
SCHEMA_ONLY: dict[str, str] = {}

# Routes that are deliberately not tools, with the reason (never-registered actions first).
NO_TOOL: dict[Route, str] = {
    ("GET", "/api/health"): "never: the supervisor's readiness probe, not an action (ServiceDef.health_path)",
    ("POST", "/api/outputs/{output_id:path}/approve"): "never: only the owner approves videos (Queue page)",
    ("POST", "/api/outputs/{output_id:path}/reject"): "never: only the owner rejects videos (Queue page)",
    ("POST", "/api/outputs/{output_id:path}/uploaded"): "never: only the owner confirms a posted video (Queue page)",
    ("POST", "/api/settings/keys"): "never: API keys are entered by the owner (Settings page)",
    ("POST", "/api/update/apply"): "never: installing an update is the owner's decision (Settings page)",
    ("POST", "/api/update/file"): "never: installing an update is the owner's decision (Settings page)",
    ("POST", "/api/engine/stop_all"): "destructive button for the owner (Settings page)",
    ("POST", "/api/settings/export"): "never: backups are made by the owner (Settings page)",
    ("GET", "/api/settings/export/latest"): "binary zip download",
    ("POST", "/api/settings/import"): "never: restoring settings replaces files, so only the owner does it (Settings page)",
    ("GET", "/api/settings/uninstall"): "never: uninstalling is the owner's decision (Settings page)",
    ("GET", "/api/library/series"): "the workshop_list and workshop_status tools read the same files",
    ("POST", "/api/library/series/{slug}/produce"): "the workshop_produce tool queues parts (with a confirm card)",
    ("GET", "/api/events"): "event stream, not a request/response call",
    ("GET", "/api/media/{output_id:path}"): "binary video stream",
    ("GET", "/api/media/{output_id:path}/thumb.jpg"): "binary image",
    ("POST", "/api/uploads"): "chunked file upload plumbing for the browser",
    ("GET", "/api/uploads/{upload_id}"): "chunked file upload plumbing for the browser",
    ("PUT", "/api/uploads/{upload_id}"): "chunked file upload plumbing for the browser",
    ("POST", "/api/uploads/{upload_id}/complete"): "chunked file upload plumbing for the browser",
    ("GET", "/api/logs/components"): "UI filter list; tail_logs covers the content",
    ("GET", "/api/diagnostics/latest"): "binary zip download",
    ("POST", "/api/voices/preview"): "audio playback in the browser",
    ("GET", "/api/voices/pending"): "the Library page shows the voice that setup queued",
    ("POST", "/api/voices/apply-pending"): "never: creating the setup voice downloads and runs the voice model (Library page button)",
    ("POST", "/api/engine/release_gpu"): "never: freeing the graphics card stops whatever holds it (Queue page button)",
    ("POST", "/api/jobs/{job_id}/dismiss"): "a card tidy-up on the Create page (Hide); nothing about the job changes",
    ("POST", "/api/assistant/chat"): "the Assistant itself",
    ("POST", "/api/assistant/confirm"): "the Assistant itself (owner's Yes)",
    ("POST", "/api/assistant/dismiss"): "the Assistant itself (owner's No)",
    ("GET", "/api/assistant/history"): "the Assistant itself",
    ("GET", "/api/assistant/conversations"): "the Assistant itself",
    ("POST", "/api/assistant/queue/run"): "the Assistant itself",
    ("GET", "/api/assistant/tools"): "the Assistant itself (the page words its cards from the registry)",
    ("GET", "/api/thumbs/{n}/{output_id:path}"): "binary image",
    ("GET", "/api/update/state"): "progress of the detached updater, polled by the Settings page",
    ("POST", "/api/models/get"): "never: model downloads are the owner's decision (Settings page)",
    ("GET", "/api/models/state"): "progress of a model download, polled by the Settings page",
    ("GET", "/api/channels/{slug}/formats"): "the channel read tool's answer carries the same formats_in_use",
    ("GET", "/api/notes"): "notes for the bell and Last night (the owner reads them)",
    ("POST", "/api/notes/clear"): "the owner has read the notes",
    ("DELETE", "/api/library/footage/{source}/{clip_id}"): "never: deleting files is the owner's decision (Library page)",
    ("DELETE", "/api/library/ideas/{idea_id}"): "never: removing an idea is the owner's decision (Library page)",
    ("DELETE", "/api/library/stories/{job_id}"): "never: removing a story and its videos is the owner's decision (Library page)",
    ("DELETE", "/api/voices/{name}"): "never: removing a voice is the owner's decision (Library page)",
    ("POST", "/api/drafts/{draft_id}/open"): "the Open button of a draft card: the owner opens his file; the Assistant names its path",
}


def _s(desc: str, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": desc, **extra}


def _i(desc: str, **extra: Any) -> dict[str, Any]:
    return {"type": "integer", "description": desc, **extra}


def _b(desc: str) -> dict[str, Any]:
    return {"type": "boolean", "description": desc}


def _o(desc: str) -> dict[str, Any]:
    return {"type": "object", "description": desc}


def _t(
    name: str, topic: str, description: str, call: ToolCall, *, route: Route | None = None,
    props: dict[str, Any] | None = None, required: list[str] | None = None,
    side: bool = False, confirm: str = "", precheck: Callable[[dict[str, Any]], str | None] | None = None,
    dry_run: Callable[[ToolContext, dict[str, Any]], None] | None = None,
) -> ToolDef:
    spec = ToolSpec(
        name=name, description=description, args_schema=obj(props, required), side_effect=side,
        confirm_text=confirm if side else "", topic=topic,
    )
    return ToolDef(spec=spec, call=call, route=route, precheck=precheck, dry_run=dry_run)


def _outputs(c: ToolContext, a: dict[str, Any]) -> Any:
    return c.dash.outputs.list_outputs(a.get("status"), a.get("kind"), a.get("channel"), int(a.get("limit", 20)))


def _edit_channel(c: ToolContext, a: dict[str, Any]) -> Any:
    return c.dash.channels.update(a["slug"], dryrun.merged_channel(c, a))


def _set_schedule(c: ToolContext, a: dict[str, Any]) -> Any:
    body = {k: a[k] for k in ("schedule_time", "notifications") if k in a}
    if not body:
        raise ApiError(422, "invalid_options", "Give a schedule_time (HH:MM) or notifications (true/false).")
    s = c.dash.settings.put_settings(body)
    out = {k: s[k] for k in ("schedule_time", "notifications", "next_run", "schedule_result") if k in s}
    if "schedule_time" in body:  # a time alone starts nothing while the switch is off: say which it is
        out["automatic_runs"] = composite.automatic_runs(c)
    return out


def _import_analytics(c: ToolContext, a: dict[str, Any]) -> Any:
    p = Path(str(a["path"])).expanduser()
    if not p.is_file():
        raise ApiError(400, "bad_path", f"'{p}' is not a file on this PC.")
    with p.open("rb") as fh:
        return c.dash.jobs.import_learn(p.name, fh)


def _clip_source_given(args: dict[str, Any]) -> str | None:
    if not (args.get("path") or args.get("url")):
        return "Give the video as a path on this PC (path) or as a web link (url)."
    return None


def _start_clip(c: ToolContext, a: dict[str, Any]) -> Any:
    if not (a.get("path") or a.get("url")):
        raise ApiError(422, "invalid_options", "Give the video as a path on this PC or as a web link (url).")
    return c.dash.jobs.create_clip({**a, "rights_confirmed": True})


def _script_ok(args: dict[str, Any]) -> str | None:
    """The one rule the schema cannot say; the rest is `_check_script`, which knows the channel."""
    text = str(args.get("script") or "").strip()
    if len(text) < SCRIPT_MIN_CHARS:
        return f"The script is too short: it needs at least {SCRIPT_MIN_CHARS} characters, it has {len(text)}."
    return None


def _start_script(c: ToolContext, a: dict[str, Any]) -> Any:
    return c.dash.jobs.create_run(dryrun.script_body(a))


#: What `start_script_job` says to a long video: its story never fits one reply (G-43 b).
USE_A_DRAFT = ("A long video's story is too long for one reply: write it as a draft file instead, one part per call "
               "(draft_create, then draft_append_part for each part, then start_job_from_draft). If the owner pasted "
               "the story himself, he can start it on the Create page (Use my own story text).")


def _script_format(c: ToolContext, a: dict[str, Any]) -> str | None:
    """The type a `start_script_job` call would make: its own `format`, else the channel's (None if unknown)."""
    if a.get("format"):
        return str(a["format"])
    try:
        return str(c.dash.channels.load(str(a.get("channel") or "")).format)
    except ApiError:
        return None


def _check_script_kept(c: ToolContext, a: dict[str, Any]) -> None:
    """start_script_job's check before the card: a long video is refused with "use a draft", and a text that is
    refused for any reason is kept as a draft file and named, so a story the Assistant wrote is never lost (G-47)."""
    fmt = _script_format(c, a)
    try:
        if fmt == "master_first":
            raise ApiError(422, "use_a_draft", USE_A_DRAFT)
        dryrun.check_script(c, a)
    except ApiError as e:
        try:
            kept = drafts.keep_text(
                c.dash.state.paths, text=str(a.get("script") or ""), title=str(a.get("title") or ""),
                channel=str(a.get("channel") or ""), fmt=fmt if fmt in drafts.FORMATS else "master_first",
                conversation_id=c.conversation_id,
            )
        except (ApiError, OSError):  # the text could not be kept: the refusal itself still goes out
            raise e from None
        raise ApiError(e.status, e.code, f"{e.message.rstrip()} The text is kept as a draft: {kept.path} (draft id "
                       f"{kept.draft_id}).", hint=e.hint, details=e.details) from e


def _formats_brief(c: ToolContext, _a: dict[str, Any]) -> Any:
    keys = ("id", "name", "kind", "canvas", "source", "valid")
    return [{k: f[k] for k in keys} for f in c.dash.library.list_formats()]


def _describe(c: ToolContext, a: dict[str, Any]) -> Any:
    td = c.dash.registry.get(str(a.get("name", "")))
    if td is None:
        return {"found": False, "hint": "Use list_tools to see every tool name."}
    s = td.spec
    return {"found": True, "name": s.name, "topic": s.topic, "description": s.description,
            "changes_something": s.side_effect, "arguments": s.args_schema,
            "api_route": f"{td.route[0]} {td.route[1]}" if td.route else None}


def _list_tools(c: ToolContext, a: dict[str, Any]) -> Any:
    grouped = c.dash.registry.by_topic(a.get("topic"))
    if not a.get("topic"):  # the whole list would not fit the model's window: names only
        return {"topics": {topic: [s.name for s in specs] for topic, specs in grouped.items()},
                "note": "Call list_tools with a topic to see what each tool does and to get that topic's tools."}
    if not grouped:
        return {"error": {"code": "unknown_topic", "message": f"Topics are: {', '.join(TOPICS)}."}}
    return {topic: [{"name": s.name, "does": s.description, "changes_something": s.side_effect} for s in specs]
            for topic, specs in grouped.items()}


def _tools() -> list[ToolDef]:
    ch = {"slug": _s("channel slug, e.g. after-dark")}
    return [
        # ------------------------------------------------ engine / status
        _t("status", "engine", "Engine state, outbox counts, channels, next run, disk space.",
           lambda c, a: c.dash.engine.status(), route=("GET", "/api/status")),
        _t("warmup_engine", "engine", "Start the language model now so the first job is fast.",
           lambda c, a: c.dash.engine.warmup(a.get("service")), route=("POST", "/api/engine/warmup"),
           props={"service": _s("service to start", enum=["llama", "tts", "whisper", "embed"])},
           side=True, dry_run=dryrun.check_warmup, confirm="Load the language model now so the next job starts fast."),
        _t("shutdown_engine", "engine", "Quit Nightshift: stop the engine after the current stage (up to 30 s).",
           lambda c, a: c.dash.engine.shutdown(a.get("grace_s")), route=("POST", "/api/engine/shutdown"),
           props={"grace_s": _i("seconds to wait for the running stage", minimum=0, maximum=300)},
           side=True, dry_run=dryrun.check_shutdown, confirm="Quit Nightshift. A running job is cancelled after its current stage."),
        # ------------------------------------------------ channels
        _t("list_channels", "channels", "All channels with status and how many videos wait for review.",
           lambda c, a: c.dash.channels.cards(), route=("GET", "/api/channels")),
        _t("get_channel", "channels", "One channel's full profile and warnings.",
           lambda c, a: c.dash.channels.get(a["slug"]), route=("GET", "/api/channels/{slug}"),
           props=ch, required=["slug"]),
        _t("create_channel", "channels", "Create a channel from a full profile (slug, display_name, niche, ...).",
           lambda c, a: c.dash.channels.create(a["profile"]), route=("POST", "/api/channels"),
           props={"profile": _o("channel profile fields")}, required=["profile"], side=True, dry_run=dryrun.check_create_channel,
           confirm="Create the channel '{profile}'."),
        _t("edit_channel", "channels", "Change some fields of a channel profile (give only the changed fields).",
           _edit_channel, route=("PUT", "/api/channels/{slug}"),
           props={**ch, "changes": _o("fields to change, e.g. {\"voice_notes\": \"calmer\"}")},
           required=["slug", "changes"], side=True, dry_run=dryrun.check_edit_channel, confirm="Change channel {slug}: {changes}"),
        _t("pause_channel", "channels", "Stop producing for a channel until it is resumed.",
           lambda c, a: c.dash.channels.pause(a["slug"], a.get("note")),
           route=("POST", "/api/channels/{slug}/pause"), props={**ch, "note": _s("why")},
           required=["slug"], side=True, dry_run=dryrun.check_channel_exists, confirm="Pause channel {slug}. Nothing is made for it until you resume it."),
        _t("resume_channel", "channels", "Start producing for a paused channel again.",
           lambda c, a: c.dash.channels.resume(a["slug"], a.get("note")),
           route=("POST", "/api/channels/{slug}/resume"), props={**ch, "note": _s("why")},
           required=["slug"], side=True, dry_run=dryrun.check_channel_exists, confirm="Resume channel {slug}."),
        _t("validate_channel", "channels", "Check a channel profile (saved, or a draft) and list warnings.",
           lambda c, a: c.dash.channels.validate(a["slug"], a.get("draft")),
           route=("POST", "/api/channels/{slug}/validate"), props={**ch, "draft": _o("optional draft profile")},
           required=["slug"]),
        # ------------------------------------------------ queue
        _t("list_queue", "queue", "Videos by status (pending = ready for review), newest first.", _outputs,
           route=("GET", "/api/outputs"),
           props={"status": _s("which videos", enum=["pending", "approved", "rejected", "uploaded"]),
                  "kind": _s("type", enum=["long", "short", "clip"]), "channel": _s("channel slug"),
                  "limit": _i("how many", minimum=1, maximum=100)}),
        _t("get_output", "queue", "One video: title, description, tags, policy notes, story excerpt, file paths.",
           lambda c, a: c.dash.outputs.detail(a["output_id"]), route=("GET", "/api/outputs/{output_id:path}"),
           props={"output_id": _s("output id such as after-dark-20261101-ab12/s1")}, required=["output_id"]),
        _t("open_folder", "queue", "Open the folder that holds a video in Windows Explorer.",
           lambda c, a: c.dash.outputs.open_folder(a["output_id"]),
           route=("POST", "/api/outputs/{output_id:path}/open-folder"),
           props={"output_id": _s("output id")}, required=["output_id"]),
        # ------------------------------------------------ create / jobs
        _t("start_story_job", "create", "The model writes a NEW story and makes the video (and Shorts) for a channel. If the owner gives story text, use start_script_job.",
           lambda c, a: c.dash.jobs.create_run(a), route=("POST", "/api/jobs/run"),
           props={"channel": _s("channel slug"), "format": _s(
               "what to make", enum=list(offered_formats())),
                  "count": _i("how many", minimum=1, maximum=20),
                  "schedule_at": _s("start later: local date and time such as 2026-11-03T02:00; leave out to start now")},
           required=["channel"], side=True, dry_run=dryrun.check_story,
           confirm="Make a new {format} for {channel}, start: {schedule_at}. This uses the GPU for a while."),
        _t("start_script_job", "create", "Make one Short from story text the owner wrote or pasted, narrated as written. Long video: use draft_create. New story by the model: start_story_job.",
           _start_script,
           props={"channel": _s("channel slug"),
                  "format": _s("what to make: a Short or a long video; leave out for the channel's own type", enum=["shorts_only", "master_first"]),
                  "script": _s("the full story text to narrate, 200 to 30000 characters", maxLength=SCRIPT_MAX_CHARS),
                  "title": _s("working title; leave out to use the first words")},
           required=["channel", "script"], side=True, precheck=_script_ok, dry_run=_check_script_kept,
           confirm="Make a {format} for {channel} from the script you gave ({words} words). This uses the GPU for a while."),
        # ------------------------------------------------ drafts (G-43 b, G-47): a story is a file, one part per call
        _t("draft_create", "drafts", "Start a story draft file in the drafts folder for a channel. Write the story into it one part per call, never all at once.",
           drafts.tool_create,
           props={"title": _s("the story's working title"), "channel": _s("channel slug"),
                  "format": _s("what it becomes: a Short or a long video", enum=list(drafts.FORMATS)),
                  "text": _s(f"optional: the first part only, at most about {drafts.PART_WORDS_HINT} words", maxLength=drafts.PART_MAX_CHARS),
                  "part_title": _s("optional: the first part's heading")},
           required=["title", "channel"]),
        _t("draft_append_part", "drafts", f"Add ONE part of the story at the end of a draft file (at most about {drafts.PART_WORDS_HINT} words per call).",
           drafts.tool_append,
           props={"draft": _s("the draft id or its path"),
                  "text": _s(f"this part's story text only, at most about {drafts.PART_WORDS_HINT} words", maxLength=drafts.PART_MAX_CHARS),
                  "part_title": _s("this part's heading")},
           required=["draft", "text"]),
        _t("draft_read", "drafts", "Read a draft file: its parts, word count and how it ends, or one part's text. The owner may have edited it.",
           drafts.tool_read, route=("GET", "/api/drafts/{draft_id}"),
           props={"draft": _s("the draft id or its path"), "part": _i("one part's number, to read its text", minimum=1, maximum=drafts.MAX_PARTS)},
           required=["draft"]),
        _t("list_drafts", "drafts", "The newest story drafts with their paths, word counts and status.",
           drafts.tool_list, props={"limit": _i("how many", minimum=1, maximum=50)}),
        _t("start_job_from_draft", "drafts", "Make the video from a draft file. Nightshift reads the file itself when the owner says Yes; never repeat the story here.",
           drafts.tool_start,
           props={"draft": _s("the draft id or its path"), "channel": _s("channel slug"),
                  "format": _s("a Short or a long video; leave out for the draft's own type", enum=list(drafts.FORMATS)),
                  "title": _s("working title; leave out for the draft's title")},
           required=["draft", "channel"], side=True, dry_run=drafts.check_start,
           confirm="Make a {format} for {channel} from the draft {draft}, as the file reads when you press Yes. This uses the GPU for a while."),
        _t("start_clip_job", "create", "Cut clips from one of the owner's own videos: a file on this PC or a web link.",
           _start_clip, route=("POST", "/api/jobs/clip"),
           props={"path": _s("full path of the video file on this PC"), "url": _s("http or https link to the video"),
                  "channel": _s("channel slug"),
                  "template": _s("layout id, 'auto' lets Nightshift choose"),
                  "max_clips": _i("most clips", minimum=1, maximum=10),
                  "min_s": _i("shortest clip, seconds", minimum=5, maximum=120),
                  "max_s": _i("longest clip, seconds", minimum=10, maximum=179),
                  "caption_style": _s("caption style name")},
           required=["channel"], side=True, dry_run=dryrun.check_clip, precheck=_clip_source_given,
           confirm=("Make up to {max_clips} clips for {channel} from {source} (layout {template}). By pressing Yes "
                    "you confirm you own this video or are licensed to use it.")),
        _t("rerun_clips", "create", "Make the clips of a finished clip job again with another layout, without transcribing again.",
           lambda c, a: c.dash.jobs.create_clip_rerun(a), route=("POST", "/api/jobs/clip/rerun"),
           props={"job_id": _s("id of the finished clip job"), "template": _s("layout id, 'auto' lets Nightshift choose"),
                  "max_clips": _i("most clips", minimum=1, maximum=10)},
           required=["job_id", "template"], side=True, dry_run=dryrun.check_rerun,
           confirm="Make the clips of job {job_id} again with the {template} layout. The old clips are replaced."),
        _t("start_verify", "create", "Run the machine self-check (ns verify).",
           lambda c, a: c.dash.jobs.create_verify(a), route=("POST", "/api/jobs/verify"),
           props={"quick": _b("quick check only")}, side=True, dry_run=dryrun.check_verify, confirm="Run the Nightshift self-check."),
        _t("list_jobs", "create", "Recent jobs with status and progress.",
           lambda c, a: c.dash.jobs.list_jobs(a.get("status"), a.get("kind"), int(a.get("limit", 10))),
           route=("GET", "/api/jobs"),
           props={"status": _s("filter", enum=["queued", "running", "done", "failed", "cancelled"]),
                  "kind": _s("filter", enum=["run", "clip", "learn", "verify", "series_part"]),
                  "limit": _i("how many", minimum=1, maximum=50)}),
        _t("get_job", "create", "One job's status, stage and result.",
           lambda c, a: c.dash.jobs.get(a["job_id"]), route=("GET", "/api/jobs/{job_id}"),
           props={"job_id": _s("job id")}, required=["job_id"]),
        _t("resume_job", "create", "Continue a story job that stopped with an error from the stage where it stopped (same story).",
           lambda c, a: c.dash.jobs.create_resume(a["job_id"]), route=("POST", "/api/jobs/{job_id}/resume"),
           props={"job_id": _s("id of the job that stopped")}, required=["job_id"], side=True, dry_run=dryrun.check_resume,
           confirm="Continue job {job_id} from the stage where it stopped, and make the videos of its request that "
                   "had not started. This uses the GPU for a while."),
        _t("cancel_job", "create", "Cancel a queued or running job (a running one stops after its current stage).",
           lambda c, a: c.dash.jobs.cancel(a["job_id"]), route=("POST", "/api/jobs/{job_id}/cancel"),
           props={"job_id": _s("job id")}, required=["job_id"], side=True, dry_run=dryrun.check_cancel, confirm="Cancel job {job_id}."),
        # ------------------------------------------------ results / learn
        _t("list_runs", "results", "Recent nightly and manual runs with timings.",
           lambda c, a: c.dash.engine.runs(int(a.get("limit", 10))), route=("GET", "/api/runs"),
           props={"limit": _i("how many", minimum=1, maximum=50)}),
        _t("summary_results", "results", "What the learning loop found works, what changes next, unmatched videos.",
           lambda c, a: c.dash.settings.learn_summary(), route=("GET", "/api/learn/summary")),
        _t("import_analytics", "results", "Import a YouTube Studio analytics CSV file from this PC.",
           _import_analytics, route=("POST", "/api/learn/import"),
           props={"path": _s("full path of the .csv file")}, required=["path"], side=True, dry_run=dryrun.check_import,
           confirm="Import the YouTube analytics file {path} and update what Nightshift learns."),
        # ------------------------------------------------ settings / diagnostics
        _t("get_settings", "settings", "Run time, notifications, engine profile and flags, which keys are set (never the keys).",
           lambda c, a: c.dash.settings.get_settings(), route=("GET", "/api/settings")),
        _t("set_schedule", "settings", "Change the nightly run time (HH:MM) or turn notifications on or off. It cannot switch automatic runs on or off: the owner does that in Settings.",
           _set_schedule, route=("PUT", "/api/settings"),
           props={"schedule_time": _s("24-hour time", pattern=r"^([01]\d|2[0-3]):[0-5]\d$"),
                  "notifications": _b("show Windows notifications")}, side=True, dry_run=dryrun.check_schedule,
           confirm="Set the nightly run time to {schedule_time} (notifications: {notifications})."),
        _t("tail_logs", "diagnostics", "Latest log lines, optionally for one component and a minimum level.",
           lambda c, a: c.dash.settings.tail(a.get("component"), a.get("level"), int(a.get("n", 40)), None),
           route=("GET", "/api/logs/tail"),
           props={"component": _s("e.g. supervisor, studio, dashboard"),
                  "level": _s("minimum level", enum=["DEBUG", "INFO", "WARN", "ERROR"]),
                  "n": _i("how many lines", minimum=1, maximum=200)}),
        _t("make_diagnostics", "diagnostics", "Create the diagnostics zip to send to the developer.",
           lambda c, a: c.dash.settings.diagnostics(), route=("POST", "/api/diagnostics"),
           side=True, dry_run=dryrun.check_diagnostics, confirm="Create a diagnostics report (a zip of logs and settings, no keys)."),
        _t("check_update", "settings", "Is a newer Nightshift version available on GitHub?",
           lambda c, a: c.dash.library.update_check(), route=("GET", "/api/update/check")),
        # ------------------------------------------------ lists
        _t("list_templates", "create", "Clip layouts available for the Clipper.",
           lambda c, a: [{k: t[k] for k in ("id", "label", "description", "when")} for t in c.dash.settings.templates()],
           route=("GET", "/api/templates")),
        _t("list_voices", "library", "Narration voices (presets, designed, cloned).",
           lambda c, a: c.dash.settings.voices(), route=("GET", "/api/voices")),
        _t("design_voice", "library", "Create a new narration voice from a description in words, and speak a test line.",
           lambda c, a: c.dash.voices.design(a), route=("POST", "/api/voices/design"),
           props={"name": _s("voice name: 1 to 40 letters, digits, - or _, starting with a letter or digit"), "description": _s("how the voice sounds, in plain English"),
                  "test_line": _s("a sentence the new voice speaks as a preview")},
           required=["name", "description", "test_line"], side=True, dry_run=dryrun.check_voice_design,
           confirm="Create the voice '{name}' ({description}) and speak: {test_line}. This uses the GPU for about a minute."),
        _t("clone_voice", "library", "Create a narration voice from a recording of a speaker, and speak a test line.",
           lambda c, a: c.dash.voices.clone({**a, "consent": True}), route=("POST", "/api/voices/clone"),
           props={"name": _s("voice name: 1 to 40 letters, digits, - or _, starting with a letter or digit"), "sample_path": _s("full path of a 3-30 second recording of one speaker on this PC"),
                  "test_line": _s("a sentence the new voice speaks as a preview")},
           required=["name", "sample_path", "test_line"], side=True, dry_run=dryrun.check_voice_clone,
           confirm=("Clone the voice in {sample_path} as '{name}' and speak: {test_line}. By pressing Yes you confirm "
                    "you have the right to clone this voice.")),
        # ------------------------------------------------ library
        _t("list_ideas", "library", "The idea bank, optionally for one channel or status.",
           lambda c, a: c.dash.library.list_ideas(a.get("channel"), a.get("status"), a.get("q"), int(a.get("limit", 30))),
           route=("GET", "/api/library/ideas"),
           props={"channel": _s("channel slug"), "status": _s("filter", enum=["new", "approved", "used", "rejected"]),
                  "q": _s("search words"), "limit": _i("how many", minimum=1, maximum=100)}),
        _t("add_idea", "library", "Add a story idea to the bank for a channel.",
           lambda c, a: c.dash.library.add_idea({**a, "source": "assistant"}), route=("POST", "/api/library/ideas"),
           props={"channel": _s("channel slug"), "premise": _s("the idea in 1-3 sentences"),
                  "title": _s("short title"), "twist": _s("the twist"),
                  "status": _s("start as", enum=["new", "approved"])},
           required=["channel", "premise"], side=True, dry_run=dryrun.check_add_idea, confirm="Add this idea for {channel}: {premise}"),
        _t("set_idea_status", "library", "Mark an idea new, approved for production, used or rejected.",
           lambda c, a: c.dash.library.set_idea_status(int(a["idea_id"]), a.get("status")),
           route=("PUT", "/api/library/ideas/{idea_id}"),
           props={"idea_id": _i("idea id"), "status": _s("new status", enum=["new", "approved", "used", "rejected"])},
           required=["idea_id", "status"], side=True, dry_run=dryrun.check_idea_status, confirm="Mark idea {idea_id} as {status}."),
        _t("search_stories", "library", "Written stories with their videos and where they were posted.",
           lambda c, a: c.dash.library.stories(a.get("q"), a.get("channel"), int(a.get("limit", 10))),
           route=("GET", "/api/library/stories"),
           props={"q": _s("search words"), "channel": _s("channel slug"), "limit": _i("how many", minimum=1, maximum=50)}),
        _t("search_footage", "library", "Cached stock footage by keyword, licence and last use.",
           lambda c, a: c.dash.library.footage(a.get("q"), int(a.get("limit", 10))),
           route=("GET", "/api/library/footage"),
           props={"q": _s("keyword"), "limit": _i("how many", minimum=1, maximum=50)}),
        _t("fetch_footage", "library",
           "Download stock clips for a scene into the footage cache now and return their absolute file paths. Needs a Pixabay or Pexels key set in Settings.",
           lambda c, a: c.dash.library.fetch_footage(a.get("q"), a.get("orientation"), a.get("count"), a.get("source")),
           route=("POST", "/api/library/footage/fetch"),
           props={"q": _s("what the scene shows, in a few words, e.g. 'foggy forest road at night'"),
                  "orientation": _s("clip shape", enum=["landscape", "portrait"], default="landscape"),
                  "count": _i("how many clips (at most 5)", minimum=1, maximum=5, default=1),
                  "source": _s("where to look", enum=["auto", "pixabay", "pexels"], default="auto")},
           required=["q"], side=True, dry_run=dryrun.check_footage,
           confirm="Download up to {count} {orientation} stock clips for '{q}' from {source} into the footage cache."),
        _t("list_formats", "library", "Video formats and clip layouts (editable presets).", _formats_brief,
           route=("GET", "/api/library/formats")),
        _t("duplicate_format", "library", "Make a copy of a format, optionally changing some fields.",
           lambda c, a: c.dash.library.create_format(a), route=("POST", "/api/library/formats"),
           props={"duplicate_of": _s("format id to copy"), "id": _s("new format id"),
                  "changes": _o("fields to change in the copy")},
           required=["duplicate_of"], side=True, dry_run=dryrun.check_format_copy, confirm="Copy format {duplicate_of} as {id}."),
        _t("edit_format", "library", "Change fields of a format (editing a shipped one saves your own copy).",
           lambda c, a: c.dash.library.update_format(a["id"], {"changes": a["changes"]}),
           route=("PUT", "/api/library/formats/{format_id}"),
           props={"id": _s("format id"), "changes": _o("fields to change")}, required=["id", "changes"],
           side=True, dry_run=dryrun.check_format_edit, confirm="Change format {id}: {changes}"),
        # ------------------------------------------------ orientation (no route)
        _t("system_map", "orientation", "Compact map: pages, folders, services, channels, formats, engine state.",
           orientation.system_map),
        _t("find", "orientation", "Search outputs, stories, ideas, channels, jobs, logs and docs at once.",
           orientation.find, props={"query": _s("words to look for")}, required=["query"]),
        _t("where_is", "orientation", "Absolute path or page for an output, job, channel, model, log or setting.",
           orientation.where_is, props={"thing": _s("e.g. an output id, 'logs', 'llm', 'schedule', 'footage cache'")},
           required=["thing"]),
        _t("describe_tool", "orientation", "Full description and arguments of one tool.", _describe,
           props={"name": _s("tool name")}, required=["name"]),
        _t("list_tools", "orientation", "Tools by topic. With a topic it lists that topic's tools and shows them to you from the next step.",
           _list_tools, props={"topic": _s("only this topic", enum=list(TOPICS))}),
        _t("search_docs", "orientation", "Ranked passages from the README, operator guide, error list and what the Assistant can do.",
           orientation.search_docs,
           props={"query": _s("what to look up"), "limit": _i("how many", minimum=1, maximum=10)},
           required=["query"]),
        _t("explain_error", "orientation", "Meaning and fix hint of an NS-E / NS-W code, plus recent log lines.",
           orientation.explain_error, props={"code": _s("e.g. NS-E203")}, required=["code"]),
    ]


def _described(schema: dict[str, Any]) -> dict[str, Any]:
    """The workshop's argument schema with a description on every property (the registry requires one)."""
    props = {
        k: v if (v.get("description") or "enum" in v) else {**v, "description": k.replace("_", " ")}
        for k, v in schema.get("properties", {}).items()
    }
    return {**schema, "properties": props}


def _workshop_call(name: str) -> Callable[[ToolContext, dict[str, Any]], Any]:
    """Run workshop tool `name` on a `Workshop` over this home (fake services only in fake mode)."""

    def call(c: ToolContext, args: dict[str, Any]) -> Any:
        from ns_dashboard import studio_bridge
        from ns_dashboard.supervisor_link import make_acquirer
        from ns_studio.series.tools import call_tool

        state = c.dash.state
        fake = studio_bridge.FAKE_ALL if state.fake_services else ()
        acquirer = None if state.fake_services else make_acquirer(state.link.base_url)
        with studio_bridge.workshop_session(acquirer=acquirer, fake=fake) as ws:
            out = call_tool(ws, name, args)
        if name == "workshop_produce":  # the rows are in dashboard_jobs; wake the worker now
            for item in out:
                state.worker.enqueue(str(item["job_id"]))
        return out

    return call


def _workshop_tools() -> list[ToolDef]:
    """The Series Workshop tools of `ns_studio.series.tools`, mounted with confirm cards (gap 7)."""
    from ns_studio.series.tools import TOOLS

    out: list[ToolDef] = []
    for d in TOOLS:
        side = bool(d["side_effect"])
        spec = ToolSpec(
            name=d["name"], description=d["description"], args_schema=_described(d["args_schema"]),
            side_effect=side, confirm_text=d["confirm_text"] if side else "", topic="series",
        )
        out.append(ToolDef(spec=spec, call=_workshop_call(d["name"]), dry_run=dryrun.workshop_check(d["name"]) if side else None))
    return out


#: Result fields that survive when a long result is shrunk or digested for the model (`prompt.shrink_result`).
SUMMARY_FIELDS: dict[str, tuple[str, ...]] = {
    "list_queue": ("output_id", "title", "channel", "kind", "status", "duration_s"),
    "get_output": ("output_id", "title", "channel", "kind", "status", "policy_notes", "ai_disclosure", "made_for_kids", "folder", "story_excerpt", "description", "tags"),
    "list_jobs": ("job_id", "kind", "channel", "status", "stage", "message"),
    "get_job": ("job_id", "kind", "channel", "status", "stage", "message", "result"),
    "search_stories": ("job_id", "channel", "working_title", "opening_line", "outputs"),
    "list_ideas": ("idea_id", "channel", "title", "premise", "status"),
    "find": ("query", "hits"),
    "search_docs": ("query", "hits"),
}


def _with_summary(td: ToolDef) -> ToolDef:
    fields = SUMMARY_FIELDS.get(td.spec.name)
    if not fields or td.spec.summary_fields:
        return td
    return replace(td, spec=replace(td.spec, summary_fields=fields))


def build_registry() -> Registry:
    """Registry of every tool (route-derived, orientation, composite read tools and the workshop)."""
    return Registry([_with_summary(t) for t in [*_tools(), *composite.tools(), *_workshop_tools()]])
