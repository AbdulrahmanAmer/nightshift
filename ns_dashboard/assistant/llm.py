"""OpenAI-compatible tool-calling chat for the Assistant, plus a scripted fake (component C9).

`chat_tools` is implemented here on top of `ns_common.llm_client.LlmClient` until a CR moves it there.
"""
# Dense descriptor tables and long signatures: wrapping them hurts readability.
# ruff: noqa: E501

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any, Protocol

from ns_common.acquire import Acquirer
from ns_common.errors import NsError
from ns_common.llm_client import LlmClient, strip_thinking

OWNER = "dashboard:assistant"
#: The Assistant runs on the Balanced base model (`code`), never on the story model: in two-model mode `story` is a
#: 21 GB abliterated writer that does not fit the card and is the wrong choice for an agent that must obey confirm rules.
MODEL_ALIAS = "code"
#: How often a held GPU lease is renewed. The engine reclaims a `dashboard:assistant` hold that stays silent for
#: its lease time (10 minutes), so a dashboard that died in the middle of a turn cannot keep the GPU forever (P4).
BEAT_S = 30.0


class ToolLlm(Protocol):
    """Anything that can answer one chat turn with optional tool calls."""

    def chat_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, max_tokens: int | None = None
    ) -> dict[str, Any]:
        """Return an assistant message `{role, content, tool_calls?}`; `max_tokens` caps the reply."""
        ...


class EngineBusy(Exception):
    """The GPU is held by someone else (a nightly run, Nightshift Code)."""

    def __init__(self, owner: str) -> None:
        super().__init__(owner)
        self.owner = owner


class EngineOffline(Exception):
    """The supervisor does not answer."""


class ToolLlmClient(LlmClient):
    """`LlmClient` plus `chat_tools` (chat completions with `tools`)."""

    def chat_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """One tool-calling chat turn; `tool_calls[].function.arguments` stays a JSON string."""
        body: dict[str, Any] = {
            "model": self.alias,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": temperature,
            "max_tokens": max_tokens or 1200,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        try:
            r = self.client.post(f"{self.base_url}/v1/chat/completions", json=body)
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
        except NsError:
            raise
        except Exception as e:  # network or malformed reply: wrap into the coded error
            raise NsError("NS-E301", f"assistant chat failed: {e}", url=self.base_url) from e
        out: dict[str, Any] = {"role": "assistant", "content": strip_thinking(msg.get("content") or "")}
        calls = []
        for i, c in enumerate(msg.get("tool_calls") or []):
            fn = c.get("function") or {}
            args = fn.get("arguments", "{}")
            calls.append(
                {
                    "id": c.get("id") or f"call_{i}",
                    "type": "function",
                    "function": {"name": fn.get("name", ""), "arguments": args if isinstance(args, str) else json.dumps(args)},
                }
            )
        if calls:
            out["tool_calls"] = calls
        return out


class LlmProvider(Protocol):
    """Gives the loop an LLM for one turn and takes it back afterwards."""

    def acquire(self) -> ToolLlm:
        """Raise `EngineBusy` / `EngineOffline` when the model cannot be used right now."""
        ...

    def release(self) -> None:
        """Hand the model back."""
        ...


class SupervisorLlmProvider:
    """Uses the `llama` service through the supervisor; refuses while someone else holds the GPU."""

    def __init__(
        self, status_fn: Callable[[], dict[str, Any] | None], acquirer: Acquirer, *, beat_s: float = BEAT_S
    ) -> None:
        self.status_fn = status_fn
        self.acquirer = acquirer
        self.beat_s = beat_s
        self._beat_stop = threading.Event()
        self._beat: threading.Thread | None = None

    def acquire(self) -> ToolLlm:
        """Acquire `llama` for the Assistant (short wait) and build a client for its endpoint."""
        st = self.status_fn()
        if st is None:
            raise EngineOffline("the supervisor does not answer")
        owner = st.get("gpu_owner")
        if owner and owner != OWNER:
            raise EngineBusy(str(owner))
        ep = self.acquirer.acquire("llama", OWNER, model_alias=MODEL_ALIAS, timeout_s=120)
        self._start_beat()
        return ToolLlmClient(ep.base_url, ep.model_alias or MODEL_ALIAS, timeout_s=300)

    def release(self) -> None:
        """Release `llama`; the supervisor unloads it after its idle timeout."""
        self._stop_beat()
        self.acquirer.release("llama", OWNER)

    def _start_beat(self) -> None:
        """Renew the lease while the turn lasts (P4): a silent hold is reclaimed by the engine."""
        beat = getattr(self.acquirer, "heartbeat", None)
        if beat is None:
            return
        self._stop_beat()
        stop = threading.Event()
        self._beat_stop = stop

        def loop() -> None:
            while not stop.wait(self.beat_s):
                try:
                    beat(OWNER)
                except Exception:  # noqa: BLE001 - a missed beat is retried; the turn must not break
                    pass

        self._beat = threading.Thread(target=loop, name="ns-assistant-beat", daemon=True)
        self._beat.start()

    def _stop_beat(self) -> None:
        self._beat_stop.set()
        t = self._beat
        if t is not None and t is not threading.current_thread():
            t.join(1.0)
        self._beat = None


class StaticProvider:
    """Provider around an already built LLM (tests, `--fake-services`)."""

    def __init__(self, llm: ToolLlm, busy_owner: str | None = None) -> None:
        self.llm = llm
        self.busy_owner = busy_owner
        self.released = 0

    def acquire(self) -> ToolLlm:
        """The wrapped LLM, or `EngineBusy` when `busy_owner` is set."""
        if self.busy_owner:
            raise EngineBusy(self.busy_owner)
        return self.llm

    def release(self) -> None:
        """Count releases."""
        self.released += 1


# ---------------------------------------------------------------- scripted fake


def say(text: str) -> dict[str, Any]:
    """A final assistant answer."""
    return {"role": "assistant", "content": text}


def call(name: str, args: dict[str, Any] | None = None, *, content: str = "", call_id: str | None = None) -> dict[str, Any]:
    """An assistant turn that calls one tool."""
    return calls([(name, args or {})], content=content, ids=[call_id] if call_id else None)


def calls(items: list[tuple[str, dict[str, Any]]], *, content: str = "", ids: list[str | None] | None = None) -> dict[str, Any]:
    """An assistant turn that calls several tools."""
    out = []
    for i, (name, args) in enumerate(items):
        cid = (ids[i] if ids and i < len(ids) else None) or f"call_{name}_{i}"
        out.append({"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}})
    return {"role": "assistant", "content": content, "tool_calls": out}


class ScriptedToolLlm:
    """Plays back a list of assistant messages (or callables that build one from the conversation)."""

    def __init__(self, script: list[dict[str, Any] | Callable[[list[dict[str, Any]]], dict[str, Any]]]) -> None:
        self.script = list(script)
        self.requests: list[list[dict[str, Any]]] = []
        self.tool_lists: list[list[dict[str, Any]]] = []
        self.max_tokens: list[int | None] = []

    def chat_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, max_tokens: int | None = None
    ) -> dict[str, Any]:
        """Next scripted message; after the script ends it answers with a stock sentence."""
        self.requests.append([dict(m) for m in messages])
        self.tool_lists.append(tools)
        self.max_tokens.append(max_tokens)
        if not self.script:
            return say("(script finished)")
        item = self.script.pop(0)
        return item(messages) if callable(item) else item


class EchoToolLlm:
    """Offline stand-in for `--fake-services`: answers with the last thing the owner said."""

    def chat_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, max_tokens: int | None = None
    ) -> dict[str, Any]:
        """Echo the latest user message."""
        last = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        return say(f"(fake assistant) I heard: {last}")
