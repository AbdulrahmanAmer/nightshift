"""The qwentts.cpp contract: exact command lines, stdin, output files, voice files, error codes.

`tests/fixtures/tts/fake_qwen.py` stands in for `qwen-tts.exe` and `qwen-codec.exe`. It accepts only the flags
the pinned upstream `--help` documents, so a guessed flag fails the test the way it fails on the GPU PC.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from ns_common import ffmpeg
from ns_common.errors import NsError
from ns_tts import engine as engine_mod
from ns_tts.audio import read_wav, write_wav
from ns_tts.engine import (
    EXE_SUFFIX,
    QWEN_CODEC,
    QWEN_TTS,
    REFERENCE_MAGIC,
    TALKER_KIND,
    TALKER_TEMPLATE,
    TOKENIZER_FILE,
    QwenttsEngine,
)
from ns_tts.voices import SAMPLE_TEXT, VoiceLibrary, VoiceSpec

SHIM = Path(__file__).parent / "fixtures" / "tts" / "fake_qwen.py"
CUSTOM = "qwen-talker-1.7b-customvoice-Q8_0.gguf"
BASE = "qwen-talker-1.7b-base-Q8_0.gguf"
DESIGN = "qwen-talker-1.7b-voicedesign-Q8_0.gguf"
RYAN = VoiceSpec(name="Ryan", mode="preset", speaker="Ryan")


def launcher() -> dict[str, list[str]]:
    return {tool: [sys.executable, str(SHIM), tool] for tool in (QWEN_TTS, QWEN_CODEC)}


@pytest.fixture
def models(tmp_path: Path) -> Path:
    d = tmp_path / "models"
    d.mkdir()
    for name in (CUSTOM, BASE, DESIGN, TOKENIZER_FILE):
        (d / name).write_bytes(b"gguf")
    return d


@pytest.fixture
def calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The JSON-lines file the stand-in appends every accepted call to."""
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("NS_FAKE_QWEN_LOG", str(log))
    monkeypatch.delenv("NS_FAKE_QWEN_FAIL", raising=False)
    return log


@pytest.fixture
def eng(tmp_path: Path, models: Path, calls: Path) -> QwenttsEngine:
    return QwenttsEngine(tmp_path / "bin", models, timeout_s=60, launcher=launcher())


def ok_without_output(cmd: list[str], **kw: Any) -> Any:
    """A subprocess.run that succeeds and writes nothing."""
    return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


def recorded(log: Path) -> list[dict[str, Any]]:
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def voice_folder(
    root: Path, name: str = "me", *, spk: bool = True, rvq: bool = True, ref: bool = True
) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    if spk:
        (folder / "speaker.spk").write_bytes(b"\x01" * 16)
    if rvq:
        (folder / "speaker.rvq").write_bytes(b"RVQ")
    if ref:
        (folder / "ref.txt").write_text("what I said\n", encoding="utf-8")
    return folder


def ref_wav(path: Path, seconds: float = 1.0) -> Path:
    n = int(24000 * seconds)
    write_wav(path, (0.3 * np.sin(2 * np.pi * 220 * np.arange(n) / 24000)).astype(np.float32), 24000)
    return path


# ---------------------------------------------------------------- the contract


def test_executable_names_are_the_ones_the_release_zip_contains(repo_root: Path, tmp_path: Path):
    workflow = (repo_root / ".github" / "workflows" / "build-qwentts.yml").read_text(encoding="utf-8")
    for tool in (QWEN_TTS, QWEN_CODEC):
        assert f"'{tool}{EXE_SUFFIX}'" in workflow
    assert "qwentts.exe" not in workflow.replace("Nightshift-VoiceEngine-win-cuda", "")
    e = QwenttsEngine(tmp_path / "bin" / "qwentts", tmp_path)
    assert e.exe(QWEN_TTS) == tmp_path / "bin" / "qwentts" / "qwen-tts.exe"
    assert e.exe(QWEN_CODEC) == tmp_path / "bin" / "qwentts" / "qwen-codec.exe"
    assert e.tts_command("preset", out=tmp_path / "o.wav")[0].endswith("qwen-tts.exe")


def test_preset_command_line_and_stdin(eng: QwenttsEngine, calls: Path, models: Path):
    x = eng.synth("Hello there friend.", RYAN, seed=11)
    (call,) = recorded(calls)
    assert call["tool"] == "qwen-tts"
    argv = call["argv"]
    assert argv[:-1] == [
        "--model", str(models / CUSTOM),
        "--codec", str(models / TOKENIZER_FILE),
        "--speaker", "ryan",
        "--lang", "English",
        "--seed", "11",
        "-o",
    ]  # fmt: skip
    assert Path(argv[-1]).name == "out.wav"
    assert call["stdin"] == "Hello there friend."  # the text travels on stdin, never in argv
    assert x.dtype == np.float32 and len(x) == int(24000 * 0.6) and eng.sr == 24000


@pytest.mark.parametrize(
    "name,speaker", [("Uncle_Fu", "uncle_fu"), ("Ono_Anna", "ono_anna"), ("Vivian", "vivian")]
)
def test_preset_speaker_names_are_lowercase(eng: QwenttsEngine, calls: Path, name: str, speaker: str):
    eng.synth("Hello there friend.", VoiceSpec(name=name, mode="preset", speaker=name))
    argv = recorded(calls)[0]["argv"]
    assert argv[argv.index("--speaker") + 1] == speaker


def test_no_seed_flag_without_seed_and_device_only_when_set(eng: QwenttsEngine, calls: Path, models: Path):
    eng.synth("Hello there friend.", RYAN)
    assert "--seed" not in recorded(calls)[0]["argv"] and "--device" not in recorded(calls)[0]["argv"]
    eng.device = "CPU"
    eng.synth("Hello there friend.", RYAN)
    argv = recorded(calls)[1]["argv"]
    assert argv[argv.index("--codec") + 2 : argv.index("--codec") + 4] == ["--device", "CPU"]


def test_text_with_odd_characters_survives_stdin(eng: QwenttsEngine, calls: Path):
    text = 'She said "stop", then left: café — ñ 你好.'
    eng.synth(text, RYAN)
    assert recorded(calls)[0]["stdin"] == text
    assert text not in " ".join(recorded(calls)[0]["argv"])


def test_stored_voice_uses_base_talker_and_reference_files(
    eng: QwenttsEngine, calls: Path, models: Path, tmp_path: Path
):
    folder = voice_folder(tmp_path / "voices")
    voice = VoiceSpec(name="me", mode="clone", embedding_path=str(folder / "speaker.spk"), language="French")
    eng.synth("Bonjour.", voice, seed=3)
    argv = recorded(calls)[0]["argv"]
    assert argv[:-1] == [
        "--model", str(models / BASE),
        "--codec", str(models / TOKENIZER_FILE),
        "--ref-spk", str(folder / "speaker.spk"),
        "--ref-rvq", str(folder / "speaker.rvq"),
        "--ref-text", str(folder / "ref.txt"),
        "--lang", "French",
        "--seed", "3",
        "-o",
    ]  # fmt: skip
    design = VoiceSpec(name="d", mode="design", embedding_path=str(folder / "speaker.spk"))
    eng.synth("Hi.", design)
    assert recorded(calls)[1]["argv"][1] == str(models / BASE)  # designed voices are stored as clones


def test_make_embedding_runs_qwen_codec_and_returns_the_three_files(
    eng: QwenttsEngine, calls: Path, models: Path, tmp_path: Path
):
    ref = ref_wav(tmp_path / "ref.wav")
    files = eng.make_embedding(ref, "  what I said  ", "English")
    assert isinstance(files, dict) and set(files) == {"speaker.spk", "speaker.rvq", "ref.txt"}
    assert files["ref.txt"] == b"what I said\n" and files["speaker.spk"] and files["speaker.rvq"]
    (call,) = recorded(calls)
    assert call["tool"] == "qwen-codec"
    expected = ["--model", str(models / TOKENIZER_FILE), "--talker", str(models / BASE), "-i"]
    assert call["argv"][:-1] == expected
    assert Path(call["argv"][-1]).name == "speaker.wav"


def test_design_renders_with_voicedesign_then_encodes_through_base(
    eng: QwenttsEngine, calls: Path, models: Path
):
    files, sample = eng.design_to_embedding("male, young adult, moderate pitch", "English")
    render, encode = recorded(calls)
    assert render["tool"] == "qwen-tts" and render["stdin"] == SAMPLE_TEXT
    assert render["argv"][:-1] == [
        "--model", str(models / DESIGN),
        "--codec", str(models / TOKENIZER_FILE),
        "--instruct", "male, young adult, moderate pitch",
        "--lang", "English",
        "-o",
    ]  # fmt: skip
    assert encode["tool"] == "qwen-codec" and encode["argv"][-1] == render["argv"][-1]  # the render itself
    assert encode["argv"][3] == str(models / BASE) and Path(encode["argv"][-1]).name == "render.wav"
    assert isinstance(files, dict) and files["ref.txt"] == (SAMPLE_TEXT + "\n").encode()
    assert set(files) == {"speaker.spk", "speaker.rvq", "ref.txt"} and len(sample) > 0


def test_voice_library_round_trip_with_the_qwentts_files(eng: QwenttsEngine, calls: Path, tmp_path: Path):
    lib = VoiceLibrary(tmp_path / "voices")
    v = lib.add_clone("me", str(ref_wav(tmp_path / "ref.wav")), "what I said", True, engine=eng)
    folder = tmp_path / "voices" / "me"
    assert {p.name for p in folder.iterdir()} == {
        "voice.json", "speaker.spk", "speaker.rvq", "ref.txt", "sample.wav",
    }  # fmt: skip
    meta = json.loads((folder / "voice.json").read_text(encoding="utf-8"))
    assert meta["engine"] == "qwentts" and meta["files"] == ["ref.txt", "speaker.rvq", "speaker.spk"]
    assert meta["ref_text"] == "what I said" and meta["mode"] == "clone"
    assert v.embedding_path == str(folder / "speaker.spk")
    assert VoiceLibrary(tmp_path / "voices").get("me").embedding_path == str(folder / "speaker.spk")
    audio, sr = read_wav(folder / "sample.wav")
    assert sr == 24000 and len(audio) > 0
    assert len(eng.synth("Hello there friend.", lib.get("me"))) > 0
    # export: several files travel as one zip
    assert lib.export_embedding("me")[:2] == b"PK"
    lib.add_design("velvet", "deep calm male narrator", engine=eng)
    assert (tmp_path / "voices" / "velvet" / "speaker.rvq").is_file()


# ---------------------------------------------------------------- the stand-in itself rejects the old guesses


@pytest.mark.parametrize(
    "bad",
    [
        ["--text", "hello"],  # the text goes to stdin
        ["--out", "x.wav"],  # the flag is -o
        ["--tokenizer", "t.gguf"],  # the flag is --codec
        ["--language", "English"],  # the flag is --lang
        ["--ref-audio", "r.wav"],  # the flag is --ref-wav
        ["--speaker-embedding", "e.bin"],
        ["--batch-file", "b.txt"],
    ],
)
def test_the_stand_in_rejects_flags_the_real_tool_does_not_have(models: Path, tmp_path: Path, bad: list[str]):
    ok = ["--model", str(models / CUSTOM), "--codec", str(models / TOKENIZER_FILE), "--speaker", "ryan"]
    proc = subprocess.run(
        [sys.executable, str(SHIM), "qwen-tts", *ok, *bad, "-o", str(tmp_path / "o.wav")],
        input=b"hello there", capture_output=True, check=False, timeout=60,
    )  # fmt: skip
    assert proc.returncode == 1 and b"unknown argument" in proc.stderr


def test_the_stand_in_enforces_the_upstream_mode_rules(models: Path, tmp_path: Path):
    def run(model: str, *extra: str, stdin: bytes = b"hello there") -> int:
        cmd = [sys.executable, str(SHIM), "qwen-tts", "--model", str(models / model)]
        cmd += ["--codec", str(models / TOKENIZER_FILE), *extra, "-o", str(tmp_path / "o.wav")]
        return subprocess.run(cmd, input=stdin, capture_output=True, check=False, timeout=60).returncode

    assert run(CUSTOM, "--speaker", "ryan") == 0
    assert run(CUSTOM) == 1  # CustomVoice needs a speaker
    assert run(CUSTOM, "--speaker", "Ryan") == 1  # speaker names are lowercase
    assert run(CUSTOM, "--speaker", "ryan", stdin=b"") == 1  # empty stdin
    assert run(BASE, "--instruct", "x") == 1  # --instruct is rejected for Base
    assert run(DESIGN) == 1  # VoiceDesign needs --instruct
    assert run(DESIGN, "--instruct", "calm") == 0


# ---------------------------------------------------------------- error codes


def test_synthesis_failures_are_ns_e310(eng: QwenttsEngine, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NS_FAKE_QWEN_FAIL", "qwen-tts")
    with pytest.raises(NsError) as ei:
        eng.synth("Hi there.", RYAN)
    assert ei.value.code == "NS-E310" and "exited 3" in ei.value.detail and "out of memory" in ei.value.detail

    def raising(exc: BaseException) -> Any:
        def run(cmd: list[str], **kw: Any) -> Any:
            raise exc

        return run

    monkeypatch.delenv("NS_FAKE_QWEN_FAIL")
    monkeypatch.setattr(subprocess, "run", raising(subprocess.TimeoutExpired(["x"], 33)))
    with pytest.raises(NsError) as ei2:
        eng.synth("Hi there.", RYAN)
    assert ei2.value.code == "NS-E310" and "timed out" in ei2.value.detail
    monkeypatch.setattr(subprocess, "run", raising(FileNotFoundError("nope")))
    with pytest.raises(NsError) as ei3:
        eng.synth("Hi there.", RYAN)
    assert ei3.value.code == "NS-E310" and "cannot start" in ei3.value.detail


def test_missing_or_unreadable_output_is_ns_e310(eng: QwenttsEngine, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(subprocess, "run", ok_without_output)
    with pytest.raises(NsError) as ei:
        eng.synth("Hi there.", RYAN)
    assert ei.value.code == "NS-E310" and "no output" in ei.value.detail

    def junk(cmd: list[str], **kw: Any) -> Any:
        Path(cmd[-1]).write_bytes(b"not a wav")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", junk)
    with pytest.raises(NsError) as ei2:
        eng.synth("Hi there.", RYAN)
    assert ei2.value.code == "NS-E310" and "WAV" in ei2.value.detail


def test_voice_building_failures_are_ns_e315(
    eng: QwenttsEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    ref = ref_wav(tmp_path / "ref.wav")
    monkeypatch.setenv("NS_FAKE_QWEN_FAIL", "qwen-codec")
    with pytest.raises(NsError) as ei:
        eng.make_embedding(ref, "words")
    assert ei.value.code == "NS-E315" and "exited 3" in ei.value.detail
    monkeypatch.setenv("NS_FAKE_QWEN_FAIL", "qwen-tts")
    with pytest.raises(NsError) as ei2:
        eng.design_to_embedding("calm voice")
    assert ei2.value.code == "NS-E315"
    monkeypatch.delenv("NS_FAKE_QWEN_FAIL")
    with pytest.raises(NsError) as ei3:
        eng.make_embedding(ref, "   ")
    assert ei3.value.code == "NS-E315" and "transcript" in ei3.value.detail
    with pytest.raises(NsError) as ei4:
        eng.make_embedding(tmp_path / "missing.wav", "words")
    assert ei4.value.code == "NS-E315"
    short = ref_wav(tmp_path / "short.wav", seconds=0.01)  # shorter than one codec frame: qwen-codec refuses
    with pytest.raises(NsError) as ei5:
        eng.make_embedding(short, "words")
    assert ei5.value.code == "NS-E315"


def test_codec_that_writes_nothing_is_ns_e315(
    eng: QwenttsEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(subprocess, "run", ok_without_output)
    with pytest.raises(NsError) as ei:
        eng.make_embedding(ref_wav(tmp_path / "ref.wav"), "words")
    assert ei.value.code == "NS-E315" and "speaker.spk" in ei.value.detail


def test_unusable_stored_voices_are_ns_e311_and_start_no_process(
    eng: QwenttsEngine, calls: Path, tmp_path: Path
):
    with pytest.raises(NsError) as none:
        eng.synth("Hi.", VoiceSpec(name="me", mode="clone"))
    assert none.value.code == "NS-E311"
    bin_path = tmp_path / "speaker.bin"
    bin_path.write_bytes(REFERENCE_MAGIC + b"xyz")
    with pytest.raises(NsError) as ref:
        eng.synth("Hi.", VoiceSpec(name="v", mode="clone", embedding_path=str(bin_path)))
    assert ref.value.code == "NS-E311" and "reference engine" in ref.value.detail
    half = voice_folder(tmp_path / "voices", "half", rvq=False)
    with pytest.raises(NsError) as no_rvq:
        eng.synth("Hi.", VoiceSpec(name="half", mode="clone", embedding_path=str(half / "speaker.spk")))
    assert no_rvq.value.code == "NS-E311" and "speaker.rvq" in no_rvq.value.detail
    assert recorded(calls) == []


def test_startup_problems_stay_ns_e202(tmp_path: Path, models: Path):
    e = QwenttsEngine(tmp_path / "bin", models)
    with pytest.raises(NsError) as ei:
        e.load()
    assert ei.value.code == "NS-E202" and "qwen-tts.exe" in ei.value.detail and not e.loaded
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "qwen-tts.exe").write_bytes(b"MZ")
    (models / CUSTOM).unlink()
    with pytest.raises(NsError) as ei2:
        QwenttsEngine(tmp_path / "bin", models).load()
    assert ei2.value.code == "NS-E202" and ei2.value.data["missing_model"] == CUSTOM
    (models / CUSTOM).write_bytes(b"x")
    ok = QwenttsEngine(tmp_path / "bin", models)
    ok.load()
    assert ok.loaded and ok.name == "qwentts"


def test_design_without_voicedesign_gguf_names_the_missing_file(
    eng: QwenttsEngine, calls: Path, models: Path
):
    (models / DESIGN).unlink()
    assert eng.missing_model("design") == DESIGN
    with pytest.raises(NsError) as ei:
        eng.design_to_embedding("x")
    assert ei.value.code == "NS-E314" and ei.value.data["missing_model"] == DESIGN
    assert "Get voice design" in ei.value.detail
    assert recorded(calls) == []


def test_missing_model_order(tmp_path: Path):
    models = tmp_path / "m"
    models.mkdir()
    e = QwenttsEngine(tmp_path, models)
    assert e.missing_model("preset") == CUSTOM
    (models / CUSTOM).write_bytes(b"x")
    assert e.missing_model("preset") == TOKENIZER_FILE
    (models / TOKENIZER_FILE).write_bytes(b"x")
    assert e.missing_model("preset") is None
    assert e.missing_model("clone") == BASE
    (models / BASE).write_bytes(b"x")
    assert e.missing_model("clone") is None
    assert e.missing_model("design") == DESIGN


# ---------------------------------------------------------------- batches and speed


def test_batch_is_one_process_for_all_chunks_in_order(eng: QwenttsEngine, calls: Path):
    out = eng.synth_batch(["First one here.", "Second\none there.", "Third."], RYAN, seed=2)
    assert len(out) == 3 and eng.synth_batch([], RYAN) == []
    (rec,) = recorded(calls)  # one process, one line per chunk (a newline inside a chunk is a space)
    assert rec["stdin"] == "First one here.\nSecond one there.\nThird.\n"
    assert rec["argv"][rec["argv"].index("--seed") + 1] == "2" and "--stream-by-line" in rec["argv"]


def test_speed_goes_through_the_pitch_safe_stretch(eng: QwenttsEngine, monkeypatch: pytest.MonkeyPatch):
    seen: list[tuple[int, float]] = []

    def fake_stretch(x: np.ndarray, sr: int, speed: float) -> np.ndarray:
        seen.append((sr, speed))
        return x

    monkeypatch.setattr(engine_mod, "time_stretch", fake_stretch)
    eng.synth("Hello there friend.", RYAN, speed=0.8)
    assert seen == [(24000, 0.8)]


@pytest.mark.skipif(not ffmpeg.available(), reason="needs ffmpeg")
def test_speed_changes_length_but_not_pitch(eng: QwenttsEngine):
    base = eng.synth("one two three four five six", RYAN, seed=1)
    slow = eng.synth("one two three four five six", RYAN, seed=1, speed=0.8)
    fast = eng.synth("one two three four five six", RYAN, seed=1, speed=1.25)
    assert abs(len(slow) / len(base) - 1.25) < 0.05 and abs(len(fast) / len(base) - 0.8) < 0.05

    def pitch(x: np.ndarray) -> float:
        spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        return float(np.argmax(spec)) * 24000 / len(x)

    assert abs(pitch(slow) - pitch(base)) < 12 and abs(pitch(fast) - pitch(base)) < 12


def test_model_file_names_match_manifest(repo_root: Path):
    text = (repo_root / "manifest.json").read_text(encoding="utf-8")
    assert set(TALKER_KIND) == {"preset", "clone", "design"}
    for kind in TALKER_KIND.values():
        assert TALKER_TEMPLATE.format(size="1.7b", kind=kind) in text
    assert TOKENIZER_FILE in text
    assert TALKER_TEMPLATE.format(size="0.6b", kind="customvoice") in text


def test_e314_clears_without_restarting_the_service(tmp_path: Path, models: Path, calls: Path):
    """`ns models get voicedesign` drops the talker next to the running service: the next request just works.

    The model check is per request (a file test, not a startup snapshot), so no restart or reload is needed.
    """
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ns_tts.server import create_app

    (models / DESIGN).unlink()
    engine = QwenttsEngine(tmp_path / "bin", models, timeout_s=60, launcher=launcher())
    engine.load()
    client = TestClient(create_app(engine, VoiceLibrary(tmp_path / "voices")))
    body = {"name": "velvet", "description": "deep calm male narrator"}
    before = client.post("/v1/voices/design", json=body)
    assert before.status_code == 409 and before.json()["error"] == "NS-E314"
    assert before.json()["missing_model"] == DESIGN
    (models / DESIGN).write_bytes(b"gguf")  # the download finished; the service keeps running
    after = client.post("/v1/voices/design", json=body)
    assert after.status_code == 200, after.text
    assert after.json()["name"] == "velvet"


# ---------------------------------------------------------------- ASCII-only paths (0.1.7, NS-W301)
# qwen-tts.exe and qwen-codec.exe never convert argv to UTF-8, so every path handed to them has to be ASCII.
# `%TEMP%` holds the Windows user name, which may be Arabic: the scratch files live under the install folder.

ARABIC = "\u0645\u0633\u062a\u062e\u062f\u0645"  # "user"


def path_args(argv: list[str]) -> list[str]:
    flags = {"--model", "--codec", "--talker", "--ref-spk", "--ref-rvq", "--ref-text", "-o", "-i"}
    return [v for f, v in zip(argv, argv[1:], strict=False) if f in flags]


@pytest.fixture
def arabic_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`%TEMP%` of a Windows user with an Arabic name."""
    temp = tmp_path / "Users" / ARABIC / "AppData" / "Local" / "Temp"
    temp.mkdir(parents=True)
    for var in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(var, str(temp))
    monkeypatch.setattr("tempfile.tempdir", None)
    return temp


def test_the_stand_in_rejects_a_non_ascii_path_like_the_real_tool(models: Path, tmp_path: Path):
    out = tmp_path / ARABIC / "o.wav"
    out.parent.mkdir()
    cmd = [sys.executable, str(SHIM), "qwen-tts", "--model", str(models / CUSTOM)]
    cmd += ["--codec", str(models / TOKENIZER_FILE), "--speaker", "ryan", "-o", str(out)]
    proc = subprocess.run(cmd, input=b"hello there", capture_output=True, check=False, timeout=60)
    assert proc.returncode == 1 and b"non-ASCII" in proc.stderr


def test_synth_never_uses_the_windows_temp_folder(
    arabic_temp: Path, tmp_path: Path, models: Path, calls: Path
):
    scratch = tmp_path / "home" / "cache" / "tts-tmp"
    eng = QwenttsEngine(tmp_path / "bin", models, timeout_s=60, launcher=launcher(), tmp_dir=scratch)
    assert len(eng.synth("Hello there friend.", RYAN)) > 0
    (call,) = recorded(calls)
    out = Path(call["argv"][-1])
    assert out.name == "out.wav" and scratch in out.parents
    assert all(a.isascii() for a in path_args(call["argv"]))
    assert list(scratch.iterdir()) == []  # cleaned after the call
    assert list(arabic_temp.iterdir()) == []  # nothing was written to %TEMP%


def test_the_scratch_folder_is_cleaned_when_the_tool_fails(
    tmp_path: Path, models: Path, calls: Path, monkeypatch: pytest.MonkeyPatch
):
    scratch = tmp_path / "home" / "cache" / "tts-tmp"
    eng = QwenttsEngine(tmp_path / "bin", models, timeout_s=60, launcher=launcher(), tmp_dir=scratch)
    monkeypatch.setenv("NS_FAKE_QWEN_FAIL", "qwen-tts")
    with pytest.raises(NsError):
        eng.synth("Hello there friend.", RYAN)
    assert scratch.is_dir() and list(scratch.iterdir()) == []


def test_old_scratch_folders_of_a_crashed_run_are_removed(tmp_path: Path, models: Path, calls: Path):
    scratch = tmp_path / "home" / "cache" / "tts-tmp"
    old, fresh = scratch / "c-old", scratch / "c-fresh"
    old.mkdir(parents=True)
    fresh.mkdir()
    (old / "out.wav").write_bytes(b"x")
    stamp = old.stat().st_mtime - 3 * 86400
    os.utime(old, (stamp, stamp))
    eng = QwenttsEngine(tmp_path / "bin", models, timeout_s=60, launcher=launcher(), tmp_dir=scratch)
    eng.synth("Hello there friend.", RYAN)
    assert not old.exists() and fresh.is_dir()


def test_voice_encoding_and_design_use_the_scratch_folder_too(
    arabic_temp: Path, tmp_path: Path, models: Path, calls: Path
):
    scratch = tmp_path / "home" / "cache" / "tts-tmp"
    eng = QwenttsEngine(tmp_path / "bin", models, timeout_s=60, launcher=launcher(), tmp_dir=scratch)
    eng.make_embedding(ref_wav(tmp_path / "ref.wav"), "what I said", "English")
    eng.design_to_embedding("male, young adult, moderate pitch", "English")
    runs = recorded(calls)
    assert [r["tool"] for r in runs] == ["qwen-codec", "qwen-tts", "qwen-codec"]
    for r in runs:
        assert all(a.isascii() for a in path_args(r["argv"]))
        assert all(scratch in Path(a).parents for a in path_args(r["argv"])[-1:])
    assert list(scratch.iterdir()) == [] and list(arabic_temp.iterdir()) == []


@pytest.mark.parametrize("batch", [False, True])
def test_a_voice_stored_outside_ascii_paths_is_copied_before_the_tool_reads_it(
    tmp_path: Path, models: Path, calls: Path, batch: bool
):
    folder = voice_folder(tmp_path / ARABIC, "me")
    voice = VoiceSpec(name="me", mode="clone", embedding_path=str(folder / "speaker.spk"))
    scratch = tmp_path / "home" / "cache" / "tts-tmp"
    eng = QwenttsEngine(tmp_path / "bin", models, timeout_s=60, launcher=launcher(), tmp_dir=scratch)
    if batch:
        out = eng.synth_batch(["First one here.", "Second one there."], voice)
        assert len(out) == 2
    else:
        assert len(eng.synth("Hello there friend.", voice)) > 0
    (call,) = recorded(calls)
    argv = call["argv"]
    assert all(a.isascii() for a in path_args(argv))
    for flag in ("--ref-spk", "--ref-rvq", "--ref-text"):
        assert scratch in Path(argv[argv.index(flag) + 1]).parents
    assert (folder / "speaker.spk").is_file()  # the stored voice itself is untouched
    assert list(scratch.iterdir()) == []


def test_ascii_voice_files_are_passed_as_they_are(tmp_path: Path, models: Path, calls: Path):
    folder = voice_folder(tmp_path / "voices", "me")
    voice = VoiceSpec(name="me", mode="clone", embedding_path=str(folder / "speaker.spk"))
    eng = QwenttsEngine(tmp_path / "bin", models, timeout_s=60, launcher=launcher())
    eng.synth_batch(["First one here.", "Second one there."], voice)
    argv = recorded(calls)[0]["argv"]
    assert argv[argv.index("--ref-spk") + 1] == str(folder / "speaker.spk")


def test_the_service_engine_scratches_under_the_install_folder(tmp_path: Path):
    from ns_common.models import TtsCfg
    from ns_common.paths import Paths
    from ns_tts.engine import select_engine

    paths = Paths(tmp_path / "Nightshift")
    eng = select_engine(TtsCfg(), paths, prefer="qwentts")
    assert isinstance(eng, QwenttsEngine)
    assert eng.tmp_root == paths.home / "cache" / "tts-tmp" == paths.cache_tts_tmp
