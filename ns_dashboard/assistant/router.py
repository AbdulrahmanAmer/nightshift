"""The topic router: which tools the model is shown for one question (component C9).

A small local model picks the wrong tool when it is offered 60. The router is deterministic and makes no model
call. It scores the topics from the page the owner is on, keyword phrases, entities in the question (an output
id, an `NS-` code, a channel slug) and the topics of the previous turn, keeps the best two, and offers

    8 core tools (always) + at most 10 tools of those topics = at most 18 tools per request.

The model can ask for more: calling `list_tools(topic)` swaps that topic's pack in for the next step of the same
turn (once per topic and turn), and the tool result says which tools were added.
"""
# Dense descriptor tables and long signatures: wrapping them hurts readability.
# ruff: noqa: E501

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ns_dashboard.assistant.registry import TOPICS, Registry

#: Always offered, in this (alphabetical, so byte-stable) order. Memory tools join them in later slices.
CORE: tuple[str, ...] = (
    "diagnose", "explain_error", "find", "list_tools", "open_path", "search_docs", "status", "where_is",
)
MAX_TOPIC_TOOLS = 10
MAX_TOOLS = len(CORE) + MAX_TOPIC_TOOLS
PRIMARY_SHARE = 6  # of the 10 topic slots, the best topic gets this many before the second topic gets its share

#: Topic -> tools in the order they matter. A tool may sit in two topics. Names that are not registered (a tool
#: that ships in another change, such as `fetch_footage`) are skipped, so the table can name them ahead of time.
PACKS: dict[str, tuple[str, ...]] = {
    "queue": ("list_queue", "get_output", "review_brief", "video_stats", "get_story", "open_folder"),
    "create": ("start_story_job", "start_script_job", "start_clip_job", "tonight_plan", "list_jobs", "get_job", "cancel_job", "rerun_clips", "resume_job", "list_templates", "start_verify"),
    "library": ("search_footage", "fetch_footage", "list_ideas", "add_idea", "search_stories", "get_story", "list_voices", "design_voice", "clone_voice", "list_formats", "set_idea_status", "duplicate_format", "edit_format"),
    "channels": ("list_channels", "get_channel", "edit_channel", "pause_channel", "resume_channel", "create_channel", "validate_channel"),
    "results": ("what_worked", "video_stats", "summary_results", "list_runs", "import_analytics"),
    "settings": ("get_settings", "set_schedule", "check_update", "make_diagnostics"),
    "engine": ("warmup_engine", "shutdown_engine", "tonight_plan", "list_jobs"),
    "diagnostics": ("tail_logs", "make_diagnostics", "start_verify", "list_jobs", "get_job", "cancel_job"),
    "orientation": ("system_map", "describe_tool"),
    "series": ("workshop_list", "workshop_status", "workshop_start", "workshop_concepts", "workshop_regenerate", "workshop_choose", "workshop_map", "workshop_write_part", "workshop_revise_part", "workshop_approve", "workshop_design", "workshop_produce"),
    "drafts": ("draft_create", "draft_append_part", "draft_read", "start_job_from_draft", "list_drafts", "start_script_job"),
}

#: Words in a question that pull a tool to the front of its pack (packs longer than the slots they get).
HINTS: dict[str, tuple[str, ...]] = {
    "fetch_footage": ("fetch", "download", "get me", "pull", "bring"),
    "search_footage": ("do we have", "footage", "clip of", "clips of"),
    "list_ideas": ("idea", "topics"),
    "add_idea": ("add an idea", "new idea", "save this idea"),
    "search_stories": ("story", "stories", "wrote", "written"),
    "get_story": ("story", "stories", "ending", "last night"),
    "list_voices": ("voices", "narrator", "which voice", "hear", "listen", "sound like"),
    "design_voice": ("design", "new voice", "gravelly", "deep voice", "create a voice"),
    "clone_voice": ("clone", "recording", "sample"),
    "list_formats": ("format", "layout"),
    "duplicate_format": ("copy of a format", "duplicate"),
    "edit_format": ("edit the format", "change the format"),
    "set_idea_status": ("approve the idea", "reject the idea", "mark the idea"),
    "workshop_status": ("reveal", "continuity", "part", "did we", "status"),
    "workshop_list": ("series", "which stories", "list"),
    "workshop_start": ("start", "new series", "begin"),
    "workshop_write_part": ("write part", "write the next"),
    "workshop_revise_part": ("rewrite", "revise", "change part"),
    "workshop_produce": ("produce", "make part", "render", "make the video"),
    "workshop_approve": ("approve",),
    "workshop_design": ("design", "look of", "cover"),
    "workshop_map": ("map", "outline", "plan the parts"),
    "workshop_concepts": ("concept", "pitch"),
    "workshop_choose": ("choose", "pick concept"),
    "workshop_regenerate": ("regenerate", "other concept"),
    "start_script_job": ("script", "this text", "my text", "my story", "this story", "narrate"),
    "draft_create": ("new draft", "write the story", "write a story", "full story", "whole story"),
    "draft_append_part": ("next part", "rest of the story", "keep writing", "add a part", "continue"),
    "draft_read": ("read the draft", "show the draft", "where is the draft"),
    "start_job_from_draft": ("start", "make the video", "from the draft", "produce"),
    "list_drafts": ("my drafts", "which drafts", "list"),
    "start_clip_job": ("clip", "cut", "my video"),
    "rerun_clips": ("again", "another layout"),
    "list_templates": ("layout", "template"),
    "cancel_job": ("cancel", "stop the job"),
    "resume_job": ("retry", "continue", "resume", "try again", "pick up", "where it stopped"),
}

#: The page the owner is on gives its topic a head start.
PAGE_TOPIC: dict[str, str] = {
    "queue": "queue", "create": "create", "library": "library", "channels": "channels", "results": "results",
    "settings": "settings",
}

#: Phrases per topic (lower case). A single word matches as a whole word (plural allowed); a phrase as written.
LEXICON: dict[str, tuple[str, ...]] = {
    "queue": (
        "review", "approve", "reject", "post", "posting", "upload", "youtube", "queue", "pending", "outbox", "waiting",
        "this video", "this one", "title", "description", "tags", "ok to", "disclosure", "check before", "thumbnail",
    ),
    "create": (
        "start", "create", "new video", "new story", "short", "shorts", "cut clips", "clips from", "render",
        "tonight", "tomorrow", "schedule", "run at", "cancel", "job", "jobs", "what runs", "plan", "at 2",
        "retry", "where it stopped", "script", "this text", "narrate",
    ),
    "library": (
        "footage", "stock", "pexels", "pixabay", "idea", "ideas", "voice", "voices", "narrator", "story", "stories",
        "format", "formats", "layout", "fetch", "download", "clip of", "clips of", "rain", "library", "topics", "hear", "listen", "clips",
    ),
    "channels": (
        "channel", "channels", "niche", "pause", "resume", "cadence", "per week", "profile", "avoid topics", "avoid_topics",
        "calmer", "creepier", "tone", "narration", "voice notes", "keywords", "never use", "don't use", "do not use",
        "always use", "from now on",
    ),
    "results": (
        "works", "worked", "working", "performing", "performance", "views", "analytics", "retention", "learn",
        "csv", "export", "stats", "best", "top", "how is", "hooks", "results", "growing",
    ),
    "settings": (
        "setting", "settings", "run time", "notification", "notifications", "update", "version", "key", "keys",
        "api key", "diagnostics report",
    ),
    "engine": ("gpu", "engine", "stuck", "warm up", "warmup", "load the model", "shut down", "quit", "graphics card"),
    "diagnostics": (
        "fail", "failed", "failure", "error", "errors", "broke", "broken", "crash", "why did", "log", "logs", "stopped",
        "problem", "not working", "doesn't work", "went wrong", "diagnose", "verify", "self-check",
    ),
    "series": ("series", "episode", "season", "workshop", "reveal", "continuity", "part 1", "part 2", "part 3", "next part"),
    # G-43 b, G-47: a story the Assistant writes is a draft file, filled one part per call
    "drafts": (
        "draft", "drafts", "full story", "whole story", "write the story", "write a story", "story file", "md file",
        "next part", "rest of the story", "keep writing",
    ),
}

_NS_CODE = re.compile(r"\bNS-[EW]\d{3}\b", re.IGNORECASE)
_OUTPUT_ID = re.compile(r"\b[a-z0-9][a-z0-9-]*-\d{8}-[0-9a-f]{4}\b")


def _compile(phrases: Iterable[str]) -> list[tuple[str, re.Pattern[str]]]:
    out = []
    for p in phrases:
        body = re.escape(p)
        pat = rf"\b{body}s?\b" if " " not in p and p.isalpha() else rf"(?<!\w){body}(?!\w)"
        out.append((p, re.compile(pat)))
    return out


_LEX: dict[str, list[tuple[str, re.Pattern[str]]]] = {t: _compile(ps) for t, ps in LEXICON.items()}
_HINT: dict[str, list[tuple[str, re.Pattern[str]]]] = {n: _compile(ps) for n, ps in HINTS.items()}


def score_topics(
    question: str,
    context: Mapping[str, Any] | None = None,
    slugs: Sequence[str] = (),
    previous: Sequence[str] = (),
) -> dict[str, float]:
    """Topic -> score (only topics above zero)."""
    ctx = context or {}
    q = question.lower()
    scores: dict[str, float] = {}

    def add(topic: str, pts: float) -> None:
        if topic in TOPICS:
            scores[topic] = scores.get(topic, 0.0) + pts

    for topic, pats in _LEX.items():
        hits = sum(1 for _p, rx in pats if rx.search(q))
        if hits:
            add(topic, 2.0 * min(hits, 3))
    page = str(ctx.get("page") or "").lower()
    if page in PAGE_TOPIC:
        add(PAGE_TOPIC[page], 1.5)
    if ctx.get("output_id") or ctx.get("item_ids"):
        add("queue", 1.0)
    if _OUTPUT_ID.search(q):
        add("queue", 3.0)
    if _NS_CODE.search(question):
        add("diagnostics", 4.0)
    if any(s and (s in q or s.replace("-", " ") in q) for s in slugs):
        add("channels", 1.0)
    for topic in previous:
        add(topic, 1.0)
    return scores


def route(
    question: str,
    context: Mapping[str, Any] | None = None,
    slugs: Sequence[str] = (),
    previous: Sequence[str] = (),
) -> list[str]:
    """The best (at most two) topics for the question; an empty list means the core tools are enough."""
    scores = score_topics(question, context, slugs, previous)
    order = {t: i for i, t in enumerate(TOPICS)}
    ranked = sorted(scores, key=lambda t: (-scores[t], order[t]))
    out = [t for t in ranked[:1] if scores[t] >= 1.5]
    out += [t for t in ranked[1:2] if scores[t] >= 2.0 and out]
    return out


def topics_of_calls(registry: Registry, names: Iterable[str]) -> list[str]:
    """Topics of tools that were called (used to make the previous turn's topics sticky)."""
    seen: list[str] = []
    for n in names:
        td = registry.get(n)
        if td is not None and td.spec.topic not in seen and td.spec.topic != "orientation":
            seen.append(td.spec.topic)
    return seen


def _pack(registry: Registry, topic: str, question: str) -> list[str]:
    """Registered, non-core tools of `topic`, hint matches first, each group in table order."""
    names = [n for n in PACKS.get(topic, ()) if registry.get(n) is not None]
    for td in registry.all():  # a registered tool of this topic that the table forgot is not lost
        if td.spec.topic == topic and td.spec.name not in names:
            names.append(td.spec.name)
    names = [n for n in names if n not in CORE]
    q = question.lower()
    hit = [n for n in names if any(rx.search(q) for _p, rx in _HINT.get(n, ()))]
    return hit + [n for n in names if n not in hit]


def select_tools(registry: Registry, topics: Sequence[str], question: str) -> list[str]:
    """Core tools (those registered) followed by at most `MAX_TOPIC_TOOLS` tools of `topics`."""
    core = [n for n in CORE if registry.get(n) is not None]
    packs = [_pack(registry, t, question) for t in topics]
    chosen: list[str] = []

    def take(items: Iterable[str], room: int) -> None:
        for n in items:
            if room <= 0:
                break
            if n not in chosen and n not in core:
                chosen.append(n)
                room -= 1

    if len(packs) >= 2:
        take(packs[0], PRIMARY_SHARE)
        take(packs[1], MAX_TOPIC_TOOLS - len(chosen))
        take([*packs[0], *packs[1]], MAX_TOPIC_TOOLS - len(chosen))
    elif packs:
        take(packs[0], MAX_TOPIC_TOOLS)
    return [*core, *chosen[:MAX_TOPIC_TOOLS]]


@dataclass
class ToolOffer:
    """The tools shown during one turn; `add_topic` is the `list_tools` escape hatch."""

    registry: Registry
    question: str
    topics: list[str]
    names: list[str] = field(default_factory=list)
    opened: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.names = select_tools(self.registry, self.topics, self.question)

    @property
    def core_count(self) -> int:
        """How many of `names` are core tools (they come first)."""
        return sum(1 for n in self.names if n in CORE)

    def specs(self) -> list[dict[str, Any]]:
        """The OpenAI-style tool objects for the request, core first."""
        out = []
        for n in self.names:
            td = self.registry.get(n)
            if td is not None:
                out.append(td.spec.openai())
        return out

    def add_topic(self, topic: str) -> list[str]:
        """Show `topic`'s pack from the next step on (once per topic); returns the tool names that are new."""
        if topic not in TOPICS or topic in self.opened:
            return []
        self.opened.add(topic)
        before = list(self.names)
        keep_extras = [n for n in before if n not in CORE]
        pack = _pack(self.registry, topic, self.question)[:MAX_TOPIC_TOOLS]
        extras = [*pack, *[n for n in keep_extras if n not in pack]][:MAX_TOPIC_TOOLS]
        core = [n for n in before if n in CORE]
        self.names = [*core, *extras]
        self.topics = [topic, *[t for t in self.topics if t != topic]][:2]
        return [n for n in self.names if n not in before]
