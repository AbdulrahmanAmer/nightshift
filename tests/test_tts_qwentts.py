import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from ns_common.errors import NsError
from ns_tts import engine as engine_mod
from ns_tts.audio import write_wav
from ns_tts.engine import (
    QWENTTS_FLAGS,
    TALKER_KIND,
    TALKER_TEMPLATE,
    TOKENIZER_FILE,
    QwenttsEngine,
)
from ns_tts.voices import SAMPLE_TEXT, VoiceSpec

CUSTOM = "qwen-talker-1.7b-customvoice-Q8_0.gguf"
BASE = "qwen-talker-1.7b-base-Q8_0.gguf"
DESIGN = "qwen-talker-1.7b-voicedesign-Q8_0.gguf"
RYAN = VoiceSpec(name="Ryan", mode="preset", speaker="Ryan")


def arg(cmd: list[str], key: str) -> str:
    flag = QWENTTS_FLAGS[key]
    assert flag is not None and flag in cmd, f"{key} flag missing from {cmd}"
    return cmd[cmd.index(flag) + 1]


def has(cmd: list[str], key: str) -> bool:
    flag = QWENTTS_FLAGS[key]
    return flag is not None and flag in cmd


class FakeRun:
    """Stands in for subprocess.run: parses the command line with QWENTTS_FLAGS and writes the outputs."""

    def __init__(
        self,
        *,
        sr: int = 22050,
        seconds: float = 0.5,
        returncode: int = 0,
        stderr: str = "",
        raises: BaseException | None = None,
        write_out: bool = True,
    ) -> None:
        self.sr, self.seconds, self.returncode = sr, seconds, returncode
        self.stderr, self.raises, self.write_out = stderr, raises, write_out
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []
        self.ref_audio_existed: list[bool] = []

    def _audio(self) -> np.ndarray:
        n = int(self.sr * self.seconds)
        return (0.3 * np.sin(2 * np.pi * 220 * np.arange(n) / self.sr)).astype(np.float32)

    def __call__(self, cmd: list[str], **kwargs: Any) -> Any:
        self.calls.append(list(cmd))
        self.kwargs.append(kwargs)
        if self.raises:
            raise self.raises
        if self.returncode:
            return SimpleNamespace(returncode=self.returncode, stdout="", stderr=self.stderr)
        if self.write_out:
            if has(cmd, "out"):
                write_wav(Path(arg(cmd, "out")), self._audio(), self.sr)
            if has(cmd, "batch_file"):
                lines = Path(arg(cmd, "batch_file")).read_text(encoding="utf-8").splitlines()
                pattern = QWENTTS_FLAGS["batch_out_name"] or ""
                for i in range(len(lines)):
                    write_wav(Path(arg(cmd, "out_dir")) / pattern.format(index=i), self._audio(), self.sr)
            if has(cmd, "save_embedding"):
                Path(arg(cmd, "save_embedding")).write_bytes(b"EMBED" * 10)
        if has(cmd, "ref_audio"):
            self.ref_audio_existed.append(Path(arg(cmd, "ref_audio")).is_file())
        return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def models(tmp_path: Path) -> Path:
    d = tmp_path / "models"
    d.mkdir()
    for name in (CUSTOM, BASE, DESIGN, TOKENIZER_FILE):
        (d / name).write_bytes(b"gguf")
    return d


@pytest.fixture
def eng(tmp_path: Path, models: Path) -> QwenttsEngine:
    exe = tmp_path / "bin" / "qwentts" / "qwentts.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    return QwenttsEngine(exe, models, timeout_s=33)


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def test_preset_command_line_is_built_from_the_flag_table(eng: QwenttsEngine, run: FakeRun, models: Path):
    x = eng.synth("Hello there friend.", RYAN, seed=11)
    (cmd,) = run.calls
    assert cmd[0] == str(eng.exe)
    assert arg(cmd, "talker") == str(models / CUSTOM)
    assert arg(cmd, "tokenizer") == str(models / TOKENIZER_FILE)
    assert arg(cmd, "text") == "Hello there friend."
    assert arg(cmd, "speaker") == "Ryan"
    assert arg(cmd, "language") == "English"
    assert arg(cmd, "seed") == "11"
    assert not has(cmd, "speaker_embedding") and not has(cmd, "ref_audio")
    assert run.kwargs[0]["timeout"] == 33 and "shell" not in run.kwargs[0]
    assert x.dtype == np.float32 and len(x) == int(22050 * 0.5)
    assert eng.sr == 22050  # sample rate comes from the WAV that was read back


def test_no_seed_flag_without_seed(eng: QwenttsEngine, run: FakeRun):
    eng.synth("Hello there friend.", RYAN)
    assert not has(run.calls[0], "seed")


def test_embedding_voice_uses_base_talker(eng: QwenttsEngine, run: FakeRun, models: Path, tmp_path: Path):
    emb = tmp_path / "speaker.bin"
    emb.write_bytes(b"EMB")
    voice = VoiceSpec(name="me", mode="clone", embedding_path=str(emb), language="French")
    eng.synth("Bonjour.", voice)
    cmd = run.calls[0]
    assert arg(cmd, "talker") == str(models / BASE)
    assert arg(cmd, "speaker_embedding") == str(emb)
    assert arg(cmd, "language") == "French"
    assert not has(cmd, "speaker")
    design = VoiceSpec(name="d", mode="design", embedding_path=str(emb))
    eng.synth("Hi.", design)
    assert arg(run.calls[1], "talker") == str(models / BASE)  # designed voices are embeddings too


def test_embedding_voice_without_file_is_an_error(eng: QwenttsEngine, run: FakeRun):
    with pytest.raises(NsError) as ei:
        eng.synth("Hi.", VoiceSpec(name="me", mode="clone"))
    assert ei.value.code == "NS-E301" and not run.calls


def test_flags_come_only_from_the_table(
    eng: QwenttsEngine, run: FakeRun, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setitem(QWENTTS_FLAGS, "speaker", "--spk")
    monkeypatch.setitem(QWENTTS_FLAGS, "talker", "-m")
    monkeypatch.setitem(QWENTTS_FLAGS, "seed", None)
    eng.synth("Hello there friend.", RYAN, seed=5)
    cmd = run.calls[0]
    assert "--spk" in cmd and "-m" in cmd and "--seed" not in cmd and "5" not in cmd
    monkeypatch.setitem(QWENTTS_FLAGS, "text", None)
    with pytest.raises(NsError) as ei:
        eng.synth("Hello.", RYAN)
    assert ei.value.code == "NS-E202" and "text" in ei.value.detail


def test_speed_resamples_the_result(eng: QwenttsEngine, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(subprocess, "run", FakeRun(sr=24000, seconds=1.0))
    x = eng.synth("Hello there friend.", RYAN, speed=2.0)
    assert abs(len(x) - 12000) <= 2


def test_process_failures_become_ns_errors(eng: QwenttsEngine, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(subprocess, "run", FakeRun(returncode=3, stderr="line1\nCUDA error: out of memory"))
    with pytest.raises(NsError) as ei:
        eng.synth("Hi there.", RYAN)
    assert ei.value.code == "NS-E202" and "exited 3" in ei.value.detail and "out of memory" in ei.value.detail

    monkeypatch.setattr(subprocess, "run", FakeRun(raises=subprocess.TimeoutExpired(["x"], 33)))
    with pytest.raises(NsError) as ei:
        eng.synth("Hi there.", RYAN)
    assert ei.value.code == "NS-E202" and "timed out" in ei.value.detail

    monkeypatch.setattr(subprocess, "run", FakeRun(raises=FileNotFoundError("nope")))
    with pytest.raises(NsError) as ei:
        eng.synth("Hi there.", RYAN)
    assert ei.value.code == "NS-E202" and "cannot start" in ei.value.detail

    monkeypatch.setattr(subprocess, "run", FakeRun(write_out=False))
    with pytest.raises(NsError) as ei:
        eng.synth("Hi there.", RYAN)
    assert ei.value.code == "NS-E202" and "no output" in ei.value.detail


def test_unreadable_wav_is_an_error(eng: QwenttsEngine, monkeypatch: pytest.MonkeyPatch):
    def junk(cmd: list[str], **kw: Any) -> Any:
        Path(arg(cmd, "out")).write_bytes(b"not a wav")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", junk)
    with pytest.raises(NsError) as ei:
        eng.synth("Hi there.", RYAN)
    assert ei.value.code == "NS-E202" and "WAV" in ei.value.detail


def test_make_embedding_uses_base_talker_and_returns_saved_bytes(
    eng: QwenttsEngine, run: FakeRun, models: Path, tmp_path: Path
):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFF")
    blob = eng.make_embedding(ref, "what I said", "English")
    assert blob == b"EMBED" * 10
    cmd = run.calls[0]
    assert arg(cmd, "talker") == str(models / BASE)
    assert arg(cmd, "ref_audio") == str(ref) and arg(cmd, "ref_text") == "what I said"
    assert has(cmd, "save_embedding")


def test_make_embedding_requires_the_saved_file(
    eng: QwenttsEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(subprocess, "run", FakeRun(write_out=False, seconds=0.1))
    with pytest.raises(NsError) as ei:
        eng.make_embedding(tmp_path / "ref.wav", "words")
    assert ei.value.code == "NS-E202" and "embedding" in ei.value.detail


def test_design_renders_then_reencodes_through_base(
    eng: QwenttsEngine, run: FakeRun, models: Path
):
    blob, sample = eng.design_to_embedding("deep calm male narrator", "English")
    first, second = run.calls
    assert arg(first, "talker") == str(models / DESIGN)
    assert arg(first, "instruct") == "deep calm male narrator" and arg(first, "text") == SAMPLE_TEXT
    assert arg(second, "talker") == str(models / BASE)
    assert arg(second, "ref_audio") == arg(first, "out") and arg(second, "ref_text") == SAMPLE_TEXT
    assert run.ref_audio_existed == [True]  # the render was still on disk for the second run
    assert blob == b"EMBED" * 10 and len(sample) == int(22050 * 0.5)


def test_design_without_voicedesign_gguf_names_the_missing_file(
    eng: QwenttsEngine, run: FakeRun, models: Path
):
    (models / DESIGN).unlink()
    assert eng.missing_model("design") == DESIGN
    with pytest.raises(NsError) as ei:
        eng.design_to_embedding("x")
    assert ei.value.code == "NS-E202" and ei.value.data["missing_model"] == DESIGN
    assert not run.calls


def test_missing_model_order(tmp_path: Path):
    models = tmp_path / "m"
    models.mkdir()
    e = QwenttsEngine(tmp_path / "q.exe", models)
    assert e.missing_model("preset") == CUSTOM
    (models / CUSTOM).write_bytes(b"x")
    assert e.missing_model("preset") == TOKENIZER_FILE
    (models / TOKENIZER_FILE).write_bytes(b"x")
    assert e.missing_model("preset") is None
    assert e.missing_model("clone") == BASE
    (models / BASE).write_bytes(b"x")
    assert e.missing_model("clone") is None
    assert e.missing_model("design") == DESIGN


def test_load_checks_binary_and_models(tmp_path: Path, models: Path):
    e = QwenttsEngine(tmp_path / "missing.exe", models)
    with pytest.raises(NsError) as ei:
        e.load()
    assert ei.value.code == "NS-E202" and "not found" in ei.value.detail and not e.loaded
    exe = tmp_path / "q.exe"
    exe.write_bytes(b"MZ")
    (models / CUSTOM).unlink()
    with pytest.raises(NsError) as ei2:
        QwenttsEngine(exe, models).load()
    assert ei2.value.data["missing_model"] == CUSTOM
    (models / CUSTOM).write_bytes(b"x")
    ok = QwenttsEngine(exe, models)
    ok.load()
    assert ok.loaded and ok.name == "qwentts"


def test_batch_runs_one_process(eng: QwenttsEngine, run: FakeRun):
    out = eng.synth_batch(["First one here.", "Second\none there.", "Third."], RYAN, seed=2)
    assert len(run.calls) == 1 and len(out) == 3
    cmd = run.calls[0]
    assert not has(cmd, "text") and not has(cmd, "out")
    assert arg(cmd, "speaker") == "Ryan" and arg(cmd, "seed") == "2"


def test_batch_writes_one_line_per_text(eng: QwenttsEngine, monkeypatch: pytest.MonkeyPatch):
    seen: list[list[str]] = []

    class Spy(FakeRun):
        def __call__(self, cmd: list[str], **kw: Any) -> Any:
            seen.append(Path(arg(cmd, "batch_file")).read_text(encoding="utf-8").splitlines())
            return super().__call__(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", Spy())
    eng.synth_batch(["First one here.", "Second\n  one there."], RYAN)
    assert seen == [["First one here.", "Second one there."]]


def test_batch_without_batch_flags_falls_back_to_one_process_per_chunk(
    eng: QwenttsEngine, run: FakeRun, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setitem(QWENTTS_FLAGS, "batch_file", None)
    out = eng.synth_batch(["a b c", "d e f"], RYAN)
    assert len(out) == 2 and len(run.calls) == 2
    assert eng.synth_batch([], RYAN) == []


def test_model_file_names_match_manifest(repo_root: Path):
    text = (repo_root / "manifest.json").read_text(encoding="utf-8")
    assert set(TALKER_KIND) == {"preset", "clone", "design"}
    for kind in TALKER_KIND.values():
        assert TALKER_TEMPLATE.format(size="1.7b", kind=kind) in text
    assert TOKENIZER_FILE in text
    assert TALKER_TEMPLATE.format(size="0.6b", kind="customvoice") in text


def test_flag_table_has_every_key_the_engine_reads():
    required = {"talker", "tokenizer", "text", "out"}
    optional = {
        "speaker", "language", "instruct", "ref_audio", "ref_text", "save_embedding",
        "speaker_embedding", "seed", "batch_file", "out_dir", "batch_out_name",
    }  # fmt: skip
    assert set(QWENTTS_FLAGS) == required | optional
    assert all(QWENTTS_FLAGS[k] for k in required)
    assert engine_mod.QWENTTS_FLAGS is QWENTTS_FLAGS
