import pickle
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from ns_common.errors import NsError
from ns_common.models import TtsCfg
from ns_common.paths import TTS_HF_SUBDIR, Paths
from ns_tts.audio import write_wav
from ns_tts.engine import (
    REFERENCE_MAGIC,
    FakeEngine,
    QwenttsEngine,
    ReferenceEngine,
    select_engine,
)
from ns_tts.voices import VoiceLibrary, VoiceSpec

RYAN = VoiceSpec(name="Ryan", mode="preset", speaker="Ryan")


# ---------------------------------------------------------------- fake engine
def test_fake_engine_duration_scales_with_words_and_speed():
    e = FakeEngine()
    e.load()
    assert e.loaded and e.name == "fake" and e.sr == 24000
    x = e.synth("one two three four five", RYAN)
    assert x.dtype == np.float32 and 1.5 <= len(x) / e.sr <= 2.5
    fast = e.synth("one two three four five", RYAN, speed=2.0)
    assert len(fast) < len(x)
    assert float(np.abs(x).max()) > 0.1


def test_fake_engine_is_deterministic_and_voice_dependent():
    e = FakeEngine()
    other = VoiceSpec(name="Aiden", mode="preset", speaker="Aiden")
    assert np.array_equal(e.synth("hello there", RYAN), e.synth("hello there", RYAN))
    assert not np.array_equal(e.synth("hello there", RYAN), e.synth("hello there", other))


def test_fake_embeddings_are_256_bytes_and_deterministic(tmp_path: Path):
    e = FakeEngine()
    ref = tmp_path / "ref.wav"
    write_wav(ref, np.zeros(2400, dtype=np.float32), 24000)
    a = e.make_embedding(ref, "hello")
    assert len(a) == 256 and a == e.make_embedding(ref, "hello") and a != e.make_embedding(ref, "bye")
    emb, sample = e.design_to_embedding("deep calm voice")
    assert len(emb) == 256 and sample.dtype == np.float32 and len(sample) > 0
    assert emb != e.design_to_embedding("high bright voice")[0]


def test_fake_engine_uses_the_stored_embedding(tmp_path: Path):
    e = FakeEngine()
    lib = VoiceLibrary(tmp_path / "voices")
    ref = tmp_path / "ref.wav"
    write_wav(ref, np.zeros(2400, dtype=np.float32), 24000)
    voice = lib.add_clone("me", str(ref), "hello", True, engine=e)
    assert len(e.synth("hello there friend", voice)) > 0
    Path(voice.embedding_path or "").unlink()
    with pytest.raises(NsError) as ei:
        e.synth("hello there friend", voice)
    assert ei.value.code == "NS-E311"
    with pytest.raises(NsError):
        e.synth("hi", VoiceSpec(name="x", mode="clone"))


def test_fake_batch_and_missing_model():
    e = FakeEngine()
    out = e.synth_batch(["one two", "three four five"], RYAN)
    assert len(out) == 2 and len(out[1]) > len(out[0])
    assert e.missing_model("design") is None


# ---------------------------------------------------------------- select_engine
def test_select_engine_env_fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("NS_TTS_FAKE", "1")
    assert select_engine(TtsCfg(), Paths(tmp_path)).name == "fake"
    assert select_engine(TtsCfg(engine="reference"), Paths(tmp_path), "reference").name == "fake"


def test_select_engine_prefer_fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("NS_TTS_FAKE", raising=False)
    assert select_engine(TtsCfg(), Paths(tmp_path), prefer="fake").name == "fake"


def test_select_engine_default_is_qwentts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("NS_TTS_FAKE", raising=False)
    paths = Paths(tmp_path)
    e = select_engine(TtsCfg(), paths)  # config default "faster" is the legacy name for qwentts
    assert isinstance(e, QwenttsEngine) and e.name == "qwentts"
    assert e.bin_dir == paths.bin_qwentts == tmp_path / "bin" / "qwentts"
    assert e.exe("qwen-tts") == tmp_path / "bin" / "qwentts" / "qwen-tts.exe"
    assert e.models_dir == paths.models_tts and e.size == "1.7b" and e.device is None
    assert select_engine(TtsCfg(engine="reference"), paths, prefer="qwentts").name == "qwentts"


def test_select_engine_reference(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("NS_TTS_FAKE", raising=False)
    paths = Paths(tmp_path)
    e = select_engine(TtsCfg(engine="reference"), paths)
    assert isinstance(e, ReferenceEngine)
    assert e.models_dir == paths.models_tts_hf == paths.models_tts / TTS_HF_SUBDIR
    assert e.size == "1.7B" and e.device == "cuda"
    # model_dir is the TTS models root (the installer writes models	ts): the checkouts sit in its hf folder
    same = select_engine(TtsCfg(engine="reference", model_dir=str(paths.models_tts)), paths)
    assert isinstance(same, ReferenceEngine) and same.models_dir == paths.models_tts_hf
    cpu = select_engine(TtsCfg(engine="reference", device="cpu", model_dir=str(tmp_path / "m")), paths)
    assert isinstance(cpu, ReferenceEngine) and cpu.size == "0.6B" and cpu.models_dir == tmp_path / "m" / "hf"
    assert isinstance(select_engine(TtsCfg(), paths, prefer="reference"), ReferenceEngine)


# ---------------------------------------------------------------- reference engine with fake torch/qwen_tts
@dataclass
class _PromptItem:
    ref_text: str


class _FakeModel:
    def __init__(self, path: str, kwargs: dict[str, Any], log: list[Any]) -> None:
        self.path, self.kwargs, self.log = path, kwargs, log

    def _audio(self) -> tuple[list[np.ndarray], int]:
        return [np.full(2400, 0.1, dtype=np.float32)], 24000

    def generate_custom_voice(self, **kw: Any) -> Any:
        self.log.append(("custom", kw))
        return self._audio()

    def generate_voice_design(self, **kw: Any) -> Any:
        self.log.append(("design", kw))
        return self._audio()

    def generate_voice_clone(self, **kw: Any) -> Any:
        self.log.append(("clone", kw))
        return self._audio()

    def create_voice_clone_prompt(self, **kw: Any) -> Any:
        self.log.append(("prompt", kw))
        return [_PromptItem(ref_text=kw["ref_text"])]


class _Env:
    def __init__(self) -> None:
        self.loads: list[tuple[str, dict[str, Any]]] = []
        self.calls: list[Any] = []
        self.seeds: list[int] = []


@pytest.fixture
def ref_env(monkeypatch: pytest.MonkeyPatch) -> _Env:
    env = _Env()

    class Qwen3TTSModel:
        @classmethod
        def from_pretrained(cls, path: str, **kwargs: Any) -> _FakeModel:
            env.loads.append((Path(path).name, kwargs))
            return _FakeModel(path, kwargs, env.calls)

    torch = types.SimpleNamespace(
        bfloat16="bf16",
        float32="f32",
        manual_seed=env.seeds.append,
        save=lambda obj, fh: pickle.dump(obj, fh),
        load=lambda fh, map_location=None, weights_only=True: pickle.load(fh),
        is_tensor=lambda o: False,
        cuda=types.SimpleNamespace(empty_cache=lambda: None),
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "qwen_tts", types.SimpleNamespace(Qwen3TTSModel=Qwen3TTSModel))
    return env


def _models(tmp_path: Path, size: str = "1.7B") -> Path:
    for kind in ("CustomVoice", "VoiceDesign", "Base"):
        (tmp_path / f"Qwen3-TTS-12Hz-{size}-{kind}").mkdir(parents=True)
    return tmp_path


def test_reference_preset_synth_cuda(ref_env: _Env, tmp_path: Path):
    e = ReferenceEngine(_models(tmp_path), size="1.7B", device="cuda")
    e.load()
    assert e.loaded and e.name == "reference"
    voice = VoiceSpec(name="Ryan", mode="preset", speaker="Ryan", language="English")
    x = e.synth("hello there", voice, seed=7)
    assert x.dtype == np.float32 and len(x) == 2400 and e.sr == 24000
    assert ref_env.seeds == [7]
    name, kw = ref_env.loads[0]
    assert name == "Qwen3-TTS-12Hz-1.7B-CustomVoice"
    assert kw == {"device_map": "cuda:0", "dtype": "bf16", "attn_implementation": "sdpa"}
    kind, call = ref_env.calls[0]
    assert kind == "custom" and call == {"text": "hello there", "language": "English", "speaker": "Ryan"}
    e.synth("again", RYAN)
    assert len(ref_env.loads) == 1  # model stays loaded


def test_reference_cpu_uses_float32_and_unloads_previous(ref_env: _Env, tmp_path: Path):
    e = ReferenceEngine(_models(tmp_path, "0.6B"), size="0.6B", device="cpu")
    e.synth("hi", RYAN)
    assert ref_env.loads[0][1] == {"device_map": "cpu", "dtype": "f32", "attn_implementation": "sdpa"}
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"")
    e.make_embedding(ref, "hello")
    assert [n for n, _ in ref_env.loads] == ["Qwen3-TTS-12Hz-0.6B-CustomVoice", "Qwen3-TTS-12Hz-0.6B-Base"]
    assert list(e._models) == ["clone"]  # CustomVoice was unloaded on the cpu profile


def test_reference_clone_embedding_round_trip(ref_env: _Env, tmp_path: Path):
    e = ReferenceEngine(_models(tmp_path / "hf"), device="cuda")
    ref = tmp_path / "ref.wav"
    write_wav(ref, np.zeros(2400, dtype=np.float32), 24000)
    lib = VoiceLibrary(tmp_path / "voices")
    voice = lib.add_clone("me", str(ref), "hello world", True, engine=e)
    blob = lib.export_embedding("me")
    assert blob.startswith(REFERENCE_MAGIC)
    ref_env.calls.clear()
    e.synth("some words", voice, speed=1.0, seed=3)
    kind, call = ref_env.calls[0]
    assert kind == "clone" and call["voice_clone_prompt"] == [_PromptItem(ref_text="hello world")]
    assert call["text"] == "some words" and "ref_audio" not in call
    e.synth("more words", voice)
    assert len([k for k, _ in ref_env.calls if k == "clone"]) == 2


def test_reference_design_unloads_design_model_on_cuda(ref_env: _Env, tmp_path: Path):
    e = ReferenceEngine(_models(tmp_path), device="cuda")
    emb, sample = e.design_to_embedding("warm low voice", "English")
    assert emb.startswith(REFERENCE_MAGIC) and len(sample) == 2400
    assert [n.rsplit("-", 1)[1] for n, _ in ref_env.loads] == ["VoiceDesign", "Base"]
    assert "design" not in e._models and "clone" in e._models
    design_call = next(c for k, c in ref_env.calls if k == "design")
    assert design_call["instruct"] == "warm low voice" and design_call["language"] == "English"
    prompt_call = next(c for k, c in ref_env.calls if k == "prompt")
    ref_audio, sr = prompt_call["ref_audio"]
    assert sr == 24000 and len(ref_audio) == 2400


def test_reference_rejects_foreign_embedding(ref_env: _Env, tmp_path: Path):
    e = ReferenceEngine(_models(tmp_path), device="cuda")
    bin_path = tmp_path / "speaker.bin"
    bin_path.write_bytes(b"\x00" * 64)
    with pytest.raises(NsError) as ei:
        e.synth("hi", VoiceSpec(name="v", mode="clone", embedding_path=str(bin_path)))
    assert ei.value.code == "NS-E311"  # gap 20: a voice made by another engine, with the re-create hint
    assert "re-create" in ei.value.definition.hint.lower()


def test_qwentts_rejects_reference_embedding(tmp_path: Path):
    bin_path = tmp_path / "speaker.bin"
    bin_path.write_bytes(REFERENCE_MAGIC + b"xyz")
    e = QwenttsEngine(tmp_path / "bin", tmp_path)
    with pytest.raises(NsError) as ei:
        e.synth("hi", VoiceSpec(name="v", mode="clone", embedding_path=str(bin_path)))
    assert ei.value.code == "NS-E311"


def test_reference_missing_model_and_missing_torch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    e = ReferenceEngine(tmp_path, device="cuda")
    assert e.missing_model("preset") == "Qwen3-TTS-12Hz-1.7B-CustomVoice"
    assert e.missing_model("design") == "Qwen3-TTS-12Hz-1.7B-VoiceDesign"
    with pytest.raises(NsError) as ei:
        e.load()
    assert ei.value.code == "NS-E202" and ei.value.data["missing_model"].endswith("CustomVoice")
    _models(tmp_path)
    assert e.missing_model("design") is None
    monkeypatch.setitem(sys.modules, "torch", None)
    with pytest.raises(NsError) as ei2:
        ReferenceEngine(tmp_path, device="cuda").load()
    assert ei2.value.code == "NS-E202" and "torch" in ei2.value.detail


def test_reference_load_failure_is_wrapped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    class Boom:
        @classmethod
        def from_pretrained(cls, path: str, **kw: Any) -> None:
            raise RuntimeError("CUDA out of memory")

    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(bfloat16="b", float32="f"))
    monkeypatch.setitem(sys.modules, "qwen_tts", types.SimpleNamespace(Qwen3TTSModel=Boom))
    with pytest.raises(NsError) as ei:
        ReferenceEngine(_models(tmp_path), device="cuda").load()
    assert ei.value.code == "NS-E202" and "out of memory" in ei.value.detail


def test_cpu_profile_runs_qwentts_on_the_cpu(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("NS_TTS_FAKE", raising=False)
    e = select_engine(TtsCfg(device="cpu", model_dir=str(tmp_path / "m")), Paths(tmp_path))
    assert isinstance(e, QwenttsEngine) and e.device == "CPU" and e.size == "0.6b"
    assert e.models_dir == tmp_path / "m"


def test_installer_downloads_where_the_reference_engine_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Row 4 of the review: the installer wrote models\tts\hf\..., the engine looked in models\tts\..."""
    from installer.stage2.fallback import fallback_items
    from installer.stage2.runtime_cfg import build_runtime
    from installer.stage2.testing import example_answers, make_test_context

    monkeypatch.delenv("NS_TTS_FAKE", raising=False)
    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True)
    rt = build_runtime(ctx)
    rt.tts.engine = "reference"
    engine = select_engine(rt.tts, ctx.paths)
    assert isinstance(engine, ReferenceEngine)
    dests = {item.dest.parent for item in fallback_items(ctx)}
    assert dests == {engine.model_dir("preset"), engine.model_dir("clone")}
    assert engine.models_dir == ctx.paths.models_tts_hf
