import json
from pathlib import Path

import numpy as np
import pytest

from ns_common.errors import NsError
from ns_tts.audio import read_wav, write_wav
from ns_tts.engine import FakeEngine
from ns_tts.voices import PRESETS, VoiceLibrary, VoiceSpec


@pytest.fixture
def engine() -> FakeEngine:
    e = FakeEngine()
    e.load()
    return e


@pytest.fixture
def ref_wav(tmp_path: Path) -> Path:
    p = tmp_path / "ref.wav"
    write_wav(p, (0.2 * np.sin(np.arange(24000) / 10.0)).astype(np.float32), 24000)
    return p


def test_presets_are_built_in(tmp_path: Path):
    lib = VoiceLibrary(tmp_path / "voices")
    assert PRESETS == ["Ryan", "Aiden", "Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ono_Anna", "Sohee"]
    assert [v.name for v in lib.list()] == PRESETS
    ryan = lib.get("Ryan")
    assert ryan.mode == "preset" and ryan.speaker == "Ryan" and ryan.embedding_path is None
    with pytest.raises(KeyError):
        lib.get("nobody")
    with pytest.raises(KeyError):
        lib.get("../escape")


def test_add_design_writes_folder_and_persists(tmp_path: Path, engine: FakeEngine):
    root = tmp_path / "voices"
    lib = VoiceLibrary(root)
    v = lib.add_design("velvet", "deep calm male narrator, slow", engine=engine)
    assert v.mode == "design" and v.description == "deep calm male narrator, slow"
    folder = root / "velvet"
    assert {p.name for p in folder.iterdir()} == {"voice.json", "speaker.bin", "sample.wav"}
    assert (folder / "speaker.bin").stat().st_size == 256
    meta = json.loads((folder / "voice.json").read_text(encoding="utf-8"))
    assert meta["engine"] == "fake" and meta["mode"] == "design"
    audio, sr = read_wav(folder / "sample.wav")
    assert sr == 24000 and len(audio) > 0
    again = VoiceLibrary(root).get("velvet")
    assert again.embedding_path == str(folder / "speaker.bin") and again.description == v.description
    assert not any(p.name.startswith(".") for p in root.iterdir())


def test_add_clone_requires_rights_first(tmp_path: Path, engine: FakeEngine, ref_wav: Path):
    lib = VoiceLibrary(tmp_path / "voices")
    with pytest.raises(ValueError, match="rights"):
        lib.add_clone("me", str(tmp_path / "missing.wav"), "hello there", False, engine=engine)
    assert not (tmp_path / "voices").exists()
    v = lib.add_clone("me", str(ref_wav), "hello there", True, engine=engine)
    assert v.mode == "clone"
    meta = json.loads((tmp_path / "voices" / "me" / "voice.json").read_text(encoding="utf-8"))
    assert meta["ref_text"] == "hello there" and meta["rights_confirmed"] is True
    assert not list((tmp_path / "voices" / "me").glob("ref*"))  # reference audio is not kept


def test_clone_and_design_listing_and_export(tmp_path: Path, engine: FakeEngine, ref_wav: Path):
    lib = VoiceLibrary(tmp_path / "voices")
    lib.add_design("velvet", "deep calm male narrator", engine=engine)
    lib.add_clone("me", str(ref_wav), "hello", True, engine=engine)
    names = [v.name for v in lib.list()]
    assert names == [*PRESETS, "me", "velvet"]
    blob = lib.export_embedding("me")
    assert blob == (tmp_path / "voices" / "me" / "speaker.bin").read_bytes() and len(blob) == 256
    with pytest.raises(ValueError):
        lib.export_embedding("Ryan")
    with pytest.raises(KeyError):
        lib.export_embedding("ghost")


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "Ryan", "ryan", ".hidden", "x" * 41, "with space"])
def test_bad_names_rejected(tmp_path: Path, engine: FakeEngine, bad: str):
    with pytest.raises(ValueError):
        VoiceLibrary(tmp_path / "voices").add_design(bad, "a voice", engine=engine)


def test_duplicate_name_rejected_case_insensitively(tmp_path: Path, engine: FakeEngine):
    lib = VoiceLibrary(tmp_path / "voices")
    lib.add_design("velvet", "deep calm male narrator", engine=engine)
    with pytest.raises(ValueError, match="exists"):
        lib.add_design("Velvet", "another", engine=engine)


def test_input_validation(tmp_path: Path, engine: FakeEngine, ref_wav: Path):
    lib = VoiceLibrary(tmp_path / "voices")
    with pytest.raises(ValueError):
        lib.add_design("v1", "   ", engine=engine)
    with pytest.raises(ValueError, match="not found"):
        lib.add_clone("v2", str(tmp_path / "nope.wav"), "hi", True, engine=engine)
    with pytest.raises(ValueError, match="ref_text"):
        lib.add_clone("v3", str(ref_wav), " ", True, engine=engine)


def test_failed_sample_render_leaves_nothing(tmp_path: Path, ref_wav: Path):
    class BrokenSynth(FakeEngine):
        def synth(self, text, voice, *, speed=1.0, seed=None):  # type: ignore[no-untyped-def]
            raise NsError("NS-E202", "boom")

    lib = VoiceLibrary(tmp_path / "voices")
    with pytest.raises(NsError):
        lib.add_clone("me", str(ref_wav), "hello", True, engine=BrokenSynth())
    assert list((tmp_path / "voices").iterdir()) == []
    assert [v.name for v in lib.list()] == PRESETS


def test_empty_embedding_is_an_error(tmp_path: Path, ref_wav: Path):
    class EmptyEmbedding(FakeEngine):
        def make_embedding(self, ref_wav, ref_text, language="English"):  # type: ignore[no-untyped-def]
            return b""

    with pytest.raises(NsError) as ei:
        VoiceLibrary(tmp_path / "voices").add_clone("me", str(ref_wav), "hi", True, engine=EmptyEmbedding())
    assert ei.value.code == "NS-E315"


def test_voicespec_defaults():
    v = VoiceSpec(name="x", mode="design")
    assert v.language == "English" and v.embedding_path is None and v.speaker is None
