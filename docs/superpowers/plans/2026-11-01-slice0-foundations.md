# Nightshift Slice 0 — Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the Nightshift repository skeleton, the shared `ns_common` library (paths, config, logging, errors, DB, LLM/TTS/embed clients with fakes, text stats, FFmpeg helpers), every Slice 1 contract (pydantic models → JSON schemas, SQL schema, error codes, policy lists), the pinned `manifest.json` with a resolver, and the dev/gate scripts, so Slice 1 workers can build against frozen interfaces.

**Architecture:** One Python 3.12 repo with four packages (`ns_common`, `ns_studio`, `ns_tts`, `ns_supervisor`); only `ns_common` and `contracts/` are filled in this slice. Every heavy external system is behind a Protocol with a fake that writes real files, so the whole pipeline can later run with `--fake-services` on a machine without a GPU. Pydantic models are the single source of truth; JSON schemas in `contracts/schemas/` are exported from them and a test asserts they match.

**Tech Stack:** Python 3.12, pydantic v2, httpx, pytest + pytest-httpx, PyYAML, numpy, rich, typer, SQLite (stdlib), FFmpeg (already installed on the build machine), uv (installed only inside the repo's `.venv`).

**Spec:** `D:\NightShift\BUILD_SPEC.md` (v2). Read §0, §1.1, §2, §4, §15 before starting.

## Global Constraints

- Python `>=3.12,<3.13`; run everything with the repo's `.venv` (`D:\NightShift\.venv`). Create it with `py -3.12 -m venv .venv`.
- **Nothing heavy on the build machine** (spec §1.1): no torch, no models, no llama.cpp, no whisper, no spaCy model downloads. The `tts` and `studio` extras are declared but **not installed** here; only `.[dev]` is installed.
- Package directories equal import names: `ns_common/`, `ns_studio/`, `ns_tts/`, `ns_supervisor/` (spec §4).
- Home env var `NIGHTSHIFT_HOME`, default `%LOCALAPPDATA%\Nightshift` (spec §15.1). Tests must never touch the real home: always set `NIGHTSHIFT_HOME` to a `tmp_path`.
- Ports: llama 8080, embed 8081, TTS 8091, supervisor 8099, all on 127.0.0.1 (spec §15.3).
- Logical model aliases `code`, `story`; services `llama`, `embed`, `tts`, `whisper` (spec §15.1).
- Logging: JSON lines `logs\<component>-YYYYMMDD.jsonl` with fields `ts, level, component, event, msg, data` (spec §15.11).
- Error code ranges (spec §15.12): `NS-E1xx` install, `NS-E2xx` runtime/GPU lock, `NS-E3xx` text/LLM, `NS-E4xx` media/QC, `NS-E5xx` stock APIs/network, `NS-E6xx` policy gate, `NS-E7xx` update. Warnings use `NS-W`.
- Job id format `<slug>-<YYYYMMDD>-<4hex>` (spec §15.8).
- `contracts/` and `ns_common/` are edited only by the main session (spec §0 rule 4). This whole slice is main-session work; workers may be used for Tasks 8, 9 and 14 (`sonnet`).
- Commit after every task with a conventional prefix. Tag `contracts-s1` at the end of Task 16.
- Windows line endings: add `.gitattributes` with `* text=auto eol=lf` and `*.ps1 text eol=crlf`, `*.cmd text eol=crlf` in Task 1.

---

## File structure created by this slice

```
D:\NightShift\
  .gitattributes .gitignore .editorconfig
  pyproject.toml                      # project metadata, extras, scripts, ruff/pyright/pytest config
  README.md                           # §A text
  CLAUDE.md                           # how to develop here
  manifest.json                       # pins (Task 14)
  .github/workflows/ci.yml
  contracts/
    CONTRACTS.md CHANGE_REQUESTS.md errors.json schema.sql
    schemas/*.schema.json             # exported from ns_common.models
    policy/banned_phrases.txt misleading_claims.txt unsafe_topics.yaml real_entities_block.txt real_entities_allow.txt
  ns_common/
    __init__.py
    paths.py        # Paths: every directory under NIGHTSHIFT_HOME
    errors.py       # NsError + errors.json loader
    logging.py      # NsLogger JSONL
    models.py       # pydantic models + schema export
    config.py       # load/save runtime, channel, install_state; default_runtime(profile)
    db.py           # Db: connect, migrate, query, backup
    textstats.py    # sentences, word count, est_seconds, FK grade, token normalization
    ffmpeg.py       # ffmpeg/ffprobe helpers + synthetic clip generator
    llm_client.py   # Llm Protocol, LlmClient (llama-server), FakeLlm (fixtures)
    tts_client.py   # Tts Protocol, TtsClient, FakeTts (tone-burst WAV)
    embed_client.py # Embed Protocol, EmbedClient, FakeEmbed, cosine
    acquire.py      # Acquirer Protocol, Endpoint, DevAcquirer
  ns_studio/__init__.py  ns_tts/__init__.py  ns_supervisor/__init__.py   # empty packages
  ns_studio/cli.py  ns_supervisor/cli.py                                 # stubs printing version
  tools/resolve_manifest.py
  dev/gate_s0.py dev/run_services_dev.ps1
  tests/
    conftest.py
    fixtures/llm/_example.json
    test_paths.py test_errors.py test_logging.py test_models.py test_config.py test_db.py
    test_textstats.py test_ffmpeg.py test_llm_client.py test_tts_client.py test_embed_client.py
    test_acquire.py test_resolve_manifest.py
```

---

### Task 1: Repository scaffold, venv, pyproject, CI

**Files:**
- Create: `.gitattributes`, `.gitignore`, `.editorconfig`, `pyproject.toml`, `ns_common/__init__.py`, `ns_studio/__init__.py`, `ns_studio/cli.py`, `ns_tts/__init__.py`, `ns_supervisor/__init__.py`, `ns_supervisor/cli.py`, `tests/conftest.py`, `tests/test_scaffold.py`, `.github/workflows/ci.yml`

**Interfaces:**
- Produces: `ns_common.__version__ = "0.1.0"`; console scripts `ns` and `studio`; pytest fixture `tmp_home(tmp_path, monkeypatch) -> Path` that sets `NIGHTSHIFT_HOME`.

- [ ] **Step 1: Create the venv and install tooling**

```powershell
cd D:\NightShift
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip uv
```

- [ ] **Step 2: Write `.gitattributes`, `.gitignore`, `.editorconfig`**

`.gitattributes`:
```
* text=auto eol=lf
*.ps1 text eol=crlf
*.cmd text eol=crlf
*.png binary
*.jpg binary
*.wav binary
*.mp4 binary
```

`.gitignore`:
```
.venv/
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
dist/
build/
*.egg-info/
logs/
tests/_out/
```

`.editorconfig`:
```
root = true
[*]
indent_style = space
indent_size = 4
end_of_line = lf
charset = utf-8
trim_trailing_whitespace = true
insert_final_newline = true
[*.{ps1,cmd}]
end_of_line = crlf
```

- [ ] **Step 3: Write `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "nightshift"
version = "0.1.0"
description = "Nightshift: local story-to-video studio for YouTube long-form + Shorts"
requires-python = ">=3.12,<3.13"
dependencies = [
  "pydantic>=2.9,<3",
  "httpx>=0.27",
  "pyyaml>=6",
  "jinja2>=3.1",
  "rich>=13",
  "typer>=0.12",
  "numpy>=1.26,<3",
]

[project.optional-dependencies]
# Installed on the target machine only (spec §1.1). torch + TTS engines are installed by the
# installer from manifest.json pins, not from here, because the cu128 index cannot be locked here.
studio = ["Pillow>=10", "spacy>=3.7,<4", "faster-whisper>=1.0", "imagehash>=4.3", "keyring>=25"]
tts = ["fastapi>=0.115", "uvicorn>=0.30", "soundfile>=0.12"]
supervisor = ["fastapi>=0.115", "uvicorn>=0.30", "psutil>=6"]
dev = ["pytest>=8", "pytest-httpx>=0.32", "ruff>=0.6", "pyright>=1.1.380", "jsonschema>=4.23"]

[project.scripts]
ns = "ns_supervisor.cli:main"
studio = "ns_studio.cli:main"

[tool.setuptools.packages.find]
include = ["ns_common*", "ns_studio*", "ns_tts*", "ns_supervisor*"]

[tool.setuptools.package-data]
ns_studio = ["prompts/*.md", "presets/*.yaml", "presets/fonts/*", "exemplars/**/*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"

[tool.ruff]
line-length = 110
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP"]

[tool.pyright]
pythonVersion = "3.12"
typeCheckingMode = "standard"
include = ["ns_common", "ns_studio", "ns_tts", "ns_supervisor", "tools"]
```

- [ ] **Step 4: Write package inits and CLI stubs**

`ns_common/__init__.py`:
```python
__version__ = "0.1.0"
```

`ns_studio/__init__.py`, `ns_tts/__init__.py`, `ns_supervisor/__init__.py`: empty files.

`ns_studio/cli.py`:
```python
import typer

from ns_common import __version__

app = typer.Typer(help="Nightshift Studio")


@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(f"studio {__version__}")


def main() -> None:
    app()
```

`ns_supervisor/cli.py`:
```python
import typer

from ns_common import __version__

app = typer.Typer(help="Nightshift runtime tools")


@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(f"ns {__version__}")


def main() -> None:
    app()
```

- [ ] **Step 5: Write `tests/conftest.py`**

```python
from pathlib import Path

import pytest


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(home))
    return home


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]
```

- [ ] **Step 6: Write the failing scaffold test**

`tests/test_scaffold.py`:
```python
import subprocess
import sys


def test_version_importable():
    import ns_common

    assert ns_common.__version__ == "0.1.0"


def test_cli_stubs_run():
    for mod in ("ns_studio.cli", "ns_supervisor.cli"):
        out = subprocess.run([sys.executable, "-m", mod], capture_output=True, text=True, check=True)
        assert "0.1.0" in out.stdout
```

Add to the bottom of both `cli.py` files:
```python
if __name__ == "__main__":
    main()
```

- [ ] **Step 7: Install editable and run**

```powershell
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest tests/test_scaffold.py -v
```
Expected: 2 passed.

- [ ] **Step 8: Write CI**

`.github/workflows/ci.yml`:
```yaml
name: ci
on: [push, pull_request]
jobs:
  test:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: choco install ffmpeg -y --no-progress
      - run: python -m pip install -e ".[dev]"
      - run: python -m ruff check .
      - run: python -m pyright
      - run: python -m pytest
```

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "chore: repo scaffold, pyproject, CLI stubs, CI"
```

---

### Task 2: `ns_common/paths.py`

**Files:**
- Create: `ns_common/paths.py`, `tests/test_paths.py`

**Interfaces:**
- Produces:
```python
class Paths:  # frozen dataclass
    home: Path
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Paths"
    # directories (all Path properties):
    bin_llama, bin_opencode, models_llm, models_embed, models_tts, models_tts_voices, models_whisper, models_spacy,
    venvs, config, channels_dir, data, backup_dir, jobs_dir, calibration_dir, cache_clips, cache_search,
    imports_analytics, outbox, rejected, logs
    # files:
    runtime_file, install_state_file, opencode_file, db_path, clips_index
    def channel_file(self, slug: str) -> Path
    def job_dir(self, job_id: str) -> Path
    def outbox_dir(self, slug: str, date: str, job_id: str) -> Path
    def rejected_dir(self, slug: str, date: str, job_id: str) -> Path
    def ensure(self) -> None          # mkdir -p every directory
def repo_root() -> Path               # parent of the ns_common package
def contracts_dir() -> Path           # repo_root()/contracts
```

- [ ] **Step 1: Write the failing test**

```python
# tests/test_paths.py
from pathlib import Path

from ns_common.paths import Paths, contracts_dir, repo_root


def test_from_env_uses_nightshift_home(tmp_home: Path):
    p = Paths.from_env()
    assert p.home == tmp_home
    assert p.models_llm == tmp_home / "models" / "llm"
    assert p.runtime_file == tmp_home / "config" / "runtime.json"
    assert p.channel_file("after-dark") == tmp_home / "config" / "channels" / "after-dark.yaml"
    assert p.job_dir("after-dark-20261101-ab12") == tmp_home / "data" / "jobs" / "after-dark-20261101-ab12"
    assert p.outbox_dir("after-dark", "2026-11-01", "j1") == tmp_home / "outbox" / "after-dark" / "2026-11-01" / "j1"


def test_default_home_is_localappdata(monkeypatch):
    monkeypatch.delenv("NIGHTSHIFT_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    assert Paths.from_env().home == Path(r"C:\Users\x\AppData\Local\Nightshift")


def test_ensure_creates_all_dirs(tmp_home: Path):
    p = Paths.from_env()
    p.ensure()
    for d in (p.logs, p.cache_clips, p.models_tts_voices, p.backup_dir, p.channels_dir, p.rejected):
        assert d.is_dir()


def test_repo_root_contains_contracts():
    assert (repo_root() / "pyproject.toml").exists()
    assert contracts_dir().name == "contracts"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.\.venv\Scripts\python -m pytest tests/test_paths.py -v`
Expected: FAIL, `ModuleNotFoundError: ns_common.paths`.

- [ ] **Step 3: Implement**

```python
# ns_common/paths.py
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def contracts_dir() -> Path:
    return repo_root() / "contracts"


@dataclass(frozen=True)
class Paths:
    home: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Paths:
        e = os.environ if env is None else env
        raw = e.get("NIGHTSHIFT_HOME")
        if raw:
            return cls(Path(raw))
        local = e.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return cls(Path(local) / "Nightshift")

    # --- directories ---
    @property
    def bin_llama(self) -> Path: return self.home / "bin" / "llama"
    @property
    def bin_opencode(self) -> Path: return self.home / "bin" / "opencode"
    @property
    def models_llm(self) -> Path: return self.home / "models" / "llm"
    @property
    def models_embed(self) -> Path: return self.home / "models" / "embed"
    @property
    def models_tts(self) -> Path: return self.home / "models" / "tts"
    @property
    def models_tts_voices(self) -> Path: return self.models_tts / "voices"
    @property
    def models_whisper(self) -> Path: return self.home / "models" / "whisper"
    @property
    def models_spacy(self) -> Path: return self.home / "models" / "spacy"
    @property
    def venvs(self) -> Path: return self.home / "venvs"
    @property
    def config(self) -> Path: return self.home / "config"
    @property
    def channels_dir(self) -> Path: return self.config / "channels"
    @property
    def data(self) -> Path: return self.home / "data"
    @property
    def backup_dir(self) -> Path: return self.data / "backup"
    @property
    def jobs_dir(self) -> Path: return self.data / "jobs"
    @property
    def calibration_dir(self) -> Path: return self.data / "calibration"
    @property
    def cache_clips(self) -> Path: return self.home / "cache" / "clips"
    @property
    def cache_search(self) -> Path: return self.home / "cache" / "search"
    @property
    def imports_analytics(self) -> Path: return self.home / "imports" / "analytics"
    @property
    def outbox(self) -> Path: return self.home / "outbox"
    @property
    def rejected(self) -> Path: return self.home / "rejected"
    @property
    def logs(self) -> Path: return self.home / "logs"

    # --- files ---
    @property
    def runtime_file(self) -> Path: return self.config / "runtime.json"
    @property
    def install_state_file(self) -> Path: return self.config / "install_state.json"
    @property
    def opencode_file(self) -> Path: return self.config / "opencode.json"
    @property
    def db_path(self) -> Path: return self.data / "studio.db"
    @property
    def clips_index(self) -> Path: return self.cache_clips / "index.sqlite"

    def channel_file(self, slug: str) -> Path:
        return self.channels_dir / f"{slug}.yaml"

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def outbox_dir(self, slug: str, date: str, job_id: str) -> Path:
        return self.outbox / slug / date / job_id

    def rejected_dir(self, slug: str, date: str, job_id: str) -> Path:
        return self.rejected / slug / date / job_id

    def all_dirs(self) -> list[Path]:
        return [
            self.bin_llama, self.bin_opencode, self.models_llm, self.models_embed, self.models_tts,
            self.models_tts_voices, self.models_whisper, self.models_spacy, self.venvs, self.config,
            self.channels_dir, self.data, self.backup_dir, self.jobs_dir, self.calibration_dir,
            self.cache_clips, self.cache_search, self.imports_analytics, self.outbox, self.rejected, self.logs,
        ]

    def ensure(self) -> None:
        for d in self.all_dirs():
            d.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.\.venv\Scripts\python -m pytest tests/test_paths.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add ns_common/paths.py tests/test_paths.py
git commit -m "feat(common): Paths for every NIGHTSHIFT_HOME location"
```

---

### Task 3: Error codes — `contracts/errors.json` and `ns_common/errors.py`

**Files:**
- Create: `contracts/errors.json`, `ns_common/errors.py`, `tests/test_errors.py`

**Interfaces:**
- Produces:
```python
@dataclass(frozen=True)
class ErrorDef: code: str; message: str; hint: str; kind: Literal["error", "warning"]
class NsError(Exception):
    def __init__(self, code: str, detail: str = "", **data: Any)
    code: str; detail: str; data: dict; definition: ErrorDef
    def __str__(self) -> str   # "NS-E301: <message> (<detail>)"
def load_errors() -> dict[str, ErrorDef]     # cached, from contracts/errors.json
def describe(code: str) -> ErrorDef           # raises KeyError for unknown codes
```

- [ ] **Step 1: Write the failing test**

```python
# tests/test_errors.py
import json

import pytest

from ns_common.errors import NsError, describe, load_errors
from ns_common.paths import contracts_dir


def test_errors_json_is_well_formed():
    data = json.loads((contracts_dir() / "errors.json").read_text(encoding="utf-8"))
    for code, d in data.items():
        assert code.startswith("NS-E") or code.startswith("NS-W"), code
        assert set(d) == {"message", "hint"}, code


def test_describe_known_code():
    d = describe("NS-E102")
    assert d.kind == "error"
    assert "NVIDIA" in d.message


def test_nserror_str_and_data():
    err = NsError("NS-E301", "timeout after 600s", url="http://127.0.0.1:8080")
    assert str(err).startswith("NS-E301: ")
    assert "timeout after 600s" in str(err)
    assert err.data == {"url": "http://127.0.0.1:8080"}
    assert err.definition.kind == "error"


def test_warning_kind():
    assert describe("NS-W401").kind == "warning"


def test_unknown_code_raises():
    with pytest.raises(KeyError):
        describe("NS-E999")
    assert "NS-E999" in load_errors() or True  # load_errors never raises
```

- [ ] **Step 2: Run to verify it fails**

Run: `.\.venv\Scripts\python -m pytest tests/test_errors.py -v` → FAIL (module missing).

- [ ] **Step 3: Write `contracts/errors.json`**

```json
{
  "NS-E101": {"message": "Unsupported Windows architecture (ARM64 or 32-bit).", "hint": "Nightshift needs 64-bit Windows 10 or 11 on an x64 PC."},
  "NS-E102": {"message": "No NVIDIA GPU detected.", "hint": "Nightshift needs an NVIDIA RTX GPU with 16 GB. Check that the GPU driver is installed (nvidia-smi must work)."},
  "NS-E103": {"message": "NVIDIA driver too old (CUDA < 12.8).", "hint": "Install the latest Game Ready or Studio driver from nvidia.com/drivers, reboot, then re-run setup."},
  "NS-E104": {"message": "Not enough free disk space.", "hint": "Free at least 60 GB on the drive that holds NIGHTSHIFT_HOME (80 GB in two-model mode), or set NIGHTSHIFT_HOME to a bigger drive."},
  "NS-E105": {"message": "A required download site is unreachable.", "hint": "Check the internet connection and any firewall/proxy. Needed: huggingface.co, github.com, download.pytorch.org, api.pexels.com, pixabay.com."},
  "NS-W106": {"message": "NIGHTSHIFT_HOME is on a hard disk, not an SSD.", "hint": "Model loading will be slow. Consider moving NIGHTSHIFT_HOME to an NVMe/SSD drive."},
  "NS-W107": {"message": "Wake timers are disabled in the power plan.", "hint": "The nightly task cannot wake the PC. Enable 'Allow wake timers' in Power Options > Advanced > Sleep."},
  "NS-E108": {"message": "PyTorch cannot see the GPU (CUDA unavailable or compute capability too old).", "hint": "In the tts venv run: uv pip install --force-reinstall torch --index-url https://download.pytorch.org/whl/cu128"},
  "NS-E109": {"message": "A downloaded file failed its SHA256 check.", "hint": "Delete the file and re-run setup. If it fails again, the pin in manifest.json is wrong; report it."},
  "NS-E110": {"message": "A required tool failed to install via winget.", "hint": "Install it manually (Git, Windows Terminal, FFmpeg) and re-run setup."},
  "NS-E201": {"message": "GPU is busy with another task; the nightly run gave up after waiting.", "hint": "Close Nightshift Code or wait; the next scheduled run will retry."},
  "NS-E202": {"message": "A service failed to start or crashed repeatedly.", "hint": "Run 'ns doctor' and check logs\\supervisor-*.jsonl."},
  "NS-E203": {"message": "Service health check timed out.", "hint": "The model may be too large for this profile. Run 'ns tune' or check runtime.json."},
  "NS-E301": {"message": "The language model did not return a usable answer.", "hint": "Check that llama-server is healthy (ns doctor). The job will retry once."},
  "NS-E302": {"message": "The model's answer did not match the required JSON schema.", "hint": "Usually transient; the stage retries with a repair prompt. If persistent, the prompt or schema needs a fix."},
  "NS-E303": {"message": "A text validator rejected the story.", "hint": "See the detail for which rule (length, banned phrase, repeated opening, reading level)."},
  "NS-E304": {"message": "No story passed the critic after 3 ideas.", "hint": "Lower critic_bar in the channel profile or widen the pools."},
  "NS-E305": {"message": "The long-form story is still under 9 minutes after 2 expansions.", "hint": "Raise long_minutes or the chapter count in the channel profile."},
  "NS-E306": {"message": "This format is not implemented yet.", "hint": "Use --format master_first or shorts_only."},
  "NS-W301": {"message": "faster-qwen3-tts failed its smoke test; using the reference qwen-tts engine.", "hint": "Slower but equivalent output. See logs\\tts-*.jsonl for the failure."},
  "NS-W302": {"message": "MTP speculative decoding was not faster on this machine; disabled.", "hint": "No action needed."},
  "NS-E401": {"message": "FFmpeg render failed.", "hint": "See the detail for FFmpeg's error. Run 'ns verify --quick' to test the encoder."},
  "NS-W401": {"message": "NVENC hardware encoder unavailable; using libx264 (slower).", "hint": "Update the NVIDIA driver, then run 'ns verify --quick' to re-test."},
  "NS-E402": {"message": "No usable stock clip found for a beat.", "hint": "The keywords were too narrow or the clip pool is exhausted. The beat falls back to a mood-only search; if that fails, widen seed_keywords."},
  "NS-E403": {"message": "Captions drift too far from the audio.", "hint": "The transcription did not match the script. Check the voice's speed setting."},
  "NS-E404": {"message": "QC check failed.", "hint": "See qc\\*.json in the job folder for the failing check and value."},
  "NS-E501": {"message": "Pexels API error.", "hint": "Check the API key and the hourly limit (200 requests/hour)."},
  "NS-E502": {"message": "Pixabay API error.", "hint": "Check the API key and the per-minute limit (100 requests/minute)."},
  "NS-E503": {"message": "Network request failed.", "hint": "Check the connection; the stage will retry."},
  "NS-E601": {"message": "Policy gate: unsafe topic.", "hint": "The story touched a blocked topic (see contracts/policy/unsafe_topics.yaml)."},
  "NS-E602": {"message": "Policy gate: real person, brand or event detected.", "hint": "Fiction must not name real people, brands or real crimes/tragedies."},
  "NS-E603": {"message": "Policy gate: misleading claim in metadata.", "hint": "Remove 'true story', 'real footage' and similar from titles/descriptions/tags."},
  "NS-E604": {"message": "Policy gate: hook or title does not match the story.", "hint": "The judge found the promise was not delivered."},
  "NS-E605": {"message": "Policy gate: story too similar to a previous one.", "hint": "Embedding similarity above threshold; a new idea will be drawn."},
  "NS-E606": {"message": "Policy gate: metadata spam.", "hint": "Too many tags, over 500 characters, or irrelevant tags."},
  "NS-E607": {"message": "Policy gate: selected Short does not stand alone.", "hint": "The moment needs context from the master; another moment will be chosen."},
  "NS-E611": {"message": "Policy gate: render too similar to a previous video.", "hint": "Perceptual hash matched an earlier render; visuals are being redrawn."},
  "NS-E612": {"message": "Policy gate: long-form runtime or mid-roll placement rule violated.", "hint": "Master must be at least 8:30 and mid-rolls outside the first/last 2 minutes."},
  "NS-E613": {"message": "Policy gate: stock clip reuse rule violated.", "hint": "See spec §7.4 reuse windows."},
  "NS-E614": {"message": "Policy gate: non-voice audio detected.", "hint": "Nightshift videos carry narration only, no music."},
  "NS-E701": {"message": "Update package failed verification.", "hint": "Re-download nightshift-app-<version>.zip and its .sha256 file."},
  "NS-E702": {"message": "Update failed; rolled back to the previous version.", "hint": "Run 'ns doctor --bundle' and send the diagnostics."}
}
```

- [ ] **Step 4: Implement `ns_common/errors.py`**

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

from ns_common.paths import contracts_dir


@dataclass(frozen=True)
class ErrorDef:
    code: str
    message: str
    hint: str
    kind: Literal["error", "warning"]


@lru_cache(maxsize=1)
def load_errors() -> dict[str, ErrorDef]:
    raw = json.loads((contracts_dir() / "errors.json").read_text(encoding="utf-8"))
    out: dict[str, ErrorDef] = {}
    for code, d in raw.items():
        kind: Literal["error", "warning"] = "warning" if code.startswith("NS-W") else "error"
        out[code] = ErrorDef(code=code, message=d["message"], hint=d["hint"], kind=kind)
    return out


def describe(code: str) -> ErrorDef:
    return load_errors()[code]


class NsError(Exception):
    def __init__(self, code: str, detail: str = "", **data: Any) -> None:
        self.code = code
        self.detail = detail
        self.data = data
        self.definition = describe(code)
        super().__init__(str(self))

    def __str__(self) -> str:
        base = f"{self.code}: {self.definition.message}"
        return f"{base} ({self.detail})" if self.detail else base
```

- [ ] **Step 5: Run to verify it passes**

Run: `.\.venv\Scripts\python -m pytest tests/test_errors.py -v` → 5 passed.

- [ ] **Step 6: Commit**

```bash
git add contracts/errors.json ns_common/errors.py tests/test_errors.py
git commit -m "feat(common): error code registry and NsError"
```

---

### Task 4: `ns_common/logging.py`

**Files:**
- Create: `ns_common/logging.py`, `tests/test_logging.py`

**Interfaces:**
- Produces:
```python
class NsLogger:
    def __init__(self, component: str, logs_dir: Path, *, echo: bool | None = None, clock: Callable[[], datetime] | None = None)
    def log(self, level: Literal["DEBUG","INFO","WARN","ERROR"], event: str, msg: str = "", **data: Any) -> None
    def debug/info/warn/error(self, event: str, msg: str = "", **data) -> None
    def file_for(self, when: datetime) -> Path     # logs_dir / f"{component}-{YYYYMMDD}.jsonl"
    @contextmanager
    def timed(self, event: str, **data) -> Iterator[None]   # logs f"{event}_start" and f"{event}_end" with duration_s
def get_logger(component: str, paths: Paths) -> NsLogger
```
- Echo to stderr (rich) when `echo=True` or env `NS_LOG_STDERR=1`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_logging.py
import json
from datetime import UTC, datetime
from pathlib import Path

from ns_common.logging import NsLogger


def test_writes_jsonl_with_required_fields(tmp_path: Path):
    fixed = datetime(2026, 11, 1, 3, 0, 0, tzinfo=UTC)
    log = NsLogger("studio", tmp_path, echo=False, clock=lambda: fixed)
    log.info("stage_start", "starting ideas", job_id="j1", tenant="llama")
    f = tmp_path / "studio-20261101.jsonl"
    assert f.exists()
    rec = json.loads(f.read_text(encoding="utf-8").splitlines()[0])
    assert rec == {
        "ts": "2026-11-01T03:00:00+00:00",
        "level": "INFO",
        "component": "studio",
        "event": "stage_start",
        "msg": "starting ideas",
        "data": {"job_id": "j1", "tenant": "llama"},
    }


def test_timed_logs_start_and_end(tmp_path: Path):
    log = NsLogger("studio", tmp_path, echo=False)
    with log.timed("render", job_id="j1"):
        pass
    lines = [json.loads(line) for line in (next(tmp_path.glob("*.jsonl"))).read_text().splitlines()]
    assert [r["event"] for r in lines] == ["render_start", "render_end"]
    assert "duration_s" in lines[1]["data"]


def test_non_serializable_data_is_stringified(tmp_path: Path):
    log = NsLogger("x", tmp_path, echo=False)
    log.warn("odd", path=Path("C:/a"))
    rec = json.loads(next(tmp_path.glob("*.jsonl")).read_text().splitlines()[0])
    assert rec["data"]["path"] == "C:\\a" or rec["data"]["path"] == "C:/a"
```

- [ ] **Step 2: Run to verify it fails** → FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# ns_common/logging.py
from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ns_common.paths import Paths

Level = Literal["DEBUG", "INFO", "WARN", "ERROR"]


class NsLogger:
    def __init__(
        self,
        component: str,
        logs_dir: Path,
        *,
        echo: bool | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.component = component
        self.logs_dir = logs_dir
        self.echo = (os.environ.get("NS_LOG_STDERR") == "1") if echo is None else echo
        self.clock = clock or (lambda: datetime.now(UTC))
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def file_for(self, when: datetime) -> Path:
        return self.logs_dir / f"{self.component}-{when:%Y%m%d}.jsonl"

    def log(self, level: Level, event: str, msg: str = "", **data: Any) -> None:
        now = self.clock()
        rec = {
            "ts": now.isoformat(),
            "level": level,
            "component": self.component,
            "event": event,
            "msg": msg,
            "data": data,
        }
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self.file_for(now).open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if self.echo:
            print(f"[{level}] {self.component} {event} {msg} {json.dumps(data, default=str)}", file=sys.stderr)

    def debug(self, event: str, msg: str = "", **data: Any) -> None: self.log("DEBUG", event, msg, **data)
    def info(self, event: str, msg: str = "", **data: Any) -> None: self.log("INFO", event, msg, **data)
    def warn(self, event: str, msg: str = "", **data: Any) -> None: self.log("WARN", event, msg, **data)
    def error(self, event: str, msg: str = "", **data: Any) -> None: self.log("ERROR", event, msg, **data)

    @contextmanager
    def timed(self, event: str, **data: Any) -> Iterator[None]:
        t0 = time.perf_counter()
        self.info(f"{event}_start", **data)
        try:
            yield
        finally:
            self.info(f"{event}_end", duration_s=round(time.perf_counter() - t0, 3), **data)


def get_logger(component: str, paths: Paths) -> NsLogger:
    return NsLogger(component, paths.logs)
```

- [ ] **Step 4: Run to verify it passes** → 3 passed.

- [ ] **Step 5: Commit**

```bash
git add ns_common/logging.py tests/test_logging.py
git commit -m "feat(common): JSONL logger with timed blocks"
```

---
### Task 5: Pydantic models and JSON schema export — `ns_common/models.py`

**Files:**
- Create: `ns_common/models.py`, `contracts/schemas/*.schema.json` (generated), `tests/test_models.py`

**Interfaces:**
- Produces the models below (field names are the contract; spec §15.6). `SCHEMA_MODELS: dict[str, type[BaseModel]]` maps schema name → model. `export_schemas(out_dir: Path) -> list[Path]` writes `<name>.schema.json`. `python -m ns_common.models` exports into `contracts/schemas/`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models.py
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ns_common import models as m
from ns_common.paths import contracts_dir


def test_channel_defaults_and_validation():
    ch = m.Channel(slug="after-dark", display_name="After Dark", niche="horror", language="en")
    assert ch.status == "active"
    assert ch.format == "master_first"
    assert ch.long_minutes == (10, 14)
    assert ch.pools.hooks  # defaults non-empty
    with pytest.raises(ValidationError):
        m.Channel(slug="Bad Slug!", display_name="x", niche="y", language="en")


def test_job_id_pattern():
    md = m.Metadata(
        channel="after-dark", job_id="after-dark-20261101-ab12", kind="short", k=1,
        title="t", description="d", tags=["a"], hashtags=["#Shorts"], publish_after="2026-11-02T18:00:00",
        ai_disclosure="no", made_for_kids=False, age_restrict_suggested=False, clips=[], combo_id="c1",
        prompt_versions={"hook": 1},
    )
    assert md.kind == "short"
    with pytest.raises(ValidationError):
        md.model_copy(update={"job_id": "nope"}).model_validate(md.model_dump() | {"job_id": "nope"})


def test_runtime_default_profiles():
    rt = m.Runtime.default("cpu-dev")
    assert rt.video.encoder == "libx264"
    assert rt.llama.port == 8080 and rt.embed.port == 8081 and rt.tts.port == 8091
    gpu = m.Runtime.default("32gb")
    assert gpu.video.encoder == "h264_nvenc"
    assert gpu.llama.flags.ctx == 16384


def test_every_schema_file_matches_models():
    out = contracts_dir() / "schemas"
    for name, model in m.SCHEMA_MODELS.items():
        f = out / f"{name}.schema.json"
        assert f.exists(), f"missing {f}; run python -m ns_common.models"
        assert json.loads(f.read_text(encoding="utf-8")) == model.model_json_schema(), name


def test_export_writes_all(tmp_path: Path):
    files = m.export_schemas(tmp_path)
    assert {f.stem.replace(".schema", "") for f in files} == set(m.SCHEMA_MODELS)
```

- [ ] **Step 2: Run to verify it fails** → FAIL (module missing).

- [ ] **Step 3: Implement `ns_common/models.py`**

```python
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SLUG_RE = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
JOB_ID_RE = r"^[a-z0-9]+(?:-[a-z0-9]+)*-\d{8}-[0-9a-f]{4}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------- channel ----------
class Voice(Strict):
    mode: Literal["preset", "design", "clone"] = "preset"
    speaker: str | None = "Ryan"
    description: str | None = None
    name: str | None = None          # library name for design/clone voices
    speed: float = Field(1.0, ge=0.7, le=1.3)


class Pools(Strict):
    hooks: list[str] = ["cold_open", "question", "confession", "warning"]
    structures: list[str] = ["twist", "reverse_reveal", "slow_burn", "unreliable_narrator"]
    pov_tones: list[str] = ["first_person_intimate", "narrator_calm"]
    pacings: list[str] = ["standard", "measured"]
    caption_styles: list[str] = ["punch_yellow", "clean_white"]
    visual_treatments: list[str] = ["dark_slowzoom", "handheld_grit"]
    title_formulas: list[str] = ["question", "statement_twist", "i_never"]


class Channel(Strict):
    slug: str = Field(pattern=SLUG_RE)
    display_name: str
    status: Literal["active", "paused"] = "active"
    language: str = "en"
    niche: str
    seed_keywords: list[str] = []
    voice_notes: str = ""
    voice: Voice = Voice()
    format: Literal["master_first", "shorts_only", "series_compilation"] = "master_first"
    masters_per_week: int = Field(1, ge=1, le=3)
    long_minutes: tuple[int, int] = (10, 14)
    shorts_per_master: tuple[int, int] = (4, 6)
    shorts_per_day_max: int = Field(2, ge=1, le=3)
    length_buckets: list[int] = [35, 50, 75]
    publish_window: str = Field("18:00-21:00", pattern=r"^\d{2}:\d{2}-\d{2}:\d{2}$")
    audience: Literal["general", "mature"] = "general"
    made_for_kids: bool = False
    fiction_label: bool = True
    creator_note: str = ""
    critic_bar: float = Field(8.0, ge=5.0, le=10.0)
    pools: Pools = Pools()
    avoid_topics: list[str] = []
    created_at: str | None = None

    @field_validator("long_minutes")
    @classmethod
    def _floor(cls, v: tuple[int, int]) -> tuple[int, int]:
        if v[0] < 9 or v[1] < v[0]:
            raise ValueError("long_minutes must be [>=9, >=min]")
        return v


# ---------- runtime ----------
class LlamaFlags(Strict):
    n_gpu_layers: int = 99
    n_cpu_moe: int = 0
    ctx: int = 16384
    kv_type: Literal["f16", "q8_0"] = "f16"
    spec_type: Literal["draft-mtp"] | None = None
    spec_draft_n_max: int | None = None
    threads: int | None = None


class LlamaCfg(Strict):
    host: str = "127.0.0.1"
    port: int = 8080
    model_path: str = ""
    story_model_path: str | None = None     # two-model mode only
    aliases: list[str] = ["code", "story"]
    flags: LlamaFlags = LlamaFlags()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class EmbedCfg(Strict):
    host: str = "127.0.0.1"
    port: int = 8081
    model_path: str = ""

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class TtsCfg(Strict):
    host: str = "127.0.0.1"
    port: int = 8091
    engine: Literal["faster", "reference"] = "faster"
    model_dir: str = ""
    device: Literal["cuda", "cpu"] = "cuda"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class WhisperCfg(Strict):
    model: str = "small.en"
    device: Literal["cuda", "cpu"] = "cuda"
    compute_type: str = "float16"


class VideoCfg(Strict):
    encoder: Literal["h264_nvenc", "libx264"] = "h264_nvenc"
    preset: str = "p4"
    fps: int = 30


class Runtime(Strict):
    profile: Literal["32gb", "16gb", "cpu-dev"] = "32gb"
    llama: LlamaCfg = LlamaCfg()
    embed: EmbedCfg = EmbedCfg()
    tts: TtsCfg = TtsCfg()
    whisper: WhisperCfg = WhisperCfg()
    video: VideoCfg = VideoCfg()
    supervisor_port: int = 8099

    @classmethod
    def default(cls, profile: Literal["32gb", "16gb", "cpu-dev"]) -> Runtime:
        if profile == "32gb":
            return cls(profile=profile)
        if profile == "16gb":
            return cls(profile=profile, llama=LlamaCfg(flags=LlamaFlags(ctx=8192, kv_type="q8_0")))
        return cls(
            profile=profile,
            llama=LlamaCfg(flags=LlamaFlags(n_gpu_layers=0, ctx=8192, threads=4)),
            tts=TtsCfg(engine="reference", device="cpu"),
            whisper=WhisperCfg(model="tiny", device="cpu", compute_type="int8"),
            video=VideoCfg(encoder="libx264", preset="ultrafast"),
        )


# ---------- install ----------
class StepState(Strict):
    status: Literal["pending", "done", "failed", "skipped"] = "pending"
    error: str | None = None
    finished_at: str | None = None


class InstallState(Strict):
    version: str = "0.1.0"
    app_path: str = ""
    profile: Literal["32gb", "16gb", "cpu-dev"] | None = None
    steps: dict[str, StepState] = {}
    started_at: str | None = None
    completed_at: str | None = None


class AnswersChannel(Strict):
    display_name: str
    niche: str
    language: str = "en"
    seed_keywords: list[str] = []


class Answers(Strict):
    channel: AnswersChannel
    voice: Voice = Voice()
    voice_ref_audio_path: str | None = None
    voice_ref_text: str | None = None
    voice_rights_confirmed: bool = False
    pexels_api_key: str | None = None
    pixabay_api_key: str | None = None
    masters_per_week: int = 1
    run_time: str = Field("03:00", pattern=r"^\d{2}:\d{2}$")
    install_code: bool = True
    allow_cpu: bool = False


# ---------- manifest ----------
class HfFile(Strict):
    repo: str
    file: str
    sha256: str | None = None
    bytes: int | None = None


class HfRepo(Strict):
    repo: str
    revision: str = "main"
    files_sha256: dict[str, str] = {}


class BinPin(Strict):
    version: str
    url: str
    sha256: str | None = None


class ProfileModels(Strict):
    llm: HfFile
    story_llm: HfFile | None = None
    embed: HfFile
    tts: list[HfRepo]
    whisper_model: str


class Manifest(Strict):
    version: str
    profiles: dict[str, ProfileModels]
    bins: dict[str, BinPin]
    python_version: str = "3.12"
    spacy_model: str = "en_core_web_md"
    tts_packages: list[str]     # exact pip specs installed into the tts venv by the installer


# ---------- text stages ----------
class Idea(Strict):
    premise: str
    twist: str
    setting: str
    protagonist: str
    why_chapters: str = ""
    keywords: list[str] = Field(min_length=1, max_length=8)


class Part(Strict):
    n: int = Field(ge=1)
    text: str
    hook: str = ""
    word_count: int = Field(ge=1)
    est_seconds: float = Field(gt=0)
    cliffhanger: bool = False


class Seo(Strict):
    title: str = Field(max_length=100)
    description: str = Field(max_length=5000)
    tags: list[str] = Field(min_length=1, max_length=20)
    hashtags: list[str] = Field(max_length=5)
    keywords: list[str] = []


class VisualBeat(Strict):
    part: int | None = None
    chapter: int | None = None
    beat: int = Field(ge=0)
    sentence_start: int = Field(ge=0)
    sentence_end: int = Field(ge=0)
    keywords: list[str] = Field(min_length=1, max_length=3)
    mood: str
    need_vertical: bool = True


class Combo(Strict):
    combo_id: str
    hook: str
    structure: str
    pov_tone: str
    pacing: str
    caption_style: str
    visual_treatment: str
    title_formula: str
    length_bucket: int | None = None


# ---------- QC / policy / metadata ----------
class QcCheck(Strict):
    passed: bool
    value: float | str | None = None
    limit: str | None = None


class QcResult(Strict):
    item: str                      # "long" | "s<k>"
    passed: bool
    checks: dict[str, QcCheck]
    reason: str | None = None


class PolicyCheck(Strict):
    passed: bool
    detail: str = ""


class PolicyAdvice(Strict):
    ai_disclosure: Literal["yes", "no"] = "no"
    made_for_kids: bool = False
    age_restrict_suggested: bool = False


class PolicyResult(Strict):
    stage: Literal["text", "final"]
    passed: bool
    checks: dict[str, PolicyCheck]
    advice: PolicyAdvice = PolicyAdvice()


class ClipRef(Strict):
    source: Literal["pexels", "pixabay", "fake"]
    id: str
    url: str
    license: str


class Metadata(Strict):
    channel: str = Field(pattern=SLUG_RE)
    job_id: str = Field(pattern=JOB_ID_RE)
    kind: Literal["long", "short"]
    k: int | None = None
    title: str = Field(max_length=100)
    description: str
    tags: list[str]
    hashtags: list[str]
    publish_after: str
    chapters: list[dict] | None = None
    midroll_suggestions_s: list[float] | None = None
    thumbnails: list[str] | None = None
    related_video_instruction: str | None = None
    parent_job_id: str | None = None
    ai_disclosure: Literal["yes", "no"]
    made_for_kids: bool
    age_restrict_suggested: bool
    clips: list[ClipRef]
    combo_id: str
    prompt_versions: dict[str, int]


class CalibrationRow(Strict):
    story_id: str
    rubric: dict[str, float]
    critic_avg: float
    human_avg: float | None = None


class GateCheck(Strict):
    name: str
    passed: bool
    value: float | str | None = None
    limit: str | None = None
    log: str | None = None


class GateReport(Strict):
    slice: str
    date: str
    machine: dict[str, str | float | None]
    checks: list[GateCheck]
    benchmarks: dict[str, float] = {}

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)


SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "channel": Channel, "runtime": Runtime, "install_state": InstallState, "answers": Answers,
    "manifest": Manifest, "idea": Idea, "parts": Part, "seo": Seo, "visual_plan": VisualBeat,
    "combo": Combo, "qc": QcResult, "policy": PolicyResult, "metadata": Metadata,
    "calibration_row": CalibrationRow, "gate_report": GateReport,
}


def export_schemas(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, model in SCHEMA_MODELS.items():
        f = out_dir / f"{name}.schema.json"
        f.write_text(json.dumps(model.model_json_schema(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(f)
    return written


if __name__ == "__main__":
    from ns_common.paths import contracts_dir

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else contracts_dir() / "schemas"
    for p in export_schemas(target):
        print(p)
```

Note: `plan`, `story`, `shorts_plan`, `chapters`, `seo_long` are Slice 2 schemas and are added then (spec §15.6 lists them; this slice freezes Slice 1's set).

- [ ] **Step 4: Export schemas and run tests**

```powershell
.\.venv\Scripts\python -m ns_common.models
.\.venv\Scripts\python -m pytest tests/test_models.py -v
```
Expected: 5 passed; 15 files under `contracts/schemas/`.

- [ ] **Step 5: Commit**

```bash
git add ns_common/models.py contracts/schemas tests/test_models.py
git commit -m "feat(contracts): pydantic models and exported JSON schemas for slice 1"
```

---

### Task 6: `ns_common/config.py`

**Files:**
- Create: `ns_common/config.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
```python
def load_runtime(paths: Paths) -> Runtime            # raises NsError("NS-E203", "runtime.json missing") if absent
def save_runtime(paths: Paths, rt: Runtime) -> Path
def load_channel(paths: Paths, slug: str) -> Channel  # YAML; raises FileNotFoundError
def save_channel(paths: Paths, ch: Channel) -> Path
def list_channels(paths: Paths) -> list[str]         # slugs sorted
def load_install_state(paths: Paths) -> InstallState # default InstallState() if absent
def save_install_state(paths: Paths, st: InstallState) -> Path
def load_manifest(path: Path | None = None) -> Manifest  # default repo_root()/manifest.json
```
- YAML written with `sort_keys=False`, `allow_unicode=True`. Tuples serialize as lists.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
from pathlib import Path

import pytest

from ns_common import config as c
from ns_common.errors import NsError
from ns_common.models import Channel, InstallState, Runtime
from ns_common.paths import Paths


def test_runtime_roundtrip(tmp_home: Path):
    p = Paths.from_env(); p.ensure()
    with pytest.raises(NsError) as ei:
        c.load_runtime(p)
    assert ei.value.code == "NS-E203"
    c.save_runtime(p, Runtime.default("cpu-dev"))
    assert c.load_runtime(p).video.encoder == "libx264"


def test_channel_roundtrip_and_list(tmp_home: Path):
    p = Paths.from_env(); p.ensure()
    ch = Channel(slug="after-dark", display_name="After Dark", niche="horror", language="en")
    f = c.save_channel(p, ch)
    assert f.name == "after-dark.yaml"
    text = f.read_text(encoding="utf-8")
    assert text.startswith("slug: after-dark")          # insertion order kept
    assert "long_minutes:\n- 10\n- 14" in text or "long_minutes: [10, 14]" in text
    assert c.load_channel(p, "after-dark") == ch
    c.save_channel(p, ch.model_copy(update={"slug": "calm-tales"}))
    assert c.list_channels(p) == ["after-dark", "calm-tales"]


def test_install_state_default_and_save(tmp_home: Path):
    p = Paths.from_env(); p.ensure()
    assert c.load_install_state(p) == InstallState()
    st = InstallState(app_path="D:/NightShift")
    c.save_install_state(p, st)
    assert c.load_install_state(p).app_path == "D:/NightShift"


def test_load_manifest_from_repo():
    mf = c.load_manifest()
    assert "32gb" in mf.profiles and "cpu-dev" in mf.profiles
```

- [ ] **Step 2: Run to verify it fails** → FAIL.

- [ ] **Step 3: Implement**

```python
# ns_common/config.py
from __future__ import annotations

import json
from pathlib import Path

import yaml

from ns_common.errors import NsError
from ns_common.models import Channel, InstallState, Manifest, Runtime
from ns_common.paths import Paths, repo_root


def _write_json(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load_runtime(paths: Paths) -> Runtime:
    if not paths.runtime_file.exists():
        raise NsError("NS-E203", f"{paths.runtime_file} missing; run ns tune or the installer")
    return Runtime.model_validate_json(paths.runtime_file.read_text(encoding="utf-8"))


def save_runtime(paths: Paths, rt: Runtime) -> Path:
    return _write_json(paths.runtime_file, rt.model_dump(mode="json"))


def load_channel(paths: Paths, slug: str) -> Channel:
    f = paths.channel_file(slug)
    if not f.exists():
        raise FileNotFoundError(f)
    return Channel.model_validate(yaml.safe_load(f.read_text(encoding="utf-8")))


def save_channel(paths: Paths, ch: Channel) -> Path:
    f = paths.channel_file(ch.slug)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        yaml.safe_dump(ch.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return f


def list_channels(paths: Paths) -> list[str]:
    if not paths.channels_dir.exists():
        return []
    return sorted(p.stem for p in paths.channels_dir.glob("*.yaml"))


def load_install_state(paths: Paths) -> InstallState:
    if not paths.install_state_file.exists():
        return InstallState()
    return InstallState.model_validate_json(paths.install_state_file.read_text(encoding="utf-8"))


def save_install_state(paths: Paths, st: InstallState) -> Path:
    return _write_json(paths.install_state_file, st.model_dump(mode="json"))


def load_manifest(path: Path | None = None) -> Manifest:
    f = path or (repo_root() / "manifest.json")
    return Manifest.model_validate_json(f.read_text(encoding="utf-8"))
```

- [ ] **Step 4: Run** → `test_load_manifest_from_repo` still fails until Task 14 writes `manifest.json`. Mark it `@pytest.mark.xfail(strict=True, reason="manifest written in Task 14")` for now and remove the marker in Task 14. Other 3 pass.

- [ ] **Step 5: Commit**

```bash
git add ns_common/config.py tests/test_config.py
git commit -m "feat(common): runtime/channel/install-state/manifest loaders"
```

---

### Task 7: SQLite — `contracts/schema.sql` and `ns_common/db.py`

**Files:**
- Create: `contracts/schema.sql`, `ns_common/db.py`, `tests/test_db.py`

**Interfaces:**
- Produces:
```python
class Db:
    def __init__(self, path: Path)
    def connect(self) -> sqlite3.Connection      # WAL, foreign_keys on, row_factory=sqlite3.Row; cached
    def migrate(self) -> None                    # executes contracts/schema.sql (idempotent)
    def execute(self, sql: str, params: Sequence | Mapping = ()) -> sqlite3.Cursor   # commits
    def executemany(self, sql: str, rows: Iterable[Sequence]) -> None
    def query(self, sql: str, params=()) -> list[sqlite3.Row]
    def one(self, sql: str, params=()) -> sqlite3.Row | None
    def backup(self, backup_dir: Path, keep: int = 14, today: date | None = None) -> Path  # studio-YYYYMMDD.db
    def close(self) -> None
    def __enter__/__exit__
def new_job_id(slug: str, when: datetime | None = None) -> str   # "<slug>-<YYYYMMDD>-<4hex>"
```

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db.py
import re
from datetime import date
from pathlib import Path

from ns_common.db import Db, new_job_id


def test_migrate_creates_tables(tmp_path: Path):
    with Db(tmp_path / "studio.db") as db:
        db.migrate()
        names = {r["name"] for r in db.query("select name from sqlite_master where type='table'")}
    assert {"channels", "ideas", "stories", "parts", "clips", "clip_usage", "combos", "outputs", "runs",
            "uploads", "analytics", "pool_weights", "calibration", "incidents"} <= names


def test_migrate_is_idempotent(tmp_path: Path):
    db = Db(tmp_path / "s.db"); db.migrate(); db.migrate(); db.close()


def test_insert_and_query(tmp_path: Path):
    with Db(tmp_path / "s.db") as db:
        db.migrate()
        db.execute("insert into channels(slug, display_name, created_at) values (?,?,?)", ("a", "A", "2026-11-01"))
        assert db.one("select display_name from channels where slug=?", ("a",))["display_name"] == "A"


def test_backup_rotates(tmp_path: Path):
    db = Db(tmp_path / "s.db"); db.migrate()
    bdir = tmp_path / "backup"
    for d in range(1, 17):
        db.backup(bdir, keep=14, today=date(2026, 11, d))
    files = sorted(p.name for p in bdir.glob("studio-*.db"))
    assert len(files) == 14 and files[0] == "studio-20261103.db"
    db.close()


def test_new_job_id_format():
    jid = new_job_id("after-dark")
    assert re.fullmatch(r"after-dark-\d{8}-[0-9a-f]{4}", jid)
```

- [ ] **Step 2: Run to verify it fails** → FAIL.

- [ ] **Step 3: Write `contracts/schema.sql`**

```sql
-- Nightshift studio.db (spec §15.7). All statements idempotent.
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS channels (
  slug TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  mode TEXT NOT NULL,               -- daily | manual | gate
  summary_json TEXT
);

CREATE TABLE IF NOT EXISTS ideas (
  idea_id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT NOT NULL REFERENCES channels(slug),
  job_id TEXT,
  premise TEXT NOT NULL,
  twist TEXT NOT NULL,
  embedding BLOB,                    -- float32 little-endian vector
  created_at TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stories (
  job_id TEXT PRIMARY KEY,
  channel TEXT NOT NULL REFERENCES channels(slug),
  format TEXT NOT NULL,              -- master_first | shorts_only | series_compilation
  working_title TEXT,
  text TEXT NOT NULL,
  word_count INTEGER NOT NULL,
  opening_line TEXT NOT NULL,
  embedding BLOB,
  critic_avg REAL,
  revisions INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parts (
  job_id TEXT NOT NULL REFERENCES stories(job_id),
  n INTEGER NOT NULL,
  text TEXT NOT NULL,
  hook TEXT,
  word_count INTEGER NOT NULL,
  est_seconds REAL NOT NULL,
  PRIMARY KEY (job_id, n)
);

CREATE TABLE IF NOT EXISTS clips (
  source TEXT NOT NULL,              -- pexels | pixabay | fake
  clip_id TEXT NOT NULL,
  url TEXT NOT NULL,
  license TEXT NOT NULL,
  width INTEGER, height INTEGER, duration REAL,
  local_path TEXT,
  fetched_at TEXT NOT NULL,
  keywords TEXT,                     -- JSON array
  PRIMARY KEY (source, clip_id)
);

CREATE TABLE IF NOT EXISTS clip_usage (
  source TEXT NOT NULL,
  clip_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  job_id TEXT NOT NULL,
  item TEXT NOT NULL,                -- long | s<k>
  used_at TEXT NOT NULL,
  FOREIGN KEY (source, clip_id) REFERENCES clips(source, clip_id)
);
CREATE INDEX IF NOT EXISTS idx_clip_usage_clip ON clip_usage(source, clip_id, used_at);
CREATE INDEX IF NOT EXISTS idx_clip_usage_channel ON clip_usage(channel, used_at);

CREATE TABLE IF NOT EXISTS combos (
  combo_id TEXT PRIMARY KEY,
  channel TEXT NOT NULL,
  job_id TEXT NOT NULL,
  item TEXT NOT NULL,
  hook TEXT, structure TEXT, pov_tone TEXT, pacing TEXT,
  caption_style TEXT, visual_treatment TEXT, title_formula TEXT, length_bucket INTEGER,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_combos_channel ON combos(channel, created_at);

CREATE TABLE IF NOT EXISTS outputs (
  output_id TEXT PRIMARY KEY,        -- "<job_id>/long" | "<job_id>/s<k>"
  job_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  kind TEXT NOT NULL,                -- long | short
  k INTEGER,
  title TEXT NOT NULL,
  path TEXT NOT NULL,
  phash TEXT,
  duration_s REAL,
  combo_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected | uploaded
  review_note TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outputs_channel ON outputs(channel, created_at);

CREATE TABLE IF NOT EXISTS uploads (
  output_id TEXT PRIMARY KEY REFERENCES outputs(output_id),
  video_url TEXT NOT NULL,
  published_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analytics (
  output_id TEXT NOT NULL REFERENCES outputs(output_id),
  date TEXT NOT NULL,
  views INTEGER, viewed_pct REAL, avg_pct_viewed REAL, avg_view_duration_s REAL,
  likes INTEGER, comments INTEGER, shares INTEGER, subs_gained INTEGER,
  PRIMARY KEY (output_id, date)
);

CREATE TABLE IF NOT EXISTS pool_weights (
  channel TEXT NOT NULL,
  pool TEXT NOT NULL,
  option TEXT NOT NULL,
  weight REAL NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (channel, pool, option)
);

CREATE TABLE IF NOT EXISTS calibration (
  channel TEXT NOT NULL,
  story_id TEXT NOT NULL,
  critic_json TEXT NOT NULL,
  human_json TEXT,
  PRIMARY KEY (channel, story_id)
);

CREATE TABLE IF NOT EXISTS incidents (
  incident_id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT NOT NULL,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,                -- pause | resume | warning | strike
  note TEXT
);
```

- [ ] **Step 4: Implement `ns_common/db.py`**

```python
from __future__ import annotations

import secrets
import shutil
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from ns_common.paths import contracts_dir


def new_job_id(slug: str, when: datetime | None = None) -> str:
    when = when or datetime.now(UTC)
    return f"{slug}-{when:%Y%m%d}-{secrets.token_hex(2)}"


class Db:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            self._conn = conn
        return self._conn

    def migrate(self) -> None:
        sql = (contracts_dir() / "schema.sql").read_text(encoding="utf-8")
        self.connect().executescript(sql)

    def execute(self, sql: str, params: Sequence | Mapping = ()) -> sqlite3.Cursor:
        return self.connect().execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Sequence]) -> None:
        conn = self.connect()
        with conn:
            conn.executemany(sql, rows)

    def query(self, sql: str, params: Sequence | Mapping = ()) -> list[sqlite3.Row]:
        return list(self.connect().execute(sql, params).fetchall())

    def one(self, sql: str, params: Sequence | Mapping = ()) -> sqlite3.Row | None:
        return self.connect().execute(sql, params).fetchone()

    def backup(self, backup_dir: Path, keep: int = 14, today: date | None = None) -> Path:
        today = today or datetime.now(UTC).date()
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / f"studio-{today:%Y%m%d}.db"
        dest = sqlite3.connect(target)
        try:
            self.connect().backup(dest)
        finally:
            dest.close()
        existing = sorted(backup_dir.glob("studio-*.db"))
        for old in existing[:-keep] if len(existing) > keep else []:
            old.unlink()
        return target

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Db:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["Db", "new_job_id", "shutil"]
```
(Remove `shutil` from `__all__` and the import if ruff flags it unused; it is not needed.)

- [ ] **Step 5: Run** → 5 passed.

- [ ] **Step 6: Commit**

```bash
git add contracts/schema.sql ns_common/db.py tests/test_db.py
git commit -m "feat(contracts): SQLite schema and Db helper with rotating backup"
```

---
### Task 8: `ns_common/textstats.py`

**Files:**
- Create: `ns_common/textstats.py`, `tests/test_textstats.py`

**Interfaces:**
- Produces:
```python
def split_sentences(text: str) -> list[str]      # regex sentence splitter; keeps terminal punctuation; no empty items
def word_count(text: str) -> int
def est_seconds(text: str, wpm: float) -> float    # word_count/wpm*60, rounded to 2 dp
def fk_grade(text: str) -> float                   # Flesch-Kincaid grade with a syllable heuristic
def normalize_tokens(text: str) -> list[str]       # lowercase, digits spelled via num2words-free table for 0-99, punctuation stripped
def syllables(word: str) -> int
```

- [ ] **Step 1: Write the failing test**

```python
# tests/test_textstats.py
from ns_common.textstats import est_seconds, fk_grade, normalize_tokens, split_sentences, syllables, word_count


def test_split_sentences_basic():
    s = split_sentences("I heard it again. Did you? \"Run,\" she said!  Nothing happened...")
    assert s == ["I heard it again.", "Did you?", "\"Run,\" she said!", "Nothing happened..."]


def test_split_handles_abbreviations_and_newlines():
    s = split_sentences("Mr. Hale left at 3 p.m. on Monday.\n\nThe door was open.")
    assert s == ["Mr. Hale left at 3 p.m. on Monday.", "The door was open."]


def test_word_count_and_est_seconds():
    t = "one two three four five six seven eight nine ten"
    assert word_count(t) == 10
    assert est_seconds(t, 150) == 4.0


def test_syllables_and_grade():
    assert syllables("the") == 1 and syllables("quiet") == 2 and syllables("basement") == 2
    simple = "The dog ran. The cat sat. It was dark. I was cold."
    assert fk_grade(simple) < 4
    hard = "The epistemological ramifications of the aforementioned investigation remained fundamentally indeterminate."
    assert fk_grade(hard) > 12


def test_normalize_tokens():
    assert normalize_tokens("It's 3 A.M., isn't it?") == ["its", "three", "am", "isnt", "it"]
```

- [ ] **Step 2: Run to verify it fails** → FAIL.

- [ ] **Step 3: Implement**

```python
# ns_common/textstats.py
from __future__ import annotations

import re

_ABBREV = {"mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "etc", "a.m", "p.m", "e.g", "i.e"}
_SENT_END = re.compile(r"([.!?]+[\"'”’)]*)(\s+|$)")
_WORD = re.compile(r"[A-Za-z0-9']+")
_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
         "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text.strip())
    out: list[str] = []
    start = 0
    for m in _SENT_END.finditer(text):
        end = m.end(1)
        candidate = text[start:end].strip()
        prev_word = re.findall(r"[A-Za-z.]+", candidate[-8:])
        tail = (prev_word[-1].rstrip(".").lower() if prev_word else "")
        if tail in _ABBREV and m.group(1) == ".":
            continue
        if candidate:
            out.append(candidate)
        start = m.end()
    rest = text[start:].strip()
    if rest:
        out.append(rest)
    return out


def word_count(text: str) -> int:
    return len(_WORD.findall(text))


def est_seconds(text: str, wpm: float) -> float:
    return round(word_count(text) / wpm * 60.0, 2)


def syllables(word: str) -> int:
    w = word.lower().strip("'")
    if not w:
        return 0
    if len(w) <= 3:
        return 1
    w = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", w)
    w = re.sub(r"^y", "", w)
    groups = re.findall(r"[aeiouy]+", w)
    return max(1, len(groups))


def fk_grade(text: str) -> float:
    sentences = split_sentences(text) or [text]
    words = _WORD.findall(text)
    if not words:
        return 0.0
    syl = sum(syllables(w) for w in words)
    grade = 0.39 * (len(words) / len(sentences)) + 11.8 * (syl / len(words)) - 15.59
    return round(grade, 2)


def _num_words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + ("" if ones == 0 else " " + _ONES[ones])
    return str(n)


def normalize_tokens(text: str) -> list[str]:
    text = text.lower().replace("’", "'")
    text = re.sub(r"(?<=\w)\.(?=\w\.)", "", text)   # a.m. -> am.
    text = re.sub(r"\b(\d{1,2})\b", lambda m: _num_words(int(m.group(1))), text)
    text = text.replace("'", "")
    return re.findall(r"[a-z0-9]+", text)
```

- [ ] **Step 4: Run** → 5 passed. If `test_split_handles_abbreviations_and_newlines` fails on "p.m." adjust `_ABBREV` handling: the candidate tail for "3 p.m." is `p.m` after stripping one dot. The regex `[A-Za-z.]+` captures `p.m.`; `rstrip(".")` gives `p.m`, which is in the set.

- [ ] **Step 5: Commit**

```bash
git add ns_common/textstats.py tests/test_textstats.py
git commit -m "feat(common): sentence split, word stats, FK grade, token normalization"
```

---

### Task 9: `ns_common/ffmpeg.py`

**Files:**
- Create: `ns_common/ffmpeg.py`, `tests/test_ffmpeg.py`

**Interfaces:**
- Produces:
```python
def ffmpeg_exe() -> str            # from env NS_FFMPEG or PATH; raises NsError("NS-E401", "ffmpeg not found")
def ffprobe_exe() -> str
def run(args: list[str], *, timeout_s: int = 3600, log: NsLogger | None = None) -> subprocess.CompletedProcess[str]
    # runs ffmpeg with -hide_banner -y -nostdin, raises NsError("NS-E401", stderr tail) on non-zero
def probe(path: Path) -> dict        # ffprobe -show_format -show_streams as dict
def duration_s(path: Path) -> float
def video_size(path: Path) -> tuple[int, int]
def has_faststart(path: Path) -> bool   # 'moov' atom before 'mdat' in the file
def make_test_clip(out: Path, *, width: int, height: int, seconds: float, kind: Literal["testsrc2","color"]="testsrc2", color: str="0x202020", fps: int=30) -> Path
def make_silence(out: Path, seconds: float, sr: int = 24000) -> Path
def make_tone_wav(out: Path, seconds: float, sr: int = 24000, freq: float = 440.0) -> Path
```

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ffmpeg.py
from pathlib import Path

import pytest

from ns_common import ffmpeg as ff

pytestmark = pytest.mark.skipif(not ff.available(), reason="ffmpeg not on PATH")


def test_make_test_clip_and_probe(tmp_path: Path):
    clip = ff.make_test_clip(tmp_path / "c.mp4", width=320, height=180, seconds=1.0)
    assert clip.exists()
    assert ff.video_size(clip) == (320, 180)
    assert 0.9 <= ff.duration_s(clip) <= 1.1
    assert ff.has_faststart(clip)


def test_make_tone_wav(tmp_path: Path):
    wav = ff.make_tone_wav(tmp_path / "t.wav", seconds=0.5)
    info = ff.probe(wav)
    assert info["streams"][0]["codec_type"] == "audio"
    assert int(info["streams"][0]["sample_rate"]) == 24000


def test_run_raises_nserror_on_failure(tmp_path: Path):
    from ns_common.errors import NsError

    with pytest.raises(NsError) as ei:
        ff.run(["-i", str(tmp_path / "missing.mp4"), str(tmp_path / "o.mp4")])
    assert ei.value.code == "NS-E401"
```

- [ ] **Step 2: Run to verify it fails** → FAIL.

- [ ] **Step 3: Implement**

```python
# ns_common/ffmpeg.py
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from ns_common.errors import NsError
from ns_common.logging import NsLogger


def _find(name: str, env_var: str) -> str | None:
    return os.environ.get(env_var) or shutil.which(name)


def available() -> bool:
    return _find("ffmpeg", "NS_FFMPEG") is not None and _find("ffprobe", "NS_FFPROBE") is not None


def ffmpeg_exe() -> str:
    exe = _find("ffmpeg", "NS_FFMPEG")
    if not exe:
        raise NsError("NS-E401", "ffmpeg not found on PATH (set NS_FFMPEG)")
    return exe


def ffprobe_exe() -> str:
    exe = _find("ffprobe", "NS_FFPROBE")
    if not exe:
        raise NsError("NS-E401", "ffprobe not found on PATH (set NS_FFPROBE)")
    return exe


def run(args: list[str], *, timeout_s: int = 3600, log: NsLogger | None = None) -> subprocess.CompletedProcess[str]:
    cmd = [ffmpeg_exe(), "-hide_banner", "-y", "-nostdin", *args]
    if log:
        log.debug("ffmpeg_run", cmd=" ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-8:])
        raise NsError("NS-E401", tail, cmd=cmd)
    return proc


def probe(path: Path) -> dict:
    cmd = [ffprobe_exe(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise NsError("NS-E401", proc.stderr.strip()[-400:], path=str(path))
    return json.loads(proc.stdout)


def duration_s(path: Path) -> float:
    return float(probe(path)["format"]["duration"])


def video_size(path: Path) -> tuple[int, int]:
    for s in probe(path)["streams"]:
        if s["codec_type"] == "video":
            return int(s["width"]), int(s["height"])
    raise NsError("NS-E401", "no video stream", path=str(path))


def has_faststart(path: Path) -> bool:
    with path.open("rb") as fh:
        head = fh.read(4 * 1024 * 1024)
    return head.find(b"moov") != -1 and (head.find(b"mdat") == -1 or head.find(b"moov") < head.find(b"mdat"))


def make_test_clip(
    out: Path, *, width: int, height: int, seconds: float,
    kind: Literal["testsrc2", "color"] = "testsrc2", color: str = "0x202020", fps: int = 30,
) -> Path:
    src = f"testsrc2=size={width}x{height}:rate={fps}" if kind == "testsrc2" else f"color=c={color}:size={width}x{height}:rate={fps}"
    out.parent.mkdir(parents=True, exist_ok=True)
    run(["-f", "lavfi", "-i", src, "-t", f"{seconds:.3f}", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)])
    return out


def make_silence(out: Path, seconds: float, sr: int = 24000) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    run(["-f", "lavfi", "-i", f"anullsrc=r={sr}:cl=mono", "-t", f"{seconds:.3f}", "-c:a", "pcm_s16le", str(out)])
    return out


def make_tone_wav(out: Path, seconds: float, sr: int = 24000, freq: float = 440.0) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    run(["-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate={sr}", "-t", f"{seconds:.3f}",
         "-ac", "1", "-c:a", "pcm_s16le", str(out)])
    return out
```

- [ ] **Step 4: Run** → 3 passed (FFmpeg is installed on the build machine).

- [ ] **Step 5: Commit**

```bash
git add ns_common/ffmpeg.py tests/test_ffmpeg.py
git commit -m "feat(common): ffmpeg/ffprobe helpers and synthetic media generators"
```

---

### Task 10: LLM client and fake — `ns_common/llm_client.py`

**Files:**
- Create: `ns_common/llm_client.py`, `tests/test_llm_client.py`, `tests/fixtures/llm/_example.json`

**Interfaces:**
- Produces:
```python
class Llm(Protocol):
    def complete_json(self, *, prompt_name: str, system: str, user: str, schema: dict,
                      thinking: bool = False, temperature: float = 0.8, max_tokens: int = 4096) -> dict
    def complete_text(self, *, prompt_name: str, system: str, user: str,
                      thinking: bool = False, temperature: float = 0.8, max_tokens: int = 2048) -> str
    def health(self) -> bool

class LlmClient(Llm):
    def __init__(self, base_url: str, alias: str, *, timeout_s: float = 600, client: httpx.Client | None = None)
    # POST {base_url}/v1/chat/completions with model=alias; JSON mode uses response_format json_schema;
    # thinking toggled via chat_template_kwargs.enable_thinking; strips <think>…</think>; one repair retry on bad JSON;
    # raises NsError("NS-E301") on transport/HTTP failure, NsError("NS-E302") after the repair retry fails.

class FakeLlm(Llm):
    def __init__(self, fixtures_dir: Path, *, overrides: dict[str, dict | list[dict]] | None = None)
    # complete_json returns fixtures_dir/<prompt_name>.json (or the next item of a list override, cycling);
    # complete_text returns fixtures_dir/<prompt_name>.txt or "FAKE TEXT for <prompt_name>".
    calls: list[dict]   # every call recorded: {prompt_name, system, user, thinking, schema?}
```

- [ ] **Step 1: Write the failing test**

```python
# tests/test_llm_client.py
import json
from pathlib import Path

import httpx
import pytest

from ns_common.errors import NsError
from ns_common.llm_client import FakeLlm, LlmClient

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}


def _client(handler):
    transport = httpx.MockTransport(handler)
    return LlmClient("http://127.0.0.1:8080", "story", client=httpx.Client(transport=transport))


def test_complete_json_sends_schema_and_parses():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "<think>hm</think>{\"a\": 3}"}}]})

    llm = _client(handler)
    out = llm.complete_json(prompt_name="ideas", system="S", user="U", schema=SCHEMA, thinking=True)
    assert out == {"a": 3}
    assert seen["model"] == "story"
    assert seen["response_format"]["type"] == "json_schema"
    assert seen["response_format"]["json_schema"]["schema"] == SCHEMA
    assert seen["chat_template_kwargs"] == {"enable_thinking": True}
    assert seen["messages"][0] == {"role": "system", "content": "S"}


def test_bad_json_triggers_one_repair_then_e302():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

    with pytest.raises(NsError) as ei:
        _client(handler).complete_json(prompt_name="x", system="S", user="U", schema=SCHEMA)
    assert ei.value.code == "NS-E302"
    assert len(calls) == 2
    assert "repair" in calls[1]["messages"][-1]["content"].lower()


def test_transport_error_is_e301():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(NsError) as ei:
        _client(handler).complete_text(prompt_name="x", system="S", user="U")
    assert ei.value.code == "NS-E301"


def test_health():
    ok = _client(lambda r: httpx.Response(200, json={"status": "ok"})).health()
    assert ok is True
    bad = _client(lambda r: httpx.Response(503, json={"status": "loading"})).health()
    assert bad is False


def test_fake_llm_fixtures_and_overrides(tmp_path: Path):
    (tmp_path / "ideas.json").write_text(json.dumps({"a": 1}), encoding="utf-8")
    fake = FakeLlm(tmp_path, overrides={"judge": [{"a": 9}, {"a": 8}]})
    assert fake.complete_json(prompt_name="ideas", system="", user="", schema=SCHEMA) == {"a": 1}
    assert fake.complete_json(prompt_name="judge", system="", user="", schema=SCHEMA) == {"a": 9}
    assert fake.complete_json(prompt_name="judge", system="", user="", schema=SCHEMA) == {"a": 8}
    assert fake.complete_json(prompt_name="judge", system="", user="", schema=SCHEMA) == {"a": 9}
    assert fake.complete_text(prompt_name="nothing", system="", user="").startswith("FAKE TEXT")
    assert [c["prompt_name"] for c in fake.calls] == ["ideas", "judge", "judge", "judge", "nothing"]
    with pytest.raises(FileNotFoundError):
        fake.complete_json(prompt_name="missing", system="", user="", schema=SCHEMA)
```

- [ ] **Step 2: Run to verify it fails** → FAIL.

- [ ] **Step 3: Implement**

```python
# ns_common/llm_client.py
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol

import httpx

from ns_common.errors import NsError

_THINK = re.compile(r"<think>.*?</think>\s*", re.S)


class Llm(Protocol):
    def complete_json(self, *, prompt_name: str, system: str, user: str, schema: dict,
                      thinking: bool = False, temperature: float = 0.8, max_tokens: int = 4096) -> dict: ...
    def complete_text(self, *, prompt_name: str, system: str, user: str,
                      thinking: bool = False, temperature: float = 0.8, max_tokens: int = 2048) -> str: ...
    def health(self) -> bool: ...


def strip_thinking(text: str) -> str:
    return _THINK.sub("", text).strip()


class LlmClient:
    def __init__(self, base_url: str, alias: str, *, timeout_s: float = 600, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.alias = alias
        self.client = client or httpx.Client(timeout=timeout_s)

    def _chat(self, messages: list[dict], *, thinking: bool, temperature: float, max_tokens: int,
              schema: dict | None) -> str:
        body: dict[str, Any] = {
            "model": self.alias,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": thinking},
        }
        if schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}}
        try:
            r = self.client.post(f"{self.base_url}/v1/chat/completions", json=body)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise NsError("NS-E301", str(e), url=self.base_url) from e
        try:
            return strip_thinking(r.json()["choices"][0]["message"]["content"])
        except (KeyError, IndexError, ValueError) as e:
            raise NsError("NS-E301", f"malformed response: {e}") from e

    def complete_json(self, *, prompt_name: str, system: str, user: str, schema: dict,
                      thinking: bool = False, temperature: float = 0.8, max_tokens: int = 4096) -> dict:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        raw = self._chat(messages, thinking=thinking, temperature=temperature, max_tokens=max_tokens, schema=schema)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        messages += [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": "Repair: your previous answer was not valid JSON. Reply with only the JSON object."},
        ]
        raw2 = self._chat(messages, thinking=False, temperature=0.2, max_tokens=max_tokens, schema=schema)
        try:
            return json.loads(raw2)
        except json.JSONDecodeError as e:
            raise NsError("NS-E302", f"{prompt_name}: {raw2[:200]}") from e

    def complete_text(self, *, prompt_name: str, system: str, user: str,
                      thinking: bool = False, temperature: float = 0.8, max_tokens: int = 2048) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return self._chat(messages, thinking=thinking, temperature=temperature, max_tokens=max_tokens, schema=None)

    def health(self) -> bool:
        try:
            r = self.client.get(f"{self.base_url}/health", timeout=5)
            return r.status_code == 200 and r.json().get("status") == "ok"
        except (httpx.HTTPError, ValueError):
            return False


class FakeLlm:
    def __init__(self, fixtures_dir: Path, *, overrides: dict[str, dict | list[dict]] | None = None) -> None:
        self.fixtures_dir = fixtures_dir
        self.overrides = overrides or {}
        self._cursor: dict[str, int] = {}
        self.calls: list[dict] = []

    def _record(self, **kw: Any) -> None:
        self.calls.append(kw)

    def complete_json(self, *, prompt_name: str, system: str, user: str, schema: dict,
                      thinking: bool = False, temperature: float = 0.8, max_tokens: int = 4096) -> dict:
        self._record(prompt_name=prompt_name, system=system, user=user, thinking=thinking, schema=schema)
        if prompt_name in self.overrides:
            ov = self.overrides[prompt_name]
            if isinstance(ov, list):
                i = self._cursor.get(prompt_name, 0)
                self._cursor[prompt_name] = (i + 1) % len(ov)
                return json.loads(json.dumps(ov[i]))
            return json.loads(json.dumps(ov))
        f = self.fixtures_dir / f"{prompt_name}.json"
        if not f.exists():
            raise FileNotFoundError(f)
        return json.loads(f.read_text(encoding="utf-8"))

    def complete_text(self, *, prompt_name: str, system: str, user: str,
                      thinking: bool = False, temperature: float = 0.8, max_tokens: int = 2048) -> str:
        self._record(prompt_name=prompt_name, system=system, user=user, thinking=thinking)
        f = self.fixtures_dir / f"{prompt_name}.txt"
        return f.read_text(encoding="utf-8") if f.exists() else f"FAKE TEXT for {prompt_name}"

    def health(self) -> bool:
        return True
```

`tests/fixtures/llm/_example.json`: `{"note": "Slice 1 adds one fixture per prompt name here"}`

- [ ] **Step 4: Run** → 5 passed.

- [ ] **Step 5: Commit**

```bash
git add ns_common/llm_client.py tests/test_llm_client.py tests/fixtures/llm/_example.json
git commit -m "feat(common): llama-server client with JSON-schema mode and FakeLlm"
```

---

### Task 11: TTS client and fake — `ns_common/tts_client.py`

**Files:**
- Create: `ns_common/tts_client.py`, `tests/test_tts_client.py`

**Interfaces:**
- Produces:
```python
@dataclass(frozen=True) class TtsItem: id: str; text: str; voice: str; speed: float = 1.0
@dataclass(frozen=True) class TtsFile: id: str; path: Path; seconds: float
class Tts(Protocol):
    def speak(self, text: str, voice: str, speed: float, out_path: Path, *, seed: int | None = None) -> float
    def speak_batch(self, items: list[TtsItem], out_dir: Path, *, seed: int | None = None) -> list[TtsFile]
    def voices(self) -> list[dict]
    def health(self) -> bool
class TtsClient(Tts):  __init__(base_url, *, timeout_s=1800, client=None)   # spec §15.4 endpoints; NsError NS-E301 on failure (TTS shares the 3xx range: use "NS-E301" with detail "tts: …")
class FakeTts(Tts):   __init__(*, wpm: float = 155, sr: int = 24000)
    # duration = max(0.4, words/wpm*60); one 80 ms tone burst per word, evenly spaced; written with stdlib wave
def wav_seconds(path: Path) -> float
```

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tts_client.py
import json
import wave
from pathlib import Path

import httpx

from ns_common.tts_client import FakeTts, TtsClient, TtsItem, wav_seconds


def test_fake_tts_duration_tracks_words(tmp_path: Path):
    fake = FakeTts(wpm=150)
    secs = fake.speak("one two three four five six seven eight nine ten", "Ryan", 1.0, tmp_path / "a.wav")
    assert abs(secs - 4.0) < 0.01
    with wave.open(str(tmp_path / "a.wav"), "rb") as w:
        assert w.getframerate() == 24000 and w.getnchannels() == 1
    assert abs(wav_seconds(tmp_path / "a.wav") - 4.0) < 0.01


def test_fake_batch(tmp_path: Path):
    files = FakeTts().speak_batch([TtsItem("s1", "hello there", "Ryan"), TtsItem("s2", "bye", "Ryan")], tmp_path)
    assert [f.id for f in files] == ["s1", "s2"]
    assert all(f.path.exists() and f.seconds >= 0.4 for f in files)


def test_client_speak_writes_wav(tmp_path: Path):
    wav_bytes = (tmp_path / "src.wav")
    FakeTts().speak("hi", "Ryan", 1.0, wav_bytes)

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["model"] == "qwen3-tts" and body["response_format"] == "wav" and body["voice"] == "Ryan"
        return httpx.Response(200, content=wav_bytes.read_bytes(), headers={"content-type": "audio/wav"})

    cli = TtsClient("http://127.0.0.1:8091", client=httpx.Client(transport=httpx.MockTransport(handler)))
    secs = cli.speak("hi", "Ryan", 1.0, tmp_path / "out.wav")
    assert (tmp_path / "out.wav").exists() and secs > 0


def test_client_batch_and_voices(tmp_path: Path):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/batch"):
            return httpx.Response(200, json={"files": [{"id": "a", "path": str(tmp_path / "a.wav"), "seconds": 1.5}], "engine": "faster", "rtf": 0.3})
        return httpx.Response(200, json=[{"name": "Ryan", "mode": "preset", "source": "builtin"}])

    cli = TtsClient("http://127.0.0.1:8091", client=httpx.Client(transport=httpx.MockTransport(handler)))
    out = cli.speak_batch([TtsItem("a", "x", "Ryan")], tmp_path)
    assert out[0].seconds == 1.5 and out[0].path == tmp_path / "a.wav"
    assert cli.voices()[0]["name"] == "Ryan"
```

- [ ] **Step 2: Run to verify it fails** → FAIL.

- [ ] **Step 3: Implement**

```python
# ns_common/tts_client.py
from __future__ import annotations

import math
import struct
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from ns_common.errors import NsError
from ns_common.textstats import word_count


@dataclass(frozen=True)
class TtsItem:
    id: str
    text: str
    voice: str
    speed: float = 1.0


@dataclass(frozen=True)
class TtsFile:
    id: str
    path: Path
    seconds: float


class Tts(Protocol):
    def speak(self, text: str, voice: str, speed: float, out_path: Path, *, seed: int | None = None) -> float: ...
    def speak_batch(self, items: list[TtsItem], out_dir: Path, *, seed: int | None = None) -> list[TtsFile]: ...
    def voices(self) -> list[dict]: ...
    def health(self) -> bool: ...


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


class TtsClient:
    def __init__(self, base_url: str, *, timeout_s: float = 1800, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=timeout_s)

    def speak(self, text: str, voice: str, speed: float, out_path: Path, *, seed: int | None = None) -> float:
        body = {"model": "qwen3-tts", "input": text, "voice": voice, "speed": speed, "response_format": "wav"}
        if seed is not None:
            body["seed"] = seed
        try:
            r = self.client.post(f"{self.base_url}/v1/audio/speech", json=body)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise NsError("NS-E301", f"tts: {e}") from e
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(r.content)
        return wav_seconds(out_path)

    def speak_batch(self, items: list[TtsItem], out_dir: Path, *, seed: int | None = None) -> list[TtsFile]:
        body = {"items": [i.__dict__ | {"input": i.text} for i in items], "out_dir": str(out_dir)}
        for it in body["items"]:
            it.pop("text", None)
        if seed is not None:
            body["seed"] = seed
        try:
            r = self.client.post(f"{self.base_url}/v1/audio/speech/batch", json=body)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise NsError("NS-E301", f"tts batch: {e}") from e
        return [TtsFile(f["id"], Path(f["path"]), float(f["seconds"])) for f in r.json()["files"]]

    def voices(self) -> list[dict]:
        try:
            r = self.client.get(f"{self.base_url}/v1/voices")
            r.raise_for_status()
            return list(r.json())
        except httpx.HTTPError as e:
            raise NsError("NS-E301", f"tts voices: {e}") from e

    def health(self) -> bool:
        try:
            r = self.client.get(f"{self.base_url}/health", timeout=5)
            return r.status_code == 200 and bool(r.json().get("ok"))
        except (httpx.HTTPError, ValueError):
            return False


class FakeTts:
    def __init__(self, *, wpm: float = 155, sr: int = 24000) -> None:
        self.wpm = wpm
        self.sr = sr

    def speak(self, text: str, voice: str, speed: float, out_path: Path, *, seed: int | None = None) -> float:
        words = max(1, word_count(text))
        seconds = max(0.4, words / (self.wpm * speed) * 60.0)
        n = int(seconds * self.sr)
        burst = int(0.08 * self.sr)
        step = n / words
        frames = bytearray()
        for i in range(n):
            pos = i % step if step > 0 else 0
            amp = 0.3 * math.sin(2 * math.pi * 440.0 * i / self.sr) if pos < burst else 0.0
            frames += struct.pack("<h", int(amp * 32767))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sr)
            w.writeframes(bytes(frames))
        return round(n / self.sr, 3)

    def speak_batch(self, items: list[TtsItem], out_dir: Path, *, seed: int | None = None) -> list[TtsFile]:
        out: list[TtsFile] = []
        for it in items:
            p = out_dir / f"{it.id}.wav"
            out.append(TtsFile(it.id, p, self.speak(it.text, it.voice, it.speed, p, seed=seed)))
        return out

    def voices(self) -> list[dict]:
        return [{"name": "Ryan", "mode": "preset", "source": "fake"}, {"name": "Aiden", "mode": "preset", "source": "fake"}]

    def health(self) -> bool:
        return True
```

- [ ] **Step 4: Run** → 4 passed.

- [ ] **Step 5: Commit**

```bash
git add ns_common/tts_client.py tests/test_tts_client.py
git commit -m "feat(common): TTS client per §15.4 and FakeTts tone-burst WAVs"
```

---

### Task 12: Embedding client and fake — `ns_common/embed_client.py`

**Files:**
- Create: `ns_common/embed_client.py`, `tests/test_embed_client.py`

**Interfaces:**
- Produces:
```python
class Embed(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]
    def health(self) -> bool
class EmbedClient(Embed): __init__(base_url, *, client=None)   # POST /v1/embeddings {"input": texts} → data[i].embedding; NsError NS-E301 "embed: …"
class FakeEmbed(Embed):   __init__(dim: int = 256)             # hashed bag-of-words, L2-normalised; deterministic
def cosine(a: Sequence[float], b: Sequence[float]) -> float
def to_blob(vec: Sequence[float]) -> bytes / def from_blob(b: bytes) -> list[float]   # float32 LE for SQLite
```

- [ ] **Step 1: Write the failing test**

```python
# tests/test_embed_client.py
import httpx

from ns_common.embed_client import EmbedClient, FakeEmbed, cosine, from_blob, to_blob


def test_fake_embed_is_deterministic_and_meaningful():
    e = FakeEmbed()
    a = e.embed(["the old house was dark"])[0]
    b = e.embed(["the old house was dark"])[0]
    c = e.embed(["a sunny beach party"])[0]
    assert a == b
    assert cosine(a, b) > 0.999
    assert cosine(a, c) < 0.3


def test_blob_roundtrip():
    v = [0.1, -0.5, 2.0]
    assert [round(x, 5) for x in from_blob(to_blob(v))] == v


def test_client_parses_openai_shape():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}, {"index": 1, "embedding": [0.0, 1.0]}]})

    cli = EmbedClient("http://127.0.0.1:8081", client=httpx.Client(transport=httpx.MockTransport(handler)))
    out = cli.embed(["a", "b"])
    assert out == [[1.0, 0.0], [0.0, 1.0]]
    assert round(cosine(out[0], out[1]), 6) == 0.0
```

- [ ] **Step 2: Run to verify it fails** → FAIL.

- [ ] **Step 3: Implement**

```python
# ns_common/embed_client.py
from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Sequence
from typing import Protocol

import httpx

from ns_common.errors import NsError
from ns_common.textstats import normalize_tokens


class Embed(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...
    def health(self) -> bool: ...


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 0.0 if na == 0 or nb == 0 else dot / (na * nb)


def to_blob(vec: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def from_blob(b: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(b) // 4}f", b))


class EmbedClient:
    def __init__(self, base_url: str, *, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=120)

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            r = self.client.post(f"{self.base_url}/v1/embeddings", json={"input": texts})
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            return [[float(x) for x in d["embedding"]] for d in data]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise NsError("NS-E301", f"embed: {e}") from e

    def health(self) -> bool:
        try:
            return self.client.get(f"{self.base_url}/health", timeout=5).status_code == 200
        except httpx.HTTPError:
            return False


class FakeEmbed:
    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def _one(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in normalize_tokens(text):
            h = hashlib.blake2b(tok.encode(), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "little") % self.dim
            sign = 1.0 if h[4] % 2 == 0 else -1.0
            v[idx] += sign
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def health(self) -> bool:
        return True
```

- [ ] **Step 4: Run** → 3 passed.

- [ ] **Step 5: Commit**

```bash
git add ns_common/embed_client.py tests/test_embed_client.py
git commit -m "feat(common): embeddings client, FakeEmbed and cosine helpers"
```

---
### Task 13: Service acquisition interface — `ns_common/acquire.py`

**Files:**
- Create: `ns_common/acquire.py`, `tests/test_acquire.py`

**Interfaces:**
- Produces:
```python
Service = Literal["llama", "embed", "tts", "whisper"]
@dataclass(frozen=True) class Endpoint: base_url: str; model_alias: str | None = None
class Acquirer(Protocol):
    def acquire(self, service: Service, owner: str, *, model_alias: str | None = None, timeout_s: int = 7200) -> Endpoint
    def release(self, service: Service, owner: str) -> None
class DevAcquirer(Acquirer):
    def __init__(self, runtime: Runtime)      # returns the fixed endpoints from runtime.json; records a log of acquire/release pairs
    held: dict[Service, str]                  # service → owner; acquiring a held GPU service by another owner raises NsError("NS-E201")
```
`whisper` is in-process; `acquire("whisper", …)` returns `Endpoint("inproc")` but still participates in the GPU lock.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_acquire.py
import pytest

from ns_common.acquire import DevAcquirer
from ns_common.errors import NsError
from ns_common.models import Runtime


def test_dev_acquirer_endpoints_and_lock():
    acq = DevAcquirer(Runtime.default("cpu-dev"))
    ep = acq.acquire("llama", "studio:j1", model_alias="story")
    assert ep.base_url == "http://127.0.0.1:8080" and ep.model_alias == "story"
    assert acq.acquire("embed", "studio:j1").base_url == "http://127.0.0.1:8081"   # embed never locks
    with pytest.raises(NsError) as ei:
        acq.acquire("tts", "studio:j2")      # GPU already held by j1 via llama
    assert ei.value.code == "NS-E201"
    acq.release("llama", "studio:j1")
    assert acq.acquire("tts", "studio:j2").base_url == "http://127.0.0.1:8091"
    acq.release("tts", "studio:j2")
    assert acq.acquire("whisper", "studio:j2").base_url == "inproc"
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement**

```python
# ns_common/acquire.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from ns_common.errors import NsError
from ns_common.models import Runtime

Service = Literal["llama", "embed", "tts", "whisper"]
GPU_SERVICES: tuple[Service, ...] = ("llama", "tts", "whisper")


@dataclass(frozen=True)
class Endpoint:
    base_url: str
    model_alias: str | None = None


class Acquirer(Protocol):
    def acquire(self, service: Service, owner: str, *, model_alias: str | None = None, timeout_s: int = 7200) -> Endpoint: ...
    def release(self, service: Service, owner: str) -> None: ...


class DevAcquirer:
    """No real process management: hands out the configured endpoints and enforces the one-GPU-tenant rule in memory."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.held: dict[Service, str] = {}
        self.events: list[tuple[str, Service, str]] = []

    def _gpu_owner(self) -> tuple[Service, str] | None:
        for s in GPU_SERVICES:
            if s in self.held:
                return s, self.held[s]
        return None

    def acquire(self, service: Service, owner: str, *, model_alias: str | None = None, timeout_s: int = 7200) -> Endpoint:
        if service in GPU_SERVICES:
            cur = self._gpu_owner()
            if cur and cur[1] != owner:
                raise NsError("NS-E201", f"GPU held by {cur[1]} via {cur[0]}; {owner} wanted {service}")
            if cur and cur[0] != service:
                raise NsError("NS-E201", f"{owner} already holds {cur[0]}; release it before {service}")
            self.held[service] = owner
        self.events.append(("acquire", service, owner))
        if service == "llama":
            return Endpoint(self.runtime.llama.base_url, model_alias or "story")
        if service == "embed":
            return Endpoint(self.runtime.embed.base_url)
        if service == "tts":
            return Endpoint(self.runtime.tts.base_url)
        return Endpoint("inproc")

    def release(self, service: Service, owner: str) -> None:
        if self.held.get(service) == owner:
            del self.held[service]
        self.events.append(("release", service, owner))
```

- [ ] **Step 4: Run** → 1 passed.

- [ ] **Step 5: Commit**

```bash
git add ns_common/acquire.py tests/test_acquire.py
git commit -m "feat(common): Acquirer protocol and in-memory DevAcquirer"
```

---

### Task 14: `manifest.json` and `tools/resolve_manifest.py`

**Files:**
- Create: `manifest.json`, `tools/__init__.py`, `tools/resolve_manifest.py`, `tests/test_resolve_manifest.py`
- Modify: `tests/test_config.py` (remove the xfail marker from Task 6)

**Interfaces:**
- Produces: `manifest.json` validating against `Manifest`. `python tools/resolve_manifest.py --fill` fills every `sha256`/`bytes` from the Hugging Face tree API (`GET https://huggingface.co/api/models/{repo}/tree/{revision}?recursive=true` → items `{path, size, lfs:{oid}}`; `oid` is the SHA256) and GitHub release assets (`GET https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}` → `assets[].digest` = `"sha256:…"`). `--check` sends HEAD/GET to every URL and exits non-zero if any is unreachable or any `sha256` is still null. **Nothing is downloaded.** Functions: `hf_tree(repo, revision, client) -> dict[str, tuple[str, int]]` (path → (sha256, bytes)), `github_release_assets(owner, repo, tag, client) -> dict[str, tuple[str, str]]` (asset name → (url, sha256)), `fill(manifest: dict, client) -> dict`, `check(manifest: dict, client) -> list[str]` (problems).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_resolve_manifest.py
import json

import httpx

from tools import resolve_manifest as rm

TREE = [
    {"type": "file", "path": "Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL.gguf", "size": 21000000000, "lfs": {"oid": "ab" * 32, "size": 21000000000}},
    {"type": "file", "path": "README.md", "size": 100},
]
RELEASE = {"assets": [{"name": "llama-b9999-bin-win-cuda-12.8-x64.zip", "browser_download_url": "https://github.com/ggml-org/llama.cpp/releases/download/b9999/llama-b9999-bin-win-cuda-12.8-x64.zip", "digest": "sha256:" + "cd" * 32}]}


def _client():
    def handler(req: httpx.Request) -> httpx.Response:
        if "huggingface.co/api/models" in str(req.url):
            return httpx.Response(200, json=TREE)
        if "api.github.com" in str(req.url):
            return httpx.Response(200, json=RELEASE)
        return httpx.Response(200)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_hf_tree_maps_sha():
    t = rm.hf_tree("unsloth/Qwen3.6-35B-A3B-MTP-GGUF", "main", _client())
    assert t["Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL.gguf"] == ("ab" * 32, 21000000000)
    assert "README.md" not in t


def test_fill_sets_sha_and_bytes():
    mf = {"version": "0.1.0", "python_version": "3.12", "spacy_model": "en_core_web_md", "tts_packages": [],
          "profiles": {"32gb": {"llm": {"repo": "unsloth/Qwen3.6-35B-A3B-MTP-GGUF", "file": "Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL.gguf"},
                                "embed": {"repo": "x/y", "file": "Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL.gguf"},
                                "tts": [{"repo": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"}], "whisper_model": "small.en"}},
          "bins": {"llama": {"version": "b9999", "url": "https://github.com/ggml-org/llama.cpp/releases/download/b9999/llama-b9999-bin-win-cuda-12.8-x64.zip"}}}
    out = rm.fill(mf, _client())
    assert out["profiles"]["32gb"]["llm"]["sha256"] == "ab" * 32
    assert out["profiles"]["32gb"]["llm"]["bytes"] == 21000000000
    assert out["profiles"]["32gb"]["tts"][0]["files_sha256"]["Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL.gguf"] == "ab" * 32
    assert out["bins"]["llama"]["sha256"] == "cd" * 32
    assert rm.check(out, _client()) == []


def test_check_reports_missing_sha():
    mf = {"profiles": {"32gb": {"llm": {"repo": "a/b", "file": "f.gguf", "sha256": None}, "embed": {"repo": "a/b", "file": "e", "sha256": "0" * 64}, "tts": [], "whisper_model": "x"}},
          "bins": {}}
    problems = rm.check(mf, _client())
    assert any("llm" in p and "sha256" in p for p in problems)


def test_repo_manifest_validates():
    from ns_common.config import load_manifest
    mf = load_manifest()
    assert mf.profiles["32gb"].llm.repo.startswith(("unsloth/", "ggml-org/", "mudler/"))
    assert mf.profiles["cpu-dev"].llm.repo == "Qwen/Qwen3-1.7B-GGUF"
    assert "llama_cuda" in mf.bins and "llama_cpu" in mf.bins and "cudart" in mf.bins
    assert any(p.startswith("torch==") for p in mf.tts_packages)
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Write `manifest.json`**

Versions marked `RESOLVE` are placeholders only inside this step's text; the file committed at Step 5 must contain the real values produced by `--fill` plus the version strings looked up by hand on 2026-11-01 (llama.cpp latest release tag with MTP support, OpenCode latest Windows release, torch 2.9.x cu128 wheel, `qwen-tts` and `faster-qwen3-tts` latest on PyPI). Verify each exists with the `--check` run in Step 4; do not commit a manifest that fails `--check`.

```json
{
  "version": "0.1.0",
  "python_version": "3.12",
  "spacy_model": "en_core_web_md",
  "tts_packages": [
    "torch==RESOLVE+cu128 --index-url https://download.pytorch.org/whl/cu128",
    "qwen-tts==RESOLVE",
    "faster-qwen3-tts==RESOLVE",
    "soundfile>=0.12",
    "fastapi>=0.115",
    "uvicorn>=0.30"
  ],
  "profiles": {
    "32gb": {
      "llm": {"repo": "unsloth/Qwen3.6-35B-A3B-MTP-GGUF", "file": "RESOLVE (the UD-Q4_K_XL .gguf filename from the repo tree)", "sha256": null, "bytes": null},
      "story_llm": null,
      "embed": {"repo": "nomic-ai/nomic-embed-text-v1.5-GGUF", "file": "nomic-embed-text-v1.5.Q8_0.gguf", "sha256": null, "bytes": null},
      "tts": [
        {"repo": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", "revision": "main", "files_sha256": {}},
        {"repo": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign", "revision": "main", "files_sha256": {}},
        {"repo": "Qwen/Qwen3-TTS-12Hz-1.7B-Base", "revision": "main", "files_sha256": {}}
      ],
      "whisper_model": "small.en"
    },
    "16gb": {
      "llm": {"repo": "mudler/Qwen3.6-35B-A3B-APEX-MTP-GGUF", "file": "RESOLVE (I-Mini filename)", "sha256": null, "bytes": null},
      "story_llm": null,
      "embed": {"repo": "nomic-ai/nomic-embed-text-v1.5-GGUF", "file": "nomic-embed-text-v1.5.Q8_0.gguf", "sha256": null, "bytes": null},
      "tts": [
        {"repo": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", "revision": "main", "files_sha256": {}},
        {"repo": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign", "revision": "main", "files_sha256": {}},
        {"repo": "Qwen/Qwen3-TTS-12Hz-1.7B-Base", "revision": "main", "files_sha256": {}}
      ],
      "whisper_model": "small.en"
    },
    "cpu-dev": {
      "llm": {"repo": "Qwen/Qwen3-1.7B-GGUF", "file": "Qwen3-1.7B-Q4_K_M.gguf", "sha256": null, "bytes": null},
      "story_llm": null,
      "embed": {"repo": "nomic-ai/nomic-embed-text-v1.5-GGUF", "file": "nomic-embed-text-v1.5.Q8_0.gguf", "sha256": null, "bytes": null},
      "tts": [{"repo": "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice", "revision": "main", "files_sha256": {}}],
      "whisper_model": "tiny"
    }
  },
  "bins": {
    "llama_cuda": {"version": "RESOLVE", "url": "https://github.com/ggml-org/llama.cpp/releases/download/RESOLVE/llama-RESOLVE-bin-win-cuda-12.8-x64.zip", "sha256": null},
    "cudart":     {"version": "RESOLVE", "url": "https://github.com/ggml-org/llama.cpp/releases/download/RESOLVE/cudart-llama-bin-win-cuda-12.8-x64.zip", "sha256": null},
    "llama_cpu":  {"version": "RESOLVE", "url": "https://github.com/ggml-org/llama.cpp/releases/download/RESOLVE/llama-RESOLVE-bin-win-cpu-x64.zip", "sha256": null},
    "opencode":   {"version": "RESOLVE", "url": "https://github.com/anomalyco/opencode/releases/download/vRESOLVE/opencode-windows-x64.zip", "sha256": null},
    "uv":         {"version": "RESOLVE", "url": "https://github.com/astral-sh/uv/releases/download/RESOLVE/uv-x86_64-pc-windows-msvc.zip", "sha256": null}
  }
}
```

How to find the real values (all read-only, no downloads): the Hugging Face tree API lists filenames and SHAs; `https://api.github.com/repos/ggml-org/llama.cpp/releases/latest` gives the tag and asset names (the CUDA 12.8 Windows zip is named `llama-<tag>-bin-win-cuda-12.8-x64.zip` and `cudart-llama-bin-win-cuda-12.8-x64.zip`); `https://pypi.org/pypi/qwen-tts/json` and `https://pypi.org/pypi/faster-qwen3-tts/json` give latest versions; `https://download.pytorch.org/whl/cu128/torch/` lists the available `torch-2.9.x+cu128-cp312-cp312-win_amd64.whl`.

- [ ] **Step 4: Implement `tools/resolve_manifest.py`**

```python
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

HF = "https://huggingface.co"
GH = "https://api.github.com"


def hf_tree(repo: str, revision: str, client: httpx.Client) -> dict[str, tuple[str, int]]:
    r = client.get(f"{HF}/api/models/{repo}/tree/{revision}", params={"recursive": "true"})
    r.raise_for_status()
    out: dict[str, tuple[str, int]] = {}
    for item in r.json():
        if item.get("type") == "file" and "lfs" in item:
            out[item["path"]] = (item["lfs"]["oid"], int(item["lfs"]["size"]))
    return out


def github_release_assets(owner: str, repo: str, tag: str, client: httpx.Client) -> dict[str, tuple[str, str]]:
    r = client.get(f"{GH}/repos/{owner}/{repo}/releases/tags/{tag}")
    r.raise_for_status()
    out: dict[str, tuple[str, str]] = {}
    for a in r.json()["assets"]:
        digest = (a.get("digest") or "").removeprefix("sha256:")
        out[a["name"]] = (a["browser_download_url"], digest)
    return out


def _fill_hf_file(entry: dict, client: httpx.Client, cache: dict) -> None:
    key = (entry["repo"], entry.get("revision", "main"))
    if key not in cache:
        cache[key] = hf_tree(*key, client)
    sha, size = cache[key][entry["file"]]
    entry["sha256"], entry["bytes"] = sha, size


def fill(manifest: dict, client: httpx.Client) -> dict:
    cache: dict = {}
    for prof in manifest["profiles"].values():
        for k in ("llm", "story_llm", "embed"):
            if prof.get(k):
                _fill_hf_file(prof[k], client, cache)
        for repo in prof.get("tts", []):
            key = (repo["repo"], repo.get("revision", "main"))
            if key not in cache:
                cache[key] = hf_tree(*key, client)
            repo["files_sha256"] = {p: sha for p, (sha, _) in cache[key].items()}
    for name, b in manifest.get("bins", {}).items():
        url = b["url"]
        if "github.com" in url and "/releases/download/" in url:
            _, rest = url.split("github.com/", 1)
            owner, repo, _, _, tag, asset = rest.split("/", 5)
            assets = github_release_assets(owner, repo, tag, client)
            if asset in assets and assets[asset][1]:
                b["sha256"] = assets[asset][1]
    return manifest


def check(manifest: dict, client: httpx.Client) -> list[str]:
    problems: list[str] = []
    for pname, prof in manifest.get("profiles", {}).items():
        for k in ("llm", "story_llm", "embed"):
            e = prof.get(k)
            if e and not e.get("sha256"):
                problems.append(f"profiles.{pname}.{k}: sha256 missing")
            if e:
                url = f"{HF}/{e['repo']}/resolve/main/{e['file']}"
                try:
                    r = client.head(url, follow_redirects=True)
                    if r.status_code >= 400:
                        problems.append(f"{url}: HTTP {r.status_code}")
                except httpx.HTTPError as ex:
                    problems.append(f"{url}: {ex}")
    for name, b in manifest.get("bins", {}).items():
        if not b.get("sha256"):
            problems.append(f"bins.{name}: sha256 missing")
        try:
            r = client.head(b["url"], follow_redirects=True)
            if r.status_code >= 400:
                problems.append(f"bins.{name}: HTTP {r.status_code}")
        except httpx.HTTPError as ex:
            problems.append(f"bins.{name}: {ex}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=str(Path(__file__).resolve().parents[1] / "manifest.json"))
    ap.add_argument("--fill", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    path = Path(a.manifest)
    mf = json.loads(path.read_text(encoding="utf-8"))
    with httpx.Client(timeout=60, headers={"User-Agent": "nightshift-resolve"}) as client:
        if a.fill:
            mf = fill(mf, client)
            path.write_text(json.dumps(mf, indent=2) + "\n", encoding="utf-8")
            print(f"filled {path}")
        if a.check:
            probs = check(mf, client)
            for p in probs:
                print("PROBLEM:", p)
            return 1 if probs else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

`tools/__init__.py`: empty.

- [ ] **Step 5: Resolve, check, test**

```powershell
.\.venv\Scripts\python tools/resolve_manifest.py --fill --check
.\.venv\Scripts\python -m pytest tests/test_resolve_manifest.py tests/test_config.py -v
```
Expected: `--check` prints nothing and exits 0; all tests pass (remove the `xfail` marker in `tests/test_config.py` first).

- [ ] **Step 6: Commit**

```bash
git add manifest.json tools tests/test_resolve_manifest.py tests/test_config.py
git commit -m "feat: pinned manifest with resolved SHA256s and resolver tool"
```

---

### Task 15: Policy lists, contracts docs, README, CLAUDE.md

**Files:**
- Create: `contracts/policy/banned_phrases.txt`, `contracts/policy/misleading_claims.txt`, `contracts/policy/unsafe_topics.yaml`, `contracts/policy/real_entities_block.txt`, `contracts/policy/real_entities_allow.txt`, `contracts/CONTRACTS.md`, `contracts/CHANGE_REQUESTS.md`, `README.md`, `CLAUDE.md`, `tests/test_policy_lists.py`

**Interfaces:**
- Produces: `ns_common.policy_lists` is **not** created here (Slice 3 owns the matcher); this task ships the data files and a test that they parse. Format: `.txt` one entry per line, `#` comments, case-insensitive; `unsafe_topics.yaml` is `{categories: [{id, label, description, examples: [..]}]}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_policy_lists.py
import yaml

from ns_common.paths import contracts_dir


def _lines(name: str) -> list[str]:
    p = contracts_dir() / "policy" / name
    return [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]


def test_lists_non_empty_and_lowercase():
    for name in ("banned_phrases.txt", "misleading_claims.txt", "real_entities_block.txt", "real_entities_allow.txt"):
        lines = _lines(name)
        assert len(lines) >= 10, name
        assert all(ln == ln.lower() for ln in lines), name


def test_unsafe_topics_yaml_shape():
    data = yaml.safe_load((contracts_dir() / "policy" / "unsafe_topics.yaml").read_text(encoding="utf-8"))
    ids = [c["id"] for c in data["categories"]]
    assert {"real_people", "minors", "graphic_harm", "advice", "fake_news"} <= set(ids)
    for c in data["categories"]:
        assert c["examples"] and c["description"]
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Write the lists**

`contracts/policy/banned_phrases.txt`:
```
# stock openers and filler the critic and validators reject (spec §16.3). one per line, lowercase.
it was a dark and stormy night
little did i know
what happened next will shock you
you won't believe
in this video
welcome back
hey guys
let me tell you a story
without further ado
at the end of the day
and that's when it hit me
to be honest
i'll never forget the day
i'm not sure how to explain this
buckle up
```

`contracts/policy/misleading_claims.txt`:
```
# phrases that claim fiction is real (spec §18.4). matched case-insensitively in titles, descriptions, tags, hooks.
true story
based on a true story
based on true events
real story
real footage
actual footage
caught on camera
caught on tape
this really happened
happened to me
my true story
not fiction
100% real
real life
real events
police report
news report
breaking news
leaked
```

`contracts/policy/real_entities_block.txt`:
```
# well-known real people, brands and events that must never appear in fiction output. extend over time. lowercase.
donald trump
joe biden
elon musk
taylor swift
kim kardashian
jeffrey epstein
ted bundy
jeffrey dahmer
john wayne gacy
columbine
sandy hook
9/11
september 11
world trade center
boston marathon bombing
chernobyl
fukushima
titanic
mcdonald's
walmart
amazon
google
facebook
tesla
disney
netflix
```

`contracts/policy/real_entities_allow.txt`:
```
# common first names that NER tags as PERSON but are fine in fiction. lowercase.
sarah
emma
olivia
ava
mia
nora
ellie
hannah
grace
lily
maya
clara
james
liam
noah
ethan
owen
caleb
daniel
sam
ben
jack
leo
eli
mr. hale
mrs. park
```

`contracts/policy/unsafe_topics.yaml`:
```yaml
# spec §18.3. The LLM classifier (policy_check.md) returns the matching category ids; any match fails the text gate.
categories:
  - id: real_people
    label: Real people, brands or events as fact
    description: Names a real living or dead person, a real brand as a villain, or a real crime/tragedy presented as fact.
    examples: ["a story about what Elon Musk did last night", "the killer from the 2012 theater shooting"]
  - id: minors
    label: Minors in sexual, violent or endangering contexts
    description: Any minor in a sexual context, graphic violence against a minor, or instructions that endanger minors.
    examples: ["a 12-year-old is tortured on camera"]
  - id: graphic_harm
    label: Graphic gore, self-harm methods, weapon or drug instructions
    description: Explicit gore descriptions, suicide or self-harm methods, how to make weapons or drugs.
    examples: ["step by step how he built the device", "she describes exactly how to overdose"]
  - id: hate
    label: Hate or harassment of protected groups
    description: Demeaning content targeting race, religion, ethnicity, gender, sexual orientation, disability, nationality.
    examples: ["the twist is that all people of X are evil"]
  - id: advice
    label: Medical, legal, financial or political advice
    description: The narrator gives real-world advice on health, law, money or politics.
    examples: ["and that's why you should never take antibiotics", "vote against them"]
  - id: fake_news
    label: Imitates news reports or emergency alerts
    description: Formatted or voiced as a real news bulletin, emergency broadcast or official warning.
    examples: ["this is an emergency alert for residents of Ohio"]
```

- [ ] **Step 4: Write `contracts/CONTRACTS.md` and `CHANGE_REQUESTS.md`**

`contracts/CONTRACTS.md`:
```markdown
# Nightshift contracts (frozen per slice)

Source of truth for every shared name. Code in `ns_common/` implements these; `contracts/schemas/*.schema.json`
are exported from `ns_common/models.py` and must match (test `test_every_schema_file_matches_models`).

| Area | Where |
|---|---|
| Names, paths, ports | spec §15.1–15.3; `ns_common/paths.py`, `ns_common/models.py::Runtime` |
| Supervisor API | spec §15.3 (implemented in Slice 4) |
| TTS API | spec §15.4; client `ns_common/tts_client.py` |
| Embed API | spec §15.5; client `ns_common/embed_client.py` |
| JSON schemas | `contracts/schemas/`; models in `ns_common/models.py` |
| SQLite | `contracts/schema.sql`; `ns_common/db.py` |
| Stage interface | spec §15.8 (implemented in Slice 1 `ns_studio/stages.py`) |
| CLI | spec §15.9 |
| Logging | spec §15.11; `ns_common/logging.py` |
| Error codes | `contracts/errors.json`; `ns_common/errors.py` |
| Policy lists | `contracts/policy/` |
| Prompt front-matter | spec §15.13 |

## Freeze tags
- `contracts-s1` — everything above as of Slice 0 completion.

## Rules
Only the main session edits `contracts/` and `ns_common/`. Workers file requests in `CHANGE_REQUESTS.md`.
```

`contracts/CHANGE_REQUESTS.md`:
```markdown
# Change requests to frozen contracts

Format per entry:

## CR-<n>: <short title>
- **From:** WP-<letter> / task
- **Change:** what to add or alter (exact names, types)
- **Why:** the concrete need
- **Status:** open | applied in <commit> | rejected (<reason>)

(none yet)
```

- [ ] **Step 5: Write `README.md`** — the §A text of the spec, verbatim, followed by a "Developer quick start" section:

````markdown
## Developer quick start
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
```
Run the pipeline without any model or GPU: `studio run --fake-services --format shorts_only --count 1` (Slice 1+).
````

- [ ] **Step 6: Write `CLAUDE.md`** (project instructions for any Claude Code session in this repo)

```markdown
# Nightshift — working in this repo

- Spec: `BUILD_SPEC.md`. Plans: `docs/superpowers/plans/`. Read the plan index before starting work.
- **Never download models, torch, llama.cpp or whisper on a machine that is not the GPU target.** Tests use fakes
  (`ns_common` Fake* classes) and the FFmpeg already on PATH.
- `contracts/` and `ns_common/` are edited only by the main session. Workers file `contracts/CHANGE_REQUESTS.md`.
- Tests: `.\.venv\Scripts\python -m pytest`. Lint: `ruff check .`. Types: `pyright`. All three must pass before a commit.
- Every stage, client and CLI logs through `ns_common.logging.NsLogger`; errors are `NsError` with a code from
  `contracts/errors.json`.
- Conventional commits (`feat:`, `fix:`, `test:`, `chore:`, `docs:`), one task per commit.
- Spawned agents: `sonnet` for ordinary code, `opus` for prompt/policy/orchestrator design, `haiku` for sweeps.
- Gate scripts under `dev/` must keep working with `--fake-services` on a machine with no GPU.
```

- [ ] **Step 7: Run tests, lint, types**

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m pyright
```
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add contracts README.md CLAUDE.md tests/test_policy_lists.py
git commit -m "docs: policy lists, contracts registry, README, CLAUDE.md"
```

---

### Task 16: Gate script, dev service script, freeze tag

**Files:**
- Create: `dev/gate_s0.py`, `dev/run_services_dev.ps1`, `tests/test_gate_s0.py`

**Interfaces:**
- Produces: `python dev/gate_s0.py [--out logs/gate-s0-<date>.json]` → writes a `GateReport` (slice `"s0"`) and exits 0 iff all checks pass. Checks: `pytest` exit code, `ruff`, `pyright`, `manifest_check` (`tools/resolve_manifest.py --check`), `fake_llm_schema` (FakeLlm fixture `_example.json` loads), `fake_tts_wav` (FakeTts writes a WAV ≥ 0.4 s), `fake_embed_vector` (FakeEmbed returns 256 floats), `ffmpeg_clip` (`make_test_clip` 320×180 2 s renders and has faststart).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gate_s0.py
import json
from pathlib import Path

from dev.gate_s0 import run_checks


def test_run_checks_structure(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    report = run_checks(skip_slow=True)   # skips pytest/ruff/pyright/manifest subprocesses
    names = [c.name for c in report.checks]
    assert {"fake_llm_schema", "fake_tts_wav", "fake_embed_vector", "ffmpeg_clip"} <= set(names)
    assert report.slice == "s0"
    json.dumps(report.model_dump(mode="json"))
```

Add `dev/__init__.py` (empty) so the test can import it.

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement `dev/gate_s0.py`**

```python
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from ns_common import ffmpeg as ff
from ns_common.embed_client import FakeEmbed
from ns_common.llm_client import FakeLlm
from ns_common.models import GateCheck, GateReport
from ns_common.paths import Paths, repo_root
from ns_common.tts_client import FakeTts

PY = sys.executable


def _sub(name: str, args: list[str]) -> GateCheck:
    p = subprocess.run([PY, *args], capture_output=True, text=True, cwd=repo_root())
    return GateCheck(name=name, passed=p.returncode == 0, value=p.returncode, log=(p.stdout + p.stderr)[-2000:])


def run_checks(*, skip_slow: bool = False) -> GateReport:
    checks: list[GateCheck] = []
    if not skip_slow:
        checks.append(_sub("pytest", ["-m", "pytest", "-q"]))
        checks.append(_sub("ruff", ["-m", "ruff", "check", "."]))
        checks.append(_sub("pyright", ["-m", "pyright"]))
        checks.append(_sub("manifest_check", ["tools/resolve_manifest.py", "--check"]))
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        try:
            FakeLlm(repo_root() / "tests" / "fixtures" / "llm").complete_json(
                prompt_name="_example", system="", user="", schema={"type": "object"})
            checks.append(GateCheck(name="fake_llm_schema", passed=True))
        except Exception as e:  # noqa: BLE001
            checks.append(GateCheck(name="fake_llm_schema", passed=False, log=str(e)))
        secs = FakeTts().speak("hello from the gate", "Ryan", 1.0, tmp / "t.wav")
        checks.append(GateCheck(name="fake_tts_wav", passed=secs >= 0.4, value=secs, limit=">=0.4"))
        vec = FakeEmbed().embed(["x"])[0]
        checks.append(GateCheck(name="fake_embed_vector", passed=len(vec) == 256, value=len(vec), limit="256"))
        try:
            clip = ff.make_test_clip(tmp / "c.mp4", width=320, height=180, seconds=2.0)
            ok = ff.has_faststart(clip) and 1.9 <= ff.duration_s(clip) <= 2.1
            checks.append(GateCheck(name="ffmpeg_clip", passed=ok, value=ff.duration_s(clip), limit="2.0±0.1"))
        except Exception as e:  # noqa: BLE001
            checks.append(GateCheck(name="ffmpeg_clip", passed=False, log=str(e)))
    return GateReport(
        slice="s0",
        date=datetime.now(UTC).isoformat(),
        machine={"os": platform.platform(), "python": platform.python_version(), "gpu": None},
        checks=checks,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    report = run_checks()
    paths = Paths.from_env(); paths.ensure()
    out = Path(a.out) if a.out else paths.logs / f"gate-s0-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.model_dump(mode="json"), indent=2), encoding="utf-8")
    for c in report.checks:
        print(("PASS" if c.passed else "FAIL"), c.name, c.value if c.value is not None else "")
    print("report:", out)
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Write `dev/run_services_dev.ps1`** (for a GPU machine; not run here)

```powershell
# Starts llama-server, the embed server and the TTS server in three visible windows using config\runtime.json.
# Usage: .\dev\run_services_dev.ps1   (on a machine where the installer has already placed models and venvs)
param([string]$Home = $(if ($env:NIGHTSHIFT_HOME) { $env:NIGHTSHIFT_HOME } else { "$env:LOCALAPPDATA\Nightshift" }))
$rt = Get-Content "$Home\config\runtime.json" -Raw | ConvertFrom-Json
$llama = "$Home\bin\llama\llama-server.exe"
$f = $rt.llama.flags
$args = @("-m", $rt.llama.model_path, "--port", $rt.llama.port, "--host", "127.0.0.1", "-c", $f.ctx,
          "-ngl", $f.n_gpu_layers, "--alias", ($rt.llama.aliases -join ","), "-ctk", $f.kv_type, "-ctv", $f.kv_type)
if ($f.n_cpu_moe -gt 0) { $args += @("--n-cpu-moe", $f.n_cpu_moe) }
if ($f.spec_type) { $args += @("--spec-type", $f.spec_type, "--spec-draft-n-max", $f.spec_draft_n_max, "-np", "1") }
Start-Process powershell -ArgumentList "-NoExit", "-Command", "& '$llama' $($args -join ' ')"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "& '$llama' -m '$($rt.embed.model_path)' --embeddings --port $($rt.embed.port) --host 127.0.0.1 -ngl 0"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "& '$Home\venvs\tts\Scripts\python.exe' -m ns_tts.server --port $($rt.tts.port)"
Write-Host "Started llama ($($rt.llama.port)), embed ($($rt.embed.port)), tts ($($rt.tts.port)). Close the windows to stop."
```

- [ ] **Step 5: Run the gate and the tests**

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python dev/gate_s0.py
```
Expected: every line `PASS`; exit 0; report path printed.

- [ ] **Step 6: Commit and tag**

```bash
git add dev tests/test_gate_s0.py
git commit -m "feat: slice 0 gate script and dev service launcher"
git tag contracts-s1
```

---

## Self-review (done while writing)

- **Spec coverage for Slice 0** (spec §12 row 0): repo tree ✔ (T1, T4-7, T15), manifest with verified pins ✔ (T14), `ns_common` incl. fakes ✔ (T2–T13), Slice 1 contracts/schemas ✔ (T5, T7, T3), policy lists ✔ (T15), `pyproject` ✔ (T1; `uv.lock` is produced by `.\.venv\Scripts\uv lock` in T1 Step 7 if desired — optional here because torch is not lockable on this machine; the installer uses `uv pip install -e ".[studio]"` with the manifest pins for the tts venv), CI ✔ (T1), `dev\` scripts ✔ (T16), `CLAUDE.md` + `README.md` ✔ (T15).
- **Placeholders:** the only `RESOLVE` tokens are inside Task 14 Step 3 and are explicitly resolved in Step 5 before commit.
- **Type consistency:** `Paths` property names used in T6/T16 match T2; `NsError` codes used in T6/T9/T10/T11/T12/T13 exist in T3's `errors.json`; `Runtime.default("cpu-dev")` in T13/T6 matches T5; `GateCheck`/`GateReport` fields in T16 match T5; `TtsItem(id, text, voice)` positional order in T11 tests matches the dataclass.
