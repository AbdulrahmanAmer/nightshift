# M1 C5 Story text (shorts-only) Implementation Plan

> **For agentic workers:** execute task by task; commit per task; push after every 2-3 tasks; CI (`windows-latest`) is the only authority on green (ENGINEERING.md sections 1 rule 13, 5). Nothing is executed on the authoring laptop.

**Goal:** the shorts-only text stages (ideas, plan, draft, critic_revise, split, hooks, seo, visual_plan) with prompts, validators, budgets, fixtures and exemplars, producing from `FakeLlm` fixtures one coherent ~117-word horror story that passes the validators for a 50 s bucket at 155 wpm.

**Reference plans (authoritative for code):** `_s1_part1.md` Task 2, `_s1_part2.md` Tasks 3, 4, 5. This file lists what this builder does and where it deviates. Contracts pinned to tag `contracts-m0` (the current `main`; `ns_common` names used exactly).

**Owned paths:** `ns_studio/text/`, `ns_studio/prompts/`, `ns_studio/exemplars/`, `tests/fixtures/llm/`, `tests/test_prompts.py`, `tests/test_validators.py`, `tests/test_text_stages*.py`, `tests/test_exemplars.py`, `tests/test_budget.py`.

**Dependencies:** C7 modules (`context`, `stages`, `orchestrator`, `tests/helpers.py`) and `contracts/policy/*` are on `main` (PR #1).

## Deviations from the reference plan (all for CI cleanliness; behaviour is the same)

- (dropped) `prompts.py` takes `JobContext` directly now that C7 is on `main`.
- `call_json` / `call_text` gain two optional keyword-only parameters: `fixture` (the `prompt_name` handed to the LLM, so a fake can serve a different fixture file per call, e.g. `chapter_draft_3`; the real client ignores it) and, for `call_json`, `schema_name` (override of the front-matter schema). Both default to `None`; the documented signature still works.
- Stage classes annotate their attributes (`name: str`, `tenant: Tenant`, `inputs: tuple[str, ...]`) so pyright accepts them as `Stage`.
- Test snippets in the reference plans use `;` and one-line `class` bodies (ruff `E70x`); written out in normal form here.
- A missing front-matter raises `ValueError` (programming error, not a runtime failure), not an assert.
- `finalize_seo` gains optional `master_title` and `extra_lines` keyword parameters (M2 reuses it); defaults keep the M1 behaviour.

## Tasks

### Task 2: Prompt loader, ten shorts-only prompts, fixtures (opus-grade design, built by one worker)
- [x] Tests first: `tests/test_prompts.py` (front-matter on every prompt, thinking flags match spec 16.1, render of `ideas`, missing front-matter rejected, `call_json` records the prompt version and forwards schema/thinking, every fixture validates against `PROMPT_SCHEMAS`, the fixture story is the same text in `story_draft.txt` / `split.json`).
- [x] `ns_studio/text/prompts.py`, ten prompt files, ten fixtures. Fixture story: 117 words +-20 %, grade 5-8, no sentence over 35 words.
- [x] Commit `feat(c5): prompt loader with front-matter, shorts-only prompts and fixtures`.

### Task 3: Budgets and hard validators
- [x] `tests/test_validators.py`, `tests/test_budget.py` (short budget math: `round(50*155/60)=129`, minus 12 hook words = 117).
- [x] `ns_studio/text/budget.py` (`PACING_WPM`, `Budget`, `short_budget`), `ns_studio/text/validators.py`.
- [x] Commit `feat(c5): word budgets and hard validators`.

### Task 4: Shorts-only text stages (starts after C7 is on `main`)
- [x] `tests/test_text_stages.py` (registration order, full fixture pipeline, critic fail twice gives `NS-E304` with two `revise` calls, idea dedupe against history, `finalize_seo` rules, run twice is a no-op through `run_stage`).
- [x] `ns_studio/text/stages_shorts.py` exactly as `_s1_part2.md` Task 4, with typed attributes.
- [x] Commit `feat(c5): shorts-only text stages ideas to visual plan with critic loop`.

### Task 5: Exemplars
- [x] `tests/test_exemplars.py` (niche mapping, 3 exemplars per niche, 600-900 words, grade 4-9, no "true story" / "based on", every `meta.yaml` field present).
- [x] `ns_studio/text/exemplars.py`; 12 original stories and 4 `meta.yaml` (written by sonnet workers against a gawk port of `textstats.py`, re-checked by the builder).
- [x] Commit `feat(c5): exemplar library with 12 original stories across 4 niches`.

## Done when
CI run on `feat/c5-text` is green (ruff, pyright, pytest) and the PR is open.
