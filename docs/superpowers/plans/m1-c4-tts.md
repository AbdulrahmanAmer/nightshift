# M1 C4 - TTS service `ns_tts` Implementation Plan

> **For agentic workers:** execute task by task; steps use checkbox syntax. Every "Run" step means "commit, push, CI green" (ENGINEERING.md section 5, rule 13). Nothing executes on the authoring laptop.

**Goal:** A FastAPI service (spec 15.4, port 8091) that turns text into 24 kHz mono WAV through an `Engine` protocol with three engines: `qwentts` (the `qwentts.cpp` CLI, default), `reference` (PyTorch `qwen-tts`, fallback), `fake` (CI). Voices are a library of stored speaker embeddings (spec 15.4 "Voice library = embeddings").

**Architecture:** `audio.py` (pure numpy + stdlib: chunk, trim, stitch, WAV io) -> `voices.py` (folder-per-voice library, engine-agnostic) -> `engine.py` (protocol + 3 engines + `select_engine`) -> `server.py` (routes; no loudness normalization, C6 owns that). Heavy imports (`qwen_tts`, `torch`) are lazy; `fastapi`/`uvicorn` are imported only by `server.py`.

**Spec:** BUILD_SPEC.md section 2 (TTS row), 6.1, 7.5, 14.2, 15.4, 21.3. Previous design adapted: `_s1_part3.md` Task 6 (FasterEngine dropped; `qwentts` engine and embedding-based voice library added).

**Contract pin:** `contracts-m0` surface only: `ns_common.models.Runtime.tts` (`TtsCfg`), `ns_common.paths.Paths` (`bin_llama`, `models_tts`, `models_tts_voices`, `logs`), `ns_common.errors.NsError`, `ns_common.logging.NsLogger`, `ns_common.textstats.split_sentences/word_count`, `ns_common.tts_client.TtsItem/TtsFile` (shapes the server must serve).

## Global constraints

- Own only `ns_tts/` and `tests/test_tts_*.py`. Change requests go to `contracts/CHANGE_REQUESTS.md` (append only).
- Error codes only from `contracts/errors.json`: `NS-E202` (qwentts missing, crashed, timed out, no output), `NS-E301` (bad voice data / unsupported request, same code the client raises). `NS-W301` is reported by `ns verify`, not raised here.
- `fastapi` is not in the CI install (`.[dev]`). Server tests use `pytest.importorskip("fastapi")`; `server.py` carries a `# pyright: reportMissingImports=false` line until CR-C4-1 lands.
- `qwentts.cpp` CLI flags are unverifiable here: all of them live in ONE dict `QWENTTS_FLAGS` at the top of `engine.py`.
- Chunking per spec 7.5: sentence split, merge < 25 chars, cap 280, pauses 350 / 700 / 1200 ms (+120 ms between clause pieces of one long sentence), trim at -45 dBFS.

## File structure

```
ns_tts/audio.py voices.py engine.py server.py
tests/test_tts_audio.py test_tts_voices.py test_tts_engine.py test_tts_qwentts.py test_tts_server.py
contracts/CHANGE_REQUESTS.md   (append CRs only)
```

---

### Task 1: `ns_tts/audio.py` (chunk, trim, stitch, WAV io)

**Interfaces:**
```python
@dataclass(frozen=True) class Chunk: text: str; pause_after_ms: int
def chunk_text(text, *, max_chars=280, min_chars=25, cliff_lines: Iterable[str] | None = None) -> list[Chunk]
def trim_silence(x, sr, thresh_db=-45.0) -> np.ndarray
def stitch(chunks: list[tuple[np.ndarray, int]], sr) -> np.ndarray
def write_wav(path, x, sr) -> float      # 16-bit PCM, returns seconds
def read_wav(path) -> tuple[np.ndarray, int]   # float32 mono; PCM 8/16/24/32 and float32/64
```
Pause after a chunk is decided by what follows: next is a clause piece of the same sentence 120, next is a cliff line 1200, next opens a new paragraph (blank line) 700, else 350; the last chunk 0. A sentence in `cliff_lines` is never merged into the sentence before it.

- [ ] Step 1: tests `tests/test_tts_audio.py` (pauses, cap, merge, cliff, paragraph, trim, stitch, WAV round trip, float WAV, stereo downmix)
- [ ] Step 2: implement `audio.py`
- [ ] Step 3: commit `feat(c4): audio chunking, trim, stitch, wav io`

### Task 2: `ns_tts/voices.py` + `FakeEngine`

**Interfaces:**
```python
PRESETS = ["Ryan","Aiden","Vivian","Serena","Uncle_Fu","Dylan","Eric","Ono_Anna","Sohee"]
@dataclass class VoiceSpec: name; mode: "preset"|"design"|"clone"; speaker=None; description=None; language="English"; embedding_path=None
class VoiceLibrary(root: Path)   # root = paths.models_tts_voices; <name>/{voice.json, speaker.bin, sample.wav}
    list(); get(name); add_design(name, description, *, engine, language); add_clone(name, ref_audio_path, ref_text, rights_confirmed, *, engine, language); export_embedding(name) -> bytes
```
`add_clone` raises `ValueError` first when `rights_confirmed` is false (before touching engine or disk). Voices are written to a temp folder and renamed, so a failed engine call leaves nothing behind. Names: `[A-Za-z0-9][A-Za-z0-9_-]{0,39}`, not a preset, unique case-insensitively (Windows).

- [ ] Step 1: tests `tests/test_tts_voices.py` + FakeEngine part of `tests/test_tts_engine.py`
- [ ] Step 2: implement `voices.py` and the engine protocol + `FakeEngine` (tone bursts, 256-byte deterministic embeddings)
- [ ] Step 3: commit `feat(c4): voice library with stored embeddings and fake engine`

### Task 3: real engines and `select_engine`

- `QwenttsEngine(exe, models_dir)`: command from `QWENTTS_FLAGS` + `MODEL_FILES` (names pinned in `manifest.json`); presets use the CustomVoice talker with `--speaker`; designed and cloned voices use the Base talker with `--speaker-embedding`; embedding creation uses Base with `--ref-audio/--ref-text` and the save-embedding flag; design renders with the VoiceDesign talker then re-encodes the render through Base. `subprocess.run` with timeout; WAV read back with `read_wav`. `synth_batch` uses one process per batch when `batch_file`/`out_dir` flags are set, else one per chunk. `missing_model(op)` names the first absent GGUF.
- `ReferenceEngine(models_dir, size, device)`: lazy `qwen_tts`; `generate_custom_voice` / `generate_voice_design` / `generate_voice_clone`; embeddings are `create_voice_clone_prompt` output serialized as a tensor dict behind an `NSREF1` magic header, loaded with `weights_only=True`.
- `select_engine(cfg, paths, prefer)`: `NS_TTS_FAKE=1` or `prefer="fake"` -> Fake; `prefer`/`cfg.engine` `"reference"` -> Reference; anything else (`"qwentts"`, legacy `"faster"`) -> Qwentts.

- [ ] Step 1: tests `tests/test_tts_qwentts.py` (monkeypatched `subprocess.run`: command line, WAV read back, failure and timeout paths, missing model, batch mode, manifest filenames), select_engine tests
- [ ] Step 2: implement
- [ ] Step 3: commit `feat(c4): qwentts and reference engines`

### Task 4: `ns_tts/server.py`

Routes exactly per spec 15.4: `POST /v1/audio/speech`, `POST /v1/audio/speech/batch` (`{files:[{id,path,seconds}], engine, rtf}`), `GET /v1/voices`, `POST /v1/voices/design` (409 `{"missing_model": file}` when a required GGUF is absent; downloads nothing), `POST /v1/voices/clone` (400 without rights), `GET /v1/voices/{name}/embedding`, `GET /health`. Fixed per-voice seed (crc32 of the voice name) when the request has none, so timbre is stable. Optional additive field `cliff_lines` on speech and batch items. `python -m ns_tts.server --port --engine`.

- [ ] Step 1: tests `tests/test_tts_server.py` with `FakeEngine` + `TestClient`
- [ ] Step 2: implement
- [ ] Step 3: commit `feat(c4): FastAPI TTS server`

### Task 5: CRs, CI

- [ ] Append CRs (fastapi in `dev`; `TtsCfg.engine` accepts `qwentts`; ruff TID251 per-file ignore for `ns_tts/engine.py`; optional `TtsItem.cliff_lines`).
- [ ] Push, read CI (`gh run list --branch feat/c4-tts`), fix until green, `gh pr create --fill`.
