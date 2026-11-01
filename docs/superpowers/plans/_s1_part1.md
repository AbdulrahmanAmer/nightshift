# Nightshift Slice 1 — One Short Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce one complete vertical Short end to end (story → hooks → SEO → voice → captions → stock clips → render → QC → Outbox sheet) through the real stage/orchestrator code, runnable with `studio run --fake-services --format shorts_only --count 1` on a machine with no GPU, and with real engines (llama-server, Qwen3-TTS, faster-whisper, Pexels/Pixabay) on the target PC.

**Architecture:** `ns_studio` holds a `Stage` registry and a sequential orchestrator that groups stages by GPU tenant; stages read/write JSON files in the job directory and are idempotent. Text stages call `Llm` through Jinja2 prompts with YAML front-matter; media stages call `Tts`, a `Transcriber`, `ClipSource`s and FFmpeg. `ns_tts` is a FastAPI service wrapping an `Engine` protocol with two real engines and a fake. Everything heavy is selected by `build_services(runtime, fake=set)`.

**Tech Stack:** Python 3.12, pydantic, Jinja2, httpx, FastAPI + uvicorn (TTS server), faster-whisper (target only), `qwen-tts` / `faster-qwen3-tts` (target only), FFmpeg (filter graphs, `ass` subtitles, `loudnorm`, `ebur128`, `blackdetect`), SQLite.

**Spec:** `D:\NightShift\BUILD_SPEC.md` v2 — §1.1 (fakes), §7.3 (shorts-only flow), §7.4 (clip budget), §7.5 (TTS chunking), §7.6 (caption alignment), §7.7 (QC targets), §9.2 (Shorts SEO), §15.4 (TTS API), §15.8 (stage interface, job dir), §16 (prompts, rubric, validators, exemplars, calibration), §17 (presets), §18.5 (retention rules).

## Global Constraints

- Everything in the Slice 0 plan's Global Constraints still applies.
- **No heavy installs on the build machine.** `faster-whisper`, `qwen-tts`, `faster-qwen3-tts`, `spacy`, `torch` are imported lazily inside the real implementations only; every module in this slice must import cleanly without them. Tests use `FakeTts`, `FakeLlm`, `FakeEmbed`, `FakeTranscriber`, `FakeClipSource`, `FakeEngine`.
- Shorts: 1080×1920, 30 fps, AAC 192k 48 kHz, `-movflags +faststart`; encoder from `runtime.video.encoder` (spec §2).
- Shorts duration bucket ±15 %, max 179 s; hook spoken within the first 1.5 s; first caption at 0.0 s (spec §7.7, §18.5).
- Loudness −14 LUFS ±1, true peak ≤ −1 dBTP; caption drift ≤ 120 ms; 0 black frames > 0.3 s (spec §7.7).
- TTS chunking: sentence split, merge < 25 chars, cap 280 chars; 350 ms between sentences, 700 ms at paragraph breaks, 1,200 ms before a cliff line; trim at −45 dBFS (spec §7.5).
- Clip rules: Shorts shot length 1.5–3 s; one search per unique keyword per source per orientation, page size 80; Pexels bucket 180/h, Pixabay 90/min; cache 90 days; Pixabay search responses cached 24 h; no clip twice in one video; per channel 14 days; cross-channel 30 days (spec §7.4).
- SEO (Shorts, spec §9.2): title ≤ 70 target / 100 max, keyword in first 40 chars; description 1–2 lines + "From the full story: <master title>" (shorts-only: omitted) + "Original fiction." when `fiction_label` + 3–5 hashtags including `#Shorts`; tags 10–20, ≤ 500 chars.
- Critic pass: average ≥ `channel.critic_bar` (default 8) and no score < 6; max 2 revisions; max 3 ideas per job (spec §16.2).
- Validators (spec §16.3): words within bucket ±20 %, banned phrases, opening-line similarity < 0.8 vs last 50, reading grade 5–8, no sentence > 35 words.
- Prompt files carry YAML front-matter `version`, `thinking`, `schema` (spec §15.13); `metadata.prompt_versions` records them.
- Worker allocation: Tasks 2, 5 (prompts, exemplars) → `opus`; Task 1 (orchestrator) → `opus`; all other tasks → `sonnet`. WP ownership per spec §4: D = `ns_studio/text,prompts,exemplars,calibrate`; E = `ns_studio/media,presets`; F = `ns_studio/stages.py,orchestrator.py,cli.py,channels`; C = `ns_tts`; G = `tests`.

---

## File structure created by this slice

```
ns_studio/
  context.py          # JobContext, Services, build_services(runtime, fake=...)
  stages.py           # Stage protocol, StageState file, registry, run_stage()
  orchestrator.py     # run_job(), tenant grouping, resume
  cli.py              # studio run / channel / voice / calibrate
  channels/__init__.py channels/crud.py
  text/__init__.py text/prompts.py text/validators.py text/stages_shorts.py text/budget.py
  prompts/ideas.md story_plan.md story_draft.md critic.md revise.md split.md hook.md judge.md seo.md visual_plan.md
  exemplars/<niche>/<slug>.md + meta.yaml   (horror, mystery, revenge, wholesome_twist × 3)
  media/__init__.py media/voice.py media/captions.py media/transcribe.py media/clips.py media/clip_sources.py
  media/render.py media/qc.py media/outbox.py
  presets/caption_styles.yaml visual_treatments.yaml
  calibrate/__init__.py calibrate/run.py
ns_tts/
  engine.py           # Engine protocol, FakeEngine, ReferenceEngine, FasterEngine, select_engine()
  voices.py           # voice library (presets + design/clone entries) under models\tts\voices\voices.json
  audio.py            # chunk_text(), stitch(), trim_silence()
  server.py           # FastAPI app (§15.4) + __main__
tests/
  fixtures/llm/{ideas,story_plan,story_draft,critic,revise,split,hook,judge,seo,visual_plan}.json
  fixtures/clips/pexels_search.json pixabay_search.json
  test_context.py test_stages.py test_orchestrator.py test_prompts.py test_validators.py test_text_stages.py
  test_exemplars.py test_tts_engine.py test_tts_audio.py test_tts_server.py test_voice_stage.py
  test_transcribe.py test_captions.py test_clip_sources.py test_clips.py test_render.py test_qc.py test_outbox.py
  test_cli.py test_calibrate.py test_gate_s1.py
dev/gate_s1.py
```

---

### Task 1: Studio context, stage registry, orchestrator skeleton (WP-F, `opus`)

**Files:**
- Create: `ns_studio/context.py`, `ns_studio/stages.py`, `ns_studio/orchestrator.py`, `tests/test_context.py`, `tests/test_stages.py`, `tests/test_orchestrator.py`

**Interfaces:**
- Produces:
```python
# ns_studio/context.py
@dataclass
class Services:
    llm: Llm; tts: Tts; embed: Embed; transcriber: "Transcriber"; clip_sources: list["ClipSource"]; acquirer: Acquirer
FAKEABLE = {"llm", "tts", "whisper", "embed", "clips"}
def build_services(runtime: Runtime, *, fake: set[str], paths: Paths, keys: dict[str, str] | None = None) -> Services
    # fake ⊇ FAKEABLE → all fakes; real ones built lazily (imports inside); "clips" real needs keys["pexels"], keys["pixabay"]
@dataclass
class JobContext:
    job_id: str; channel: Channel; combo: Combo; fmt: Literal["master_first","shorts_only","series_compilation"]
    paths: Paths; job_dir: Path; db: Db; runtime: Runtime; services: Services; log: NsLogger
    prompt_versions: dict[str, int]      # filled by text stages
    def file(self, name: str) -> Path                     # job_dir / name
    def read_json(self, name: str) -> Any / def write_json(self, name: str, data: Any) -> Path
    def subdir(self, name: str) -> Path                   # mkdir -p job_dir/name
# ns_studio/stages.py
Tenant = Literal["llama", "tts", "whisper", None]
class Stage(Protocol):
    name: str; tenant: Tenant; inputs: tuple[str, ...]; outputs: tuple[str, ...]
    def run(self, ctx: JobContext) -> None
class StageState:   # stage_state.json: {"<stage>": {"status": "done", "finished_at": iso, "inputs_hash": hex}}
    @classmethod load(job_dir) / save(job_dir); mark_done(name, inputs_hash); is_done(name, inputs_hash) -> bool
def inputs_hash(ctx: JobContext, stage: Stage) -> str     # sha256 over bytes of each input file that exists
def run_stage(ctx: JobContext, stage: Stage, *, force: bool = False) -> bool   # returns True if it ran, False if skipped; logs timed
REGISTRY: dict[str, Stage]; def register(stage: Stage) -> Stage (decorator-style helper)
# ns_studio/orchestrator.py
FORMAT_STAGES: dict[str, list[str]]     # "shorts_only": [...] ordered stage names (filled in later tasks via append)
def plan_job(fmt) -> list[Stage]        # looks up REGISTRY; raises NsError("NS-E306") for unknown/unimplemented formats
def run_job(ctx: JobContext, *, resume: bool = True, dry_run: bool = False) -> list[str]
    # sequential; for each stage: acquire its tenant through ctx.services.acquirer (owner f"studio:{job_id}"), run, release;
    # consecutive stages with the same tenant share one acquisition; dry_run prints the plan and returns names
```

- [ ] **Step 1: Write failing tests**

```python
# tests/test_context.py
from pathlib import Path

from ns_common.models import Channel, Combo, Runtime
from ns_common.paths import Paths
from ns_studio.context import FAKEABLE, build_services


def test_build_all_fakes(tmp_home: Path):
    p = Paths.from_env(); p.ensure()
    s = build_services(Runtime.default("cpu-dev"), fake=set(FAKEABLE), paths=p)
    assert s.llm.health() and s.tts.health() and s.embed.health()
    assert s.clip_sources and s.clip_sources[0].name == "fake"
    assert s.transcriber.name == "fake"
```

```python
# tests/test_stages.py
import json
from pathlib import Path

from ns_studio.stages import StageState, inputs_hash, run_stage
from tests.helpers import make_ctx


class Double:
    name = "double"; tenant = None; inputs = ("a.json",); outputs = ("b.json",)
    def __init__(self): self.runs = 0
    def run(self, ctx):
        self.runs += 1
        ctx.write_json("b.json", {"v": ctx.read_json("a.json")["v"] * 2})


def test_run_stage_skips_when_inputs_unchanged(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.write_json("a.json", {"v": 2})
    st = Double()
    assert run_stage(ctx, st) is True
    assert ctx.read_json("b.json") == {"v": 4}
    assert run_stage(ctx, st) is False            # skipped
    ctx.write_json("a.json", {"v": 5})
    assert run_stage(ctx, st) is True             # inputs changed
    assert st.runs == 2
    state = StageState.load(ctx.job_dir)
    assert state.is_done("double", inputs_hash(ctx, st))


def test_force_reruns(tmp_home: Path):
    ctx = make_ctx(tmp_home); ctx.write_json("a.json", {"v": 1}); st = Double()
    run_stage(ctx, st); assert run_stage(ctx, st, force=True) is True and st.runs == 2
```

```python
# tests/test_orchestrator.py
from pathlib import Path

import pytest

from ns_common.errors import NsError
from ns_studio import orchestrator as orch
from ns_studio.stages import REGISTRY, register
from tests.helpers import make_ctx


def _stage(name, tenant):
    class S:
        inputs = (); outputs = (f"{name}.json",)
        def run(self, ctx): ctx.write_json(f"{name}.json", {"ok": True})
    S.name, S.tenant = name, tenant
    return S()


def test_run_job_groups_tenants(tmp_home: Path, monkeypatch):
    monkeypatch.setitem(orch.FORMAT_STAGES, "test_fmt", ["t1", "t2", "t3", "t4"])
    for n, t in (("t1", "llama"), ("t2", "llama"), ("t3", "tts"), ("t4", None)):
        register(_stage(n, t))
    ctx = make_ctx(tmp_home, fmt="test_fmt")
    ran = orch.run_job(ctx)
    assert ran == ["t1", "t2", "t3", "t4"]
    ev = ctx.services.acquirer.events
    assert ev == [("acquire", "llama", f"studio:{ctx.job_id}"), ("release", "llama", f"studio:{ctx.job_id}"),
                  ("acquire", "tts", f"studio:{ctx.job_id}"), ("release", "tts", f"studio:{ctx.job_id}")]


def test_unknown_format_is_e306(tmp_home: Path):
    with pytest.raises(NsError) as ei:
        orch.plan_job("series_compilation")
    assert ei.value.code == "NS-E306"


def test_dry_run_lists_without_running(tmp_home: Path, monkeypatch):
    monkeypatch.setitem(orch.FORMAT_STAGES, "f2", ["x1"]); register(_stage("x1", None))
    ctx = make_ctx(tmp_home, fmt="f2")
    assert orch.run_job(ctx, dry_run=True) == ["x1"] and not ctx.file("x1.json").exists()
```

```python
# tests/helpers.py
from pathlib import Path

from ns_common.db import Db, new_job_id
from ns_common.logging import NsLogger
from ns_common.models import Channel, Combo, Runtime
from ns_common.paths import Paths
from ns_studio.context import FAKEABLE, JobContext, build_services


def make_channel(**kw) -> Channel:
    base = dict(slug="after-dark", display_name="After Dark", niche="original short horror fiction set in everyday places",
                language="en", seed_keywords=["horror story", "scary story"], voice_notes="Plain, intimate first person.",
                format="shorts_only")
    return Channel(**(base | kw))


def make_combo(**kw) -> Combo:
    base = dict(combo_id="c-test", hook="cold_open", structure="twist", pov_tone="first_person_intimate",
                pacing="standard", caption_style="punch_yellow", visual_treatment="dark_slowzoom",
                title_formula="question", length_bucket=50)
    return Combo(**(base | kw))


def make_ctx(home: Path, *, fmt: str = "shorts_only", channel: Channel | None = None, combo: Combo | None = None,
             fake: set[str] | None = None) -> JobContext:
    paths = Paths.from_env(); paths.ensure()
    channel = channel or make_channel()
    runtime = Runtime.default("cpu-dev")
    services = build_services(runtime, fake=set(FAKEABLE) if fake is None else fake, paths=paths)
    db = Db(paths.db_path); db.migrate()
    db.execute("insert or ignore into channels(slug, display_name, created_at) values (?,?,?)",
               (channel.slug, channel.display_name, "2026-11-01T00:00:00"))
    job_id = new_job_id(channel.slug)
    job_dir = paths.job_dir(job_id); job_dir.mkdir(parents=True, exist_ok=True)
    return JobContext(job_id=job_id, channel=channel, combo=combo or make_combo(), fmt=fmt, paths=paths,  # type: ignore[arg-type]
                      job_dir=job_dir, db=db, runtime=runtime, services=services, log=NsLogger("studio", paths.logs, echo=False),
                      prompt_versions={})
```

Add `tests/__init__.py` (empty) so `from tests.helpers import …` works.

- [ ] **Step 2: Run** → FAIL (modules missing).

- [ ] **Step 3: Implement `ns_studio/context.py`**

```python
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ns_common.acquire import Acquirer, DevAcquirer
from ns_common.db import Db
from ns_common.embed_client import Embed, EmbedClient, FakeEmbed
from ns_common.llm_client import FakeLlm, Llm, LlmClient
from ns_common.logging import NsLogger
from ns_common.models import Channel, Combo, Runtime
from ns_common.paths import Paths, repo_root
from ns_common.tts_client import FakeTts, Tts, TtsClient

if TYPE_CHECKING:
    from ns_studio.media.clip_sources import ClipSource
    from ns_studio.media.transcribe import Transcriber

FAKEABLE: frozenset[str] = frozenset({"llm", "tts", "whisper", "embed", "clips"})
FIXTURES_LLM = repo_root() / "tests" / "fixtures" / "llm"


@dataclass
class Services:
    llm: Llm
    tts: Tts
    embed: Embed
    transcriber: Transcriber
    clip_sources: list[ClipSource]
    acquirer: Acquirer


def build_services(runtime: Runtime, *, fake: set[str] | frozenset[str], paths: Paths,
                   keys: dict[str, str] | None = None, llm_fixtures: Path | None = None) -> Services:
    from ns_studio.media.clip_sources import FakeClipSource, PexelsSource, PixabaySource
    from ns_studio.media.transcribe import FakeTranscriber, WhisperTranscriber

    keys = keys or {}
    llm: Llm = FakeLlm(llm_fixtures or FIXTURES_LLM) if "llm" in fake else LlmClient(runtime.llama.base_url, "story")
    tts: Tts = FakeTts() if "tts" in fake else TtsClient(runtime.tts.base_url)
    embed: Embed = FakeEmbed() if "embed" in fake else EmbedClient(runtime.embed.base_url)
    transcriber: Transcriber = FakeTranscriber() if "whisper" in fake else WhisperTranscriber(runtime.whisper, paths)
    if "clips" in fake:
        sources: list[ClipSource] = [FakeClipSource(paths.cache_clips / "fake")]
    else:
        sources = [PexelsSource(keys["pexels"], paths.cache_search), PixabaySource(keys["pixabay"], paths.cache_search)]
    return Services(llm=llm, tts=tts, embed=embed, transcriber=transcriber, clip_sources=sources, acquirer=DevAcquirer(runtime))


@dataclass
class JobContext:
    job_id: str
    channel: Channel
    combo: Combo
    fmt: Literal["master_first", "shorts_only", "series_compilation"]
    paths: Paths
    job_dir: Path
    db: Db
    runtime: Runtime
    services: Services
    log: NsLogger
    prompt_versions: dict[str, int] = field(default_factory=dict)

    def file(self, name: str) -> Path:
        return self.job_dir / name

    def read_json(self, name: str) -> Any:
        return json.loads(self.file(name).read_text(encoding="utf-8"))

    def write_json(self, name: str, data: Any) -> Path:
        p = self.file(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return p

    def subdir(self, name: str) -> Path:
        d = self.job_dir / name
        d.mkdir(parents=True, exist_ok=True)
        return d
```

`FakeClipSource`, `FakeTranscriber`, `WhisperTranscriber`, `PexelsSource`, `PixabaySource` are created in Tasks 8–9. Until then, create minimal stubs in `ns_studio/media/transcribe.py` and `ns_studio/media/clip_sources.py` so this task's tests run:

```python
# ns_studio/media/transcribe.py  (stub, completed in Task 8)
from __future__ import annotations
from typing import Protocol
class Transcriber(Protocol):
    name: str
class FakeTranscriber:
    name = "fake"
class WhisperTranscriber:
    name = "whisper"
    def __init__(self, cfg, paths): self.cfg, self.paths = cfg, paths
```
```python
# ns_studio/media/clip_sources.py  (stub, completed in Task 9)
from __future__ import annotations
from pathlib import Path
from typing import Protocol
class ClipSource(Protocol):
    name: str
class FakeClipSource:
    name = "fake"
    def __init__(self, root: Path): self.root = root
class PexelsSource:
    name = "pexels"
    def __init__(self, key: str, cache_dir: Path): self.key, self.cache_dir = key, cache_dir
class PixabaySource:
    name = "pixabay"
    def __init__(self, key: str, cache_dir: Path): self.key, self.cache_dir = key, cache_dir
```
Add empty `ns_studio/media/__init__.py`, `ns_studio/text/__init__.py`.

- [ ] **Step 4: Implement `ns_studio/stages.py`**

```python
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from ns_studio.context import JobContext

Tenant = Literal["llama", "tts", "whisper", None]
STATE_FILE = "stage_state.json"


class Stage(Protocol):
    name: str
    tenant: Tenant
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]

    def run(self, ctx: JobContext) -> None: ...


@dataclass
class StageState:
    entries: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, job_dir: Path) -> StageState:
        f = job_dir / STATE_FILE
        return cls(json.loads(f.read_text(encoding="utf-8"))) if f.exists() else cls()

    def save(self, job_dir: Path) -> None:
        (job_dir / STATE_FILE).write_text(json.dumps(self.entries, indent=2) + "\n", encoding="utf-8")

    def mark_done(self, name: str, h: str) -> None:
        self.entries[name] = {"status": "done", "finished_at": datetime.now(UTC).isoformat(), "inputs_hash": h}

    def is_done(self, name: str, h: str) -> bool:
        e = self.entries.get(name)
        return bool(e and e.get("status") == "done" and e.get("inputs_hash") == h)


def inputs_hash(ctx: JobContext, stage: Stage) -> str:
    hsh = hashlib.sha256()
    for name in stage.inputs:
        p = ctx.file(name)
        hsh.update(name.encode())
        if p.exists():
            hsh.update(p.read_bytes() if p.is_file() else b"dir")
    return hsh.hexdigest()


def run_stage(ctx: JobContext, stage: Stage, *, force: bool = False) -> bool:
    state = StageState.load(ctx.job_dir)
    h = inputs_hash(ctx, stage)
    outputs_exist = all(ctx.file(o).exists() for o in stage.outputs)
    if not force and outputs_exist and state.is_done(stage.name, h):
        ctx.log.info("stage_skip", stage=stage.name, job_id=ctx.job_id)
        return False
    with ctx.log.timed("stage", stage=stage.name, job_id=ctx.job_id, tenant=stage.tenant):
        stage.run(ctx)
    state.mark_done(stage.name, h)
    state.save(ctx.job_dir)
    return True


REGISTRY: dict[str, Stage] = {}


def register(stage: Stage) -> Stage:
    REGISTRY[stage.name] = stage
    return stage
```

- [ ] **Step 5: Implement `ns_studio/orchestrator.py`**

```python
from __future__ import annotations

from itertools import groupby

from ns_common.errors import NsError
from ns_studio.context import JobContext
from ns_studio.stages import REGISTRY, Stage, run_stage

# Filled by stage modules at import time (text/stages_shorts.py, media/*.py append their names in order).
FORMAT_STAGES: dict[str, list[str]] = {"shorts_only": [], "master_first": [], "series_compilation": []}


def plan_job(fmt: str) -> list[Stage]:
    names = FORMAT_STAGES.get(fmt)
    if not names:
        raise NsError("NS-E306", f"format {fmt!r} has no stages registered")
    missing = [n for n in names if n not in REGISTRY]
    if missing:
        raise NsError("NS-E306", f"stages not registered: {missing}")
    return [REGISTRY[n] for n in names]


def run_job(ctx: JobContext, *, resume: bool = True, dry_run: bool = False) -> list[str]:
    stages = plan_job(ctx.fmt)
    if dry_run:
        return [s.name for s in stages]
    owner = f"studio:{ctx.job_id}"
    ran: list[str] = []
    for tenant, group in groupby(stages, key=lambda s: s.tenant):
        group_list = list(group)
        if tenant is not None:
            ctx.services.acquirer.acquire(tenant, owner, model_alias="story" if tenant == "llama" else None)
        try:
            for st in group_list:
                run_stage(ctx, st, force=not resume)
                ran.append(st.name)
        finally:
            if tenant is not None:
                ctx.services.acquirer.release(tenant, owner)
    return ran
```

- [ ] **Step 6: Run** → all tests in the three files pass.

- [ ] **Step 7: Commit**

```bash
git add ns_studio tests/helpers.py tests/__init__.py tests/test_context.py tests/test_stages.py tests/test_orchestrator.py
git commit -m "feat(studio): job context, idempotent stage runner, tenant-grouped orchestrator"
```

---

### Task 2: Prompt loader and the shorts-only prompt files (WP-D, `opus`)

**Files:**
- Create: `ns_studio/text/prompts.py`, `ns_studio/prompts/{ideas,story_plan,story_draft,critic,revise,split,hook,judge,seo,visual_plan}.md`, `tests/test_prompts.py`, `tests/fixtures/llm/{ideas,story_plan,story_draft,critic,revise,split,hook,judge,seo,visual_plan}.json`

**Interfaces:**
- Produces:
```python
@dataclass(frozen=True)
class Prompt: name: str; version: int; thinking: bool; schema: str | None; system: str; user_template: jinja2.Template
def load_prompt(name: str) -> Prompt            # cached; parses front-matter; body split at a line "---USER---"
def render(prompt: Prompt, **vars) -> str        # vars: channel, combo, exemplars, policy_rules, budget, plus stage-specific
def policy_rules_text() -> str                   # bullet list from contracts/policy/unsafe_topics.yaml + misleading_claims.txt
PROMPT_SCHEMAS: dict[str, dict]                  # schema name → JSON schema dict used for complete_json (ideas, story_plan, critic, split, hook, judge, seo, visual_plan)
def call_json(ctx: JobContext, name: str, **vars) -> dict   # render + ctx.services.llm.complete_json(prompt_name=name, …); records ctx.prompt_versions[name]
def call_text(ctx: JobContext, name: str, **vars) -> str
```
- Prompt file format:
```
---
version: 1
thinking: false
schema: ideas
---
<system text, Jinja2>
---USER---
<user text, Jinja2>
```

- [ ] **Step 1: Write failing tests**

```python
# tests/test_prompts.py
import json

from ns_common.models import SCHEMA_MODELS
from ns_studio.text import prompts as P
from tests.helpers import make_channel, make_combo

ALL = ["ideas", "story_plan", "story_draft", "critic", "revise", "split", "hook", "judge", "seo", "visual_plan"]


def test_all_prompts_load_with_frontmatter():
    for n in ALL:
        p = P.load_prompt(n)
        assert p.version >= 1 and isinstance(p.thinking, bool), n
        assert p.system.strip() and p.user_template, n


def test_thinking_flags_match_spec():
    assert {n: P.load_prompt(n).thinking for n in ALL} == {
        "ideas": False, "story_plan": True, "story_draft": False, "critic": True, "revise": False,
        "split": False, "hook": False, "judge": True, "seo": False, "visual_plan": False}


def test_render_includes_channel_and_rules():
    p = P.load_prompt("ideas")
    out = P.render(p, channel=make_channel(), combo=make_combo(), exemplars=[], policy_rules=P.policy_rules_text(),
                   budget={"words": 130}, n=8)
    assert "horror fiction" in out and "Real people" in out or "real" in out.lower()


def test_fixtures_validate_against_prompt_schemas():
    from jsonschema import validate
    for n in ALL:
        p = P.load_prompt(n)
        if p.schema:
            fx = json.loads((P.FIXTURES / f"{n}.json").read_text(encoding="utf-8"))
            validate(fx, P.PROMPT_SCHEMAS[p.schema])
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement `ns_studio/text/prompts.py`**

```python
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import jinja2
import yaml

from ns_common.models import Idea, Part, Seo, VisualBeat
from ns_common.paths import contracts_dir, repo_root
from ns_studio.context import JobContext

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
FIXTURES = repo_root() / "tests" / "fixtures" / "llm"
_ENV = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True, lstrip_blocks=True, autoescape=False)


def _arr(item: dict, **extra: Any) -> dict:
    return {"type": "array", "items": item, **extra}


SCORES = {"type": "object", "properties": {k: {"type": "number", "minimum": 1, "maximum": 10} for k in
          ("hook", "clarity", "pacing", "payoff", "cliffhangers", "originality", "voice_fit")},
          "required": ["hook", "clarity", "pacing", "payoff", "cliffhangers", "originality", "voice_fit"]}

PROMPT_SCHEMAS: dict[str, dict] = {
    "ideas": {"type": "object", "properties": {"ideas": _arr(Idea.model_json_schema(), minItems=1)}, "required": ["ideas"]},
    "story_plan": {"type": "object", "properties": {
        "beats": _arr({"type": "object", "properties": {"name": {"type": "string"}, "summary": {"type": "string"}},
                       "required": ["name", "summary"]}, minItems=3),
        "part_breaks": _arr({"type": "integer"}), "ending_line_idea": {"type": "string"}},
        "required": ["beats", "part_breaks", "ending_line_idea"]},
    "critic": {"type": "object", "properties": {"scores": SCORES, "fixes": _arr({"type": "string"}), "verdict": {"type": "string"}},
               "required": ["scores", "fixes"]},
    "split": {"type": "object", "properties": {"parts": _arr(Part.model_json_schema(), minItems=1)}, "required": ["parts"]},
    "hook": {"type": "object", "properties": {"hooks": _arr({"type": "string"}, minItems=5, maxItems=5)}, "required": ["hooks"]},
    "judge": {"type": "object", "properties": {"index": {"type": "integer", "minimum": 0}, "reason": {"type": "string"}},
              "required": ["index", "reason"]},
    "seo": Seo.model_json_schema(),
    "visual_plan": {"type": "object", "properties": {"beats": _arr(VisualBeat.model_json_schema(), minItems=1)}, "required": ["beats"]},
}


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    thinking: bool
    schema: str | None
    system: str
    user_template: jinja2.Template


@lru_cache(maxsize=None)
def load_prompt(name: str) -> Prompt:
    raw = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    assert raw.startswith("---\n"), f"{name}: missing front-matter"
    _, fm, body = raw.split("---\n", 2)
    meta = yaml.safe_load(fm)
    system, _, user = body.partition("---USER---")
    return Prompt(name=name, version=int(meta["version"]), thinking=bool(meta["thinking"]), schema=meta.get("schema"),
                  system=system.strip(), user_template=_ENV.from_string(user.strip()))


@lru_cache(maxsize=1)
def policy_rules_text() -> str:
    topics = yaml.safe_load((contracts_dir() / "policy" / "unsafe_topics.yaml").read_text(encoding="utf-8"))
    lines = [f"- {c['label']}: {c['description']}" for c in topics["categories"]]
    claims = [ln.strip() for ln in (contracts_dir() / "policy" / "misleading_claims.txt").read_text(encoding="utf-8").splitlines()
              if ln.strip() and not ln.startswith("#")]
    lines.append("- Never claim the story is real. Forbidden phrases include: " + ", ".join(claims[:8]) + ".")
    return "\n".join(lines)


def render(prompt: Prompt, **vars: Any) -> str:
    return prompt.user_template.render(**vars)


def _common_vars(ctx: JobContext) -> dict[str, Any]:
    from ns_studio.text.exemplars import load_exemplars  # Task 5

    return {"channel": ctx.channel, "combo": ctx.combo, "policy_rules": policy_rules_text(),
            "exemplars": load_exemplars(ctx.channel.niche)}


def call_json(ctx: JobContext, name: str, **vars: Any) -> dict:
    p = load_prompt(name)
    assert p.schema, f"{name} has no schema"
    ctx.prompt_versions[name] = p.version
    user = render(p, **(_common_vars(ctx) | vars))
    return ctx.services.llm.complete_json(prompt_name=name, system=p.system, user=user,
                                          schema=PROMPT_SCHEMAS[p.schema], thinking=p.thinking)


def call_text(ctx: JobContext, name: str, **vars: Any) -> str:
    p = load_prompt(name)
    ctx.prompt_versions[name] = p.version
    user = render(p, **(_common_vars(ctx) | vars))
    return ctx.services.llm.complete_text(prompt_name=name, system=p.system, user=user, thinking=p.thinking)
```

Until Task 5 exists, create `ns_studio/text/exemplars.py` with `def load_exemplars(niche: str) -> list[dict]: return []`.

- [ ] **Step 4: Write the ten prompt files**

Every file follows the format above. System text is fixed per prompt (prefix caching, spec §14.1); the user part carries the variables. Write them exactly as below; the `opus` worker may improve wording but must keep the variables, schema names, thinking flags and the rules.

`ideas.md`
```
---
version: 1
thinking: false
schema: ideas
---
You invent original short-fiction premises for a narrated YouTube channel. You never retell existing books, films, games, creepypastas, Reddit posts or news. You never use real people, real brands, or real crimes and tragedies. Fiction only. You write for listeners: concrete, visual, one clear turn.
Content rules:
{{ policy_rules }}
Return JSON only.
---USER---
Channel: {{ channel.display_name }}. Niche: {{ channel.niche }}. Language: {{ channel.language }}.
Style notes from the channel owner: {{ channel.voice_notes }}
Seed keywords: {{ channel.seed_keywords | join(", ") }}
Topics to avoid: {{ channel.avoid_topics | join(", ") if channel.avoid_topics else "none" }}
Story structure for this video: {{ combo.structure }}. Hook style: {{ combo.hook }}. Point of view: {{ combo.pov_tone }}.
Target spoken length: about {{ budget.words }} words.
{% if avoid_premises %}
Do NOT resemble any of these earlier premises:
{% for p in avoid_premises %}- {{ p }}
{% endfor %}{% endif %}
Give {{ n }} distinct premises. Each: premise (2 sentences), twist (1 sentence, the turn the listener will not see coming), setting (one everyday place), protagonist (role, no real names of famous people), why_chapters (one line on how it could stretch to chapters; "n/a" if not), keywords (3-6 literal visual nouns for stock footage).
```

`story_plan.md`
```
---
version: 1
thinking: true
schema: story_plan
---
You plan short narrated fiction so it holds a listener from the first second to the last. Beats are concrete events, not themes. The ending is earned by something planted earlier. No filler.
---USER---
Premise: {{ idea.premise }}
Twist: {{ idea.twist }}
Setting: {{ idea.setting }}. Protagonist: {{ idea.protagonist }}.
Structure: {{ combo.structure }}. Hook style: {{ combo.hook }}. POV/tone: {{ combo.pov_tone }}.
Total length about {{ budget.words }} words, to be split into {{ budget.parts }} part(s) of roughly {{ budget.words_per_part }} words.
Return beats (name + one-sentence summary; 4-7 beats: setup, escalation, turn, payoff and what sits between), part_breaks (beat indices where a part ends on a cliffhanger; empty for a single part), and ending_line_idea (for a single part: a last line that loops back to the opening image; for a series: the final cliff).
```

`story_draft.md`
```
---
version: 1
thinking: false
schema: null
---
You write short fiction to be read aloud. Spoken rhythm: short sentences, concrete nouns, present or past tense kept consistent, no stage directions, no headings, no "part one". The first sentence is the hook and must be true to the story. Never claim the story is real. Avoid stock phrases and clichés. Plain text only.
---USER---
Channel voice notes: {{ channel.voice_notes }}
POV/tone: {{ combo.pov_tone }}. Pacing: {{ combo.pacing }}. Structure: {{ combo.structure }}. Hook style: {{ combo.hook }}.
Reading level: grade 5-8.
{% if exemplars %}
Reference the craft (not the content) of these exemplars from the channel:
{% for ex in exemplars %}--- exemplar: {{ ex.title }} ---
{{ ex.text }}
{% endfor %}{% endif %}
Beat sheet:
{% for b in plan.beats %}{{ loop.index }}. {{ b.name }}: {{ b.summary }}
{% endfor %}
Ending line idea: {{ plan.ending_line_idea }}
Write the full story in about {{ budget.words }} words (between {{ budget.min_words }} and {{ budget.max_words }}).
```

`critic.md`
```
---
version: 1
thinking: true
schema: critic
---
You are a hard-nosed story editor for narrated short fiction. Score 1-10 on: hook (first line grabs and is honest), clarity (understood on one listen), pacing (no filler, every sentence moves), payoff (earned and surprising), cliffhangers (part endings pull forward; 10 if single-part and the loop line works), originality (no stock phrases; not a retelling; unlike the exemplars), voice_fit (matches the channel notes). Then list concrete fixes as imperative sentences that a writer can apply without re-planning. Return JSON only.
---USER---
Channel voice notes: {{ channel.voice_notes }}
Required structure: {{ combo.structure }}. Hook style: {{ combo.hook }}.
Banned phrases (automatic originality penalty): {{ banned | join("; ") }}
Story:
{{ story }}
```

`revise.md`
```
---
version: 1
thinking: false
schema: null
---
You apply an editor's fixes to a short story read aloud. Apply only the listed fixes. Keep everything else, including length within 10 percent. Plain text only, no commentary.
---USER---
Fixes to apply:
{% for f in fixes %}- {{ f }}
{% endfor %}
Story:
{{ story }}
```

`split.md`
```
---
version: 1
thinking: false
schema: split
---
You cut a narrated story into parts for vertical video. Each part must end on a cliffhanger except the last. The last line of a single-part story loops into its first image. You never change the words except at cut points where a bridging half-sentence is needed. Return JSON only.
---USER---
Number of parts: {{ budget.parts }}. Target words per part: {{ budget.words_per_part }} (keep within 20 percent).
Story:
{{ story }}
Return parts: n (1-based), text (the exact words of that part), hook (its first sentence), word_count, est_seconds at {{ budget.wpm }} words per minute, cliffhanger (true except the last part).
```

`hook.md`
```
---
version: 1
thinking: false
schema: hook
---
You write first lines for vertical short videos. A hook is one spoken sentence of at most 12 words that makes a stranger stop scrolling AND is strictly true to what the video delivers. No clickbait, no questions that the video does not answer, no "you won't believe". Return JSON only.
---USER---
Hook style: {{ combo.hook }} (cold_open = drop into the moment; question = a real question the story answers; confession = first-person admission; shocking_fact = a concrete detail from the story; in_medias_res = mid-action; warning = a caution the story earns).
The part this hook opens:
{{ part_text }}
{% if next_part_text %}What follows in the next part (for context, do not spoil): {{ next_part_text[:300] }}{% endif %}
Write 5 different hooks in that style.
```

`judge.md`
```
---
version: 1
thinking: true
schema: judge
---
You pick the best option for a stated purpose. Prefer honesty over flash: an option that overpromises loses. Return the 0-based index and one sentence of reason. JSON only.
---USER---
Purpose: {{ purpose }}
Constraints: {{ constraints }}
Options:
{% for o in options %}{{ loop.index0 }}. {{ o }}
{% endfor %}
```

`seo.md`
```
---
version: 1
thinking: false
schema: seo
---
You write YouTube metadata for original narrated fiction. Titles describe what happens without spoiling the turn; never "true story", "real", "happened to me". Descriptions are honest and short. Tags are relevant words and phrases only, never trending topics or other channels. Return JSON only.
---USER---
Title formula: {{ combo.title_formula }} (question / statement_twist / i_never / number_hook / warning).
Primary keyword (must appear in the first 40 characters of the title): {{ primary_keyword }}
{% if part_n %}This is Part {{ part_n }} of {{ parts_total }}; the title must include "Part {{ part_n }}".{% endif %}
Story part:
{{ part_text }}
Return title (max 70 characters), description (1-2 honest sentences only; the system appends fiction label and hashtags), tags (10-20, each 1-4 words, total under 450 characters), hashtags (3-5 including #Shorts), keywords (3-6).
```

`visual_plan.md`
```
---
version: 1
thinking: false
schema: visual_plan
---
You choose stock footage for narrated fiction. For each beat (2-3 sentences) give 3 literal, filmable search keywords (things a camera can show: "empty hallway night", "kitchen window rain", "hands holding phone"), never abstract nouns or emotions, never real people or brands, and a mood from: calm, tense, dread, sad, warm, eerie, urgent. Return JSON only.
---USER---
Visual treatment: {{ combo.visual_treatment }}. Setting: {{ setting }}.
Sentences (index: text):
{% for s in sentences %}{{ loop.index0 }}: {{ s }}
{% endfor %}
Group consecutive sentences into beats of 2-3 sentences. For each beat return part={{ part_n }}, beat (0-based), sentence_start, sentence_end (inclusive), keywords (3), mood, need_vertical=true.
```

- [ ] **Step 5: Write the fixtures** (one JSON per prompt with a schema; `story_draft.txt` and `revise.txt` for text prompts). The fixtures must make a coherent ~130-word single-part horror story so the whole fake pipeline is realistic. `ideas.json`: 8 ideas, first one "The Doorbell Camera": premise "Every night at 3:12 my doorbell camera records someone standing on my porch, facing away. Tonight the notification came while I was standing on the porch.", twist "The figure in the recording is me, and the recordings are from tomorrow.", setting "a suburban porch at night", protagonist "a night-shift nurse living alone", keywords ["doorbell camera", "porch at night", "phone notification", "empty street", "front door"]. `story_plan.json`: 5 beats, `part_breaks: []`. `story_draft.txt`: the full ~130-word story, opening "The doorbell camera pings at 3:12 every night." and closing on the porch image. `critic.json`: scores all 8–9, fixes ["Cut the second sentence of paragraph two.", "Make the last line echo the first image."]. `revise.txt`: the story with those fixes. `split.json`: one part with `word_count` matching the text and `est_seconds` = words/155×60, `cliffhanger: false`. `hook.json`: 5 honest hooks ≤ 12 words. `judge.json`: `{"index": 0, "reason": "Concrete, honest, under 12 words."}`. `seo.json`: title "My Doorbell Camera Records Tomorrow Night", description one sentence, 12 tags, hashtags ["#Shorts", "#horrorstory", "#scarystories"], keywords ["doorbell camera horror", "scary story", "short horror"]. `visual_plan.json`: beats covering all sentence indices of the story with 3 literal keywords each.

- [ ] **Step 6: Run** → 4 tests pass.

- [ ] **Step 7: Commit**

```bash
git add ns_studio/text/prompts.py ns_studio/text/exemplars.py ns_studio/prompts tests/fixtures/llm tests/test_prompts.py
git commit -m "feat(text): prompt loader with front-matter, shorts-only prompts and fixtures"
```

---
