"""Voice folders with several speaker files (qwentts: spk + rvq + ref.txt) and the old one."""

import io
import json
import zipfile
from pathlib import Path

import numpy as np
import pytest

from ns_common.errors import NsError
from ns_tts.audio import write_wav
from ns_tts.engine import FakeEngine
from ns_tts.voices import VoiceLibrary, as_files, main_file


@pytest.fixture
def ref_wav(tmp_path: Path) -> Path:
    p = tmp_path / "ref.wav"
    write_wav(p, (0.2 * np.sin(np.arange(24000) / 10.0)).astype(np.float32), 24000)
    return p


class Multi(FakeEngine):
    def make_embedding(self, ref_wav, ref_text, language="English"):  # type: ignore[no-untyped-def]
        return {"speaker.spk": b"\x07" * 8, "speaker.rvq": b"RVQ", "ref.txt": b"hello\n"}


def test_several_files_are_stored_listed_and_exported(tmp_path: Path, ref_wav: Path):
    lib = VoiceLibrary(tmp_path / "voices")
    v = lib.add_clone("me", str(ref_wav), "hello", True, engine=Multi())
    folder = tmp_path / "voices" / "me"
    assert {p.name for p in folder.iterdir()} == {
        "voice.json", "sample.wav", "speaker.spk", "speaker.rvq", "ref.txt",
    }  # fmt: skip
    meta = json.loads((folder / "voice.json").read_text(encoding="utf-8"))
    assert meta["files"] == ["ref.txt", "speaker.rvq", "speaker.spk"] and meta["engine"] == "fake"
    assert v.embedding_path == str(folder / "speaker.spk")  # the main file; the others sit next to it
    assert [p.name for p in lib.embedding_files("me")] == ["ref.txt", "speaker.rvq", "speaker.spk"]
    with zipfile.ZipFile(io.BytesIO(lib.export_embedding("me"))) as z:
        assert sorted(z.namelist()) == ["ref.txt", "speaker.rvq", "speaker.spk"]
        assert z.read("speaker.rvq") == b"RVQ"


def test_voices_stored_before_the_files_key_still_load(tmp_path: Path):
    folder = tmp_path / "voices" / "old"
    folder.mkdir(parents=True)
    (folder / "speaker.bin").write_bytes(b"\x01" * 16)
    (folder / "voice.json").write_text(json.dumps({"name": "old", "mode": "clone"}), encoding="utf-8")
    lib = VoiceLibrary(tmp_path / "voices")
    assert lib.get("old").embedding_path == str(folder / "speaker.bin")
    assert lib.export_embedding("old") == b"\x01" * 16  # one file: the file itself, not a zip


@pytest.mark.parametrize("data", [{}, {"speaker.spk": b""}, {"../evil": b"x"}, {"sub/dir.bin": b"x"}, b""])
def test_bad_engine_output_is_ns_e314_and_leaves_nothing(tmp_path: Path, ref_wav: Path, data: object):
    class Bad(FakeEngine):
        def make_embedding(self, ref_wav, ref_text, language="English"):  # type: ignore[no-untyped-def]
            return data

    lib = VoiceLibrary(tmp_path / "voices")
    with pytest.raises(NsError) as ei:
        lib.add_clone("me", str(ref_wav), "hi", True, engine=Bad())
    assert ei.value.code == "NS-E315"
    root = tmp_path / "voices"
    assert not root.exists() or not list(root.iterdir())


def test_as_files_and_main_file():
    assert as_files(b"abc") == {"speaker.bin": b"abc"}
    assert as_files({"speaker.spk": b"1", "ref.txt": b"t"}) == {"speaker.spk": b"1", "ref.txt": b"t"}
    assert main_file(["ref.txt", "speaker.rvq", "speaker.spk"]) == "speaker.spk"
    assert main_file(["speaker.bin"]) == "speaker.bin"
    assert main_file(["b.dat", "a.dat"]) == "a.dat"
