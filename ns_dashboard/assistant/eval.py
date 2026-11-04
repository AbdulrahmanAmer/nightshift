"""Offline evaluation of the Assistant on the scripted fake model (component C9).

No language model is involved, so this cannot say whether the real model picks the right tool. It proves
everything around the model, for each of the partner's questions in a cases file:

1. router: the topics the question needs are chosen and the offered tools contain the tools it needs (at most 18);
2. retrieval scope: `search_docs` serves only README.md, OPERATOR.md, the error catalogue and the generated
   capability list, and finds the passages the question needs;
3. budget: every request of the turn fits the context (messages + tools + reply reserve + margin);
4. oracle run: the case's scripted calls drive the real loop; the tool results hold the facts the answer needs,
   changes only produce confirm cards (the action stays pending), forbidden tools never run, and a key pasted
   in the question reaches neither `assistant.db` nor the model.

`tests/test_assistant_eval.py` runs the 20 cases from `tests/assistant_eval/cases.yaml` on every CI run.
"""
# Dense descriptor tables and long signatures: wrapping them hurts readability.
# ruff: noqa: E501

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ns_dashboard.assistant import docs_index, orientation, prompt, router
from ns_dashboard.assistant.llm import ScriptedToolLlm, StaticProvider, call, say
from ns_dashboard.assistant.redact import redact_text
from ns_dashboard.assistant.registry import ToolContext

MAX_OFFERED = router.MAX_TOOLS


@dataclass
class Case:
    """One question of the cases file."""

    id: int
    question: str
    context: dict[str, Any] = field(default_factory=dict)
    topics: list[str] = field(default_factory=list)
    tools_needed: list[list[str]] = field(default_factory=list)  # any-of groups
    gold: list[dict[str, Any]] = field(default_factory=list)  # {"call": {tool, args}} or {"say": text}
    facts: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    confirm: list[str] = field(default_factory=list)
    confirm_text_has: list[str] = field(default_factory=list)
    must_retrieve: list[str] = field(default_factory=list)
    no_tool_calls: bool = False
    secret: str = ""
    opens_folder: bool = False
    later: list[str] = field(default_factory=list)  # tools the full design adds; informational


@dataclass
class CaseResult:
    """What one case measured and which checks failed."""

    id: int
    question: str
    topics: list[str]
    offered: list[str]
    called: list[str]
    confirmed: list[str]
    stats: dict[str, int]
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no check failed."""
        return not self.failures


def _fill(value: Any, ids: dict[str, str]) -> Any:
    """Replace `<name>` tokens (ids that only exist once the fixture home is built)."""
    if isinstance(value, str):
        for k, v in ids.items():
            value = value.replace(f"<{k}>", v)
        return value
    if isinstance(value, list):
        return [_fill(v, ids) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, ids) for k, v in value.items()}
    return value


def load_cases(path: Path, ids: dict[str, str], secret: str = "") -> list[Case]:
    """Read the cases file; `<name>` tokens are replaced from `ids`, and `<secret>` by `secret`."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    out: list[Case] = []
    for item in raw["cases"]:
        data = _fill(item, {**ids, "secret": secret})
        case = Case(**{k: v for k, v in data.items() if k != "secret"})
        case.secret = secret if "<secret>" in str(item) else ""
        out.append(case)
    return out


def _script(case: Case) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, step in enumerate(case.gold):
        if "call" in step:
            c = step["call"]
            out.append(call(c["tool"], c.get("args") or {}, call_id=f"case{case.id}_{i}"))
        else:
            out.append(say(str(step["say"])))
    return out


def _db_text(dash: Any) -> str:
    """Every string the Assistant stored in `assistant.db`."""
    db = dash.assistant_store.db
    parts: list[str] = []
    for sql in (
        "SELECT content AS t FROM messages", "SELECT content AS t FROM assistant_messages",
        "SELECT tool_calls_json AS t FROM messages", "SELECT context_json AS t FROM messages",
        "SELECT title AS t FROM conversations", "SELECT text AS t FROM queued_messages",
        "SELECT args_json AS t FROM actions", "SELECT confirm_text AS t FROM actions",
    ):
        parts += [str(r["t"] or "") for r in db.query(sql)]
    return "\n".join(parts)


def run_case(dash: Any, case: Case, *, ctx_tokens: int = prompt.DEFAULT_CTX) -> CaseResult:
    """Run the four layers for one case on `dash` (a Dashboard over the fixture home)."""
    fails: list[str] = []
    question = redact_text(case.question)
    slugs = [c.slug for c in dash.channels.load_all()]

    # 1. router
    routed = router.route(question, case.context, slugs, [])
    for t in case.topics:
        if t not in routed:
            fails.append(f"router: topic {t!r} not chosen (got {routed})")

    # 2. retrieval scope
    hits = orientation.docs(ToolContext(dash)).search(question, 8)
    for h in hits:
        if h["file"] not in docs_index.ALLOWED_SOURCES:
            fails.append(f"scope: search_docs served {h['file']}")
    text = " ".join(f"{h['heading']} {h['text']}" for h in hits).lower()
    for needle in case.must_retrieve:
        if needle.lower() not in text:
            fails.append(f"scope: nothing retrieved for {needle!r}")

    # 4. oracle run (also produces the requests layer 1 and 3 read)
    llm = ScriptedToolLlm(_script(case))
    provider = StaticProvider(llm)
    dash.assistant.provider = provider
    dash.assistant._ctx_fn = lambda: ctx_tokens
    dash.assistant.stats.clear()
    dash.assistant.offered.clear()
    opened_before = len(dash.state.adapters.folder.opened)
    events = list(dash.assistant.chat(case.question, None, case.context))
    errors = [e for e in events if e["kind"] == "error"]
    if errors:
        fails.append(f"run: error event {errors[0]['payload']}")
    if provider.released != 1:
        fails.append(f"run: model released {provider.released} times")

    offered = list(dash.assistant.offered[0]) if dash.assistant.offered else []
    if not offered:
        fails.append("run: no request reached the model")
    for group in case.tools_needed:
        if not any(t in offered for t in group):
            fails.append(f"router: none of {group} offered")
    if len(offered) > MAX_OFFERED:
        fails.append(f"router: {len(offered)} tools offered, limit {MAX_OFFERED}")

    # 3. budget (measured on the requests the model really got, not on the assembler's own numbers)
    budget = prompt.budget_for(ctx_tokens)
    for i, (msgs, tools) in enumerate(zip(llm.requests, llm.tool_lists, strict=True)):
        used = prompt.request_tokens(msgs, tools) + budget.output_reserve + budget.margin
        if used > budget.ctx:
            fails.append(f"budget: request {i} needs {used} tokens, context is {budget.ctx}")
        if len(tools) > MAX_OFFERED:
            fails.append(f"budget: request {i} offered {len(tools)} tools")
    seen = list(dash.assistant.stats)
    stats = {k: max((getattr(s, k) for s in seen), default=0) for k in ("system", "tools", "context_block", "history")}
    stats["total"] = max((s.total for s in seen), default=0)

    called = [e["payload"]["tool"] for e in events if e["kind"] == "tool_call"]
    confirmed = [e["payload"]["tool"] for e in events if e["kind"] == "confirm"]
    if case.no_tool_calls and (called or confirmed):
        fails.append(f"gate: tools were used but none should be: {called + confirmed}")
    for t in case.forbidden:
        if t in called or t in confirmed:
            fails.append(f"gate: forbidden tool {t} was used")
    if sorted(confirmed) != sorted(case.confirm):
        fails.append(f"gate: confirm cards {confirmed}, expected {case.confirm}")
    for e in events:
        if e["kind"] == "confirm":
            action = dash.assistant_store.get_action(e["payload"]["action_id"])
            if action is None or action["status"] != "pending":
                fails.append(f"gate: {e['payload']['tool']} ran without the owner's Yes")
            for needle in case.confirm_text_has:
                if needle not in e["payload"]["text"]:
                    fails.append(f"gate: confirm text lacks {needle!r}: {e['payload']['text']}")
    results = [e["payload"] for e in events if e["kind"] == "tool_result"]
    for r in results:
        if not r.get("ok"):
            fails.append(f"run: tool {r.get('tool')} failed: {str(r.get('result') or r.get('error'))[:200]}")
    blob = json.dumps([r.get("result") for r in results], default=str).lower()
    for fact in case.facts:
        if fact.lower() not in blob:
            fails.append(f"facts: {fact!r} missing from the tool results")
    if case.opens_folder:
        opened = dash.state.adapters.folder.opened[opened_before:]
        home = Path(dash.state.paths.home).resolve()
        if not opened or not all(home in Path(p).resolve().parents or Path(p).resolve() == home for p in opened):
            fails.append(f"run: expected a folder inside the home to open, got {opened}")
    if case.secret:
        stored = _db_text(dash)
        sent = json.dumps(llm.requests, default=str)
        if case.secret in stored:
            fails.append("secret: the key is in assistant.db")
        if case.secret in sent:
            fails.append("secret: the key reached the model")
        if "[key removed]" not in sent:
            fails.append("secret: the model never saw the removal mark")
    if not [e for e in events if e["kind"] == "message"]:
        fails.append("run: no answer message")
    return CaseResult(case.id, case.question, routed, offered, called, confirmed, stats, fails)


def render_markdown(results: list[CaseResult]) -> str:
    """A table of the run: topics, offered tool count, calls, estimated tokens of the biggest request, failures."""
    lines = [
        "| # | question | topics | offered | called | confirm | tokens | result |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.id} | {r.question[:48]} | {', '.join(r.topics) or '-'} | {len(r.offered)} | {', '.join(r.called) or '-'} |"
            f" {', '.join(r.confirmed) or '-'} | {r.stats.get('total', 0)} | {'ok' if r.ok else '; '.join(r.failures)} |"
        )
    return "\n".join(lines)
