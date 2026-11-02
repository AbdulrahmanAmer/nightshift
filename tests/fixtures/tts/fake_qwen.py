"""Stand-in for qwen-tts.exe and qwen-codec.exe (qwentts.cpp), used by tests/test_tts_qwentts.py.

Run as `python fake_qwen.py <qwen-tts|qwen-codec> <real arguments...>`. It accepts exactly the flags the
pinned qwentts.cpp commit documents (docs/ARCHITECTURE.md, "CLI tools", verbatim --help) and rejects all
else with exit code 1, like the real tools: an unknown flag, a missing required flag, the text on the
command line instead of stdin, a mode that the talker GGUF does not support. Every accepted call is
appended as one JSON line {tool, argv, stdin} to the file named by NS_FAKE_QWEN_LOG, so tests can pin the
exact command line.

NS_FAKE_QWEN_FAIL=<tool|all> makes the tool exit 3 with an error on stderr. Output audio is a sine whose
length follows the word count; `qwen-codec` writes small deterministic .rvq / .spk files next to its input.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import sys
import time
import wave
from pathlib import Path

TTS_VALUE_FLAGS = {
    "--model", "--codec", "--device", "--format", "--lang", "--instruct", "--speaker", "--ref-wav",
    "--ref-spk", "--ref-rvq", "--ref-text", "--max-new", "--codec-chunk-dur", "-o", "--seed", "--temp",
    "--top-k", "--top-p", "--rep-pen", "--sub-temp", "--sub-top-k", "--sub-top-p", "--dump",
}  # fmt: skip
TTS_BOOL_FLAGS = {"--greedy", "--stream-by-line", "--no-fa", "--clamp-fp16"}
CODEC_VALUE_FLAGS = {"--model", "--device", "-i", "--talker", "--format"}
DEVICES = {"CUDA0", "Vulkan0", "MTL0", "CPU"}
# qwen3-tts.codec.language_names of the talker GGUF; matched case-insensitively, anything else is fatal
# ("[Prompt] FATAL: unknown language"), "auto" lets the model guess from the text.
LANGUAGES = {
    "auto", "chinese", "english", "german", "italian", "portuguese", "spanish", "japanese", "korean",
    "french", "russian",
}
SPEAKERS = {"serena", "vivian", "uncle_fu", "ryan", "aiden", "ono_anna", "sohee", "eric", "dylan"}
HOP = 1920  # samples per codec frame at 24 kHz


def die(msg: str, code: int = 1) -> None:
    sys.stderr.write(f"[fake-qwen] ERROR: {msg}\n")
    raise SystemExit(code)


def parse(args: list[str], values: set[str], bools: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a in bools:
            out[a] = "1"
            i += 1
        elif a in values:
            if i + 1 >= len(args):
                die(f"{a} needs a value")
            out[a] = args[i + 1]
            i += 2
        else:
            die(f"unknown argument {a!r}")  # the real tools print usage and exit 1
    return out


def log(tool: str, argv: list[str], stdin: str | None) -> None:
    path = os.environ.get("NS_FAKE_QWEN_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"tool": tool, "argv": argv, "stdin": stdin}) + "\n")


def must_exist(path: str, what: str) -> None:
    p = Path(path)
    if not p.is_file() or p.stat().st_size == 0:
        die(f"cannot open {what} '{path}'")


def tone_bytes(seconds: float, freq: float) -> bytes:
    n = int(24000 * seconds)
    return b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * freq * i / 24000))) for i in range(n))


def stream_header() -> bytes:
    """qwentts.cpp `wav_stream_write_header`: 44 bytes, RIFF and data sizes 0x7FFFFFFF (24 kHz, 16-bit)."""
    fmt = struct.pack("<IHHIIHH", 16, 1, 1, 24000, 48000, 2, 16)
    live = struct.pack("<I", 0x7FFFFFFF)
    return b"RIFF" + live + b"WAVEfmt " + fmt + b"data" + live


def write_tone(path: str, seconds: float, freq: float) -> None:
    n = int(24000 * seconds)
    frames = b"".join(
        struct.pack("<h", int(9000 * math.sin(2 * math.pi * freq * i / 24000))) for i in range(n)
    )
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(frames)


def run_tts(argv: list[str]) -> int:
    a = parse(argv, TTS_VALUE_FLAGS, TTS_BOOL_FLAGS)
    for req in ("--model", "--codec", "-o"):
        if req not in a:
            die(f"missing required {req}")
    must_exist(a["--model"], "--model")
    must_exist(a["--codec"], "--codec")
    if "--device" in a and a["--device"] not in DEVICES:
        die(f"unknown device {a['--device']!r}")
    if "--lang" in a and a["--lang"].lower() not in LANGUAGES:
        die(f"unknown language {a['--lang']!r}")
    raw = sys.stdin.buffer.read().decode("utf-8")
    text = raw.rstrip("\r\n")
    if not text:
        die("stdin is empty, nothing to synthesise")
    name = Path(a["--model"]).name
    refs = [k for k in ("--ref-wav", "--ref-spk", "--ref-rvq", "--ref-text") if k in a]
    if "-customvoice-" in name:
        if "--speaker" not in a:
            die("CustomVoice needs --speaker")
        if a["--speaker"] not in SPEAKERS:
            die(f"unknown speaker {a['--speaker']!r}")
        if refs:
            die("reference options are Base only")
    elif "-voicedesign-" in name:
        if "--instruct" not in a:
            die("VoiceDesign needs --instruct")
        if "--speaker" in a or refs:
            die("VoiceDesign takes only --instruct")
    elif "-base-" in name:
        if "--instruct" in a:
            die("--instruct is rejected for Base")
        if "--speaker" in a:
            die("--speaker is CustomVoice only")
        if "--ref-wav" in a and ("--ref-spk" in a or "--ref-rvq" in a):
            die("--ref-wav and --ref-spk are exclusive")
        if "--ref-rvq" in a and ("--ref-spk" not in a or "--ref-text" not in a):
            die("--ref-rvq requires --ref-spk and --ref-text")
        if "--ref-text" in a and "--ref-wav" not in a and "--ref-spk" not in a:
            die("--ref-text needs a reference")
    else:
        die(f"{name} is not a qwen-talker GGUF")
    for key in ("--ref-wav", "--ref-spk", "--ref-rvq", "--ref-text"):
        if key in a:
            must_exist(a[key], key)
    if "--seed" in a:
        int(a["--seed"])
    log("qwen-tts", argv, raw)
    who = (a.get("--speaker", "") + a.get("--ref-spk", "")).encode()
    freq = 200.0 + int(hashlib.sha256(who).hexdigest(), 16) % 300
    if a["-o"] == "-":
        # `-o -` streams to stdout: one wide RIFF header, then the samples; with --stream-by-line every
        # non-empty stdin line is its own utterance, each after the first opening with a fresh header.
        lines = [text]
        if "--stream-by-line" in a:
            lines = [ln.rstrip("\r") for ln in raw.split("\n") if ln.strip()]
        if os.environ.get("NS_FAKE_QWEN_SHORT"):
            lines = lines[:-1]  # a binary that drops an utterance
        out = sys.stdout.buffer
        hang_after = os.environ.get("NS_FAKE_QWEN_HANG_AFTER")  # finish N utterances, open the next, stall
        for k, ln in enumerate(lines):
            if hang_after is not None and k == int(hang_after):
                out.write(stream_header())
                out.flush()
                time.sleep(120)
                return 1
            out.write(stream_header() + tone_bytes(max(0.3, 0.2 * len(ln.split())), freq))
        out.flush()
        return 0
    write_tone(a["-o"], max(0.3, 0.2 * len(text.split())), freq)
    return 0


def run_codec(argv: list[str]) -> int:
    a = parse(argv, CODEC_VALUE_FLAGS, set())
    if "--model" not in a:
        die("missing required --model")
    must_exist(a["--model"], "--model")
    if "-i" not in a:
        return 0  # load self-test
    src = Path(a["-i"])
    if src.suffix != ".wav":
        die(f"{src}: unsupported extension (expect .wav or .rvq)")
    with wave.open(str(src), "rb") as w:
        frames = w.readframes(w.getnframes())
    if len(frames) // 2 < HOP:
        die("input shorter than one hop")
    digest = hashlib.sha256(frames).digest()
    src.with_suffix(".rvq").write_bytes(b"RVQ" + digest)
    if "--talker" in a:
        must_exist(a["--talker"], "--talker")
        if "-base-" not in Path(a["--talker"]).name:
            die(f"{a['--talker']} has no speaker encoder (Base only)")
        src.with_suffix(".spk").write_bytes(digest * 4)
    log("qwen-codec", argv, None)
    return 0


def main() -> int:
    tool, argv = sys.argv[1], sys.argv[2:]
    fail = os.environ.get("NS_FAKE_QWEN_FAIL", "")
    if fail in (tool, "all"):
        sys.stderr.write("[fake-qwen] CUDA error: out of memory\n")
        return 3
    return run_tts(argv) if tool == "qwen-tts" else run_codec(argv)


if __name__ == "__main__":
    raise SystemExit(main())
