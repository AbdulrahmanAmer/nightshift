"""C4 ns_tts: TTS engines behind one protocol (spec 2, 6.1, 14.2, 15.4).

Engines: `QwenttsEngine` (the `qwentts.cpp` CLI, default), `ReferenceEngine` (PyTorch `qwen-tts`, the
production path until a prebuilt qwentts binary passes the smoke test, and the NS-W301 fallback) and
`FakeEngine` (CI, `NS_TTS_FAKE=1`). Heavy imports (`torch`, `qwen_tts`) happen only inside methods.

Speaker embeddings (`speaker.bin`) are engine-specific files: the qwentts format is whatever the CLI
writes; the reference format is documented at `REFERENCE_MAGIC`. Each engine refuses the other's files.
"""

from __future__ import annotations

import dataclasses
import gc
import hashlib
import importlib
import io
import math
import os
import subprocess
import tempfile
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.models import TtsCfg
from ns_common.paths import Paths
from ns_common.textstats import word_count
from ns_tts.audio import read_wav
from ns_tts.voices import PRESETS, SAMPLE_TEXT, Mode, VoiceSpec

# verify against the pinned qwentts.cpp release README during M0 verify; `ns verify` reports NS-W301 and
# switches to the reference engine if the probe fails.
# Every command-line detail of qwentts lives in this one dict. A value of None means "this build has no
# such option": optional options are then skipped, required ones raise NS-E202.
# Required: talker, tokenizer, text, out. Optional: the rest. "batch_file" + "out_dir" enable one process
# per batch (one text per line in, files named by "batch_out_name" out); without them one process runs
# per chunk and reloads the model each time.
QWENTTS_FLAGS: dict[str, str | None] = {
    "talker": "--model",
    "tokenizer": "--tokenizer",
    "text": "--text",
    "out": "--out",
    "speaker": "--speaker",  # CustomVoice presets
    "language": "--language",
    "instruct": "--instruct",  # VoiceDesign description
    "ref_audio": "--ref-audio",  # Base: build an embedding from a recording
    "ref_text": "--ref-text",
    "save_embedding": "--save-speaker-embedding",
    "speaker_embedding": "--speaker-embedding",  # Base: synthesize from a stored embedding
    "seed": "--seed",
    "batch_file": "--batch-file",
    "out_dir": "--out-dir",
    "batch_out_name": "{index:04d}.wav",  # not a flag: file name pattern inside out_dir
}

# GGUF names pinned in manifest.json (models.tts / tts_optional / cpu-dev); {kind} per talker.
TALKER_TEMPLATE = "qwen-talker-{size}-{kind}-Q8_0.gguf"
TALKER_KIND: dict[str, str] = {"preset": "customvoice", "clone": "base", "design": "voicedesign"}
TOKENIZER_FILE = "qwen-tokenizer-12hz-Q8_0.gguf"

# Reference-engine speaker.bin = REFERENCE_MAGIC + torch.save() of the list of VoiceClonePromptItem
# returned by Qwen3TTSModel.create_voice_clone_prompt (pickle based: only load files you made or trust).
REFERENCE_MAGIC = b"NSREF1\n"
REFERENCE_DIRS: dict[str, str] = {"preset": "CustomVoice", "design": "VoiceDesign", "clone": "Base"}


class Engine(Protocol):
    """What the server needs from a TTS backend. Audio is float32 mono at `sr`."""

    name: str
    sr: int
    loaded: bool

    def load(self) -> None:
        """Prepare the engine; raises NsError when it cannot run."""
        ...

    def missing_model(self, op: Mode) -> str | None:
        """Name of the first model file/folder absent for `op` (preset, design or clone), else None."""
        ...

    def synth(
        self, text: str, voice: VoiceSpec, *, speed: float = 1.0, seed: int | None = None
    ) -> np.ndarray:
        """Render one chunk of text in `voice`."""
        ...

    def synth_batch(
        self, texts: Sequence[str], voice: VoiceSpec, *, speed: float = 1.0, seed: int | None = None
    ) -> list[np.ndarray]:
        """Render several chunks in order with the same voice and seed."""
        ...

    def make_embedding(self, ref_wav: Path, ref_text: str, language: str = "English") -> bytes:
        """Encode a reference recording (and its transcript) as a speaker embedding."""
        ...

    def design_to_embedding(self, description: str, language: str = "English") -> tuple[bytes, np.ndarray]:
        """Render a voice from a text description, then return (embedding of that render, the render)."""
        ...


def change_speed(x: np.ndarray, speed: float) -> np.ndarray:
    """Time-stretch by linear resampling (pitch moves with speed; fine for the 0.9-1.1 range used)."""
    if abs(speed - 1.0) < 1e-3 or x.size == 0:
        return x
    idx = np.arange(0, len(x) - 1, speed)
    return np.interp(idx, np.arange(len(x)), x).astype(np.float32)


# ---------------------------------------------------------------- fake


def _digest_bytes(seed: bytes, n: int = 256) -> bytes:
    out = b""
    i = 0
    while len(out) < n:
        out += hashlib.sha256(seed + bytes([i])).digest()
        i += 1
    return out[:n]


class FakeEngine:
    """Tone burst per word, deterministic 256-byte embeddings. Used by CI and `NS_TTS_FAKE=1`."""

    name = "fake"
    sr = 24000

    def __init__(self) -> None:
        self.loaded = False

    def load(self) -> None:
        self.loaded = True

    def missing_model(self, op: Mode) -> str | None:
        return None

    def _freq(self, voice: VoiceSpec) -> float:
        if voice.mode == "preset":
            who = voice.speaker or voice.name
            if who in PRESETS:
                return 200.0 + 40.0 * PRESETS.index(who)
            return 200.0 + zlib.crc32(who.encode()) % 400
        if not voice.embedding_path or not Path(voice.embedding_path).is_file():
            raise NsError("NS-E301", f"voice {voice.name!r} has no stored embedding")
        return 200.0 + Path(voice.embedding_path).read_bytes()[0] * 2.0

    def _tone(self, text: str, freq: float, speed: float) -> np.ndarray:
        words = max(1, word_count(text))
        seconds = max(0.4, words / (155.0 * max(speed, 0.1)) * 60.0)
        n = int(seconds * self.sr)
        idx = np.arange(n)
        gate = np.mod(idx, n / words) < int(0.08 * self.sr)
        tone = 0.3 * np.sin(2.0 * math.pi * freq * idx / self.sr)
        return np.where(gate, tone, 0.0).astype(np.float32)

    def synth(
        self, text: str, voice: VoiceSpec, *, speed: float = 1.0, seed: int | None = None
    ) -> np.ndarray:
        return self._tone(text, self._freq(voice), speed)

    def synth_batch(
        self, texts: Sequence[str], voice: VoiceSpec, *, speed: float = 1.0, seed: int | None = None
    ) -> list[np.ndarray]:
        return [self.synth(t, voice, speed=speed, seed=seed) for t in texts]

    def make_embedding(self, ref_wav: Path, ref_text: str, language: str = "English") -> bytes:
        audio = ref_wav.read_bytes() if ref_wav.is_file() else ref_wav.name.encode()
        return _digest_bytes(b"clone:" + ref_text.encode() + audio)

    def design_to_embedding(self, description: str, language: str = "English") -> tuple[bytes, np.ndarray]:
        emb = _digest_bytes(b"design:" + description.encode())
        return emb, self._tone(SAMPLE_TEXT, 200.0 + emb[0] * 2.0, 1.0)


# ---------------------------------------------------------------- qwentts.cpp


class QwenttsEngine:
    """Runs the `qwentts.cpp` CLI (`bin\\qwentts\\qwentts.exe`) once per call or once per batch."""

    name = "qwentts"

    def __init__(
        self,
        exe: Path,
        models_dir: Path,
        *,
        size: str = "1.7b",
        timeout_s: float = 900.0,
        log: NsLogger | None = None,
    ) -> None:
        self.exe = exe
        self.models_dir = models_dir
        self.size = size
        self.timeout_s = timeout_s
        self.log = log
        self.sr = 24000
        self.loaded = False

    # ---- files ----
    def talker_path(self, op: Mode) -> Path:
        """GGUF of the talker used for `op` (design -> VoiceDesign, clone -> Base, preset -> CustomVoice)."""
        return self.models_dir / TALKER_TEMPLATE.format(size=self.size, kind=TALKER_KIND[op])

    @property
    def tokenizer_path(self) -> Path:
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
        """Check the binary and the preset talker exist (the process itself starts per call)."""
        if not self.exe.is_file():
            raise NsError("NS-E202", f"qwentts binary not found: {self.exe}")
        missing = self.missing_model("preset")
        if missing:
            raise NsError("NS-E202", f"qwentts model file missing: {missing}", missing_model=missing)
        self.loaded = True

    # ---- command line ----
    def build_command(
        self,
        op: Mode,
        *,
        text: str | None = None,
        out: Path | None = None,
        speaker: str | None = None,
        instruct: str | None = None,
        ref_audio: Path | None = None,
        ref_text: str | None = None,
        embedding: Path | None = None,
        save_embedding: Path | None = None,
        seed: int | None = None,
        language: str | None = None,
        batch_file: Path | None = None,
        out_dir: Path | None = None,
    ) -> list[str]:
        """Assemble the CLI call from `QWENTTS_FLAGS`; `op` picks the talker GGUF."""
        cmd = [str(self.exe)]

        def add(key: str, value: object, *, required: bool = False) -> None:
            if value is None:
                return
            flag = QWENTTS_FLAGS.get(key)
            if flag is None:
                if required:
                    raise NsError("NS-E202", f"QWENTTS_FLAGS has no flag for {key!r}")
                return
            cmd.extend([flag, str(value)])

        add("talker", self.talker_path(op), required=True)
        add("tokenizer", self.tokenizer_path, required=True)
        add("text", text, required=True)
        add("out", out, required=True)
        add("speaker", speaker, required=True)
        add("language", language)
        add("instruct", instruct, required=True)
        add("ref_audio", ref_audio, required=True)
        add("ref_text", ref_text, required=True)
        add("speaker_embedding", embedding, required=True)
        add("save_embedding", save_embedding, required=True)
        add("seed", seed)
        add("batch_file", batch_file, required=True)
        add("out_dir", out_dir, required=True)
        return cmd

    def _run(self, cmd: list[str]) -> None:
        if self.log:
            self.log.debug("qwentts_run", cmd=" ".join(cmd))
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise NsError("NS-E202", f"qwentts timed out after {self.timeout_s:.0f}s", cmd=cmd) from e
        except OSError as e:
            raise NsError("NS-E202", f"cannot start qwentts: {e}", cmd=cmd) from e
        if proc.returncode != 0:
            tail = "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-8:])
            raise NsError("NS-E202", f"qwentts exited {proc.returncode}: {tail}", cmd=cmd)

    def _read(self, path: Path) -> np.ndarray:
        if not path.is_file():
            raise NsError("NS-E202", f"qwentts wrote no output file: {path.name}")
        try:
            x, sr = read_wav(path)
        except ValueError as e:
            raise NsError("NS-E202", f"qwentts output is not a readable WAV: {e}") from e
        self.sr = sr
        return x

    def _voice_args(self, voice: VoiceSpec) -> tuple[Mode, dict[str, Any]]:
        if voice.mode == "preset":
            return "preset", {"speaker": voice.speaker or voice.name}
        if not voice.embedding_path or not Path(voice.embedding_path).is_file():
            raise NsError("NS-E301", f"voice {voice.name!r} has no stored embedding")
        with Path(voice.embedding_path).open("rb") as fh:
            if fh.read(len(REFERENCE_MAGIC)) == REFERENCE_MAGIC:
                raise NsError(
                    "NS-E301", f"voice {voice.name!r} was created by the reference engine; re-create it"
                )
        return "clone", {"embedding": Path(voice.embedding_path)}

    # ---- engine API ----
    def synth(
        self, text: str, voice: VoiceSpec, *, speed: float = 1.0, seed: int | None = None
    ) -> np.ndarray:
        op, args = self._voice_args(voice)
        with tempfile.TemporaryDirectory(prefix="ns-qwentts-") as td:
            out = Path(td) / "out.wav"
            cmd = self.build_command(op, text=text, out=out, seed=seed, language=voice.language, **args)
            self._run(cmd)
            return change_speed(self._read(out), speed)

    def synth_batch(
        self, texts: Sequence[str], voice: VoiceSpec, *, speed: float = 1.0, seed: int | None = None
    ) -> list[np.ndarray]:
        if len(texts) < 2 or not QWENTTS_FLAGS.get("batch_file") or not QWENTTS_FLAGS.get("out_dir"):
            return [self.synth(t, voice, speed=speed, seed=seed) for t in texts]
        op, args = self._voice_args(voice)
        name_pattern = QWENTTS_FLAGS.get("batch_out_name") or "{index:04d}.wav"
        with tempfile.TemporaryDirectory(prefix="ns-qwentts-") as td:
            lines = Path(td) / "texts.txt"
            lines.write_text("\n".join(" ".join(t.split()) for t in texts) + "\n", encoding="utf-8")
            out_dir = Path(td) / "out"
            out_dir.mkdir()
            cmd = self.build_command(
                op, batch_file=lines, out_dir=out_dir, seed=seed, language=voice.language, **args
            )
            self._run(cmd)
            return [
                change_speed(self._read(out_dir / name_pattern.format(index=i)), speed)
                for i in range(len(texts))
            ]

    def make_embedding(self, ref_wav: Path, ref_text: str, language: str = "English") -> bytes:
        with tempfile.TemporaryDirectory(prefix="ns-qwentts-") as td:
            emb = Path(td) / "speaker.bin"
            cmd = self.build_command(
                "clone",
                text=ref_text,
                out=Path(td) / "ref-check.wav",
                ref_audio=ref_wav,
                ref_text=ref_text,
                save_embedding=emb,
                language=language,
            )
            self._run(cmd)
            if not emb.is_file() or emb.stat().st_size == 0:
                raise NsError("NS-E202", "qwentts did not write a speaker embedding")
            return emb.read_bytes()

    def design_to_embedding(self, description: str, language: str = "English") -> tuple[bytes, np.ndarray]:
        missing = self.missing_model("design")
        if missing:
            raise NsError("NS-E202", f"qwentts model file missing: {missing}", missing_model=missing)
        with tempfile.TemporaryDirectory(prefix="ns-qwentts-") as td:
            render = Path(td) / "design.wav"
            cmd = self.build_command(
                "design", text=SAMPLE_TEXT, out=render, instruct=description, language=language
            )
            self._run(cmd)
            sample = self._read(render)
            return self.make_embedding(render, SAMPLE_TEXT, language), sample


# ---------------------------------------------------------------- reference (PyTorch qwen-tts)


class ReferenceEngine:
    """Official `qwen-tts` package on PyTorch: presets, VoiceDesign and Base cloning.

    One `Qwen3TTSModel` per voice mode is loaded lazily (CustomVoice, VoiceDesign, Base). On the CPU
    profile only one stays resident (the previous is unloaded); on CUDA the preset and clone models stay
    and the VoiceDesign model is unloaded right after a design call to keep VRAM free. bf16 on CUDA,
    float32 on CPU, `attn_implementation="sdpa"`.
    """

    name = "reference"

    def __init__(self, models_dir: Path, *, size: str = "1.7B", device: str = "cuda") -> None:
        self.models_dir = models_dir
        self.size = size
        self.device = device
        self.sr = 24000
        self.loaded = False
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
        """Import torch and qwen_tts and load the preset model; raises NsError if unavailable."""
        self._model("preset")
        self.loaded = True

    def _model(self, op: Mode) -> Any:
        if op in self._models:
            return self._models[op]
        folder = self.model_dir(op)
        if not folder.is_dir():
            raise NsError("NS-E202", f"reference TTS model missing: {folder.name}", missing_model=folder.name)
        if self.device == "cpu":
            self._unload_all()
        try:
            torch = importlib.import_module("torch")
            qwen_tts = importlib.import_module("qwen_tts")
        except ImportError as e:
            raise NsError("NS-E202", f"reference engine needs torch and qwen-tts: {e}") from e
        cuda = self.device != "cpu"
        try:
            model = qwen_tts.Qwen3TTSModel.from_pretrained(
                str(self.model_dir(op)),
                device_map="cuda:0" if cuda else "cpu",
                dtype=torch.bfloat16 if cuda else torch.float32,
                attn_implementation="sdpa",
            )
        except Exception as e:  # wrap library failures (OOM, bad weights) into a coded error
            raise NsError("NS-E202", f"cannot load {self.model_dir(op).name}: {e}") from e
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
            raise NsError("NS-E301", f"voice {voice.name!r} has no stored embedding")
        path = Path(voice.embedding_path)
        key = (str(path), path.stat().st_mtime)
        if key not in self._prompts:
            raw = path.read_bytes()
            if not raw.startswith(REFERENCE_MAGIC):
                raise NsError(
                    "NS-E301", f"voice {voice.name!r} was not created by the reference engine; re-create it"
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
            result = model.generate_custom_voice(
                text=text, language=voice.language, speaker=voice.speaker or voice.name
            )
        else:
            prompt = self._load_prompt(voice)
            model = self._model("clone")
            result = model.generate_voice_clone(
                text=text, language=voice.language, voice_clone_prompt=prompt
            )
        return change_speed(self._wav(result), speed)

    def synth_batch(
        self, texts: Sequence[str], voice: VoiceSpec, *, speed: float = 1.0, seed: int | None = None
    ) -> list[np.ndarray]:
        return [self.synth(t, voice, speed=speed, seed=seed) for t in texts]

    def make_embedding(self, ref_wav: Path, ref_text: str, language: str = "English") -> bytes:
        model = self._model("clone")
        items = model.create_voice_clone_prompt(ref_audio=str(ref_wav), ref_text=ref_text)
        return self._dump_prompt(items)

    def design_to_embedding(self, description: str, language: str = "English") -> tuple[bytes, np.ndarray]:
        designer = self._model("design")
        sample = self._wav(
            designer.generate_voice_design(text=SAMPLE_TEXT, language=language, instruct=description)
        )
        if self.device != "cpu":
            self._unload("design")  # only needed at design time
        base = self._model("clone")
        items = base.create_voice_clone_prompt(ref_audio=(sample, self.sr), ref_text=SAMPLE_TEXT)
        return self._dump_prompt(items), sample


# ---------------------------------------------------------------- selection


def select_engine(cfg: TtsCfg, paths: Paths, prefer: str | None = None) -> Engine:
    """Pick the engine: `NS_TTS_FAKE=1` or prefer="fake" -> fake; "reference" -> PyTorch; else qwentts.

    `prefer` overrides `cfg.engine`. The legacy config value "faster" means qwentts. Nothing is loaded.
    """
    if os.environ.get("NS_TTS_FAKE") == "1" or prefer == "fake":
        return FakeEngine()
    want = prefer or cfg.engine
    cpu = cfg.device == "cpu"
    if want == "reference":
        models = Path(cfg.model_dir) if cfg.model_dir else paths.models_tts / "hf"
        return ReferenceEngine(models, size="0.6B" if cpu else "1.7B", device=cfg.device)
    exe = paths.bin_llama.parent / "qwentts" / "qwentts.exe"
    return QwenttsEngine(
        exe, paths.models_tts, size="0.6b" if cpu else "1.7b", log=NsLogger("tts", paths.logs)
    )
