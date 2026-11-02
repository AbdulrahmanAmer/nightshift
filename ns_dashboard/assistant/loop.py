"""The Assistant's tool-calling loop with confirm cards (component C9, spec 20.3a)."""
# Dense descriptor tables and long signatures: wrapping them hurts readability.
# ruff: noqa: E501

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from ns_common.errors import NsError
from ns_dashboard.assistant.llm import EngineBusy, EngineOffline, LlmProvider
from ns_dashboard.assistant.registry import Registry, ToolContext, ToolDef, render_confirm
from ns_dashboard.assistant.store import AssistantStore
from ns_dashboard.errors import AdapterUnavailable, ApiError

PROMPT_FILE = Path(__file__).resolve().parents[1] / "prompts" / "assistant.md"
MAX_STEPS = 8
HISTORY_MESSAGES = 30
RESULT_CHARS = 6000

Event = dict[str, Any]


def load_prompt(file: Path = PROMPT_FILE) -> tuple[dict[str, Any], str]:
    """Split a prompt file into (front-matter dict, body); the front-matter must carry `version`."""
    text = file.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"{file.name} has no YAML front-matter")
    _, fm, body = text.split("---", 2)
    meta = yaml.safe_load(fm) or {}
    if "version" not in meta:
        raise ValueError(f"{file.name} front-matter lacks 'version'")
    return meta, body.strip()


def fill_from_context(schema: dict[str, Any], args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Offer the page/item context as default arguments (`output_id`, `channel`, `job_id`, `idea_id`)."""
    out = dict(args)
    props = schema.get("properties", {})
    ids = context.get("item_ids") or []
    for name, prop in props.items():
        if name in out:
            continue
        value = context.get(name)
        if value is None and name == "output_id" and ids:
            value = ids[0]
        if value is None:
            continue
        want = prop.get("type")
        if (want == "string" and isinstance(value, str)) or (want == "integer" and isinstance(value, int)):
            out[name] = value
    return out


def run_tool(td: ToolDef, ctx: ToolContext, args: dict[str, Any]) -> tuple[bool, Any]:
    """Execute a tool; returns (ok, result-or-error-dict). Never raises."""
    try:
        return True, td.call(ctx, args)
    except ApiError as e:
        return False, {"error": {"code": e.code, "message": e.message, "hint": e.hint, "details": e.details}}
    except NsError as e:
        return False, {"error": {"code": e.code, "message": str(e), "hint": e.definition.hint}}
    except AdapterUnavailable as e:
        return False, {"error": {"code": "unavailable", "message": str(e)}}
    except Exception as e:  # a tool bug must come back to the model as text, not crash the turn
        return False, {"error": {"code": "tool_failed", "message": f"{type(e).__name__}: {e}"}}


def to_text(result: Any) -> str:
    """JSON text for the model, trimmed to a sane size."""
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > RESULT_CHARS:
        text = text[:RESULT_CHARS] + '..."(truncated)"'
    return text


class Assistant:
    """One instance per app: runs chat turns and executes confirmed actions."""

    def __init__(self, dash: Any, registry: Registry, store: AssistantStore, provider: LlmProvider,
                 *, max_steps: int = MAX_STEPS) -> None:
        self.dash = dash
        self.registry = registry
        self.store = store
        self.provider = provider
        self.max_steps = max_steps

    # --------------------------------------------------------------- prompt
    def system_prompt(self, ctx: ToolContext) -> str:
        """The versioned prompt with the compact system map and the owner's context filled in."""
        from ns_dashboard.assistant.orientation import compact_map

        _meta, body = load_prompt()
        shown = {k: v for k, v in ctx.context.items() if v}
        return body.replace("{{system_map}}", compact_map(ctx)).replace(
            "{{context}}", json.dumps(shown, ensure_ascii=False) if shown else "(none)"
        )

    # --------------------------------------------------------------- chat
    def chat(self, message: str, conversation_id: str | None = None, context: dict[str, Any] | None = None,
             *, record_user: bool = True) -> Iterator[Event]:
        """Run one owner message to completion, yielding events:
        `conversation`, `tool_call`, `tool_result`, `confirm`, `message`, `busy`, `error`, `done`."""
        context = dict(context or {})
        text = (message or "").strip()
        if not text:
            yield {"kind": "error", "payload": {"code": "empty_message", "message": "Type a question first."}}
            yield {"kind": "done", "payload": {}}
            return
        cid = conversation_id if conversation_id and self.store.has_conversation(conversation_id) else None
        cid = cid or self.store.new_conversation()
        self.store.set_title(cid, text)
        if record_user:
            self.store.add_message(cid, "user", text, context=context)
        yield {"kind": "conversation", "payload": {"conversation_id": cid}}
        try:
            llm = self.provider.acquire()
        except EngineBusy as e:
            note = (f"Nightshift is busy right now ({e.owner}). I saved your request and will answer "
                    "when the engine is free.")
            if record_user:  # a request replayed from the queue stays queued, not queued twice
                self.store.queue_message(cid, text, context)
                self.store.add_message(cid, "assistant", note)
            yield {"kind": "busy", "payload": {"owner": e.owner, "message": note, "queued": True}}
            yield {"kind": "done", "payload": {"conversation_id": cid}}
            return
        except EngineOffline:
            note = "The Nightshift engine is not running. Open Nightshift Dashboard from the desktop shortcut."
            yield {"kind": "error", "payload": {"code": "engine_offline", "message": note}}
            yield {"kind": "done", "payload": {"conversation_id": cid}}
            return
        except NsError as e:
            yield {"kind": "error", "payload": {"code": e.code, "message": str(e), "hint": e.definition.hint}}
            yield {"kind": "done", "payload": {"conversation_id": cid}}
            return
        try:
            yield from self._turn(llm, cid, context)
        except NsError as e:
            yield {"kind": "error", "payload": {"code": e.code, "message": str(e), "hint": e.definition.hint}}
        finally:
            self.provider.release()
        yield {"kind": "done", "payload": {"conversation_id": cid}}

    def _history(self, cid: str) -> list[dict[str, Any]]:
        msgs = self.store.messages(cid)[-HISTORY_MESSAGES:]
        # a tool message must follow the assistant message that called it: drop orphans at the cut
        while msgs and msgs[0]["role"] == "tool":
            msgs.pop(0)
        keep = ("role", "content", "tool_calls", "tool_call_id", "name")
        return [{k: m[k] for k in keep if k in m} for m in msgs]

    def _turn(self, llm: Any, cid: str, context: dict[str, Any]) -> Iterator[Event]:
        ctx = ToolContext(self.dash, context)
        tools = self.registry.openai_tools()
        for _step in range(self.max_steps):
            messages = [{"role": "system", "content": self.system_prompt(ctx)}, *self._history(cid)]
            reply = llm.chat_tools(messages, tools)
            calls = reply.get("tool_calls") or []
            content = (reply.get("content") or "").strip()
            self.store.add_message(cid, "assistant", content, tool_calls=calls or None)
            if not calls:
                yield {"kind": "message", "payload": {"text": content or "I have nothing to add."}}
                return
            if content:
                yield {"kind": "message", "payload": {"text": content, "partial": True}}
            for c in calls:
                yield from self._run_call(cid, c, ctx)
        note = "I stopped after several steps without a final answer. Please ask again more simply."
        self.store.add_message(cid, "assistant", note)
        yield {"kind": "message", "payload": {"text": note}}

    def _run_call(self, cid: str, call: dict[str, Any], ctx: ToolContext) -> Iterator[Event]:
        fn = call.get("function") or {}
        name = str(fn.get("name", ""))
        call_id = str(call.get("id") or name)
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = None
        td = self.registry.get(name)

        def reply(result: Any) -> None:
            self.store.add_message(cid, "tool", to_text(result), tool_call_id=call_id, name=name)

        if td is None:
            reply({"error": {"code": "unknown_tool", "message": f"There is no tool called {name!r}. Use list_tools."}})
            yield {"kind": "tool_result", "payload": {"tool": name, "ok": False, "error": "unknown tool"}}
            return
        if args is None:
            reply({"error": {"code": "bad_arguments", "message": "Arguments were not valid JSON."}})
            yield {"kind": "tool_result", "payload": {"tool": name, "ok": False, "error": "bad arguments"}}
            return
        if isinstance(args, dict):
            args = fill_from_context(td.spec.args_schema, args, ctx.context)
        problem = self.registry.validate(name, args)
        if problem:
            reply({"error": {"code": "bad_arguments", "message": problem}})
            yield {"kind": "tool_result", "payload": {"tool": name, "ok": False, "error": problem}}
            return
        if td.spec.side_effect:
            text = render_confirm(td.spec, args)
            aid = self.store.add_action(cid, name, args, text, ctx.context)
            reply({"status": "awaiting_owner_confirmation", "action_id": aid, "will_do": text,
                   "note": "Nothing has happened yet. Tell the owner what you asked them to confirm."})
            yield {"kind": "confirm", "payload": {"action_id": aid, "tool": name, "args": args, "text": text}}
            return
        yield {"kind": "tool_call", "payload": {"tool": name, "args": args}}
        ok, result = run_tool(td, ctx, args)
        reply(result)
        yield {"kind": "tool_result", "payload": {"tool": name, "ok": ok, "result": result}}

    # --------------------------------------------------------------- confirm / dismiss
    def confirm(self, action_id: str) -> dict[str, Any]:
        """Run a pending side-effect action after the owner said Yes."""
        action = self.store.get_action(action_id)
        if action is None:
            raise ApiError(404, "not_found", "That action was not found.")
        if not self.store.claim_action(action_id):
            raise ApiError(409, "already_decided", f"That action is already {action['status']}.")
        td = self.registry.get(action["tool"])
        cid = action["conversation_id"]
        if td is None or not td.spec.side_effect:
            self.store.finish_action(action_id, "failed", {"error": "tool no longer available"})
            raise ApiError(410, "tool_gone", "That action is no longer available.")
        problem = self.registry.validate(action["tool"], action["args"])
        if problem:
            self.store.finish_action(action_id, "failed", {"error": problem})
            raise ApiError(422, "bad_arguments", problem)
        ok, result = run_tool(td, ToolContext(self.dash, action["context"]), action["args"])
        self.store.finish_action(action_id, "done" if ok else "failed", result)
        summary = f"Done: {action['confirm_text']}" if ok else f"That did not work: {action['confirm_text']}"
        self.store.add_message(cid, "assistant", f"{summary}\nResult: {to_text(result)}")
        return {"action_id": action_id, "tool": action["tool"], "status": "done" if ok else "failed",
                "message": summary, "result": result, "conversation_id": cid}

    def dismiss(self, action_id: str) -> dict[str, Any]:
        """The owner said No."""
        action = self.store.get_action(action_id)
        if action is None:
            raise ApiError(404, "not_found", "That action was not found.")
        if not self.store.claim_action(action_id):
            raise ApiError(409, "already_decided", f"That action is already {action['status']}.")
        self.store.finish_action(action_id, "dismissed", None)
        self.store.add_message(action["conversation_id"], "assistant", f"Okay, I did not do this: {action['confirm_text']}")
        return {"action_id": action_id, "status": "dismissed", "conversation_id": action["conversation_id"]}

    # --------------------------------------------------------------- queue
    def run_queued(self) -> list[dict[str, Any]]:
        """Answer requests saved while the engine was busy; returns the events of each."""
        done: list[dict[str, Any]] = []
        me = secrets.token_hex(4)
        for q in self.store.queued():
            if not self.store.claim_queued(q["queue_id"], me):  # another caller is answering it right now
                continue
            try:
                events = list(self.chat(q["text"], q["conversation_id"], q["context"], record_user=False))
            except BaseException:
                self.store.release_queued(q["queue_id"], me)
                raise
            if any(e["kind"] == "busy" for e in events):
                self.store.release_queued(q["queue_id"], me)
                break
            self.store.mark_queued_done(q["queue_id"])
            done.append({"queue_id": q["queue_id"], "events": events})
        return done

    def history(self, cid: str) -> dict[str, Any]:
        """Transcript plus actions still waiting for an answer."""
        if not self.store.has_conversation(cid):
            raise ApiError(404, "not_found", "That conversation was not found.")
        return {"conversation_id": cid, "messages": self.store.messages(cid),
                "pending_actions": [{k: a[k] for k in ("action_id", "tool", "args", "confirm_text")}
                                    for a in self.store.pending_actions(cid)]}
