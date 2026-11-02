"""The tool table: every `/api` route either declares a tool descriptor here or is listed in NO_TOOL with
the reason (component C9, spec 20.3a).

`build_registry()` returns the `Registry`; a hygiene test compares the table with the app's real routes, so a
new route without a descriptor (or a reason) fails CI.
"""
# Dense descriptor tables and long signatures: wrapping them hurts readability.
# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ns_dashboard.assistant import orientation
from ns_dashboard.assistant.registry import Registry, ToolCall, ToolContext, ToolDef, ToolSpec, obj
from ns_dashboard.errors import ApiError
from ns_dashboard.formats import merge_dicts

Route = tuple[str, str]

# Routes that are deliberately not tools, with the reason (never-registered actions first).
NO_TOOL: dict[Route, str] = {
    ("POST", "/api/outputs/{output_id:path}/approve"): "never: only the owner approves videos (Queue page)",
    ("POST", "/api/outputs/{output_id:path}/reject"): "never: only the owner rejects videos (Queue page)",
    ("POST", "/api/outputs/{output_id:path}/uploaded"): "never: only the owner confirms a posted video (Queue page)",
    ("POST", "/api/settings/keys"): "never: API keys are entered by the owner (Settings page)",
    ("POST", "/api/update/apply"): "never: installing an update is the owner's decision (Settings page)",
    ("POST", "/api/engine/stop_all"): "destructive button for the owner (Settings page)",
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
    ("GET", "/api/templates/{template_id}/preview.png"): "binary image",
    ("POST", "/api/voices/preview"): "audio playback in the browser",
    ("POST", "/api/assistant/chat"): "the Assistant itself",
    ("POST", "/api/assistant/confirm"): "the Assistant itself (owner's Yes)",
    ("POST", "/api/assistant/dismiss"): "the Assistant itself (owner's No)",
    ("GET", "/api/assistant/history"): "the Assistant itself",
    ("GET", "/api/assistant/conversations"): "the Assistant itself",
    ("POST", "/api/assistant/queue/run"): "the Assistant itself",
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
) -> ToolDef:
    spec = ToolSpec(
        name=name, description=description, args_schema=obj(props, required), side_effect=side,
        confirm_text=confirm if side else "", topic=topic,
    )
    return ToolDef(spec=spec, call=call, route=route, precheck=precheck)


def _outputs(c: ToolContext, a: dict[str, Any]) -> Any:
    return c.dash.outputs.list_outputs(a.get("status"), a.get("kind"), a.get("channel"), int(a.get("limit", 20)))


def _edit_channel(c: ToolContext, a: dict[str, Any]) -> Any:
    current = c.dash.channels.load(a["slug"]).model_dump(mode="json")
    return c.dash.channels.update(a["slug"], merge_dicts(current, a["changes"]))


def _set_schedule(c: ToolContext, a: dict[str, Any]) -> Any:
    body = {k: a[k] for k in ("schedule_time", "notifications") if k in a}
    if not body:
        raise ApiError(422, "invalid_options", "Give a schedule_time (HH:MM) or notifications (true/false).")
    s = c.dash.settings.put_settings(body)
    return {k: s[k] for k in ("schedule_time", "notifications", "next_run", "schedule_result") if k in s}


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
           side=True, confirm="Load the language model now so the next job starts fast."),
        _t("shutdown_engine", "engine", "Quit Nightshift: stop the engine after the current stage (up to 30 s).",
           lambda c, a: c.dash.engine.shutdown(a.get("grace_s")), route=("POST", "/api/engine/shutdown"),
           props={"grace_s": _i("seconds to wait for the running stage", minimum=0, maximum=300)},
           side=True, confirm="Quit Nightshift. A running job is cancelled after its current stage."),
        # ------------------------------------------------ channels
        _t("list_channels", "channels", "All channels with status and how many videos wait for review.",
           lambda c, a: c.dash.channels.cards(), route=("GET", "/api/channels")),
        _t("get_channel", "channels", "One channel's full profile and warnings.",
           lambda c, a: c.dash.channels.get(a["slug"]), route=("GET", "/api/channels/{slug}"),
           props=ch, required=["slug"]),
        _t("create_channel", "channels", "Create a channel from a full profile (slug, display_name, niche, ...).",
           lambda c, a: c.dash.channels.create(a["profile"]), route=("POST", "/api/channels"),
           props={"profile": _o("channel profile fields")}, required=["profile"], side=True,
           confirm="Create the channel '{profile}'."),
        _t("edit_channel", "channels", "Change some fields of a channel profile (give only the changed fields).",
           _edit_channel, route=("PUT", "/api/channels/{slug}"),
           props={**ch, "changes": _o("fields to change, e.g. {\"voice_notes\": \"calmer\"}")},
           required=["slug", "changes"], side=True, confirm="Change channel {slug}: {changes}"),
        _t("pause_channel", "channels", "Stop producing for a channel until it is resumed.",
           lambda c, a: c.dash.channels.pause(a["slug"], a.get("note")),
           route=("POST", "/api/channels/{slug}/pause"), props={**ch, "note": _s("why")},
           required=["slug"], side=True, confirm="Pause channel {slug}. Nothing is made for it until you resume it."),
        _t("resume_channel", "channels", "Start producing for a paused channel again.",
           lambda c, a: c.dash.channels.resume(a["slug"], a.get("note")),
           route=("POST", "/api/channels/{slug}/resume"), props={**ch, "note": _s("why")},
           required=["slug"], side=True, confirm="Resume channel {slug}."),
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
        _t("start_story_job", "create", "Make a new story video (and Shorts) for a channel now.",
           lambda c, a: c.dash.jobs.create_run(a), route=("POST", "/api/jobs/run"),
           props={"channel": _s("channel slug"), "format": _s(
               "what to make", enum=["master_first", "shorts_only", "series_compilation"]),
                  "count": _i("how many", minimum=1, maximum=20),
                  "schedule_at": _s("start later: local date and time such as 2026-11-03T02:00; leave out to start now")},
           required=["channel"], side=True,
           confirm="Make a new {format} for {channel}, start: {schedule_at}. This uses the GPU for a while."),
        _t("start_clip_job", "create", "Cut clips from one of the owner's own videos: a file on this PC or a web link.",
           _start_clip, route=("POST", "/api/jobs/clip"),
           props={"path": _s("full path of the video file on this PC"), "url": _s("http or https link to the video"),
                  "channel": _s("channel slug"),
                  "template": _s("layout id, 'auto' lets Nightshift choose"),
                  "max_clips": _i("most clips", minimum=1, maximum=10),
                  "min_s": _i("shortest clip, seconds", minimum=5, maximum=120),
                  "max_s": _i("longest clip, seconds", minimum=10, maximum=180),
                  "caption_style": _s("caption style name")},
           required=["channel"], side=True, precheck=_clip_source_given,
           confirm=("Make up to {max_clips} clips for {channel} from {source} (layout {template}). By pressing Yes "
                    "you confirm you own this video or are licensed to use it.")),
        _t("start_verify", "create", "Run the machine self-check (ns verify).",
           lambda c, a: c.dash.jobs.create_verify(a), route=("POST", "/api/jobs/verify"),
           props={"quick": _b("quick check only")}, side=True, confirm="Run the Nightshift self-check."),
        _t("list_jobs", "create", "Recent jobs with status and progress.",
           lambda c, a: c.dash.jobs.list_jobs(a.get("status"), a.get("kind"), int(a.get("limit", 10))),
           route=("GET", "/api/jobs"),
           props={"status": _s("filter", enum=["queued", "running", "done", "failed", "cancelled"]),
                  "kind": _s("filter", enum=["run", "clip", "learn", "verify", "series_part"]),
                  "limit": _i("how many", minimum=1, maximum=50)}),
        _t("get_job", "create", "One job's status, stage and result.",
           lambda c, a: c.dash.jobs.get(a["job_id"]), route=("GET", "/api/jobs/{job_id}"),
           props={"job_id": _s("job id")}, required=["job_id"]),
        _t("cancel_job", "create", "Cancel a queued or running job (a running one stops after its current stage).",
           lambda c, a: c.dash.jobs.cancel(a["job_id"]), route=("POST", "/api/jobs/{job_id}/cancel"),
           props={"job_id": _s("job id")}, required=["job_id"], side=True, confirm="Cancel job {job_id}."),
        # ------------------------------------------------ results / learn
        _t("list_runs", "results", "Recent nightly and manual runs with timings.",
           lambda c, a: c.dash.engine.runs(int(a.get("limit", 10))), route=("GET", "/api/runs"),
           props={"limit": _i("how many", minimum=1, maximum=50)}),
        _t("summary_results", "results", "What the learning loop found works, what changes next, unmatched videos.",
           lambda c, a: c.dash.settings.learn_summary(), route=("GET", "/api/learn/summary")),
        _t("import_analytics", "results", "Import a YouTube Studio analytics CSV file from this PC.",
           _import_analytics, route=("POST", "/api/learn/import"),
           props={"path": _s("full path of the .csv file")}, required=["path"], side=True,
           confirm="Import the YouTube analytics file {path} and update what Nightshift learns."),
        # ------------------------------------------------ settings / diagnostics
        _t("get_settings", "settings", "Run time, notifications, engine profile and flags, which keys are set (never the keys).",
           lambda c, a: c.dash.settings.get_settings(), route=("GET", "/api/settings")),
        _t("set_schedule", "settings", "Change the nightly run time (HH:MM) or turn notifications on or off.",
           _set_schedule, route=("PUT", "/api/settings"),
           props={"schedule_time": _s("24-hour time", pattern=r"^([01]\d|2[0-3]):[0-5]\d$"),
                  "notifications": _b("show Windows notifications")}, side=True,
           confirm="Set the nightly run time to {schedule_time} (notifications: {notifications})."),
        _t("tail_logs", "diagnostics", "Latest log lines, optionally for one component and a minimum level.",
           lambda c, a: c.dash.settings.tail(a.get("component"), a.get("level"), int(a.get("n", 40)), None),
           route=("GET", "/api/logs/tail"),
           props={"component": _s("e.g. supervisor, studio, dashboard"),
                  "level": _s("minimum level", enum=["DEBUG", "INFO", "WARN", "ERROR"]),
                  "n": _i("how many lines", minimum=1, maximum=200)}),
        _t("make_diagnostics", "diagnostics", "Create the diagnostics zip to send to the developer.",
           lambda c, a: c.dash.settings.diagnostics(), route=("POST", "/api/diagnostics"),
           side=True, confirm="Create a diagnostics report (a zip of logs and settings, no keys)."),
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
           props={"name": _s("voice name: letters, digits, - or _"), "description": _s("how the voice sounds, in plain English"),
                  "test_line": _s("a sentence the new voice speaks as a preview")},
           required=["name", "description", "test_line"], side=True,
           confirm="Create the voice '{name}' ({description}) and speak: {test_line}. This uses the GPU for about a minute."),
        _t("clone_voice", "library", "Create a narration voice from a recording of a speaker, and speak a test line.",
           lambda c, a: c.dash.voices.clone({**a, "consent": True}), route=("POST", "/api/voices/clone"),
           props={"name": _s("voice name: letters, digits, - or _"), "sample_path": _s("full path of a 3-30 second recording of one speaker on this PC"),
                  "test_line": _s("a sentence the new voice speaks as a preview")},
           required=["name", "sample_path", "test_line"], side=True,
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
           required=["channel", "premise"], side=True, confirm="Add this idea for {channel}: {premise}"),
        _t("set_idea_status", "library", "Mark an idea new, approved for production, used or rejected.",
           lambda c, a: c.dash.library.set_idea_status(int(a["idea_id"]), a.get("status")),
           route=("PUT", "/api/library/ideas/{idea_id}"),
           props={"idea_id": _i("idea id"), "status": _s("new status", enum=["new", "approved", "used", "rejected"])},
           required=["idea_id", "status"], side=True, confirm="Mark idea {idea_id} as {status}."),
        _t("search_stories", "library", "Written stories with their videos and where they were posted.",
           lambda c, a: c.dash.library.stories(a.get("q"), a.get("channel"), int(a.get("limit", 10))),
           route=("GET", "/api/library/stories"),
           props={"q": _s("search words"), "channel": _s("channel slug"), "limit": _i("how many", minimum=1, maximum=50)}),
        _t("search_footage", "library", "Cached stock footage by keyword, licence and last use.",
           lambda c, a: c.dash.library.footage(a.get("q"), int(a.get("limit", 10))),
           route=("GET", "/api/library/footage"),
           props={"q": _s("keyword"), "limit": _i("how many", minimum=1, maximum=50)}),
        _t("list_formats", "library", "Video formats and clip layouts (editable presets).", _formats_brief,
           route=("GET", "/api/library/formats")),
        _t("duplicate_format", "library", "Make a copy of a format, optionally changing some fields.",
           lambda c, a: c.dash.library.create_format(a), route=("POST", "/api/library/formats"),
           props={"duplicate_of": _s("format id to copy"), "id": _s("new format id"),
                  "changes": _o("fields to change in the copy")},
           required=["duplicate_of"], side=True, confirm="Copy format {duplicate_of} as {id}."),
        _t("edit_format", "library", "Change fields of a format (editing a shipped one saves your own copy).",
           lambda c, a: c.dash.library.update_format(a["id"], {"changes": a["changes"]}),
           route=("PUT", "/api/library/formats/{format_id}"),
           props={"id": _s("format id"), "changes": _o("fields to change")}, required=["id", "changes"],
           side=True, confirm="Change format {id}: {changes}"),
        # ------------------------------------------------ orientation (no route)
        _t("system_map", "orientation", "Compact map: pages, folders, services, channels, formats, engine state.",
           orientation.system_map),
        _t("find", "orientation", "Search outputs, stories, ideas, channels, jobs, logs and docs at once.",
           orientation.find, props={"query": _s("words to look for")}, required=["query"]),
        _t("where_is", "orientation", "Absolute path or page for an output, job, channel, model, log or setting.",
           orientation.where_is, props={"thing": _s("e.g. an output id, 'logs', 'llm', 'schedule'")},
           required=["thing"]),
        _t("describe_tool", "orientation", "Full description and arguments of one tool.", _describe,
           props={"name": _s("tool name")}, required=["name"]),
        _t("list_tools", "orientation", "Every tool grouped by topic (queue, create, library, channels, ...).",
           _list_tools, props={"topic": _s("only this topic")}),
        _t("search_docs", "orientation", "Ranked passages from the Nightshift docs (spec, README, operator guide).",
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
        out.append(ToolDef(spec=spec, call=_workshop_call(d["name"])))
    return out


def build_registry() -> Registry:
    """Registry of every tool (route-derived and orientation)."""
    return Registry([*_tools(), *_workshop_tools()])
