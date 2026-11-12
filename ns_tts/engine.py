r"""C4 ns_tts: TTS engines behind one protocol (spec 2, 6.1, 14.2, 15.4).

Engines: `QwenttsEngine` (the qwentts.cpp command-line tools, default), `ReferenceEngine` (PyTorch `qwen-tts`,
the NS-W301 fallback when the qwentts binary fails its smoke test) and `FakeEngine` (CI, `NS_TTS_FAKE=1`; tone
bursts, or on an engine-test install the real text spoken by Windows speech, `ns_tts.sapi`).
Heavy imports (`torch`, `qwen_tts`) happen only inside methods.

The qwentts contract (pinned commit in `.github/workflows/build-qwentts.yml`; flags checked against the
`--help` text in its `docs/ARCHITECTURE.md`). The release zip holds `qwen-tts.exe`, `qwen-codec.exe` and
`tts-server.exe` in `<home>\bin\qwentts`; there is no `qwentts.exe`. `qwen-tts.exe` reads the text from
**stdin** (UTF-8), takes language and speaker as flags and writes a 24 kHz mono 16-bit WAV to `-o`. The text
is never put on the command line. `<talker>` and `<codec>` are
`<home>\models\tts\qwen-talker-<size>-<kind>-Q8_0.gguf` and `qwen-tokenizer-12hz-Q8_0.gguf`.

One process per job, not per sentence: with `--stream-by-line -o -` the binary reads stdin line by line,
synthesises each line as its own utterance with the model and speaker staying resident, and streams the WAVs
to stdout, each after the first opening with a fresh 44-byte RIFF header (sizes 0x7FFFFFFF) that
`ns_tts.audio.split_wav_stream` cuts on. `synth_batch` sends every chunk of a request that shares voice,
language and speed in one such process (the server collects the chunks of all items of a batch request).
The trade-off (upstream `--help` and `tools/qwen-tts.cpp` at the pinned commit; not yet timed on a GPU PC):
a master of about 200 chunks pays one start-up of the 1.7B talker and the codec (3-6 s estimated) instead of
200, saving 10-20 minutes of GPU tenancy; one failing chunk now fails the whole request, and the job
retries from the voice stage. A single chunk (`synth`) still uses the file route.

  preset (CustomVoice talker; speaker names are lowercase: vivian, uncle_fu, ono_anna ...):
    qwen-tts.exe --model <customvoice> --codec <codec> [--device CPU] --speaker vivian --lang English
                 [--seed N] -o <tmp>\out.wav            < text
  stored voice, cloned or designed (Base talker, in-context clone from the voice folder):
    qwen-tts.exe --model <base> --codec <codec> [--device CPU] --ref-spk <voice>\speaker.spk
                 --ref-rvq <voice>\speaker.rvq --ref-text <voice>\ref.txt --lang English [--seed N]
                 -o <tmp>\out.wav                       < text
  new clone voice (.spk and .rvq are written next to the input WAV, then stored in the voice folder):
    qwen-codec.exe --model <codec> --talker <base> [--device CPU] -i <tmp>\speaker.wav
                 -> <tmp>\speaker.spk + <tmp>\speaker.rvq   (ref.txt = the transcript, written by us)
  new designed voice (VoiceDesign talker renders the sample text, then the render is encoded as above):
    qwen-tts.exe --model <voicedesign> --codec <codec> [--device CPU] --instruct "<description>"
                 --lang English -o <tmp>\render.wav     < SAMPLE_TEXT

Why not `tts-server.exe` behind `ns_tts.server`: it loads exactly one talker GGUF, so presets (CustomVoice),
clones (Base) and designs (VoiceDesign) would need up to three servers or a restart per mode, its voice
registry lives in RAM (re-registered after every restart), and a long-lived grandchild process would outlive
the TTS service when the supervisor stops it. Its `speed` field is parsed and ignored. A short-lived
`qwen-tts` process per batch keeps the contract exact and leaves nothing behind.

Arrival stamps (card verify-limits L2): while `_run_stream` reads the stream it notes the moment each
utterance is complete, that is when the NEXT utterance's header arrives, and for the last one when the stream
ends. `synth_batch` leaves them in `last_stamps` as (`time.perf_counter()` value, audio seconds) per
utterance; the service hands them to its caller, and `ns verify` measures the speaking rate from them (the
time from the first utterance to the last over the audio made in between), so a process's start and load
are not counted as speaking. The last stamp includes the process's own exit, a fraction of a second. The
file route (`synth`) and the other engines leave `last_stamps` None. Not yet seen on a GPU PC: whether
`qwen-tts` flushes each utterance as it is made (if it held them to the end, the stamps would not arrive one
after the other, and `ns verify` says so and times the whole call instead).

What the tool said (L8): after every run of `qwen-tts` or `qwen-codec` the engine logs ONE event,
`qwentts_said`: the outcome, the backend the tool chose (`[Load] Talker backend: CUDA0 ...`), the lines and
audio seconds it made, the wall seconds, and each `[Perf] Total <ms> ms (... audio <s> s, RTF <r>)` figure.
The parse never fails: a line it does not know is left out, and the event is logged with what was found.

Speed: neither tool has a speed control, so `time_stretch` (FFmpeg `atempo`, pitch preserved) runs on each
chunk. Voices: qwentts stores `speaker.spk` + `speaker.rvq` + `ref.txt`; the reference engine and the fake
store one `speaker.bin` (`REFERENCE_MAGIC` marks the PyTorch format). Each engine refuses the other's voices
(`NS-E311`). Error codes: `NS-E202` only while starting (binary or model missing, torch import), `NS-E310`
when a synthesis fails, `NS-E315` when a voice cannot be built, `NS-E311` for a stored voice that is unusable.
"""

from __future__ import annotations

import dataclasses
import gc
import hashlib
import importlib
import io
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import zlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.models import TtsCfg
from ns_common.paths import TTS_HF_SUBDIR, Paths
from ns_common.textstats import word_count
from ns_common.tts_timeout import tts_timeout_s
from ns_tts.audio import _STREAM_HEAD, read_wav, split_wav_stream, time_stretch, trim_silence, write_wav
from ns_tts.sapi import (
    CALIBRATION_TEXT,
    PROBE_SPEAK_TIMEOUT_S,
    PowerShellSapi,
    SapiBackend,
    SapiUnavailable,
    limit_peak,
    pick_voice,
    rate_for,
)
from ns_tts.voices import PRESETS, SAMPLE_TEXT, EmbeddingData, Mode, VoiceSpec

# Executables inside the qwentts zip (build-qwentts.yml packages exactly these three).
QWEN_TTS = "qwen-tts"
QWEN_CODEC = "qwen-codec"
TTS_SERVER = "tts-server"  # shipped, not used (see the module doc)
EXE_SUFFIX = ".exe"
FAKE_VOICE_TONES_CODE = "NS-W303"  # contracts/errors.json: the fake voice made tone bursts (CR-H7-1)

# Files of a stored qwentts voice (next to voice.json); `speaker.spk` is VoiceSpec.embedding_path.
VOICE_SPK = "speaker.spk"
VOICE_RVQ = "speaker.rvq"
VOICE_REF_TEXT = "ref.txt"

# GGUF names pinned in manifest.json (models.tts / tts_optional / cpu-dev); {kind} per talker.
TALKER_TEMPLATE = "qwen-talker-{size}-{kind}-Q8_0.gguf"
TALKER_KIND: dict[str, str] = {"preset": "customvoice", "clone": "base", "design": "voicedesign"}
TOKENIZER_FILE = "qwen-tokenizer-12hz-Q8_0.gguf"

# Reference-engine speaker.bin = REFERENCE_MAGIC + torch.save() of the list of VoiceClonePromptItem
# returned by Qwen3TTSModel.create_voice_clone_prompt (pickle based: only load files you made or trust).
REFERENCE_MAGIC = b"NSREF1\n"
REFERENCE_DIRS: dict[str, str] = {"preset": "CustomVoice", "design": "VoiceDesign", "clone": "Base"}

EMPTY_AUDIO = np.zeros(0, dtype=np.float32)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash when the service has none


class BatchInterrupted(NsError):
    """A batch process was cancelled, timed out or failed after writing some utterances.

    `done` maps the index (into the `texts` of `synth_batch`) of every utterance that was finished before the
    process stopped to its audio, so the server can keep those chunks and a retry can skip them. `partial` is
    the raw stdout the process had written; `cancelled` is true when `cancel()` stopped it.
    """

    def __init__(
        self, code: str, detail: str = "", *, partial: bytes = b"", cancelled: bool = False, **data: Any
    ) -> None:
        super().__init__(code, detail, **data)
        self.partial = partial
        self.cancelled = cancelled
        self.done: dict[int, np.ndarray] = {}


def design_model_message(missing: str) -> str:
    """NS-E314 text: the VoiceDesign talker is downloaded on demand, never during synthesis."""
    return f"the voice design model is not installed ({missing}): run Settings > Models > Get voice design"


class Engine(Protocol):
    """What the server needs from a TTS backend. Audio is float32 mono at `sr`."""

    name: str
    sr: int
    loaded: bool
    batch_timeout_floor_s: float  # the server raises it to the studio client's timeout during a batch

    def load(self) -> None:
        """Prepare the engine; raises NsError (NS-E202) when it cannot run."""
        ...

    def missing_model(self, op: Mode) -> str | None:
        """Name of the first model file/folder absent for `op` (preset, design or clone), else None."""
        ...

    def synth(
        self, text: str, voice: VoiceSpec, *, speed: float = 1.0, seed: int | None = None
    ) -> np.ndarray:
        """Render one chunk of text in `voice` (NS-E310 on failure, NS-E311 for an unusable stored voice)."""
        ...

    def synth_batch(
        self, texts: Sequence[str], voice: VoiceSpec, *, speed: float = 1.0, seed: int | None = None
    ) -> list[np.ndarray]:
        """Render several chunks in order with the same voice and seed."""
        ...

    def make_embedding(self, ref_wav: Path, ref_text: str, language: str = "English") -> EmbeddingData:
        """Encode a reference recording (and its transcript) as speaker data (NS-E315 on failure).

        Returns one blob (stored as `speaker.bin`) or {file name: bytes} for engines with several files.
        """
        ...

    def design_to_embedding(
        self, description: str, language: str = "English"
    ) -> tuple[EmbeddingData, np.ndarray]:
        """Render a voice from a text description, then return (speaker data of that render, the render)."""
        ...


# ---------------------------------------------------------------- fake


def _digest_bytes(seed: bytes, n: int = 256) -> bytes:
    out = b""
    i = 0
    while len(out) < n:
        out += hashlib.sha256(seed + bytes([i])).digest()
        i += 1
    return out[:n]


FAKE_SPEECH_MODES = ("tones", "sapi")

# A developer-only delay of the FAKE engine (head order H-7, box F-E12): design, clone and speech wait
# this many seconds, in slices, before they answer, so a Cancel can land while "Create it now" runs.
# Only `FakeEngine` reads it.
FAKE_DELAY_ENV = "NS_TTS_FAKE_DELAY_S"
FAKE_DELAY_CAP_S = 120.0
FAKE_DELAY_SLICE_S = 0.1


def fake_delay_from_env(log: NsLogger | None = None) -> float:
    """Seconds `NS_TTS_FAKE_DELAY_S` asks the fake engine to wait: 0 when unset (the default), at most 120.

    A value that is not a number, is NaN or is negative counts as 0 (said in the log); "inf" and anything
    above the cap is the cap.
    """
    raw = os.environ.get(FAKE_DELAY_ENV, "").strip()
    if not raw:
        return 0.0
    try:
        seconds = float(raw)
    except ValueError:
        seconds = math.nan
    if math.isnan(seconds) or seconds < 0:
        if log is not None:
            log.warn("fake_delay_ignored", f"{FAKE_DELAY_ENV}={raw!r} is not a number of seconds; no delay")
        return 0.0
    return min(seconds, FAKE_DELAY_CAP_S)


def fake_speech_for(cfg: TtsCfg, *, dev_mode: bool) -> str:
    """How the fake engine speaks in this service: `sapi` for an engine-test install, else `tones`.

    An engine test runs the real speech recognition, which cannot align tone bursts (D-26), so only there
    (`tts.engine: fake` in runtime.json, not dev mode, not the `NS_TTS_FAKE=1` of the test suite) does the
    fake try Windows speech. `NS_TTS_FAKE_SPEECH=tones|sapi` overrides the decision (a developer who wants
    to hear it).
    """
    forced = os.environ.get("NS_TTS_FAKE_SPEECH", "").strip().lower()
    if forced in FAKE_SPEECH_MODES:
        return forced
    if os.environ.get("NS_TTS_FAKE") == "1" or dev_mode:
        return "tones"
    return "sapi" if cfg.engine == "fake" else "tones"


class FakeEngine:
    """The engine of CI, `NS_TTS_FAKE=1`, a dev install and the engine test.

    Embeddings are deterministic 256-byte digests. What it says depends on `speech`:

    - `speech="tones"` (the default): a tone burst per word, no speech;
    - `speech="sapi"` (the engine test): the actual text, spoken by the Windows speech synthesizer
      (`ns_tts.sapi`), so the real speech recognition can align the captions. When this PC has no SAPI voice
      the engine speaks tone bursts instead and says so (`describe()`, /health, a warning in the tts log).

    `sapi` is a stand-in for the Windows speech synthesizer (tests); `tmp_dir` is its scratch folder.

    Developer delay: with `NS_TTS_FAKE_DELAY_S` set (seconds, default 0, at most 120; read here and
    nowhere else, once, when the engine is made) a design, a clone and every speech call first wait that
    long, in 0.1 s slices. The wait ends at once, with NS-E310 and nothing written, when `cancel()` is
    called (the studio's `POST /v1/audio/speech/cancel`) or when the service reports that the client
    went away (`watch_client`).
    """

    name = "fake"
    sr = 24000

    def __init__(
        self,
        *,
        speech: str = "tones",
        sapi: SapiBackend | None = None,
        log: NsLogger | None = None,
        tmp_dir: Path | None = None,
    ) -> None:
        if speech not in FAKE_SPEECH_MODES:
            raise ValueError(f"speech must be one of {FAKE_SPEECH_MODES}, got {speech!r}")
        self.loaded = False
        self.batch_timeout_floor_s = 0.0
        self.languages: list[str] = []  # the language of every synth call, so tests can see what reached it
        self.speech = speech
        self.log = log
        self._sapi: SapiBackend = sapi if sapi is not None else PowerShellSapi(tmp_dir=tmp_dir)
        self._probe_lock = threading.Lock()
        self._probed = speech == "tones"
        self.voice_mode = "tones"  # what the engine really does: "sapi" or "tones"
        self.voice_name: str | None = None
        self.voice_culture = ""  # e.g. "en-US": SAPI reads every line with this voice, whatever the language
        self.voice_wpm: float | None = None  # pace of the voice at SAPI Rate 0, measured at start-up
        self.voice_warning: str | None = None  # why a requested SAPI voice fell back to tone bursts
        self.delay_s = fake_delay_from_env(log)  # the developer delay, 0 unless NS_TTS_FAKE_DELAY_S is set
        self.waits = 0  # how many delays have started (tests)
        self._cancel_wait = threading.Event()
        self._wait_active = False
        self._client_gone: Callable[[], bool] | None = None

    def load(self) -> None:
        self._probe()
        self.loaded = True

    def missing_model(self, op: Mode) -> str | None:
        return None

    # ---- Windows speech ----
    def _probe(self) -> None:
        """Pick the SAPI voice and measure its pace, once; on any failure fall back to tone bursts, loudly."""
        if self._probed:
            return
        with self._probe_lock:
            if self._probed:
                return
            try:
                self._start_sapi()
            except SapiUnavailable as e:
                self.voice_mode, self.voice_name, self.voice_wpm = "tones", None, None
                self.voice_warning = f"{e}; the fake voice makes tone bursts, which recognition cannot align"
                if self.log is not None:
                    self.log.warn(
                        "fake_voice_tones_only",
                        self.voice_warning,
                        code=FAKE_VOICE_TONES_CODE,
                        requested="sapi",
                    )
            except (NsError, OSError) as e:  # the test sentence failed, or the scratch folder is unusable
                detail = e.detail if isinstance(e, NsError) else str(e)
                self.voice_mode, self.voice_name, self.voice_wpm = "tones", None, None
                self.voice_warning = (
                    f"Windows speech failed its start-up test ({detail}); "
                    "the fake voice makes tone bursts, which speech recognition cannot align"
                )
                if self.log is not None:
                    self.log.warn(
                        "fake_voice_tones_only",
                        self.voice_warning,
                        code=FAKE_VOICE_TONES_CODE,
                        requested="sapi",
                    )
            self._probed = True

    def _start_sapi(self) -> None:
        voices = self._sapi.voices()
        voice = pick_voice(voices)
        if voice is None:
            raise SapiUnavailable("no Windows speech (SAPI) voice is installed on this PC")
        heard = self._sapi.speak(
            voice.name, [CALIBRATION_TEXT], rate=0, sr=self.sr, timeout_s=PROBE_SPEAK_TIMEOUT_S
        )
        spoken = trim_silence(heard[0], self.sr) if heard else EMPTY_AUDIO
        seconds = len(spoken) / self.sr
        wpm = word_count(CALIBRATION_TEXT) / seconds * 60.0 if seconds > 0.5 else 0.0
        if not 40.0 <= wpm <= 900.0:
            raise SapiUnavailable(f"Windows voice {voice.name!r} spoke the test text at {wpm:.0f} words/min")
        self.voice_mode, self.voice_name, self.voice_wpm = "sapi", voice.name, round(wpm, 1)
        self.voice_culture = voice.culture
        if self.log is not None:
            self.log.info(
                "fake_voice_sapi", f"the fake voice speaks with Windows voice {voice.name!r}",
                voice=voice.name, culture=voice.culture, installed=len(voices), words_per_min=self.voice_wpm,
            )  # fmt: skip

    def describe(self) -> dict[str, Any]:
        """What `/health` adds for the fake: how it speaks, with which voice, and why it fell back."""
        self._probe()
        info: dict[str, Any] = {"voice_mode": self.voice_mode, "voice": self.voice_name}
        if self.delay_s > 0:
            info["fake_delay_s"] = self.delay_s
        if self.voice_wpm is not None:
            info["voice_wpm"] = self.voice_wpm
        if self.voice_warning:
            info["voice_fallback"] = True
            info["voice_warning"] = self.voice_warning
            info["voice_warning_code"] = FAKE_VOICE_TONES_CODE
        return info

    # ---- the developer delay ----
    @property
    def waiting(self) -> bool:
        """True while a request is waiting out the developer delay."""
        return self._wait_active

    def cancel(self) -> bool:
        """End the wait of the request that is in the developer delay; True when there was one to end."""
        if not self._wait_active:
            return False
        self._cancel_wait.set()
        return True

    @contextmanager
    def watch_client(self, gone: Callable[[], bool]) -> Iterator[None]:
        """While the request runs, `gone()` tells the delay whether the client has disconnected."""
        self._client_gone = gone
        try:
            yield
        finally:
            self._client_gone = None

    def _pause(self, op: str) -> None:
        """Wait out the developer delay before `op` ("design", "clone" or "speech"); no-op when it is 0.

        Cancellable: `cancel()` or a client that went away raises `BatchInterrupted` (NS-E310, an NsError)
        before anything is written, so no half-made voice or clip is left behind.
        """
        delay = self.delay_s
        if delay <= 0:
            return
        self._cancel_wait.clear()
        self._wait_active = True
        self.waits += 1
        if self.log is not None:
            msg = f"the fake engine waits {delay:g} s before the {op}"
            self.log.info("fake_delay", msg, op=op, delay_s=delay)
        deadline = time.monotonic() + delay
        try:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return
                if self._cancel_wait.wait(min(FAKE_DELAY_SLICE_S, left)):
                    why = "the request was cancelled"
                else:
                    gone = self._client_gone
                    try:
                        left_alone = gone is None or not gone()
                    except Exception:  # a probe that cannot tell is not a reason to stop
                        left_alone = True
                    if left_alone:
                        continue
                    why = "the client went away"
                if self.log is not None:
                    self.log.warn("fake_delay_cancelled", f"{why} during the wait before the {op}", op=op)
                raise BatchInterrupted(
                    "NS-E310", f"the wait of the fake engine was cancelled: {why} ({op})", cancelled=True
                )
        finally:
            self._wait_active = False

    def _speaks(self) -> bool:
        self._probe()
        return self.voice_mode == "sapi"

    def _speak(self, texts: Sequence[str], speed: float, language: str = "English") -> list[np.ndarray]:
        """The texts spoken by the SAPI voice (one process for all), at the pace `speed` asks for.

        A line the voice cannot speak (another script, only punctuation) comes back as no audio at all; that
        is logged as `fake_voice_unspoken` so a silent clip is explained before the captions stage says
        NS-E403.
        """
        assert self.voice_name is not None and self.voice_wpm is not None
        lines = [" ".join(t.split()) for t in texts]
        todo = [i for i, line in enumerate(lines) if line]
        out: list[np.ndarray] = [EMPTY_AUDIO] * len(texts)
        if not todo:
            return out
        heard = self._sapi.speak(
            self.voice_name,
            [lines[i] for i in todo],
            rate=rate_for(speed, self.voice_wpm),
            sr=self.sr,
            timeout_s=max(120.0 + 2.0 * len(todo), self.batch_timeout_floor_s),
        )
        if len(heard) != len(todo):
            raise NsError("NS-E310", f"Windows speech returned {len(heard)} clips for {len(todo)} lines")
        for i, x in zip(todo, heard, strict=True):
            out[i] = limit_peak(x)
            if x.size == 0 or float(np.max(np.abs(x))) < 1e-4:
                self._warn_unspoken(lines[i], language)
        return out

    def _warn_unspoken(self, line: str, language: str) -> None:
        wrong_language = not language.lower().startswith("en") and self.voice_culture.lower().startswith("en")
        msg = (
            f"Windows voice {self.voice_name!r} made no sound for the line {line[:60]!r}"
            + (f"; it speaks English and the request is {language!r}" if wrong_language else "")
            + "; the clip is silent, so the captions stage may fail with NS-E403"
        )
        if self.log is not None:
            self.log.warn(
                "fake_voice_unspoken", msg, voice=self.voice_name, language=language, chars=len(line)
            )

    def _freq(self, voice: VoiceSpec) -> float:
        if voice.mode == "preset":
            who = voice.speaker or voice.name
            if who in PRESETS:
                return 200.0 + 40.0 * PRESETS.index(who)
            return 200.0 + zlib.crc32(who.encode()) % 400
        if not voice.embedding_path or not Path(voice.embedding_path).is_file():
            raise NsError("NS-E311", f"voice {voice.name!r} has no stored embedding")
        return 200.0 + Path(voice.embedding_path).read_bytes()[0] * 2.0

    def _tone(self, text: str, freq: float, speed: float) -> np.ndarray:
        words = max(1, word_count(text))
        seconds = max(0.4, words / (155.0 * max(speed, 0.1)) * 60.0)
        n = int(seconds * self.sr)
        idx = np.arange(n)
        gate = np.mod(idx, n / words) < int(0.08 * self.sr)
        tone = 0.3 * np.sin(2.0 * math.pi * freq * idx / self.sr)
        return np.where(gate, tone, 0.0).astype(np.float32)

    def _synth_spoken(self, texts: Sequence[str], voice: VoiceSpec, speed: float) -> list[np.ndarray]:
        self._freq(voice)  # the same NS-E311 for a voice without stored data as the tone engine gives
        self.languages.extend([voice.language] * len(texts))
        return self._speak(texts, speed, voice.language)

    def synth(
        self, text: str, voice: VoiceSpec, *, speed: float = 1.0, seed: int | None = None
    ) -> np.ndarray:
        # no delay here: the sample a clone stores is part of the clone request, which waited once already
        if self._speaks():
            return self._synth_spoken([text], voice, speed)[0]
        self.languages.append(voice.language)
        return self._tone(text, self._freq(voice), speed)

    def synth_batch(
        self, texts: Sequence[str], voice: VoiceSpec, *, speed: float = 1.0, seed: int | None = None
    ) -> list[np.ndarray]:
        self._pause("speech")
        if self._speaks():
            return self._synth_spoken(texts, voice, speed)
        return [self.synth(t, voice, speed=speed, seed=seed) for t in texts]

    def make_embedding(self, ref_wav: Path, ref_text: str, language: str = "English") -> EmbeddingData:
        self._pause("clone")
        audio = ref_wav.read_bytes() if ref_wav.is_file() else ref_wav.name.encode()
        return _digest_bytes(b"clone:" + ref_text.encode() + audio)

    def design_to_embedding(
        self, description: str, language: str = "English"
    ) -> tuple[EmbeddingData, np.ndarray]:
        self._pause("design")
        emb = _digest_bytes(b"design:" + description.encode())
        if self._speaks():  # the preview of a designed voice speaks the sample sentence too
            return emb, self._speak([SAMPLE_TEXT], 1.0)[0]
        return emb, self._tone(SAMPLE_TEXT, 200.0 + emb[0] * 2.0, 1.0)


# ---------------------------------------------------------------- qwentts.cpp

# What qwentts.cpp prints on stderr (seen on the studio PC, qwentts.cpp 5916bf7):
#   [Load] Talker backend: CUDA0 (CPU threads: 6)
#   [Perf] Total 1222.5 ms (50 frames, 13.68 ms/frame AR, audio 4.00 s, RTF 0.306)
_LOAD_BACKEND = re.compile(r"\[Load\][^\n]*?Talker backend:\s*([^\s(,]+)")
_PERF_TOTAL = re.compile(r"\[Perf\]\s*Total\s+(\d+(?:\.\d+)?)\s*ms([^\n]*)")
_PERF_AUDIO = re.compile(r"\baudio\s+(\d+(?:\.\d+)?)\s*s\b")
_PERF_RTF = re.compile(r"\bRTF\s+(\d+(?:\.\d+)?)")
_STREAM_HEAD_LEN = 44  # RIFF(12) + "fmt " chunk(24) + "data" chunk head(8): `ns_tts.audio._STREAM_HEAD`
_JOIN_S = 10.0  # how long the readers of a stopped process are waited for (a held pipe never hangs a batch)


def _wav_seconds(path: Path) -> float | None:
    """Seconds of the WAV a tool wrote, for the log; None when there is none or it cannot be read."""
    try:
        x, sr = read_wav(path)
    except (OSError, ValueError):
        return None
    return round(len(x) / sr, 3) if sr > 0 else None


def tool_words(stderr: bytes | str | None) -> dict[str, Any]:
    """What a qwentts tool said about itself: the backend it chose and its per-line `[Perf] Total` figures.

    `{"backend": "CUDA0" | None, "perf_total_ms": [...], "perf_audio_s": [...], "perf_rtf": [...]}`: one entry
    per `[Perf] Total` line, in order; a line without an audio or RTF figure gives None there. Never raises: a
    line it does not know is left out (the tool's words may change between versions).
    """
    out: dict[str, Any] = {"backend": None, "perf_total_ms": [], "perf_audio_s": [], "perf_rtf": []}
    try:
        text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else str(stderr or "")
        found = _LOAD_BACKEND.search(text)
        out["backend"] = found.group(1) if found else None
        for m in _PERF_TOTAL.finditer(text):
            rest = m.group(2)
            audio, rtf = _PERF_AUDIO.search(rest), _PERF_RTF.search(rest)
            out["perf_total_ms"].append(float(m.group(1)))
            out["perf_audio_s"].append(float(audio.group(1)) if audio else None)
            out["perf_rtf"].append(float(rtf.group(1)) if rtf else None)
    except Exception:  # tolerant by design (L8): the words are a record, never a reason to fail a synthesis
        pass
    return out


class StreamClock:
    """Stamps the utterances of a `--stream-by-line` stdout while it is read (card verify-limits L2).

    `feed` gets each piece of stdout with the moment it was read; `end` the moment the stream ended. An
    utterance is complete when the next one's header arrives, the last one when the stream ended: whether the
    tool writes an utterance at once or in pieces, the stamp is when all of it was there. The bytes are kept
    (`data`): they are the process's stdout.
    """

    def __init__(self) -> None:
        self.data = bytearray()
        self.heads: list[int] = []
        self.head_at: list[float] = []
        self.ended_at: float | None = None

    def feed(self, piece: bytes, now: float) -> None:
        start = max(0, len(self.data) - (_STREAM_HEAD_LEN - 1))  # a header may straddle two pieces
        if self.heads:
            start = max(start, self.heads[-1] + 1)
        self.data += piece
        for m in _STREAM_HEAD.finditer(self.data, start):
            self.heads.append(m.start())
            self.head_at.append(now)

    def end(self, now: float) -> None:
        self.ended_at = now

    def arrivals(self, *, ended_whole: bool) -> list[float]:
        """When each complete utterance was there. `ended_whole`: the process exited by itself with 0, so its
        last utterance is complete; after a kill or a failure the last one is left out (it may be cut)."""
        out = list(self.head_at[1:])
        if ended_whole and self.heads and self.ended_at is not None:
            out.append(self.ended_at)
        return out

    def audio_seconds(self) -> float:
        """Seconds of audio in the stream, from each header's format and its byte span (for the log only)."""
        total = 0.0
        for k, start in enumerate(self.heads):
            end = self.heads[k + 1] if k + 1 < len(self.heads) else len(self.data)
            try:
                ch, rate = struct.unpack_from("<HI", self.data, start + 22)
                bits = struct.unpack_from("<H", self.data, start + 34)[0]
            except struct.error:
                continue
            frame = ch * (bits // 8)
            if frame > 0 and rate > 0:
                total += max(0, end - start - _STREAM_HEAD_LEN) / frame / rate
        return round(total, 3)


class QwenttsEngine:
    """Runs `qwen-tts.exe` once per batch of chunks and `qwen-codec.exe` once per new voice (module doc).

    `bin_dir` is `<home>\\bin\\qwentts`. `launcher` maps a tool name (`qwen-tts`, `qwen-codec`) to the argv
    prefix that starts it; tests use it to run a Python shim instead of the real executables.
    """

    name = "qwentts"

    def __init__(
        self,
        bin_dir: Path,
        models_dir: Path,
        *,
        size: str = "1.7b",
        device: str | None = None,
        timeout_s: float = 900.0,
        log: NsLogger | None = None,
        launcher: Mapping[str, Sequence[str]] | None = None,
        tmp_dir: Path | None = None,
    ) -> None:
        self.bin_dir = bin_dir
        self.models_dir = models_dir
        # Scratch space for the files handed to the tools: `<home>\cache\tts-tmp` (`Paths.cache_tts_tmp`),
        # never `%TEMP%`. The tools read argv in the ANSI code page, so a Windows user name with Arabic or
        # other non-ASCII letters would break every temporary path (NS-W301).
        self.tmp_root = Path(tmp_dir) if tmp_dir is not None else models_dir / ".tts-tmp"
        self.size = size
        self.device = device  # None = the tools pick the best device; "CPU" forces it (cpu-dev profile)
        self.timeout_s = timeout_s
        self.log = log
        self.launcher = dict(launcher or {})
        self.sr = 24000
        self.loaded = False
        # the server raises this to the studio client's own timeout while it holds a batch request, so a
        # process is never killed before the client gives up (`ns_common.tts_timeout`)
        self.batch_timeout_floor_s = 0.0
        self._proc_lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._cancelled = False
        # (perf_counter moment, audio seconds) of each utterance of the last `synth_batch` that ran as one
        # stream; None after any other call or a failure (module doc, "Arrival stamps")
        self.last_stamps: list[tuple[float, float]] | None = None

    def cancel(self) -> bool:
        """Kill the batch process that is running, if any (the studio's client timed out and asked for it).

        Returns True when a process was stopped. `synth_batch` then raises `BatchInterrupted(cancelled=True)`
        carrying the utterances finished so far.
        """
        with self._proc_lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return False
            self._cancelled = True
        proc.kill()
        return True

    # ---- scratch files (ASCII paths only: the tools do not convert argv to UTF-8) ----
    @contextmanager
    def _scratch(self) -> Iterator[Path]:
        """A fresh folder under `tmp_root` for one call; removed afterwards, also when the call fails.

        Folders left by a crashed process (older than a day) are removed on the way in.
        """
        root = self.tmp_root
        root.mkdir(parents=True, exist_ok=True)
        if self.log is not None and not str(root).isascii():
            self.log.warn("qwentts_non_ascii_path", path=str(root))
        cutoff = time.time() - 86400
        for old in root.iterdir():
            try:
                if old.is_dir() and old.stat().st_mtime < cutoff:
                    shutil.rmtree(old, ignore_errors=True)
            except OSError:
                continue
        folder = Path(tempfile.mkdtemp(prefix="c-", dir=root))
        try:
            yield folder
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def _ascii_refs(self, args: dict[str, Any], stack: ExitStack) -> dict[str, Any]:
        """`args` with every file (a `Path` value) whose path is not plain ASCII copied to scratch first.

        The voice files normally sit under the install folder (ASCII). A voice stored anywhere else would
        reach the tool as garbled bytes, so it is copied. The scratch folder is opened on `stack` and lives
        until the caller's `with` ends.
        """
        if all(str(p).isascii() for p in args.values() if isinstance(p, Path)):
            return args
        scratch = stack.enter_context(self._scratch())
        out: dict[str, Any] = {}
        for key, path in args.items():
            if not isinstance(path, Path) or str(path).isascii():
                out[key] = path
                continue
            safe = scratch / f"{key}{path.suffix if path.suffix.isascii() else ''}"
            shutil.copyfile(path, safe)
            out[key] = safe
        return out

    # ---- files ----
    def exe(self, tool: str) -> Path:
        """Path of one tool of the qwentts zip, e.g. `exe("qwen-tts")`."""
        return self.bin_dir / f"{tool}{EXE_SUFFIX}"

    def _argv(self, tool: str) -> list[str]:
        return list(self.launcher.get(tool) or [str(self.exe(tool))])

    def talker_path(self, op: Mode) -> Path:
        """GGUF of the talker used for `op` (design -> VoiceDesign, clone -> Base, preset -> CustomVoice)."""
        return self.models_dir / TALKER_TEMPLATE.format(size=self.size, kind=TALKER_KIND[op])

    @property
    def tokenizer_path(self) -> Path:
        """The shared codec GGUF (`--codec` of qwen-tts, `--model` of qwen-codec)."""
        return self.models_dir / TOKENIZER_FILE

    def missing_model(self, op: Mode) -> str | None:
        needed = [self.talker_path(op)]
        if op == "design":
            needed.append(self.talker_path("clone"))  # the render is re-encoded through Base
        needed.append(self.tokenizer_path)
        for p in needed:
            if not p.is_file():
                return p.name
        return None

    def load(self) -> None:
        """Check `qwen-tts.exe` and the preset talker exist (the process itself starts per call)."""
        if QWEN_TTS not in self.launcher and not self.exe(QWEN_TTS).is_file():
            raise NsError("NS-E202", f"qwentts binary not found: {self.exe(QWEN_TTS)}")
        missing = self.missing_model("preset")
        if missing:
            raise NsError("NS-E202", f"qwentts model file missing: {missing}", missing_model=missing)
        self.loaded = True

    # ---- command lines (the contract in the module doc) ----
    def _device_args(self) -> list[str]:
        return ["--device", self.device] if self.device else []

    def tts_command(
        self,
        op: Mode,
        *,
        out: Path,
        speaker: str | None = None,
        instruct: str | None = None,
        ref_spk: Path | None = None,
        ref_rvq: Path | None = None,
        ref_text: Path | None = None,
        language: str | None = None,
        seed: int | None = None,
    ) -> list[str]:
        """argv of `qwen-tts.exe` for `op`; the text goes to stdin, never into this list."""
        cmd = [*self._argv(QWEN_TTS), "--model", str(self.talker_path(op))]
        cmd += ["--codec", str(self.tokenizer_path)]
        cmd += self._device_args()
        if speaker:
            cmd += ["--speaker", speaker.lower()]
        if instruct:
            cmd += ["--instruct", instruct]
        if ref_spk:
            cmd += ["--ref-spk", str(ref_spk)]
        if ref_rvq:
            cmd += ["--ref-rvq", str(ref_rvq)]
        if ref_text:
            cmd += ["--ref-text", str(ref_text)]
        if language:
            cmd += ["--lang", language]
        if seed is not None:
            cmd += ["--seed", str(seed)]
        return [*cmd, "-o", str(out)]

    def codec_command(self, wav: Path) -> list[str]:
        """argv of `qwen-codec.exe` that writes `<wav stem>.rvq` and `<wav stem>.spk` next to `wav`."""
        cmd = [*self._argv(QWEN_CODEC), "--model", str(self.tokenizer_path)]
        cmd += ["--talker", str(self.talker_path("clone"))]
        return [*cmd, *self._device_args(), "-i", str(wav)]

    def _run(
        self,
        cmd: list[str],
        *,
        tool: str,
        code: str,
        stdin_text: str | None = None,
        timeout_s: float | None = None,
        wav: Path | None = None,
    ) -> bytes:
        """Run `tool` to completion and return its stdout; every failure becomes `NsError(code)` (NS-E310
        synth, NS-E315 voice). `timeout_s` overrides the engine's per-process limit (batches take longer).
        `wav`: the file the tool writes its audio to, measured for the `qwentts_said` event (L8)."""
        if self.log:
            self.log.debug("qwentts_run", cmd=" ".join(cmd), stdin_chars=len(stdin_text or ""))
        data = stdin_text.encode("utf-8") if stdin_text is not None else b""
        limit = timeout_s if timeout_s is not None else self.timeout_s
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd,
                input=data,
                capture_output=True,
                timeout=limit,
                check=False,
                creationflags=_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as e:
            self._said(tool, "timed out", e.stderr, time.perf_counter() - t0, lines=0)
            raise NsError(code, f"{tool} timed out after {limit:.0f}s", cmd=cmd) from e
        except OSError as e:
            self._said(tool, "did not start", None, time.perf_counter() - t0, lines=0)
            raise NsError(code, f"cannot start {tool}: {e}", cmd=cmd) from e
        wall = time.perf_counter() - t0
        if proc.returncode != 0:
            self._said(tool, f"exit {proc.returncode}", proc.stderr, wall, lines=0)
            raw = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace")
            tail = "\n".join(raw.strip().splitlines()[-8:])
            raise NsError(code, f"{tool} exited {proc.returncode}: {tail}", cmd=cmd)
        audio = _wav_seconds(wav) if wav is not None else None
        lines = None if wav is None else (1 if audio else 0)  # qwen-codec makes no lines of speech
        self._said(tool, "ok", proc.stderr, wall, lines=lines, audio_s=audio)
        return proc.stdout or b""

    def _said(
        self, tool: str, outcome: str, stderr: bytes | str | None, wall_s: float, *,
        lines: int | None, audio_s: float | None = None,
    ) -> None:
        """The ONE `qwentts_said` event of a run of `tool` (L8). Never raises: it is a record."""
        if self.log is None:
            return
        try:
            words = tool_words(stderr)
            self.log.info(
                "qwentts_said",
                f"{tool}: {outcome}, backend {words['backend'] or 'not said'}",
                tool=tool, outcome=outcome, backend=words["backend"], lines=lines, audio_s=audio_s,
                wall_s=round(wall_s, 3), perf_total_ms=words["perf_total_ms"],
                perf_audio_s=words["perf_audio_s"], perf_rtf=words["perf_rtf"],
            )  # fmt: skip
        except Exception:  # a log that cannot be written never fails a synthesis
            pass

    def _run_stream(
        self, cmd: list[str], *, tool: str, code: str, stdin_text: str, timeout_s: float
    ) -> tuple[bytes, list[float]]:
        """Like `_run` for a `--stream-by-line` batch, but `cancel()` can stop it and a stop keeps its output.

        Returns (stdout, the moment each utterance was complete: `StreamClock.arrivals`). stdout and stderr
        are read by two threads while the process runs (so the stamps are taken as the bytes come, and
        neither pipe can fill up and stall the tool), stdin is written by a third. A timeout, a cancel, a
        non-zero exit or a stdout that could not be read to its end raises `BatchInterrupted` (an `NsError`
        with `code`) whose `partial` holds what the process had already written to stdout. Every way out logs
        one `qwentts_said` event.
        """
        if self.log:
            self.log.debug("qwentts_run", cmd=" ".join(cmd), stdin_chars=len(stdin_text))
        t0 = time.perf_counter()
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=_NO_WINDOW,
            )  # fmt: skip
        except OSError as e:
            self._said(tool, "did not start", None, time.perf_counter() - t0, lines=0)
            raise NsError(code, f"cannot start {tool}: {e}", cmd=cmd) from e
        with self._proc_lock:
            self._proc = proc
            self._cancelled = False
        clock = StreamClock()
        err = bytearray()
        seen = threading.Lock()  # guards `clock`, `err` and `whole` between the readers and this thread
        whole: list[bool] = []  # [True] once stdout was read to a clean end of file

        def read_out() -> None:
            stream = proc.stdout
            try:
                while stream is not None:
                    piece = os.read(stream.fileno(), 65536)
                    if not piece:
                        with seen:
                            whole.append(True)
                        break
                    now = time.perf_counter()
                    with seen:
                        clock.feed(piece, now)
            except (OSError, ValueError):  # the pipe broke: `whole` stays empty and the batch fails
                pass
            finally:
                with seen:
                    clock.end(time.perf_counter())

        def read_err() -> None:
            stream = proc.stderr
            try:
                while stream is not None:
                    piece = os.read(stream.fileno(), 65536)
                    if not piece:
                        break
                    with seen:
                        err.extend(piece)
            except (OSError, ValueError):
                pass

        def write_in() -> None:
            stream = proc.stdin
            if stream is None:
                return
            try:
                stream.write(stdin_text.encode("utf-8"))
            except (OSError, ValueError):  # the tool ended before it read it all: its exit code says why
                pass
            finally:
                with suppress(OSError, ValueError):
                    stream.close()

        threads = [threading.Thread(target=f, daemon=True) for f in (read_out, read_err, write_in)]
        for t in threads:
            t.start()
        timed_out = False
        try:
            try:
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                with suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=_JOIN_S)
            with self._proc_lock:
                cancelled = self._cancelled
        finally:
            with self._proc_lock:
                self._proc = None
            if proc.poll() is None:  # only on an unexpected error in this thread: never leave a tool running
                proc.kill()
        for t in threads:
            t.join(_JOIN_S)
        # a long-lived service frees each batch's handles now; never under a reader that is still in os.read
        # (a held pipe): its file number could be given to another file and read from
        for pipe, reader in ((proc.stdout, threads[0]), (proc.stderr, threads[1])):
            if pipe is not None and not reader.is_alive():
                with suppress(OSError, ValueError):
                    pipe.close()
        wall = time.perf_counter() - t0
        with seen:
            out, said, read_whole = bytes(clock.data), bytes(err), bool(whole)
            lines, audio = len(clock.heads), clock.audio_seconds()
            arrivals = clock.arrivals(ended_whole=True)
        if timed_out:
            self._said(tool, "timed out", said, wall, lines=lines, audio_s=audio)
            raise BatchInterrupted(code, f"{tool} timed out after {timeout_s:.0f}s", partial=out, cmd=cmd)
        if cancelled:
            self._said(tool, "cancelled", said, wall, lines=lines, audio_s=audio)
            raise BatchInterrupted(code, f"{tool} was cancelled", partial=out, cancelled=True, cmd=cmd)
        if proc.returncode != 0:
            self._said(tool, f"exit {proc.returncode}", said, wall, lines=lines, audio_s=audio)
            raw = (said or out or b"").decode("utf-8", errors="replace")
            tail = "\n".join(raw.strip().splitlines()[-8:])
            raise BatchInterrupted(code, f"{tool} exited {proc.returncode}: {tail}", partial=out, cmd=cmd)
        if not read_whole:
            self._said(tool, "output not read to its end", said, wall, lines=lines, audio_s=audio)
            raise BatchInterrupted(code, f"the output of {tool} could not be read to its end", partial=out,
                                   cmd=cmd)  # fmt: skip
        self._said(tool, "ok", said, wall, lines=lines, audio_s=audio)
        return out, arrivals

    @staticmethod
    def _finished(partial: bytes, todo: list[int], speed: float) -> dict[int, np.ndarray]:
        """The utterances of a stopped stream that are certainly complete, by index into the batch's texts.

        The last segment may have been cut short by the kill, so it is dropped; a damaged stream keeps none.
        """
        try:
            segments = split_wav_stream(partial, drop_last=True)
        except ValueError:
            return {}
        return {
            i: time_stretch(x, sr, speed)
            for i, (x, sr) in zip(todo, segments, strict=False)
            if x.size > 0
        }

    def _read(self, path: Path, code: str) -> np.ndarray:
        if not path.is_file():
            raise NsError(code, f"qwentts wrote no output file: {path.name}")
        try:
            x, sr = read_wav(path)
        except ValueError as e:
            raise NsError(code, f"qwentts output is not a readable WAV: {e}") from e
        if x.size == 0:
            raise NsError(code, "qwentts wrote an empty WAV")
        self.sr = sr
        return x

    def _voice_args(self, voice: VoiceSpec) -> tuple[Mode, dict[str, Any]]:
        if voice.mode == "preset":
            return "preset", {"speaker": voice.speaker or voice.name}
        path = Path(voice.embedding_path) if voice.embedding_path else None
        if path is None or not path.is_file():
            raise NsError("NS-E311", f"voice {voice.name!r} has no stored speaker data; re-create it")
        if path.suffix != ".spk":
            raise NsError(
                "NS-E311", f"voice {voice.name!r} was created by the reference engine; re-create it"
            )
        rvq, ref = path.with_name(VOICE_RVQ), path.with_name(VOICE_REF_TEXT)
        for f in (rvq, ref):
            if not f.is_file():
                raise NsError("NS-E311", f"voice {voice.name!r} is missing {f.name}; re-create it")
        return "clone", {"ref_spk": path, "ref_rvq": rvq, "ref_text": ref}

    # ---- engine API ----
    def synth(
        self, text: str, voice: VoiceSpec, *, speed: float = 1.0, seed: int | None = None
    ) -> np.ndarray:
        op, args = self._voice_args(voice)
        with ExitStack() as stack:
            scratch = stack.enter_context(self._scratch())
            args = self._ascii_refs(args, stack)
            out = scratch / "out.wav"
            cmd = self.tts_command(op, out=out, seed=seed, language=voice.language, **args)
            self._run(cmd, tool=QWEN_TTS, code="NS-E310", stdin_text=text.strip(), wav=out)
            return time_stretch(self._read(out, "NS-E310"), self.sr, speed)

    def synth_batch(
        self, texts: Sequence[str], voice: VoiceSpec, *, speed: float = 1.0, seed: int | None = None
    ) -> list[np.ndarray]:
        """All of `texts` in ONE `qwen-tts` process (`--stream-by-line -o -`), one utterance per line.

        Loading the talker and the codec and starting CUDA costs seconds per process, and a 14-minute master
        has about 200 sentence chunks. The binary reads one chunk per stdin line, keeps the model and the
        speaker resident, and writes the utterances to stdout one after another, each opened by its own WAV
        header; `split_wav_stream` cuts them apart. NS-E310 when the number of utterances differs from the
        number of lines sent.

        On success `last_stamps` holds (the moment it was complete, its audio seconds before any speed change)
        per utterance in order; it is None after a single chunk (the file route) and after any failure.
        """
        self.last_stamps = None
        lines = [" ".join(t.split()) for t in texts]
        todo = [i for i, line in enumerate(lines) if line]
        if len(todo) <= 1:
            return [self.synth(t, voice, speed=speed, seed=seed) if lines[i] else EMPTY_AUDIO
                    for i, t in enumerate(texts)]
        op, args = self._voice_args(voice)
        try:
            with ExitStack() as stack:
                args = self._ascii_refs(args, stack)
                cmd = self.tts_command(op, out=Path("-"), seed=seed, language=voice.language, **args)
                cmd.insert(-2, "--stream-by-line")  # before `-o -`
                stdout, arrivals = self._run_stream(
                    cmd,
                    tool=QWEN_TTS,
                    code="NS-E310",
                    stdin_text="\n".join(lines[i] for i in todo) + "\n",
                    timeout_s=max(tts_timeout_s(len(todo)), self.batch_timeout_floor_s),
                )
        except BatchInterrupted as e:
            e.done = self._finished(e.partial, todo, speed)
            raise
        try:
            segments = split_wav_stream(stdout)
        except ValueError as e:
            raise NsError("NS-E310", f"qwentts output is not a readable WAV stream: {e}") from e
        if len(segments) != len(todo):
            raise NsError(
                "NS-E310", f"qwentts returned {len(segments)} utterances for {len(todo)} lines of text"
            )
        out: list[np.ndarray] = [EMPTY_AUDIO] * len(texts)
        for i, (x, sr) in zip(todo, segments, strict=True):
            if x.size == 0:
                raise NsError("NS-E310", f"qwentts wrote an empty utterance for line {i + 1}")
            self.sr = sr
            out[i] = time_stretch(x, sr, speed)
        if len(arrivals) == len(segments):  # (both cut on the same header; never a guessed pairing)
            self.last_stamps = [
                (at, round(len(x) / sr, 3)) for at, (x, sr) in zip(arrivals, segments, strict=True)
            ]
        elif self.log is not None:
            self.log.warn("qwentts_stamps_unpaired", arrivals=len(arrivals), utterances=len(segments))
        return out

    def _encode(self, wav: Path) -> dict[str, bytes]:
        """Run qwen-codec on `wav` and return {speaker.spk, speaker.rvq} (the names are `wav`'s stem)."""
        self._run(self.codec_command(wav), tool=QWEN_CODEC, code="NS-E315")
        files: dict[str, bytes] = {}
        for suffix, name in ((".spk", VOICE_SPK), (".rvq", VOICE_RVQ)):
            produced = wav.with_suffix(suffix)
            if not produced.is_file() or produced.stat().st_size == 0:
                raise NsError("NS-E315", f"qwen-codec did not write {produced.name}")
            files[name] = produced.read_bytes()
        return files

    def make_embedding(self, ref_wav: Path, ref_text: str, language: str = "English") -> EmbeddingData:
        """qwen-codec on the reference WAV: `speaker.spk` + `speaker.rvq`, plus `ref.txt` (the transcript)."""
        words = ref_text.strip()
        if not words:
            raise NsError("NS-E315", "the transcript of the reference recording is empty")
        missing = self.missing_model("clone")
        if missing:
            raise NsError("NS-E315", f"qwentts model file missing: {missing}", missing_model=missing)
        try:
            x, sr = read_wav(ref_wav)
        except (OSError, ValueError) as e:
            raise NsError("NS-E315", f"the reference recording is not a readable WAV: {e}") from e
        with self._scratch() as td:
            wav = td / "speaker.wav"
            write_wav(wav, x, sr)  # plain 16-bit mono PCM, the form qwen-codec is documented to read
            files = self._encode(wav)
        files[VOICE_REF_TEXT] = (words + "\n").encode("utf-8")
        return files

    def design_to_embedding(
        self, description: str, language: str = "English"
    ) -> tuple[EmbeddingData, np.ndarray]:
        missing = self.missing_model("design")
        if missing:
            raise NsError("NS-E314", design_model_message(missing), missing_model=missing)
        with self._scratch() as td:
            render = td / "render.wav"
            cmd = self.tts_command("design", out=render, instruct=description, language=language)
            self._run(cmd, tool=QWEN_TTS, code="NS-E315", stdin_text=SAMPLE_TEXT, wav=render)
            sample = self._read(render, "NS-E315")
            files = self._encode(render)
        files[VOICE_REF_TEXT] = (SAMPLE_TEXT + "\n").encode("utf-8")
        return files, sample


# ---------------------------------------------------------------- reference (PyTorch qwen-tts)


class ReferenceEngine:
    """Official `qwen-tts` package on PyTorch: presets, VoiceDesign and Base cloning.

    One `Qwen3TTSModel` per voice mode is loaded lazily (CustomVoice, VoiceDesign, Base). On the CPU
    profile only one stays resident (the previous is unloaded); on CUDA the preset and clone models stay
    and the VoiceDesign model is unloaded right after a design call to keep VRAM free. bf16 on CUDA,
    float32 on CPU, `attn_implementation="sdpa"`. `models_dir` is `<models\\tts>\\hf` (`TTS_HF_SUBDIR`);
    the service must run in the `tts` venv, where torch lives (`ns_supervisor.services._tts`).
    """

    name = "reference"

    def __init__(self, models_dir: Path, *, size: str = "1.7B", device: str = "cuda") -> None:
        self.models_dir = models_dir
        self.size = size
        self.device = device
        self.sr = 24000
        self.loaded = False
        self.batch_timeout_floor_s = 0.0  # unused: one call per chunk
        self._models: dict[str, Any] = {}
        self._prompts: dict[tuple[str, float], Any] = {}

    def model_dir(self, op: Mode) -> Path:
        """Folder of the safetensors model for `op`."""
        return self.models_dir / f"Qwen3-TTS-12Hz-{self.size}-{REFERENCE_DIRS[op]}"

    def missing_model(self, op: Mode) -> str | None:
        needed = [self.model_dir(op)]
        if op == "design":
            needed.append(self.model_dir("clone"))
        for d in needed:
            if not d.is_dir():
                return d.name
        return None

    # ---- model management ----
    def load(self) -> None:
        """Import torch and qwen_tts and load the preset model; raises NsError (NS-E202) if unavailable."""
        self._model("preset", code="NS-E202")
        self.loaded = True

    def _model(self, op: Mode, *, code: str = "NS-E310") -> Any:
        if op in self._models:
            return self._models[op]
        folder = self.model_dir(op)
        if not folder.is_dir():
            if op == "design":
                raise NsError("NS-E314", design_model_message(folder.name), missing_model=folder.name)
            raise NsError(code, f"reference TTS model missing: {folder.name}", missing_model=folder.name)
        if self.device == "cpu":
            self._unload_all()
        try:
            torch = importlib.import_module("torch")
            qwen_tts = importlib.import_module("qwen_tts")
        except ImportError as e:
            raise NsError(code, f"reference engine needs torch and qwen-tts: {e}") from e
        cuda = self.device != "cpu"
        try:
            model = qwen_tts.Qwen3TTSModel.from_pretrained(
                str(self.model_dir(op)),
                device_map="cuda:0" if cuda else "cpu",
                dtype=torch.bfloat16 if cuda else torch.float32,
                attn_implementation="sdpa",
            )
        except Exception as e:  # wrap library failures (OOM, bad weights) into a coded error
            raise NsError(code, f"cannot load {self.model_dir(op).name}: {e}") from e
        self._models[op] = model
        return model

    def _unload(self, op: str) -> None:
        if self._models.pop(op, None) is not None:
            gc.collect()
            if self.device != "cpu":
                try:
                    importlib.import_module("torch").cuda.empty_cache()
                except ImportError:
                    pass

    def _unload_all(self) -> None:
        for op in list(self._models):
            self._unload(op)
        self._prompts.clear()

    def _seed(self, seed: int | None) -> None:
        if seed is not None:
            importlib.import_module("torch").manual_seed(seed)  # also seeds CUDA

    # ---- embeddings ----
    def _dump_prompt(self, items: Any) -> bytes:
        torch = importlib.import_module("torch")
        buf = io.BytesIO()
        buf.write(REFERENCE_MAGIC)
        torch.save(items, buf)
        return buf.getvalue()

    def _load_prompt(self, voice: VoiceSpec) -> Any:
        if not voice.embedding_path or not Path(voice.embedding_path).is_file():
            raise NsError("NS-E311", f"voice {voice.name!r} has no stored embedding")
        path = Path(voice.embedding_path)
        key = (str(path), path.stat().st_mtime)
        if key not in self._prompts:
            raw = path.read_bytes()
            if not raw.startswith(REFERENCE_MAGIC):
                raise NsError(
                    "NS-E311", f"voice {voice.name!r} was not created by the reference engine; re-create it"
                )
            torch = importlib.import_module("torch")
            # Pickle based (VoiceClonePromptItem dataclasses): library files are local and trusted.
            body = io.BytesIO(raw[len(REFERENCE_MAGIC) :])
            items = torch.load(body, map_location="cpu", weights_only=False)
            self._prompts[key] = self._to_device(items, torch)
        return self._prompts[key]

    def _to_device(self, items: Any, torch: Any) -> Any:
        """Move the tensor fields of each prompt item to the GPU (items are dataclasses)."""
        if self.device == "cpu" or not isinstance(items, list):
            return items
        moved: list[Any] = []
        for item in items:
            current: Any = item
            if dataclasses.is_dataclass(current) and not isinstance(current, type):
                changes = {
                    f.name: getattr(current, f.name).to("cuda:0")
                    for f in dataclasses.fields(current)
                    if torch.is_tensor(getattr(current, f.name))
                }
                current = dataclasses.replace(current, **changes)  # type: ignore[type-var]
            moved.append(current)
        return moved

    # ---- engine API ----
    def _wav(self, result: Any) -> np.ndarray:
        wavs, sr = result
        self.sr = int(sr)
        return np.asarray(wavs[0], dtype=np.float32).reshape(-1)

    def synth(
        self, text: str, voice: VoiceSpec, *, speed: float = 1.0, seed: int | None = None
    ) -> np.ndarray:
        self._seed(seed)
        if voice.mode == "preset":
            model = self._model("preset")
            call: dict[str, Any] = {
                "text": text,
                "language": voice.language,
                "speaker": voice.speaker or voice.name,
            }
            generate = model.generate_custom_voice
        else:
            call = {
                "text": text,
                "language": voice.language,
                "voice_clone_prompt": self._load_prompt(voice),
            }
            generate = self._model("clone").generate_voice_clone
        try:
            result = generate(**call)
        except Exception as e:  # library failures (CUDA OOM, bad input) become a coded error
            raise NsError("NS-E310", f"reference synthesis failed: {e}") from e
        return time_stretch(self._wav(result), self.sr, speed)

    def synth_batch(
        self, texts: Sequence[str], voice: VoiceSpec, *, speed: float = 1.0, seed: int | None = None
    ) -> list[np.ndarray]:
        return [self.synth(t, voice, speed=speed, seed=seed) for t in texts]

    def make_embedding(self, ref_wav: Path, ref_text: str, language: str = "English") -> EmbeddingData:
        model = self._model("clone", code="NS-E315")
        try:
            items = model.create_voice_clone_prompt(ref_audio=str(ref_wav), ref_text=ref_text)
        except Exception as e:
            raise NsError("NS-E315", f"reference engine could not encode the recording: {e}") from e
        return self._dump_prompt(items)

    def design_to_embedding(
        self, description: str, language: str = "English"
    ) -> tuple[EmbeddingData, np.ndarray]:
        designer = self._model("design", code="NS-E315")
        try:
            sample = self._wav(
                designer.generate_voice_design(text=SAMPLE_TEXT, language=language, instruct=description)
            )
        except Exception as e:
            raise NsError("NS-E315", f"reference engine could not render the voice: {e}") from e
        if self.device != "cpu":
            self._unload("design")  # only needed at design time
        base = self._model("clone", code="NS-E315")
        try:
            items = base.create_voice_clone_prompt(ref_audio=(sample, self.sr), ref_text=SAMPLE_TEXT)
        except Exception as e:
            raise NsError("NS-E315", f"reference engine could not encode the render: {e}") from e
        return self._dump_prompt(items), sample


# ---------------------------------------------------------------- selection


def tts_models_root(cfg: TtsCfg, paths: Paths) -> Path:
    """Folder that holds the TTS models: `cfg.model_dir` (the installer writes `models\\tts`) or the default.

    The qwentts GGUFs sit directly in it; the PyTorch checkouts sit in its `hf` subfolder (`TTS_HF_SUBDIR`).
    """
    return Path(cfg.model_dir) if cfg.model_dir else paths.models_tts


def select_engine(
    cfg: TtsCfg, paths: Paths, prefer: str | None = None, *, fake_speech: str = "tones"
) -> Engine:
    """Pick the engine: `NS_TTS_FAKE=1` or prefer="fake" -> fake; "reference" -> PyTorch; else qwentts.

    `prefer` overrides `cfg.engine`. The legacy config value "faster" means qwentts. Nothing is loaded.
    `fake_speech` ("tones" or "sapi", see `fake_speech_for`) is how the fake engine speaks.
    """
    if os.environ.get("NS_TTS_FAKE") == "1" or prefer == "fake":
        if fake_speech == "sapi":
            return FakeEngine(speech="sapi", log=NsLogger("tts", paths.logs), tmp_dir=paths.cache_tts_tmp)
        return FakeEngine()
    want = prefer or cfg.engine
    cpu = cfg.device == "cpu"
    root = tts_models_root(cfg, paths)
    if want == "reference":
        return ReferenceEngine(root / TTS_HF_SUBDIR, size="0.6B" if cpu else "1.7B", device=cfg.device)
    return QwenttsEngine(
        paths.bin_qwentts,
        root,
        size="0.6b" if cpu else "1.7b",
        device="CPU" if cpu else None,
        log=NsLogger("tts", paths.logs),
        tmp_dir=paths.cache_tts_tmp,
    )
