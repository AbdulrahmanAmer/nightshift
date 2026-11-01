"""C4 ns_tts: voice library = stored speaker embeddings (spec 15.4).

Layout under `paths.models_tts_voices`: one folder per designed or cloned voice,
`<name>/{voice.json, speaker.bin, sample.wav}`. Presets are built in and have no folder. A voice is
written to a temporary folder first and renamed, so a failing engine never leaves half a voice behind.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import numpy as np

from ns_common.errors import NsError
from ns_tts.audio import write_wav

if TYPE_CHECKING:
    from ns_tts.engine import Engine

PRESETS = ["Ryan", "Aiden", "Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ono_Anna", "Sohee"]
SAMPLE_TEXT = "This is a short sample of the voice, so you can hear how it sounds before using it."

Mode = Literal["preset", "design", "clone"]
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")


@dataclass
class VoiceSpec:
    """A voice as the engines see it. Designed and cloned voices carry `embedding_path` (speaker.bin)."""

    name: str
    mode: Mode
    speaker: str | None = None
    description: str | None = None
    language: str = "English"
    embedding_path: str | None = None


class VoiceLibrary:
    """Presets plus the on-disk designed/cloned voices under `root`."""

    def __init__(self, root: Path) -> None:
        self.root = root

    # ---- reading ----
    def _load(self, folder: Path) -> VoiceSpec | None:
        try:
            raw = json.loads((folder / "voice.json").read_text(encoding="utf-8"))
            mode = raw["mode"]
            if mode not in ("design", "clone"):
                return None
            return VoiceSpec(
                name=str(raw["name"]),
                mode=mode,
                description=raw.get("description"),
                language=str(raw.get("language", "English")),
                embedding_path=str(folder / "speaker.bin"),
            )
        except (OSError, ValueError, KeyError):
            return None

    def list(self) -> list[VoiceSpec]:
        """Presets first, then library voices sorted by name. Unreadable folders are skipped."""
        out = [VoiceSpec(name=p, mode="preset", speaker=p) for p in PRESETS]
        if self.root.is_dir():
            for folder in sorted(self.root.iterdir(), key=lambda f: f.name.lower()):
                if folder.is_dir() and not folder.name.startswith("."):
                    spec = self._load(folder)
                    if spec is not None:
                        out.append(spec)
        return out

    def get(self, name: str) -> VoiceSpec:
        """Return a voice by exact name; raises KeyError when it does not exist."""
        if name in PRESETS:
            return VoiceSpec(name=name, mode="preset", speaker=name)
        if _NAME_RE.match(name):
            spec = self._load(self.root / name)
            if spec is not None:
                return spec
        raise KeyError(name)

    def sample_path(self, name: str) -> Path:
        """Path of a library voice's sample.wav (may not exist for presets)."""
        return self.root / name / "sample.wav"

    def export_embedding(self, name: str) -> bytes:
        """Bytes of `speaker.bin` for backup or transfer. KeyError if unknown, ValueError for presets."""
        spec = self.get(name)
        if spec.embedding_path is None:
            raise ValueError(f"{name} is a built-in preset and has no stored embedding")
        return Path(spec.embedding_path).read_bytes()

    # ---- writing ----
    def _check_new(self, name: str) -> None:
        if not _NAME_RE.match(name):
            raise ValueError("voice name: 1-40 letters, digits, _ or -, starting with a letter or digit")
        taken = {p.lower() for p in PRESETS}
        if self.root.is_dir():
            taken |= {f.name.lower() for f in self.root.iterdir()}
        if name.lower() in taken:
            raise ValueError(f"a voice named {name!r} already exists")

    def _store(
        self,
        spec: VoiceSpec,
        embedding: bytes,
        sample: np.ndarray | None,
        engine: Engine,
        extra: dict[str, Any],
    ) -> VoiceSpec:
        if not embedding:
            raise NsError("NS-E202", "the TTS engine returned an empty speaker embedding")
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / f".tmp-{spec.name}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        try:
            (tmp / "speaker.bin").write_bytes(embedding)
            if sample is None:
                probe = VoiceSpec(
                    name=spec.name,
                    mode=spec.mode,
                    description=spec.description,
                    language=spec.language,
                    embedding_path=str(tmp / "speaker.bin"),
                )
                sample = engine.synth(SAMPLE_TEXT, probe)
            write_wav(tmp / "sample.wav", sample, engine.sr)
            meta = {
                "name": spec.name,
                "mode": spec.mode,
                "description": spec.description,
                "language": spec.language,
                "engine": engine.name,
                **extra,
            }
            (tmp / "voice.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            tmp.replace(self.root / spec.name)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return self.get(spec.name)

    def add_design(
        self, name: str, description: str, *, engine: Engine, language: str = "English"
    ) -> VoiceSpec:
        """Design a voice from a text description and store its embedding and a sample.

        `engine.design_to_embedding` renders the description (VoiceDesign) and re-encodes the render
        as a speaker embedding, so later synthesis never needs the VoiceDesign model again.
        """
        self._check_new(name)
        if not description.strip():
            raise ValueError("description must not be empty")
        embedding, sample = engine.design_to_embedding(description, language)
        spec = VoiceSpec(name=name, mode="design", description=description, language=language)
        return self._store(spec, embedding, sample, engine, {})

    def add_clone(
        self,
        name: str,
        ref_audio_path: str,
        ref_text: str,
        rights_confirmed: bool,
        *,
        engine: Engine,
        language: str = "English",
    ) -> VoiceSpec:
        """Clone a voice from a reference recording. Raises ValueError unless `rights_confirmed`.

        Only the embedding is kept; the reference audio is not copied into the library.
        """
        if not rights_confirmed:
            raise ValueError("rights_confirmed must be true: you must have the right to clone this voice")
        self._check_new(name)
        ref = Path(ref_audio_path)
        if not ref.is_file():
            raise ValueError(f"reference audio not found: {ref}")
        if not ref_text.strip():
            raise ValueError("ref_text must hold the words spoken in the reference audio")
        embedding = engine.make_embedding(ref, ref_text, language)
        spec = VoiceSpec(name=name, mode="clone", language=language)
        return self._store(spec, embedding, None, engine, {"ref_text": ref_text, "rights_confirmed": True})
