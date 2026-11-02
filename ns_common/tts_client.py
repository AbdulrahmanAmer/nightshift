from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
import numpy as np

from ns_common.errors import NsError
from ns_common.pacing import pause_plan
from ns_common.textstats import word_count


@dataclass(frozen=True)
class TtsItem:
    id: str
    text: str
    voice: str
    speed: float = 1.0
    cliff_lines: tuple[str, ...] = ()   # lines that get a 1,200 ms pause before them (spec 7.5)
    language: str | None = None         # the channel's ISO code; the service maps it to qwen-tts's --lang names


def _item_body(i: TtsItem) -> dict[str, Any]:
    body: dict[str, Any] = {"id": i.id, "input": i.text, "voice": i.voice, "speed": i.speed}
    if i.cliff_lines:
        body["cliff_lines"] = list(i.cliff_lines)
    if i.language:
        body["language"] = i.language
    return body


@dataclass(frozen=True)
class TtsFile:
    id: str
    path: Path
    seconds: float


class Tts(Protocol):
    def speak(
        self, text: str, voice: str, speed: float, out_path: Path, *, seed: int | None = None,
        language: str | None = None,
    ) -> float: ...

    def speak_batch(
        self, items: list[TtsItem], out_dir: Path, *, seed: int | None = None
    ) -> list[TtsFile]: ...

    def voices(self) -> list[dict]: ...

    def health(self) -> bool: ...


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


_SERVICE_CODES = ("NS-E310", "NS-E311", "NS-E315")


def _raise_voice_mismatch(r: httpx.Response) -> None:
    """Keep the TTS code the service answered with.

    `NS-E310` synthesis failed, `NS-E311` a voice of another engine or missing files, `NS-E315` a voice could
    not be built. Anything else is raised by the caller.
    """
    if r.status_code < 400:
        return
    try:
        body = r.json()
    except ValueError:
        return
    if r.status_code == 404 and isinstance(body, dict) and "unknown voice" in str(body.get("detail", "")):
        raise NsError(
            "NS-E311", f"{body['detail']}: create the voice first (Library > Voices, or 'studio voice design')"
        )
    if isinstance(body, dict) and body.get("error") in _SERVICE_CODES:
        raise NsError(str(body["error"]), str(body.get("detail", "")))


class TtsClient:
    def __init__(self, base_url: str, *, timeout_s: float = 1800, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=timeout_s)

    def speak(
        self, text: str, voice: str, speed: float, out_path: Path, *, seed: int | None = None,
        cliff_lines: tuple[str, ...] = (), language: str | None = None,
    ) -> float:
        body: dict[str, Any] = {
            "model": "qwen3-tts", "input": text, "voice": voice, "speed": speed, "response_format": "wav",
        }
        if cliff_lines:
            body["cliff_lines"] = list(cliff_lines)
        if language:
            body["language"] = language
        if seed is not None:
            body["seed"] = seed
        try:
            r = self.client.post(f"{self.base_url}/v1/audio/speech", json=body)
            _raise_voice_mismatch(r)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise NsError("NS-E310", f"tts: {e}") from e
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(r.content)
        return wav_seconds(out_path)

    def speak_batch(
        self, items: list[TtsItem], out_dir: Path, *, seed: int | None = None
    ) -> list[TtsFile]:
        body: dict[str, Any] = {
            "items": [_item_body(i) for i in items],
            "out_dir": str(out_dir),
        }
        if seed is not None:
            body["seed"] = seed
        try:
            r = self.client.post(f"{self.base_url}/v1/audio/speech/batch", json=body)
            _raise_voice_mismatch(r)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise NsError("NS-E310", f"tts batch: {e}") from e
        return [TtsFile(f["id"], Path(f["path"]), float(f["seconds"])) for f in r.json()["files"]]

    def voices(self) -> list[dict]:
        try:
            r = self.client.get(f"{self.base_url}/v1/voices")
            r.raise_for_status()
            return list(r.json())
        except httpx.HTTPError as e:
            raise NsError("NS-E310", f"tts voices: {e}") from e

    def health(self) -> bool:
        try:
            r = self.client.get(f"{self.base_url}/health", timeout=5)
            return r.status_code == 200 and bool(r.json().get("ok"))
        except (httpx.HTTPError, ValueError):
            return False


class FakeTts:
    """Tone-burst narration. `items` records every item it was asked to speak (voice, speed, language)."""

    def __init__(self, *, wpm: float = 155, sr: int = 24000) -> None:
        self.wpm = wpm
        self.sr = sr
        self.items: list[TtsItem] = []

    def speak(
        self, text: str, voice: str, speed: float, out_path: Path, *, seed: int | None = None,
        language: str | None = None, cliff_lines: tuple[str, ...] = (),
    ) -> float:
        samples = self._samples(text, speed, cliff_lines)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sr)
            w.writeframes(samples.tobytes())
        return round(len(samples) / self.sr, 3)

    def _tone(self, words: int, speed: float, *, floor_s: float = 0.0) -> np.ndarray:
        """One tone burst per word, `words / (wpm * speed)` minutes long (at least `floor_s`)."""
        seconds = max(floor_s, words / (self.wpm * speed) * 60.0)
        n = int(seconds * self.sr)
        burst = int(0.08 * self.sr)
        idx = np.arange(n)
        tone = 0.3 * np.sin(2 * np.pi * 440.0 * idx / self.sr)
        gate = np.mod(idx, n / max(1, words)) < burst
        return (np.where(gate, tone, 0.0) * 32767).astype("<i2")

    def _samples(self, text: str, speed: float, cliff_lines: tuple[str, ...]) -> np.ndarray:
        """Speech with the silences the real service inserts (`ns_common.pacing.pause_plan`): 350 ms after a
        sentence, 700 ms before a paragraph, 1,200 ms before a cliff line. A fake without them made every
        word budget look 8 % shorter than the audio it produces."""
        plan = pause_plan(text, cliff_lines)
        if len(plan) <= 1:
            return self._tone(max(1, word_count(text)), speed, floor_s=0.4)
        parts: list[np.ndarray] = []
        for chunk, pause in plan:
            parts.append(self._tone(max(1, word_count(chunk)), speed))
            if pause > 0:
                parts.append(np.zeros(int(pause * self.sr), dtype="<i2"))
        return np.concatenate(parts)

    def speak_batch(
        self, items: list[TtsItem], out_dir: Path, *, seed: int | None = None
    ) -> list[TtsFile]:
        out: list[TtsFile] = []
        for it in items:
            p = out_dir / f"{it.id}.wav"
            self.items.append(it)
            out.append(
                TtsFile(
                    it.id, p,
                    self.speak(it.text, it.voice, it.speed, p, seed=seed, language=it.language,
                               cliff_lines=it.cliff_lines),
                )
            )
        return out

    def voices(self) -> list[dict]:
        return [
            {"name": "Ryan", "mode": "preset", "source": "fake"},
            {"name": "Aiden", "mode": "preset", "source": "fake"},
        ]

    def health(self) -> bool:
        return True
