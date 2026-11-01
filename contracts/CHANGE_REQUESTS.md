# Change requests to frozen contracts

Format per entry:

## CR-<n>: <short title>
- **From:** WP-<letter> / task
- **Change:** what to add or alter (exact names, types)
- **Why:** the concrete need
- **Status:** open | applied in <commit> | rejected (<reason>)

(none yet)

## CR-C7-1: studio error codes NS-E320 to NS-E326
- **From:** C7 / m1-c7-studio-core Task 2 (also used by the M2 learn, review and cadence tasks)
- **Change:** add to `contracts/errors.json` (message and hint exactly as in the local shim `ns_studio/errors.py`, table `_LOCAL`):
  `NS-E320` The channel profile is invalid. · `NS-E321` Channels are not distinct enough. · `NS-E322` Unknown channel. · `NS-E323` The analytics CSV could not be read. · `NS-E324` Calibration data is missing or incomplete. · `NS-E325` The variation pools are exhausted. · `NS-E326` Output not found or not in a state that allows this action.
- **Why:** spec section 8 (pool minimums, distinctness), 16.5 (calibration), 18.9 (CSV import) and section 10 (review, mark-uploaded) have conditions with no code today; builders may not raise a code that is not in `errors.json`.
- **Workaround until applied:** `ns_studio.errors.StudioError` carries the same codes with a local message table and prefers `errors.json` when the code exists. When this CR is applied, empty `_LOCAL` and nothing else changes. Note: `tests` that scan source for `NS-E` literals will flag these codes until then.
- **Status:** open

## CR-C7-2: `policy_check` prompt, schema and fixture (owner of the files: C5)
- **From:** C7 / m2-c7-studio-core (policy gate)
- **Change:** C5 adds `ns_studio/prompts/policy_check.md` (front-matter `version: 1`, `thinking: true`, `schema: policy_check`), a `policy_check` entry in `PROMPT_SCHEMAS`, and a fixture `tests/fixtures/llm/policy_check.json` that passes every mode. C7 calls it as `call_json(ctx, "policy_check", mode=<mode>, ...)`.
  Template variables per `mode`:
  - `unsafe_topics`: `story` (str), `categories` (list of `{id, label, description, examples}` from `contracts/policy/unsafe_topics.yaml`), `audience` (`general|mature`).
  - `hook_title_honesty`: `story`, `items` (list of `{item, title, hook}`).
  - `standalone_short`: `story`, `moments` (list of `{k, text, hook}`).
  - `tag_relevance`: `story`, `items` (list of `{item, title, tags}`).
  Response schema (all modes, keys not relevant to a mode may be empty): `{"verdict": "pass"|"fail", "reason": str, "flags": [{"category": str, "reason": str}], "intensity": "low"|"medium"|"high", "failed_items": [str]}`. `flags[].category` is an id from `unsafe_topics.yaml`; `failed_items` holds item names (`long`, `s1`, ...) or Short numbers as strings.
- **Why:** spec section 18.8 checks `unsafe_topics`, `hook_title_honesty`, `standalone_short` and the tag relevance half of `metadata_spam` are judged by the LLM (section 16.1 lists `policy_check.md`, thinking on). C7 cannot add files to C5's directories.
- **Workaround until applied:** C7 imports `ns_studio.text.prompts` only through `importlib` inside `ns_studio/policy/judge.py`; with no `policy_check.md` present the CLI leaves the policy gate stages out of the plan (`policy.available()` is false) so `studio run --fake-services` keeps working.
- **Status:** open

## CR-C7-3: nothing new in pyproject (informational)
- **From:** C7
- **Change:** none. Perceptual hashing is implemented with FFmpeg and numpy (both already available), so `imagehash` and Pillow are not needed by C7. spaCy and keyring are only imported lazily from the existing `studio` extra.
- **Status:** informational
## CR-C4-1: fastapi (and httpx test client) in the `dev` extra
- **From:** C4 ns_tts / Task 4
- **Change:** add `"fastapi>=0.115"` to `[project.optional-dependencies].dev` in `pyproject.toml`. `numpy` is already a core dependency, so no change for it. After it lands, delete the `# pyright: reportMissingImports=false` line in `ns_tts/server.py` and the `pytest.importorskip("fastapi")` in `tests/test_tts_server.py`.
- **Why:** CI installs only `.[dev]`; without fastapi the server tests are skipped and pyright cannot resolve the imports. `uvicorn` is not needed in CI (only `ns_tts.server.main` imports it, lazily).
- **Status:** open

## CR-C4-2: `TtsCfg.engine` must accept `"qwentts"`
- **From:** C4 ns_tts / Task 3
- **Change:** in `ns_common/models.py` change `TtsCfg.engine` to `Literal["qwentts", "reference"]` with default `"qwentts"` (spec 15.4 `/health` engine names; the old `"faster"` engine was dropped). `select_engine` already treats a legacy `"faster"` value as qwentts, so the change is backward compatible. `Runtime.default("cpu-dev")` keeps `engine="reference"`. Also update the tests that assert the default.
- **Why:** with the current Literal a config cannot name the default engine, so `ns verify` cannot write `qwentts`/`reference` choices symmetrically (NS-W301 fallback writes `reference`; the happy path writes nothing).
- **Status:** applied on main (engine literal qwentts|reference|faster, default qwentts; NS-E310/E311 added)

## CR-C4-3: allow `subprocess` in `ns_tts/engine.py` once TID251 is enabled
- **From:** C4 ns_tts / Task 3
- **Change:** when `ruff` gets `TID251` banning `subprocess` (ENGINEERING.md section 8), add `ns_tts/engine.py` to the per-file-ignores list and to the hygiene-test allow-list, next to `ns_common/ffmpeg.py`.
- **Why:** `QwenttsEngine` runs the `qwentts.exe` CLI with `subprocess.run` (list args, never `shell=True`, with a timeout), as spec 6.1 describes.
- **Status:** open

## CR-C4-4: TTS-specific error codes
- **From:** C4 ns_tts / Task 3
- **Change:** add to `contracts/errors.json`: `NS-E31x` "The TTS engine failed (qwentts exited, timed out, or produced no audio)" and "A stored voice embedding is missing or was made by another engine". Until then `ns_tts` raises `NS-E202` (engine process/model failure) and `NS-E301` (bad voice data), whose messages do not fit well.
- **Why:** error messages shown to the operator should name TTS, not the language model.
- **Status:** applied on main (engine literal qwentts|reference|faster, default qwentts; NS-E310/E311 added)

## CR-C4-5: optional `cliff_lines` on `TtsItem`
- **From:** C4 ns_tts / Task 4
- **Change:** add `cliff_lines: tuple[str, ...] = ()` to `ns_common.tts_client.TtsItem` and send it as `cliff_lines` in `TtsClient.speak_batch` / `speak` bodies (the server already accepts the additive field). C6's voice stage fills it from `story.json`.
- **Why:** spec 7.5 wants 1,200 ms before a cliff line; the spec 15.4 request has no field to carry which lines are cliffs.
- **Status:** open

## CR-C5-1: master-first text models and schemas in `ns_common.models`
- **From:** C5 story text / M2 Task 6
- **Change:** move these pydantic models (all `Strict`, `extra="forbid"`) from `ns_studio/text/models_local.py` into `ns_common/models.py`, add them to `SCHEMA_MODELS` and export `contracts/schemas/<name>.schema.json` (spec 15.6). Exact fields:
  - `plan`: `Plan{target_minutes:int>=9, target_words:int>=1, chapters:list[PlanChapter] (5..7)}`, `PlanChapter{n:int>=1, title:str, beats:list[str] (>=1), cliff:str, word_budget:int>=1}`.
  - `story`: `Story{working_title:str, chapters:list[StoryChapter] (>=1), revisions:int>=0 =0}`, `StoryChapter{n:int>=1, title:str, text:str, summary:str, word_count:int>=1, est_seconds:float>0, cliff_line_idx:int|None>=0 =None, sentences:list[Sentence] (>=1)}`, `Sentence{idx:int>=0, text:str}`.
  - `shorts_plan` (list of `ShortsPlanItem`): `{k:int>=1, chapter:int>=1, sentence_start:int>=0, sentence_end:int>=0, start_s:float|None, end_s:float|None, hooks:list[str] (1..5), chosen_hook:str, closing_line:str, length_bucket:int>=1, combo_id:str, why_standalone:str}`. Note: spec says `hooks[5]`; the model accepts 1..5 because hooks over 12 words are dropped in code.
  - `seo_long`: `SeoLong(Seo){chapter_titles:list[ChapterTitle] (>=1)}`, `ChapterTitle{n:int>=1, title:str}`.
  - `clip_plan` item `ClipPlanItem{k:int>=1, start_s:float>=0, end_s:float>0, title:str<=100, hook_line:str|None, why:str, score:float 0..1, speakers:list[str]|None, keywords:list[str]=[]}`; the `clip_select` prompt answers `{"clips":[ClipPlanItem]}`, the file `clip_plan.json` is the bare list.
  - `policy_check` (prompt answer, used by C7's policy gate): `PolicyCheckResult{unsafe_categories:list[str], hook_honest:bool, title_honest:bool, standalone:bool|None, notes:str}`; the ids in `unsafe_categories` are `id` values of `contracts/policy/unsafe_topics.yaml`.
- **Why:** C6 (voice, captions, render), C7 (policy gate, orchestrator) and C8 (clipper) read and write these files; the single source of truth must be `ns_common`. C5 uses local copies meanwhile, so nothing blocks.
- **Migration:** C5 replaces `from ns_studio.text.models_local import X` by `from ns_common.models import X` and deletes `models_local.py` once this lands; field names do not change.
- **Status:** open
