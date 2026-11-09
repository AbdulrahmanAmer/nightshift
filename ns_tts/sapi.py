r"""C4 ns_tts: Windows speech (SAPI) for the fake voice of an engine test (head order H-7, defect D-26).

The fake engine of an engine-test install (`tts.engine: fake`, docs/DEV_MODE.md "Real engines, fake voice")
used to make a tone burst per word. Real speech recognition (the engine test runs the real whisper) hears no
words in tones, so every story or script job stopped at the captions stage with NS-E403. This module lets the
fake speak the actual text with the speech synthesizer that ships with Windows: no download, no model, no GPU.

How it runs: `System.Speech.Synthesis.SpeechSynthesizer` in Windows PowerShell 5.1 (on every Windows),
started once per batch of sentences with the text in a UTF-8 JSON file and one WAV per sentence written at the
service's sample rate. The service process itself stays pure Python; a PC without a SAPI voice, a stripped
Windows and a non-Windows test runner all end in `SapiUnavailable`, and the fake engine falls back to its tone
bursts and says so (`FakeEngine.describe`, `/health`, the service log and the job log).

Pace: SAPI's `Rate` is an integer from -10 to 10, about 11 to 12 % per step (measured on Microsoft David
Desktop with `CALIBRATION_TEXT`, silence trimmed: 109, 136, 171, 207, 267, 335, 420 words per minute at rates
-6, -4, -2, 0, 2, 4, 6). The engine measures the voice once at start-up and picks the step whose pace is
closest to `NOMINAL_WPM * speed`, so a request for speed 1.0 lands within about 6 % of the 155 wpm that the
planner and the tone bursts assume.
"""

from __future__ import annotations

import base64
import json
import math
import os
import shutil
import subprocess  # noqa: TID251  (CR-H7-1: a ruff per-file ignore for this module, like ns_tts/engine.py)
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from ns_common.errors import NsError
from ns_common.pacing import NOMINAL_WPM
from ns_tts.audio import read_wav

RATE_MIN, RATE_MAX = -10, 10
RATE_STEP = 1.115  # pace ratio of two neighbouring SAPI rates (1.107 to 1.123 measured on Microsoft David)
PEAK_CEILING = 0.89  # about -1 dBFS: SAPI voices peak near full scale; the loudness stage lifts the level

# The start-up probe (voice list, then one calibration sentence) runs before the service binds its port, and
# the supervisor gives the service 120 s (`health_timeout_s`) to answer. These two caps add up to 80 s, so a
# hung or very slow PowerShell ends in tone bursts and a warning, never in a killed service (NS-E203).
PROBE_VOICES_TIMEOUT_S = 35.0
PROBE_SPEAK_TIMEOUT_S = 45.0

# Played once at start-up to measure the voice's pace at Rate 0 (two sentences with a comma: the usual shape).
CALIBRATION_TEXT = (
    "The lighthouse keeper climbed the stairs every evening, counting each step as his father taught him. "
    "At the top he lit the great lamp and watched the dark water for any sign of a ship."
)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# One script for both jobs (`mode`: voices | speak). The job comes in a UTF-8 JSON file named by NS_SAPI_JOB
# so no text ever travels on the command line; the voice list goes back in the file the job names.
_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$job = [IO.File]::ReadAllText($env:NS_SAPI_JOB, [Text.Encoding]::UTF8) | ConvertFrom-Json
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  if ($job.mode -eq 'voices') {
    $found = @($synth.GetInstalledVoices() | Where-Object { $_.Enabled } | ForEach-Object {
      $i = $_.VoiceInfo
      [pscustomobject]@{ name = $i.Name; culture = $i.Culture.Name; gender = [string]$i.Gender }
    })
    $json = ConvertTo-Json -InputObject $found -Compress
    [IO.File]::WriteAllText([string]$job.result, $json, (New-Object Text.UTF8Encoding $false))
  } else {
    $synth.SelectVoice([string]$job.voice)
    $synth.Rate = [int]$job.rate
    $fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
      [int]$job.sr, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
      [System.Speech.AudioFormat.AudioChannel]::Mono)
    foreach ($item in @($job.items)) {
      $synth.SetOutputToWaveFile([string]$item.out, $fmt)
      $synth.Speak([string]$item.text)
      $synth.SetOutputToNull()
    }
  }
} finally {
  $synth.Dispose()
}
"""


class SapiUnavailable(Exception):
    """No usable Windows speech on this machine; `str(e)` says why (it goes into /health and the logs)."""


@dataclass(frozen=True)
class SapiVoice:
    """One installed, enabled SAPI voice."""

    name: str
    culture: str = ""  # e.g. "en-US"
    gender: str = ""


class SapiBackend(Protocol):
    """What the fake engine needs from the speech synthesizer (tests pass a stand-in)."""

    def voices(self) -> list[SapiVoice]:
        """Enabled voices; `[]` when none is installed; `SapiUnavailable` when SAPI cannot be asked at all."""
        ...

    def speak(
        self, voice: str, lines: Sequence[str], *, rate: int, sr: int, timeout_s: float
    ) -> list[np.ndarray]:
        """Speak every line with `voice` at SAPI `rate`: float32 mono at `sr`, one per line (NS-E310)."""
        ...


def pick_voice(voices: Sequence[SapiVoice]) -> SapiVoice | None:
    """The voice the fake uses, chosen only from what is installed so two runs on one PC pick the same one.

    English (en-US first, then the other en-*) before any other language, then by name, case-insensitive. On a
    stock Windows 10 or 11 that is Microsoft David Desktop (Zira sorts after it).
    """

    def rank(v: SapiVoice) -> tuple[int, str, str]:
        culture = v.culture.lower()
        tier = 0 if culture == "en-us" else 1 if culture.startswith("en") else 2
        return tier, v.name.casefold(), v.name

    return min(voices, key=rank) if voices else None


def rate_for(speed: float, base_wpm: float) -> int:
    """SAPI `Rate` whose pace is closest to `NOMINAL_WPM * speed` for a voice of `base_wpm` at Rate 0."""
    if base_wpm <= 0 or speed <= 0 or speed != speed:
        return 0
    step = math.log(NOMINAL_WPM * speed / base_wpm) / math.log(RATE_STEP)
    return max(RATE_MIN, min(RATE_MAX, round(step)))


def limit_peak(x: np.ndarray) -> np.ndarray:
    """`x` scaled down when it peaks above `PEAK_CEILING`; never boosted (the loudness stage sets level)."""
    if x.size == 0:
        return x
    peak = float(np.max(np.abs(x)))
    if peak > PEAK_CEILING:
        return (x * (PEAK_CEILING / peak)).astype(np.float32)
    return x


class PowerShellSapi:
    """`SapiBackend` over Windows PowerShell 5.1 and `System.Speech`. `platform` is injectable for tests."""

    def __init__(self, *, tmp_dir: Path | None = None, platform: str | None = None) -> None:
        self.tmp_dir = tmp_dir
        self.platform = platform or sys.platform

    def _powershell(self) -> str:
        if not self.platform.startswith("win"):
            raise SapiUnavailable(f"Windows speech (SAPI) needs Windows, this is {self.platform}")
        found = shutil.which("powershell") or shutil.which("powershell.exe")
        if found:
            return found
        root = os.environ.get("SystemRoot") or r"C:\Windows"
        fallback = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if fallback.is_file():
            return str(fallback)
        raise SapiUnavailable("Windows PowerShell was not found, so Windows speech (SAPI) cannot be asked")

    def _run(self, job: dict[str, object], *, timeout_s: float) -> None:
        exe = self._powershell()
        with self._scratch() as td:
            job_file = td / "job.json"
            try:
                job_file.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            except OSError as e:
                raise SapiUnavailable(f"cannot write the speech job file: {e}") from e
            encoded = base64.b64encode(_SCRIPT.encode("utf-16-le")).decode("ascii")
            cmd = [
                exe,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=timeout_s,
                    check=False,
                    env={**os.environ, "NS_SAPI_JOB": str(job_file)},
                    creationflags=_NO_WINDOW,
                )
            except subprocess.TimeoutExpired as e:
                raise SapiUnavailable(f"Windows speech (SAPI) did not answer within {timeout_s:.0f} s") from e
            except OSError as e:
                raise SapiUnavailable(f"cannot start Windows PowerShell: {e}") from e
            if proc.returncode != 0:
                raw = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace")
                tail = " ".join(raw.strip().splitlines()[-4:])[:400]
                raise SapiUnavailable(f"Windows speech (SAPI) failed (exit {proc.returncode}): {tail}")

    def voices(self) -> list[SapiVoice]:
        self._powershell()  # fail early (and cheaply) on a machine that cannot run it
        with self._scratch() as td:
            result = td / "voices.json"
            self._run({"mode": "voices", "result": str(result)}, timeout_s=PROBE_VOICES_TIMEOUT_S)
            try:
                raw = json.loads(result.read_text(encoding="utf-8-sig") or "[]")
            except (OSError, ValueError) as e:
                raise SapiUnavailable(f"Windows speech (SAPI) gave no readable voice list: {e}") from e
        rows = [raw] if isinstance(raw, dict) else list(raw or [])
        return [
            SapiVoice(str(r.get("name", "")), str(r.get("culture", "")), str(r.get("gender", "")))
            for r in rows
            if isinstance(r, dict) and r.get("name")
        ]

    @contextmanager
    def _scratch(self) -> Iterator[Path]:
        """A fresh scratch folder (in `tmp_dir` when given); one that cannot be made is `SapiUnavailable`."""
        try:
            if self.tmp_dir is not None:
                self.tmp_dir.mkdir(parents=True, exist_ok=True)
            td = tempfile.TemporaryDirectory(prefix="ns-sapi-", dir=self.tmp_dir)
        except OSError as e:
            raise SapiUnavailable(f"cannot make a scratch folder for Windows speech (SAPI): {e}") from e
        with td:
            yield Path(td.name)

    def speak(
        self, voice: str, lines: Sequence[str], *, rate: int, sr: int, timeout_s: float
    ) -> list[np.ndarray]:
        if not lines:
            return []
        try:
            with self._scratch() as td:
                outs = [td / f"{i:05d}.wav" for i in range(len(lines))]
                items = [{"text": text, "out": str(out)} for text, out in zip(lines, outs, strict=True)]
                self._run(
                    {"mode": "speak", "voice": voice, "rate": int(rate), "sr": sr, "items": items},
                    timeout_s=timeout_s,
                )
                return [self._read_line(out, i, sr) for i, out in enumerate(outs)]
        except SapiUnavailable as e:
            raise NsError("NS-E310", str(e)) from e

    @staticmethod
    def _read_line(out: Path, i: int, sr: int) -> np.ndarray:
        if not out.is_file():
            raise NsError("NS-E310", f"Windows speech (SAPI) wrote no audio for line {i + 1}")
        try:
            x, got = read_wav(out)
        except ValueError as e:
            raise NsError("NS-E310", f"Windows speech (SAPI) wrote an unreadable WAV: {e}") from e
        if got != sr:
            raise NsError("NS-E310", f"Windows speech (SAPI) wrote {got} Hz, expected {sr} Hz")
        return x
