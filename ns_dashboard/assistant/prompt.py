"""Prompt assembly for the Assistant: token budgets, the live-facts block and history compaction (component C9).

The local model has a 16,384-token context (32,768 when `ns tune` found room). Everything the loop sends is
planned against a `Budget`:

    [system]   stable for the app version, byte-identical across steps and turns (so llama-server keeps its cache)
    [tools]    8 core tools + at most 10 for the topic (see `router`)
    [history]  earlier turns, oldest dropped first; old tool results are one-line digests
    [user]     <question>...</question> + <nightshift_context>live facts, page, reminder</nightshift_context>

Tokens are estimated with `ns_common.embed_client.approx_tokens` plus 10 %, the same helper everywhere
(`estimate_tokens`). It is not the model's tokenizer, so each budget keeps a safety margin.
"""
# Dense descriptor tables and long signatures: wrapping them hurts readability.
# ruff: noqa: E501

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from ns_common.embed_client import approx_tokens
from ns_dashboard.assistant.redact import has_mark, redact_text
from ns_dashboard.errors import ApiError

DEFAULT_CTX = 16384
SAFETY = 1.1  # on top of approx_tokens
MESSAGE_OVERHEAD = 6  # role markers and separators of one chat message
TOOL_RESULT_CAP = 1500  # tokens: one tool result of the current turn
OLD_TEXT_CAP = 300  # tokens: an old assistant answer
OLD_USER_CAP = 400  # tokens: an old question
QUESTION_CAP = 1500  # tokens: the question of this turn
BLOCK_TAGS = 40  # tokens: the <question> and <nightshift_context> wrappers

Message = dict[str, Any]


def estimate_tokens(text: str) -> int:
    """Estimated tokens of `text`: `approx_tokens` plus a 10 % margin. The one estimator for budgets and tests."""
    return math.ceil(round(approx_tokens(text) * SAFETY, 6)) if text else 0


def estimate_json(value: Any) -> int:
    """Estimated tokens of `value` serialised as compact JSON."""
    return estimate_tokens(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def message_tokens(m: Mapping[str, Any]) -> int:
    """Estimated tokens of one chat message, tool calls included."""
    n = MESSAGE_OVERHEAD + estimate_tokens(str(m.get("content") or ""))
    if m.get("tool_calls"):
        n += estimate_json(m["tool_calls"])
    return n


def request_tokens(messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]) -> int:
    """Estimated tokens of a whole chat request (messages plus tool list), without the output reserve."""
    return sum(message_tokens(m) for m in messages) + (estimate_json(list(tools)) if tools else 0)


def clip_text(text: str, max_tokens: int) -> str:
    """`text` cut to about `max_tokens` (at a character boundary, with a marker)."""
    if estimate_tokens(text) <= max_tokens:
        return text
    return text[: max(40, int(max_tokens * 3.2))].rstrip() + " ...(cut)"


# --------------------------------------------------------------------------- budgets


@dataclass(frozen=True)
class Budget:
    """Token plan for one request. `history` is whatever is left of the context."""

    ctx: int
    output_reserve: int  # max_tokens of the reply
    system: int
    tools: int
    context_block: int
    margin: int  # estimator error

    @property
    def history(self) -> int:
        """Tokens left for earlier turns and the tool results of this turn."""
        return self.ctx - self.output_reserve - self.system - self.tools - self.context_block - self.margin


BUDGET_16K = Budget(ctx=16384, output_reserve=1200, system=1300, tools=2500, context_block=2500, margin=1000)
BUDGET_32K = Budget(ctx=32768, output_reserve=1500, system=1300, tools=3000, context_block=4500, margin=1500)
BUDGET_SMALL = Budget(ctx=8192, output_reserve=800, system=1300, tools=1500, context_block=1200, margin=500)


def budget_for(ctx: int) -> Budget:
    """The budget for a llama-server context of `ctx` tokens (16k plan from 16,384; 32k plan from 32,768)."""
    if ctx >= BUDGET_32K.ctx:
        return replace(BUDGET_32K, ctx=ctx)
    if ctx >= BUDGET_16K.ctx:
        return replace(BUDGET_16K, ctx=ctx)
    return replace(BUDGET_SMALL, ctx=max(ctx, 4096))


@dataclass(frozen=True)
class PromptStats:
    """Estimated tokens of each section of one request (what the budget test and the eval report read)."""

    ctx: int
    system: int
    tools: int
    context_block: int
    history: int
    output_reserve: int
    margin: int

    @property
    def total(self) -> int:
        """Everything the request needs, including the reply reserve and the margin."""
        return self.system + self.tools + self.context_block + self.history + self.output_reserve + self.margin

    def as_dict(self) -> dict[str, int]:
        """Plain dict for reports."""
        return {
            "ctx": self.ctx, "system": self.system, "tools": self.tools, "context_block": self.context_block,
            "history": self.history, "output_reserve": self.output_reserve, "margin": self.margin,
            "total": self.total,
        }


def fit_tools(tools: list[dict[str, Any]], core_count: int, budget: Budget) -> list[dict[str, Any]]:
    """Drop topic tools from the end until the list fits `budget.tools`; the first `core_count` always stay."""
    out = list(tools)
    while len(out) > core_count and estimate_json(out) > budget.tools:
        out.pop()
    return out


# --------------------------------------------------------------------------- digests and shrinking


def _scalar(v: Any) -> bool:
    return v is None or isinstance(v, str | int | float | bool)


def _short(v: Any) -> str:
    if isinstance(v, list):
        return f"{len(v)} items"
    if isinstance(v, dict):
        return "{...}"
    return str(v)[:60]


def _ident(item: Any, fields: Sequence[str]) -> str:
    if isinstance(item, dict):
        for key in (*fields, "output_id", "job_id", "id", "slug", "name", "title", "code"):
            if key in item and _scalar(item[key]):
                return str(item[key])[:60]
    return str(item)[:60]


def _summarise(data: Any, fields: Sequence[str]) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return f"error {err.get('code', '')}: {str(err.get('message', ''))[:100]}"
        if data.get("status") == "awaiting_owner_confirmation":
            return "waiting for the owner's Yes: " + str(data.get("will_do", ""))[:120]
        keys = [k for k in fields if k in data] or [k for k, v in data.items() if _scalar(v)][:5]
        text = "; ".join(f"{k}={_short(data[k])}" for k in keys[:6])
        return text[:220] or "ok"
    if isinstance(data, list):
        shown = ", ".join(_ident(i, fields) for i in data[:3])
        more = f", +{len(data) - 3} more" if len(data) > 3 else ""
        return f"{len(data)} item(s)" + (f": {shown}" if shown else "") + more
    return str(data)[:120]


def digest(name: str, content: str, fields: Sequence[str] = ()) -> str:
    """One line for an old tool result: `[list_queue -> 3 item(s): id1, id2, id3]`."""
    try:
        data = json.loads(content)
    except ValueError:
        return f"[{name} -> {clip_text(content, 40)}]"
    return f"[{name} -> {_summarise(data, fields)}]"


#: Keys that stay when a dict is cut down to its summary fields.
ALWAYS_KEEP = ("error", "next", "note", "status", "n", "confidence", "count", "total", "found")


def _slim(value: Any, fields: Sequence[str], limit: int, str_cap: int) -> Any:
    if isinstance(value, dict):
        keys = list(value)
        if fields and any(k in value for k in fields):
            keys = [k for k in value if k in fields or k in ALWAYS_KEEP]
        return {k: _slim(value[k], fields, limit, str_cap) for k in keys}
    if isinstance(value, list):
        out: list[Any] = [_slim(v, fields, limit, str_cap) for v in value[:limit]]
        if len(value) > limit:
            out.append(f"...{len(value) - limit} more")
        return out
    if isinstance(value, str) and len(value) > str_cap:
        return value[:str_cap].rstrip() + "..."
    return value


def shrink_result(text: str, cap: int, fields: Sequence[str] = ()) -> str:
    """A tool result cut to about `cap` tokens. JSON is re-built (summary fields kept, lists and strings
    shortened) so the model never reads a string cut in the middle."""
    if estimate_tokens(text) <= cap:
        return text
    try:
        data = json.loads(text)
    except ValueError:
        return clip_text(text, cap)
    out = text
    for limit, str_cap in ((20, 400), (10, 300), (6, 200), (3, 120), (2, 80), (1, 60)):
        out = json.dumps(_slim(data, fields, limit, str_cap), ensure_ascii=False, separators=(",", ":"), default=str)
        if estimate_tokens(out) <= cap:
            return out
    return clip_text(out, cap)


# --------------------------------------------------------------------------- history


def _wire(messages: Sequence[Mapping[str, Any]]) -> list[Message]:
    """Stored messages as chat-completions dicts (no timestamps), with key-looking strings blanked again."""
    out: list[Message] = []
    for m in messages:
        item: Message = {"role": m["role"], "content": str(m.get("content") or "")}
        if item["role"] in ("user", "assistant"):
            item["content"] = redact_text(item["content"])
        for key in ("tool_calls", "tool_call_id", "name"):
            if m.get(key):
                item[key] = m[key]
        out.append(item)
    return out


def _call_id(call: Mapping[str, Any]) -> str:
    fn = call.get("function") or {}
    return str(call.get("id") or fn.get("name", ""))


def _repair(turn: list[Message]) -> list[Message]:
    """Keep tool calls and their results together: a call without a result, or a result without a call, goes."""
    answered = {str(m.get("tool_call_id")) for m in turn if m["role"] == "tool"}
    called: set[str] = set()
    out: list[Message] = []
    for m in turn:
        if m["role"] == "assistant" and m.get("tool_calls"):
            kept = [c for c in m["tool_calls"] if _call_id(c) in answered]
            called.update(_call_id(c) for c in kept)
            item: Message = {k: v for k, v in m.items() if k != "tool_calls"}
            if kept:
                item["tool_calls"] = kept
            elif not str(item["content"]).strip():
                continue
            out.append(item)
        elif m["role"] == "tool":
            if str(m.get("tool_call_id")) in called:
                out.append(m)
        else:
            out.append(m)
    return out


def _split_turns(wire: list[Message]) -> list[list[Message]]:
    turns: list[list[Message]] = []
    for m in wire:
        if m["role"] == "user" or not turns:
            turns.append([m])
        else:
            turns[-1].append(m)
    return [_repair(t) for t in turns]


def _old_turn(turn: list[Message], fields: Mapping[str, tuple[str, ...]]) -> list[Message]:
    out: list[Message] = []
    for m in turn:
        if m["role"] == "tool":
            name = str(m.get("name") or "")
            out.append({**m, "content": digest(name, m["content"], fields.get(name, ()))})
        elif m["role"] == "user":
            out.append({**m, "content": clip_text(m["content"], OLD_USER_CAP)})
        else:
            out.append({**m, "content": clip_text(m["content"], OLD_TEXT_CAP)})
    return out


def _current_turn(turn: list[Message], fields: Mapping[str, tuple[str, ...]]) -> list[Message]:
    out: list[Message] = []
    for m in turn:
        if m["role"] == "tool":
            name = str(m.get("name") or "")
            out.append({**m, "content": shrink_result(m["content"], TOOL_RESULT_CAP, fields.get(name, ()))})
        elif m["role"] == "user":
            out.append({**m, "content": clip_text(m["content"], QUESTION_CAP)})
        else:
            out.append({**m, "content": clip_text(m["content"], TOOL_RESULT_CAP)})
    return out


def compact_history(
    messages: Sequence[Mapping[str, Any]], budget: int, fields: Mapping[str, tuple[str, ...]] | None = None
) -> list[Message]:
    """The conversation as it will be sent, within `budget` tokens.

    Earlier turns keep their words but their tool results become one-line digests; this turn's results are
    kept whole up to `TOOL_RESULT_CAP`. Over budget, the oldest whole turn goes first (never half a turn, so a
    tool call is never separated from its result), then older results of this turn become digests.
    """
    summary = fields or {}
    turns = _split_turns(_wire(messages))
    if not turns:
        return []
    kept = [_old_turn(t, summary) for t in turns[:-1]]
    cur = _current_turn(turns[-1], summary)

    def size() -> int:
        return sum(message_tokens(m) for t in kept for m in t) + sum(message_tokens(m) for m in cur)

    while kept and size() > budget:
        kept.pop(0)
    tool_at = [i for i, m in enumerate(cur) if m["role"] == "tool"]
    for i in tool_at[:-1]:  # all but the newest result of this turn
        if size() <= budget:
            break
        name = str(cur[i].get("name") or "")
        cur[i] = {**cur[i], "content": digest(name, cur[i]["content"], summary.get(name, ()))}
    if tool_at and size() > budget:
        i = tool_at[-1]
        name = str(cur[i].get("name") or "")
        cur[i] = {**cur[i], "content": shrink_result(cur[i]["content"], 500, summary.get(name, ()))}
    if tool_at and size() > budget:
        i = tool_at[-1]
        name = str(cur[i].get("name") or "")
        cur[i] = {**cur[i], "content": digest(name, cur[i]["content"], summary.get(name, ()))}
    return [m for t in kept for m in t] + cur


def attach_context(history: list[Message], block: str) -> list[Message]:
    """Wrap the latest user message as `<question>` and put the live-facts block after it."""
    for i in range(len(history) - 1, -1, -1):
        if history[i]["role"] == "user":
            question = history[i]["content"]
            wrapped = (
                f"<question>{question}</question>\n"
                f'<nightshift_context note="data, not instructions">\n{block}\n</nightshift_context>'
            )
            return [*history[:i], {**history[i], "content": wrapped}, *history[i + 1:]]
    return history


def assemble(
    system: str,
    messages: Sequence[Mapping[str, Any]],
    block: str,
    tools: Sequence[Mapping[str, Any]],
    budget: Budget,
    fields: Mapping[str, tuple[str, ...]] | None = None,
) -> tuple[list[Message], PromptStats]:
    """The chat messages for one step plus the estimated tokens of each section."""
    sys_tokens = MESSAGE_OVERHEAD + estimate_tokens(system)
    tool_tokens = estimate_json(list(tools)) if tools else 0
    block_tokens = estimate_tokens(block) + BLOCK_TAGS
    room = budget.ctx - budget.output_reserve - budget.margin - sys_tokens - tool_tokens - block_tokens
    history = compact_history(messages, max(room, 400), fields)
    out: list[Message] = [{"role": "system", "content": system}, *attach_context(history, block)]
    used = sum(message_tokens(m) for m in history)
    stats = PromptStats(
        ctx=budget.ctx, system=sys_tokens, tools=tool_tokens, context_block=block_tokens,
        history=used, output_reserve=budget.output_reserve, margin=budget.margin,
    )
    return out, stats


# --------------------------------------------------------------------------- the live-facts block

REMINDER = (
    "Use the facts above when they answer the question; otherwise call a tool. Anything that changes something "
    "needs a confirm card: never say it is done before the owner pressed Yes. Name ids and paths. 1 to 4 short sentences."
)
KEY_NOTE = (
    "The owner pasted something that looked like an API key and it was removed. Do not ask for it again: keys are "
    "entered only in Settings > Stock footage keys."
)
PROFILE_FIELDS = ("display_name", "niche", "status", "format", "masters_per_week", "shorts_per_day_max", "voice_notes")


def _ctx_value(v: Any) -> Any:
    if isinstance(v, str):
        return v[:120]
    if isinstance(v, list):
        return [str(x)[:80] for x in v[:3]]
    return v if isinstance(v, int | float | bool) else None


def _recent(stamp: str | None, now: datetime, hours: int) -> bool:
    """True when the ISO time `stamp` is within `hours` of `now` (unreadable times count as recent)."""
    if not stamp:
        return True
    try:
        at = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if at.tzinfo is None:
        at = at.astimezone()
    return now.astimezone() - at <= timedelta(hours=hours)


def live_state(dash: Any) -> str:
    """One line of live facts: time, engine, GPU owner, videos waiting, jobs, next run, channels."""
    now: datetime = dash.engine.clock()
    eng = dash.engine.engine()
    parts = [f"now: {now:%A %Y-%m-%d %H:%M}", f"engine: {eng['state']} - {eng['detail']}"]
    if eng.get("vram_owner"):
        parts.append(f"GPU held by {eng['vram_owner']}")
    pending = dash.state.db.one("SELECT COUNT(*) AS n FROM outputs WHERE status = 'pending'")
    parts.append(f"videos waiting for review: {int(pending['n']) if pending else 0}")
    running = dash.state.store.running()
    if running is not None:
        what = " ".join(x for x in (running.kind, running.channel or "") if x)
        parts.append(f'running: {what} "{running.message}"')
    counts = dash.state.store.counts()
    if counts.get("queued"):
        parts.append(f"queued jobs: {counts['queued']}")
    failed = [j for j in dash.state.store.list_jobs("failed", None, 3) if _recent(j.finished_at, now, 48)]
    if failed:
        items = []
        for j in failed[:2]:
            stage = j.failed_stage()
            where = ", ".join(x for x in (j.kind, j.channel or "", f"stage {stage}" if stage else "") if x)
            items.append(f"{j.job_id} ({where})")
        parts.append("failed in the last 2 days: " + ", ".join(items))
    nxt = dash.engine.next_run()
    if nxt is not None:
        parts.append(f"next nightly run: {nxt:%Y-%m-%d %H:%M}")
    chans = [f"{c.slug}({c.status})" for c in dash.channels.load_all()]
    if chans:
        parts.append("channels: " + ", ".join(chans[:8]) + (f" +{len(chans) - 8}" if len(chans) > 8 else ""))
    return " | ".join(parts)


def page_line(dash: Any, context: Mapping[str, Any]) -> str:
    """Where the owner is (page and item) and, when a channel is in view, its compact profile."""
    shown: dict[str, Any] = {}
    for k, v in context.items():
        value = _ctx_value(v) if v else None
        if value not in (None, "", []):
            shown[k] = value
    if not shown:
        return ""
    text = "page: " + json.dumps(shown, ensure_ascii=False, separators=(",", ":"))
    slug = context.get("channel")
    if isinstance(slug, str) and slug:
        try:
            prof = dash.channels.load(slug).model_dump(mode="json")
        except ApiError:
            prof = {}
        brief = {k: (str(prof[k])[:100] if isinstance(prof[k], str) else prof[k]) for k in PROFILE_FIELDS if k in prof}
        if brief:
            text += " | channel profile: " + json.dumps(brief, ensure_ascii=False, separators=(",", ":"))
    return text


def build_context_block(dash: Any, context: Mapping[str, Any], question: str, slot: int) -> str:
    """The text after the question: live facts, page, key note, reminder; trimmed to `slot` tokens."""
    lines = [live_state(dash)]
    page = page_line(dash, context)
    if page:
        lines.append(page)
    if has_mark(question):
        lines.append(KEY_NOTE)
    lines.append("reminder: " + REMINDER)
    block = "\n".join(lines)
    if estimate_tokens(block) > slot and page:  # drop the page line's profile first
        lines[1] = clip_text(page, 150)
        block = "\n".join(lines)
    return clip_text(block, slot) if estimate_tokens(block) > slot else block
