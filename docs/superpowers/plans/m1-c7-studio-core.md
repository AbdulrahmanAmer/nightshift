# M1 C7 Studio core: orchestrator, minimal CLI, channels, variation draw, calibrate

> **For agentic workers:** execute task by task. Every "Run: pytest" step means: commit, push, read CI (`gh run list --branch feat/c7-core --limit 1`, `gh run view <id> --log-failed`), fix until green. Nothing runs locally. Branch `feat/c7-core`, scope `c7`.

**Goal:** `studio run [--format shorts_only] [--count N] [--channel slug] [--fake-services[=list]] [--dry-run] [--resume job_id]`, `studio channel add|list|edit|validate`, `studio voice list|preview`, the variation draw with the combo ledger, and `studio calibrate`, all testable with fakes and without C5/C6 code present.

**Spec:** `BUILD_SPEC.md` §8 (variation), §11 (channel profile), §15.8-15.9 (job dir, CLI), §16.5 (calibration), §17 (pools), §18.9 (pool weights, 5 % floor).

**Owned paths:** `ns_studio/{context,stages,orchestrator,cli,errors}.py`, `ns_studio/channels/`, `ns_studio/variation/`, `ns_studio/calibrate/`, `tests/helpers.py`, `tests/test_{context,stages,orchestrator,cli,channels,variation,calibrate}.py`.

## Global constraints for this plan

- C5 and C6 modules (`ns_studio.text.*`, `ns_studio.media.*`) are reached only through `importlib.import_module` inside functions, so ruff and pyright pass while they do not exist yet (pyright reports unresolved imports otherwise) and CI has no Pillow, spaCy or keyring.
- Error codes: only codes present in `contracts/errors.json` may be raised through `NsError`. Studio-specific conditions that have no code yet (channel profile invalid, channels not distinct, unknown channel, bad analytics CSV, calibration data missing, variation pools exhausted) raise `StudioError` from `ns_studio/errors.py`, a shim that prefers `errors.json` when the code exists and otherwise carries its own message. Codes `NS-E320` to `NS-E325` are requested in `contracts/CHANGE_REQUESTS.md` (CR-C7-1). When the CR lands, the shim table is emptied and nothing else changes.
- Logging only through `NsLogger`; `print` only inside `cli.py` (typer echo).
- Heavy things stay lazy: `keyring` (API keys), spaCy.

## File structure

```
ns_studio/errors.py            StudioError shim
ns_studio/channels/__init__.py
ns_studio/channels/crud.py     pool minimums, distinctness, add/edit/validate/list, sync to channels table
ns_studio/variation/__init__.py
ns_studio/variation/draw.py    weights_for, draw_combo, record_combo, ledger queries
ns_studio/calibrate/__init__.py
ns_studio/calibrate/run.py     spearman, generate, score, recommend_bar, refusal rate
ns_studio/cli.py               typer app: run, channel, voice, calibrate
tests/test_channels.py test_variation.py test_calibrate.py test_cli.py
contracts/CHANGE_REQUESTS.md   CR-C7-1 (error codes), CR-C7-2 (policy_check prompt, schema, fixture for C5)
```

---

### Task 1: Studio context, stage registry, orchestrator skeleton (done)

Implemented exactly as written in `_s1_part1.md` Task 1; CI run https://github.com/AbdulrahmanAmer/nightshift/actions/runs/41338423736 (success). Deviation: the group key in `run_job` is the module function `_tenant_of` (pyright widens a lambda's `Literal` result to `str`).

- [x] Steps 1-7 of `_s1_part1.md` Task 1.

---

### Task 2: Error shim and channel CRUD with pool minimums and distinctness (`sonnet`)

**Files:** Create `ns_studio/errors.py`, `ns_studio/channels/__init__.py`, `ns_studio/channels/crud.py`, `tests/test_channels.py`.

**Interfaces:**
```python
# ns_studio/errors.py
class StudioError(Exception):
    code: str; detail: str; hint: str
    def __init__(self, code: str, detail: str = "") -> None
# ns_studio/channels/crud.py
MAX_CHANNELS = 3
def pool_problems(pools: Pools) -> list[str]        # hard minimums (spec 8)
def pool_warnings(pools: Pools) -> list[str]        # below 4x4 hooks x structures
def distinct_problems(new: Channel, others: list[Channel]) -> list[str]
def validate_channel(ch: Channel, others: list[Channel]) -> tuple[list[str], list[str]]  # (problems, warnings)
def add_channel(paths: Paths, db: Db, ch: Channel, *, now: datetime | None = None) -> Path
def edit_channel(paths: Paths, db: Db, slug: str, updates: dict[str, Any]) -> Channel     # dotted keys: "pools.hooks"
def get_channel(paths: Paths, slug: str) -> Channel            # StudioError NS-E322 when missing
def all_channels(paths: Paths) -> list[Channel]
def sync_channel_row(db: Db, ch: Channel, now: datetime) -> None   # channels(slug, display_name, created_at, status)
```
Rules: hooks >= 3, structures >= 3, `len(hooks) * len(structures) >= 9`, caption_styles >= 2, visual_treatments >= 2; every pool entry unique; warning when hooks < 4 or structures < 4. Distinctness (spec 8): two channels fail when niche (case and whitespace normalised), voice (mode, speaker, name, description; speed ignored), caption-style set and visual-treatment set are all equal; slug must be unique; at most 3 channels per install (spec 18.6).

- [x] **Step 1: tests first** (`tests/test_channels.py`): minimum failures each report their rule; 4x4 default pools pass without warnings, 3x3 passes with a warning; add twice with equal everything fails `NS-E321`; different voice passes; fourth channel fails; `edit_channel(..., {"pools.hooks": ["a"]})` fails `NS-E320` and leaves the YAML untouched; `edit_channel` with `critic_bar` persists; `add_channel` writes the YAML and inserts the `channels` row.
- [x] **Step 2: implement** `errors.py` and `crud.py` (edit applies the updates to `model_dump()`, validates through `Channel.model_validate`, validates pools and distinctness, then saves).
- [x] **Step 3: Run** (commit, push, CI) then `git commit -m "feat(c7): channel CRUD with pool minimums and distinctness"`.

---

### Task 3: Variation draw with weights, floor and combo ledger (`opus`: ledger logic)

**Files:** Create `ns_studio/variation/__init__.py`, `ns_studio/variation/draw.py`, `tests/test_variation.py`.

**Interfaces:**
```python
FLOOR = 0.05
POOLS = ("hooks", "structures", "pov_tones", "pacings", "caption_styles", "visual_treatments", "title_formulas", "length_buckets")
def apply_floor(weights: dict[str, float], floor: float = FLOOR) -> dict[str, float]   # normalised, every value >= floor
def weights_for(db: Db, channel: str, pool: str, options: list[str]) -> dict[str, float]  # pool_weights rows, default 1.0, floored
def ledger_window(pools: Pools) -> int                                   # min(10, pairs - 1)
def recent_pairs(db: Db, channel: str, window: int) -> set[tuple[str, str]]
def used_full_combos(db: Db, channel: str, now: datetime, days: int = 30) -> set[tuple[str, ...]]
def draw_combo(db: Db, channel: Channel, *, rng: random.Random, now: datetime | None = None,
               fixed: dict[str, str] | None = None) -> Combo
def record_combo(db: Db, channel: str, job_id: str, item: str, combo: Combo, now: datetime | None = None) -> None
```
Algorithm of `draw_combo`:
1. Candidate pairs = hooks x structures (restricted by `fixed["structure"]` / `fixed["hook"]`), minus `recent_pairs(window)`. Never empty because `window <= pairs - 1`; with `fixed` the exclusion is applied only if something remains, else `StudioError("NS-E325")`.
2. Weighted choice of a pair with weight `w_hook * w_structure`.
3. Draw the other six dimensions independently by weights (honouring `fixed`), up to 60 attempts until the full tuple is not in `used_full_combos`; then enumerate the remaining Cartesian product and pick the first unused by weight order; none left means `StudioError("NS-E325")`.
4. `combo_id = f"c-{rng.getrandbits(32):08x}"` (collision with the `combos` table redraws the id).

- [x] **Step 1: tests first**: `apply_floor({"a": 0.99, "b": 0.01})` gives `b >= 0.05` and sum 1; with weights `{"a": 1000}` stored, option `b` still drawn in about 5 % of 4000 seeded draws (between 3 % and 8 %); ledger: after recording a combo, `draw_combo` 50 times never returns the same `{hook, structure}` while it is inside the window; with 3x3 pools (window 8) nine consecutive recorded draws never repeat a pair inside any window of 8; 30-day rule: a combo recorded 31 days ago may repeat, one recorded 29 days ago may not (shrink pools to one option each to force it); exhaustion raises `NS-E325`; `fixed={"structure": "twist"}` keeps the structure.
- [x] **Step 2: implement.**
- [x] **Step 3: Run** then `git commit -m "feat(c7): weighted combo draw with 30-day and pair ledger"`.

---

### Task 4: `studio run` and the CLI shell (`sonnet`)

**Files:** Modify `ns_studio/cli.py`; create `tests/test_cli.py`.

**Interfaces:**
```python
STAGE_MODULES = ("ns_studio.text.stages_shorts", "ns_studio.media.stages_media")   # imported lazily, ModuleNotFoundError of exactly that module ignored
def load_stage_modules() -> list[str]            # returns names actually imported
def parse_fake(value: str | None) -> frozenset[str]   # None -> empty, "" or "all" -> FAKEABLE, "llm,tts" -> subset, unknown name -> typer.BadParameter
def load_keys() -> dict[str, str]                # keyring service "Nightshift", lazy import via importlib, {} when unavailable
def make_job(paths, db, runtime, services, channel, fmt, *, now, rng) -> JobContext   # draws combo, records it, creates job dir, writes job.json
def load_job(paths, db, runtime, services, job_id) -> JobContext                   # --resume
@app.command("run") def run(daily: bool, channel: str | None, count: int, fmt: str | None, dry_run: bool, resume: str | None, fake_services: str | None)
```
Behaviour: `--fake-services` with no channel on disk creates a `demo` channel (logged). `--dry-run` prints stage names, no job dir. `--resume job_id` reads `job.json` (channel, combo, fmt) and calls `run_job(resume=True)`. An `NsError`/`StudioError` prints `CODE: message` plus hint and exits 1. `--format` defaults to the channel's format. `job.json` = `{"job_id", "channel", "combo", "fmt", "created_at"}`. One `studio run` summary line per job. The runtime comes from `load_runtime(paths)`; when the file is missing and fakes are on, `Runtime.default("cpu-dev")`.

- [x] **Step 1: tests first** (`tests/test_cli.py`, typer `CliRunner`, fixture registers two stub stages into `FORMAT_STAGES["shorts_only"]` and disables `STAGE_MODULES`): `studio --help` lists run/channel/voice/calibrate; `run --fake-services --format shorts_only --count 2` creates two job dirs with `stage_state.json` and two `combos` rows and prints both job ids; `--dry-run` creates no job dir; `--resume` after deleting a stage output re-runs only that stage; `--fake-services=llm,tts` builds a Services with real (unconnected) whisper; bad fake name exits 2; `NsError` exit code 1 with the code in the output.
- [x] **Step 2: implement.**
- [x] **Step 3: Run** then `git commit -m "feat(c7): studio run with fake services, dry run and resume"`.

---

### Task 5: `studio channel` and `studio voice` commands (`sonnet`)

**Files:** Modify `ns_studio/cli.py`; extend `tests/test_cli.py`.

Commands: `channel add --slug --name --niche [--keywords a,b] [--voice-notes] [--speaker] [--format] [--from-yaml file]`, `channel list` (slug, status, format, pairs), `channel edit <slug> --set key=value ...` (value parsed as JSON, falling back to string; dotted keys), `channel validate [slug]` (prints problems and warnings, exit 1 on problems), `voice list [--fake-services]` (name, mode), `voice preview <name> [--text] [--out file] [--fake-services]` (acquires `tts` through the acquirer, calls `Tts.speak`, prints the path and duration).

- [x] **Step 1: tests first**: add then list shows the channel; add with 2 hooks exits 1 with the minimum rule in the output; duplicate add exits 1; edit changes `critic_bar` and rejects `pools.hooks=["a"]`; validate shows the 3x3 warning; `voice preview --fake-services` writes a WAV and the acquirer events show acquire/release `tts`.
- [x] **Step 2: implement.**
- [x] **Step 3: Run** then `git commit -m "feat(c7): studio channel and voice commands"`.

---

### Task 6: `studio calibrate` (spec 16.5) (`sonnet`)

**Files:** Create `ns_studio/calibrate/__init__.py`, `ns_studio/calibrate/run.py`, `tests/test_calibrate.py`; modify `ns_studio/cli.py`.

**Interfaces:**
```python
RUBRIC = ("hook", "clarity", "pacing", "payoff", "cliffhangers", "originality", "voice_fit")
def spearman(xs: Sequence[float], ys: Sequence[float]) -> float          # average ranks for ties; 0.0 when a side has no variance
def refusal_like(text: str) -> bool                                      # empty/very short or refusal phrasing
def recommend_bar(critic_avgs, human_avgs, human_mins, *, pass_avg=8.0, pass_min=6.0) -> BarRecommendation
@dataclass class BarRecommendation: bar: float; good_pass_rate: float; bad_pass_rate: float; satisfied: bool
Generator = Callable[[JobContext], str]; Critic = Callable[[JobContext, str], dict]
def generate(ctx_factory, n, *, generator=default_generator, critic=default_critic) -> list[CalibrationStory]
def write_generation(paths, slug, stories) -> Path                        # data/calibration/<slug>/{stories.csv, <id>.txt, scores.csv template}
def score(paths, db, channel, scores_csv: Path) -> CalibrationReport      # per-rubric and overall Spearman, mean bias, recommended bar, refusal rate; writes calibration_report.json and critic_bar into the channel YAML
```
`default_generator` runs the registered stages `ideas`, `plan`, `draft` (C5) for a fresh job and returns `story.json["text"]`; `default_critic` calls `ns_studio.text.prompts.call_json(ctx, "critic", story=..., banned=...)` through `importlib`. Both are replaceable so the tests need no C5 code. Rows go to the `calibration` table (`critic_json`, `human_json`). Refusal rate above 2 % prints the two-model-mode recommendation.

- [x] **Step 1: tests first**: Spearman of perfectly ordered series = 1, reversed = -1, ties handled (`[1,1,2]` vs `[1,2,3]` = 0.866...), constant series = 0; bar recommendation on a crafted set satisfies >= 80 % good and <= 20 % bad; generation with stub generator/critic writes `stories.csv`, 3 `.txt`, `scores.csv` header; score() round trip updates `critic_bar` in the YAML and reports refusal rate (one stub story answers "I'm sorry, I can't help with that").
- [x] **Step 2: implement.**
- [x] **Step 3: Run** then `git commit -m "feat(c7): studio calibrate harness with Spearman and bar recommendation"`.

---

### Task 7: Change requests (`sonnet`)

Append CR-1 (error codes NS-E320..E312) and CR-C7-2 (prompt `policy_check`, its schema and fixture, owner C5) to `contracts/CHANGE_REQUESTS.md`.

- [x] Commit `docs(c7): change requests for error codes and policy_check prompt`.
