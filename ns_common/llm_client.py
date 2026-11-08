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


# the clip_select prompt says "Window: 0.0 s to 53.6 s" and lists segments as "[3] 12.0 - 15.5: text"
_WINDOW_HEAD = re.compile(r"Window:\s*(\d+(?:\.\d+)?)\s*s\s+to\s+(\d+(?:\.\d+)?)\s*s")
_WINDOW_LINE = re.compile(r"^\[\d+\]\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?):", re.M)


_MAP_PARTS = re.compile(r"Plan exactly (\d+) parts")


def fit_map_fixture(data: dict, user: str) -> dict:
    """The workshop_map fixture plans three parts; a dev-mode series asking for two or four parts stopped
    with NS-E302 "returned 3 parts, expected N". The fixture is trimmed or its last part repeated so the
    count matches what the prompt asked for (part numbers renumbered)."""
    m = _MAP_PARTS.search(user or "")
    parts = data.get("parts") if isinstance(data, dict) else None
    if not m or not isinstance(parts, list) or not parts:
        return data
    want = int(m.group(1))
    if want == len(parts) or want < 1:
        return data
    fitted = [dict(p) for p in parts[:want]]
    while len(fitted) < want:
        extra = dict(parts[-1])
        extra["title"] = f"{extra.get('title', 'Part')} {len(fitted) + 1}"[:40]
        fitted.append(extra)
    for i, p in enumerate(fitted, start=1):
        p["n"] = i
        if i == 1:
            p["recap"] = ""
    return {**data, "parts": fitted}


def fit_clip_fixture(data: dict, user: str) -> dict:
    """The clip_select fixture's moments sit at 62-262 s of a test recording; on any other video (a dev-mode
    clip job on a 54 s Short) none of them fall inside the window and the selector ends with NS-E407 "the
    model returned no candidates". When no fixture clip overlaps the window the prompt describes, two moments
    are made up inside it instead (the first and the middle third), so the clipper's later stages run."""
    head = _WINDOW_HEAD.search(user or "")
    if head:
        w_start, w_end = float(head.group(1)), float(head.group(2))
    else:
        spans = [(float(a), float(b)) for a, b in _WINDOW_LINE.findall(user or "")]
        if not spans:
            return data
        w_start, w_end = min(a for a, _ in spans), max(b for _, b in spans)
    clips = data.get("clips") if isinstance(data, dict) else None
    if isinstance(clips, list) and any(
        isinstance(c, dict) and float(c.get("end_s", 0)) > w_start and float(c.get("start_s", 0)) < w_end
        for c in clips
    ):
        return data
    span = max(0.0, w_end - w_start)
    if span < 8.0:
        return data
    third = span / 3.0
    first = (w_start, w_start + max(5.0, third))
    middle = (w_start + third, w_start + 2 * third)
    made = []
    for k, (a, b) in enumerate(((first), (middle)), start=1):
        made.append({
            "k": k, "start_s": round(a, 1), "end_s": round(min(b, w_end), 1),
            "title": f"Moment {k} (dev-mode stand-in)", "hook_line": None,
            "why": "made up by the fake model so the clip pipeline can run on this video",
            "score": round(0.9 - 0.1 * (k - 1), 2), "speakers": None, "keywords": ["dev"],
        })
    return {**data, "clips": made}


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
        data = json.loads(f.read_text(encoding="utf-8"))
        if prompt_name == "clip_select":
            data = fit_clip_fixture(data, user)
        if prompt_name == "workshop_map":
            data = fit_map_fixture(data, user)
        return data

    def complete_text(self, *, prompt_name: str, system: str, user: str,
                      thinking: bool = False, temperature: float = 0.8, max_tokens: int = 2048) -> str:
        self._record(prompt_name=prompt_name, system=system, user=user, thinking=thinking,
                     temperature=temperature)
        f = self.fixtures_dir / f"{prompt_name}.txt"
        return f.read_text(encoding="utf-8") if f.exists() else f"FAKE TEXT for {prompt_name}"

    def health(self) -> bool:
        return True
