# C11 Series Workshop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The owner and the Assistant develop a multi-part story as plain files (concepts, part map, scripts, Shorts, design), approve it, and `produce` enqueues one master-first job per part whose story source is the edited script files (stage `script_import` replaces M1-M4).

**Architecture:** `ns_studio/series/` holds a folder model (`store.py`), a `Workshop` class with one method per tool (`workshop.py`), a policy text gate (`policy.py`), the `script_import` stage (`stages.py`), Assistant tool descriptors (`tools.py`) and a typer sub-app (`cli.py`). Chapter drafting, critic, revise and continuity reuse the C5 prompts through `prompts.call_json/call_text` against a scratch `JobContext`; Shorts selection reuses `resolve_moments`/`span_text`. The folder is the only source of truth; nothing needs chat history.

**Tech Stack:** Python 3.12, pydantic, PyYAML, Jinja2, typer. Nothing heavy is imported.

**Spec:** BUILD_SPEC.md §23 (all), §7.0-7.2, §9, §15.8, §16, §17.1, §18.3-18.5, §20.3a. Contracts pinned to `contracts-m2` (no contract edits; change requests only).

## Global Constraints

- Own only `ns_studio/series/`, `ns_studio/prompts/workshop_*.md`, `tests/test_series_*.py`, `tests/fixtures/llm/workshop_*`, this plan. Never edit `ns_common/`, `contracts/` (except appending to `CHANGE_REQUESTS.md`), `pyproject.toml`, `.github/`.
- Nothing is executed locally; CI (ruff, pyright, pytest on windows-latest) is the judge. Commit trailers per ENGINEERING §5.
- Folder layout exactly §23.2: `concept.md concept-A..C.md series.md design.yaml status.yaml parts/part-NN/{full/script.md, shorts/sNN.md, notes.md}`; scratch work lives under `data/workshop/<slug>/`, never in the story folder.
- Parts are 12-25 minutes; words = minutes x wpm (combo pacing, 155 default); chapters = clamp(round(words/350), 5, 12) (20 min = 9).
- `script.md`: `## N. Title` per chapter, `<!-- sK -->` before every sentence (global, 1-based), `[BREAK]` between chapters. Shorts front-matter `span: [first, last]` uses those numbers; `start_text` anchors the span if the owner edits the script.
- Policy: concept text gate at `concepts`/`regenerate`/`choose`, script gate at `write_part`/`revise_part`/`approve`; codes NS-E601 (unsafe), NS-E602 (real entity), NS-E603 (misleading claim), NS-E607 (short not standalone).
- Usage errors (unknown story, bad status, minutes out of range) raise `WorkshopError(ValueError)` until a CR adds codes; content rejections raise `NsError` with an existing code.
- Series context for C6 outbox: `produce` writes `series_context.json` = {series_slug, series_title, part_n} into every job dir; titles "<series> - Part NN" and "<series> - Part NN - Short 01".
- Worker allocation: Task 2 (prompts) `opus`; others `sonnet`.

## File structure created by this plan

```
ns_studio/series/__init__.py schemas.py store.py policy.py workshop.py stages.py tools.py cli.py
ns_studio/prompts/workshop_concepts.md workshop_regenerate.md workshop_map.md workshop_part_outline.md workshop_revise.md
tests/fixtures/llm/workshop_*.json|txt
tests/test_series_store.py test_series_prompts.py test_series_policy.py test_series_workshop.py
tests/test_series_import.py test_series_tools.py
```

### Task 1: Folder model (`store.py`)

**Files:** Create `ns_studio/series/__init__.py`, `store.py`, `tests/test_series_store.py`.

- [ ] Write tests: front-matter round trip, concept render/parse, script render/parse (breaks, numbering, paragraph fallback), short file round trip, span relocation by anchor, status/series/design models, bounds (12-25).
- [ ] Implement `StoryDir`, `SeriesDoc`, `StatusDoc`, `DesignDoc`, `ConceptDoc`, `ScriptChapter`, `ShortDoc`, md helpers.
- [ ] Commit `feat(c11): story folder model`.

### Task 2: Prompts, schemas, fixtures

**Files:** Create `schemas.py`, the five prompts, fixtures, `tests/test_series_prompts.py`.

- [ ] Tests: front-matter loads, every `workshop_*.json` fixture validates against its schema, prompts render with their variables.
- [ ] Write prompts (`workshop_concepts` thinking on, `workshop_regenerate`, `workshop_map` on, `workshop_part_outline` on, `workshop_revise` off) and fixtures (copies of the C5 doorbell story chapters and Shorts so spans are known to resolve).
- [ ] Commit `feat(c11): workshop prompts, schemas and fixtures`.

### Task 3: Policy text gate (`policy.py`)

- [ ] Tests: block-list name -> NS-E602, "true story" in a title -> NS-E603, LLM category -> NS-E601, clean text passes, short not standalone -> NS-E607.
- [ ] Implement `text_gate`, `claims_gate`, `check_standalone`.
- [ ] Commit `feat(c11): workshop policy gate`.

### Task 4: Workshop part 1 (`start`, `concepts`, `regenerate`, `choose`, `map_parts`)

- [ ] Tests: full tree after start/concepts/choose/map, regenerate replaces one draft only, map enforces 12-25, gate rejection writes nothing.
- [ ] Implement `Workshop` (env, scratch ctx, channel loader), these five methods, `default_workshop`.
- [ ] Commit `feat(c11): workshop concepts and series map`.

### Task 5: Workshop part 2 (`write_part`, `revise_part`, `approve`, `design`, `produce`, `status`, `list`)

- [ ] Tests: script.md with [BREAK], >= 4 Shorts with distinct hooks <= 12 words, notes.md scores, revise keeps unrelated chapters, approve/status transitions, design validation vs formats_allowed, produce via fake enqueue and `series_context.json`.
- [ ] Implement them plus `default_enqueue`.
- [ ] Commit `feat(c11): part writing, approval, design and produce`.

### Task 6: `script_import` stage (`stages.py`)

- [ ] Tests: story.json/plan.json/plan_meta.json/idea.json/shorts_plan.json consistent with the files; hand edit uses edited text; moved span relocates; cross-chapter span -> NS-E303.
- [ ] Implement and register `ScriptImportStage` (tenant None).
- [ ] Commit `feat(c11): script_import stage`.

### Task 7: Tools and CLI

- [ ] Tests: `TOOLS` descriptors valid (`ToolSpec` fields, JSON schemas), read-only flags, a tool call runs; CLI `--help` lists the commands and a fake-services smoke run works.
- [ ] Implement `tools.py`, `cli.py`.
- [ ] Commit `feat(c11): assistant tools and series CLI`.

### Task 8: Change requests and CI

- [ ] Append CR-C11-1..n to `contracts/CHANGE_REQUESTS.md`; push; read CI; fix until green; `gh pr create --fill`.
