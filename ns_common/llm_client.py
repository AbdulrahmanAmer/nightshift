from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol

import httpx

from ns_common.errors import NsError

_THINK = re.compile(r"<think>.*?</think>\s*", re.S)


class Llm(Protocol):
    def complete_json(self, *, prompt_name: str, system: str, user: str, schema: dict,
                      thinking: bool = False, temperature: float = 0.8, max_tokens: int = 4096) -> dict: ...

    def complete_text(self, *, prompt_name: str, system: str, user: str,
                      thinking: bool = False, temperature: float = 0.8, max_tokens: int = 2048) -> str: ...

    def health(self) -> bool: ...


def strip_thinking(text: str) -> str:
    return _THINK.sub("", text).strip()


class LlmClient:
    def __init__(
        self, base_url: str, alias: str, *, timeout_s: float = 600, client: httpx.Client | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.alias = alias
        self.client = client or httpx.Client(timeout=timeout_s)

    def _chat(self, messages: list[dict], *, thinking: bool, temperature: float, max_tokens: int,
              schema: dict | None) -> str:
        body: dict[str, Any] = {
            "model": self.alias,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": thinking},
        }
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "out", "schema": schema},
            }
        try:
            r = self.client.post(f"{self.base_url}/v1/chat/completions", json=body)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise NsError("NS-E301", str(e), url=self.base_url) from e
        try:
            return strip_thinking(r.json()["choices"][0]["message"]["content"])
        except (KeyError, IndexError, ValueError) as e:
            raise NsError("NS-E301", f"malformed response: {e}") from e

    def complete_json(self, *, prompt_name: str, system: str, user: str, schema: dict,
                      thinking: bool = False, temperature: float = 0.8, max_tokens: int = 4096) -> dict:
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        raw = self._chat(
            messages, thinking=thinking, temperature=temperature, max_tokens=max_tokens, schema=schema
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        messages += [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": "Repair: your previous answer was not valid JSON. Reply with only the JSON.",
            },
        ]
        raw2 = self._chat(messages, thinking=False, temperature=0.2, max_tokens=max_tokens, schema=schema)
        try:
            return json.loads(raw2)
        except json.JSONDecodeError as e:
            raise NsError("NS-E302", f"{prompt_name}: {raw2[:200]}") from e

    def complete_text(self, *, prompt_name: str, system: str, user: str,
                      thinking: bool = False, temperature: float = 0.8, max_tokens: int = 2048) -> str:
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return self._chat(
            messages, thinking=thinking, temperature=temperature, max_tokens=max_tokens, schema=None
        )

    def health(self) -> bool:
        try:
            r = self.client.get(f"{self.base_url}/health", timeout=5)
            return r.status_code == 200 and r.json().get("status") == "ok"
        except (httpx.HTTPError, ValueError):
            return False


class FakeLlm:
    def __init__(
        self, fixtures_dir: Path, *, overrides: dict[str, dict | list[dict]] | None = None
    ) -> None:
        self.fixtures_dir = fixtures_dir
        self.overrides = overrides or {}
        self._cursor: dict[str, int] = {}
        self.calls: list[dict] = []

    def _record(self, **kw: Any) -> None:
        self.calls.append(kw)

    def complete_json(self, *, prompt_name: str, system: str, user: str, schema: dict,
                      thinking: bool = False, temperature: float = 0.8, max_tokens: int = 4096) -> dict:
        self._record(prompt_name=prompt_name, system=system, user=user, thinking=thinking, schema=schema,
                     temperature=temperature)
        if prompt_name in self.overrides:
            ov = self.overrides[prompt_name]
            if isinstance(ov, list):
                i = self._cursor.get(prompt_name, 0)
                self._cursor[prompt_name] = (i + 1) % len(ov)
                return json.loads(json.dumps(ov[i]))
            return json.loads(json.dumps(ov))
        f = self.fixtures_dir / f"{prompt_name}.json"
        if not f.exists():
            raise FileNotFoundError(f)
        return json.loads(f.read_text(encoding="utf-8"))

    def complete_text(self, *, prompt_name: str, system: str, user: str,
                      thinking: bool = False, temperature: float = 0.8, max_tokens: int = 2048) -> str:
        self._record(prompt_name=prompt_name, system=system, user=user, thinking=thinking,
                     temperature=temperature)
        f = self.fixtures_dir / f"{prompt_name}.txt"
        return f.read_text(encoding="utf-8") if f.exists() else f"FAKE TEXT for {prompt_name}"

    def health(self) -> bool:
        return True
