# M2 C5 Long-form text (master-first) Implementation Plan

> **For agentic workers:** same execution rules as `m1-c5-text.md`. Spec: BUILD_SPEC.md 7.0, 7.1 (M1-M8), 9.1, 9.2, 15.6, 16.1-16.3, 18.4, 19.5. No local execution; CI is the authority.

**Goal:** the master-first text stages (ideas reuse, long plan, chapter drafts with running summaries, per-chapter critique and revise, continuity pass, long SEO, Shorts selection, per-beat visual plan, Shorts SEO), the prompts they need plus `policy_check`, `clip_select`, `clip_seo`, the `expand_chapter` helper for the C6/C7 duration loop, and a coherent ~1,600-word 5-chapter fixture set.

**Dependencies:** M1 C5 on the same branch; C7's `FORMAT_STAGES`, `register`, `JobContext`, `make_ctx`. `plan`, `story`, `shorts_plan`, `seo_long` models are owned by C3: change request CR filed; until it is merged they live in `ns_studio/text/models_local.py` ("temporary until the CR").

## File structure

```
ns_studio/text/budget.py          + MasterBudget, master_budget(), distribute_words(), short_body_words()
ns_studio/text/models_local.py    Plan, PlanChapter, Story, StoryChapter, Sentence, ShortsPlanItem, SeoLong, ChapterTitle, ClipPlanItem
ns_studio/text/validators.py      + validate_chapter(), check_cliff_variety(), total-length helper
ns_studio/text/seo.py             finalize_seo_long(), render_chapter_list(), fill_chapter_list()  (finalize_seo stays in stages_shorts)
ns_studio/text/stages_master.py   stages + expand_chapter()
ns_studio/prompts/                long_plan chapter_draft continuity expand long_seo shorts_select short_hook policy_check clip_select clip_seo (.md)
tests/fixtures/llm/               long_plan, chapter_draft_1..5, continuity, expand.txt, long_seo, shorts_select, short_hook, seo_short_master,
                                  policy_check, clip_select, clip_seo
tests/test_budget.py test_text_stages_master.py test_seo_long.py (+ prompt/validator cases)
```

## Design decisions (each checkable in the tests)

1. **Stage names** (REGISTRY is flat, M1 owns `plan`): `ideas` (shared with M1), `long_plan`, `chapters_draft`, `chapters_critic`, `continuity`, `seo_long`, `shorts_select`, `visual_plan_long`, `seo_shorts`. All `tenant="llama"`. Registered into `FORMAT_STAGES["master_first"]` in that order.
2. **Order deviation:** Shorts selection (spec M7) runs before the visual plan (spec M6) so that `need_vertical` is computed once from the selected spans and no stage rewrites its own input (a stage that rewrites an input file re-runs on every resume).
3. **No in-place mutation between stages:** `chapters_draft` -> `story_draft.json`; `chapters_critic` -> `story_revised.json` + `critic.json`; `continuity` -> `story.json` + `continuity.json`. `story.json` is the final `Story`. Chapter drafts are also saved to `drafts/ch<n>.json` so a crash mid-chapter resumes at the next chapter.
4. **Target length:** `master_budget(long_minutes, pacing, rng=None)`; a `(lo, hi)` tuple draws `rng.randint(lo, hi)` (stages seed `random.Random(job_id)`: deterministic per job, varied across jobs; without rng the rounded-up midpoint), an `int` is used as is. `target_words = minutes * wpm`; `min_words = 9 * wpm`; chapters `clamp(round(target_words / 375), 5, 7)`; budgets distributed to sum exactly to `target_words`; chapter limits +-20 %.
5. **Chapter fixture per call:** `call_json(..., fixture="chapter_draft_<n>")` so `FakeLlm` serves distinct chapters. `chapter_draft` returns `{"text", "summary"}`.
6. **Continuity:** whole story in one call, or pairs of adjacent chapters when `runtime.profile == "cpu-dev"` or `llama.flags.ctx <= 8192`. Returned issues are applied with `revise.md` to the affected chapter; at most one pass.
7. **Cliff variety (spec 16.3):** consecutive chapter-ending sentences with embedding cosine >= 0.92 raise `NS-E303`.
8. **Shorts selection:** the LLM proposes moments; code resolves them: clamp to the chapter, snap `length_bucket` to the nearest of `channel.length_buckets` by body words (`bucket*wpm/60 - 12 hook - 12 closing`), drop spans more than 20 % off, drop overlaps, keep `channel.shorts_per_master` (default 4-6) else `NS-E303`. Per moment: `short_hook` (5 hooks, closing line), `judge` for the hook; `combo_id` is the master's; C7 overwrites per-Short combos.
9. **`expand_chapter(ctx, chapter_n, add_words)`**: `expand.md` adds sentences and keeps all existing ones verbatim; sentence indices in `shorts_plan.json` and `visual_plan.json` are remapped with `difflib.SequenceMatcher` on the sentence lists; `stories` row updated. Also `expansion_plan(story, deficit_words)` choosing the shortest chapter and +120..200 words. Caller holds the `llama` tenant.
10. **SEO:** `finalize_seo_long` (9.1): title <= 100, keyword in first 40 chars, no "Part X", description = summary lines (<= 3) + blank + `[[CHAPTERS]]` placeholder + blank + "Original fiction." (+ creator note), no `#Shorts`, 3-5 hashtags, 10-20 tags <= 500 chars. `fill_chapter_list(description, chapters)` renders `MM:SS Title` (first `00:00`). Shorts SEO reuses `finalize_seo(master_title=...)` which adds "From the full story: <master title>" before "Original fiction.".
11. **Policy and clipper prompts:** `policy_check` schema `{unsafe_categories: [str], hook_honest, title_honest, standalone: bool|null, notes}`; ids come from `contracts/policy/unsafe_topics.yaml`. `clip_select` returns `{"clips": [ClipPlanItem]}` (the file `clip_plan.json` is the bare list), `clip_seo` uses the `seo` schema.

## Tasks

### Task 6: Budget additions, local models, validators
- [ ] Tests: `tests/test_budget.py` (10 min at 155 = 1550 words, 5 chapters of 310; 14 min brisk gives 7 chapters; sums exact; `min_words = 1395`; tuple draw is deterministic for a seeded rng), model round-trips, `validate_chapter`, `check_cliff_variety`.
- [ ] Code: `budget.py` additions, `models_local.py`, `validators.py` additions.
- [ ] Commit `feat(c5): master budgets, local plan/story models, chapter validators`.

### Task 7: Long-form, policy and clipper prompts with fixtures
- [ ] Ten prompt files with the thinking flags of spec 16.1 / 19.5; schemas added to `PROMPT_SCHEMAS`.
- [ ] Fixtures: a coherent 5-chapter "The Doorbell Camera" (1,550-1,650 words, each chapter inside 248-372 words, grade 5-8) and the metadata/selection fixtures keyed to its real sentence indices.
- [ ] Tests: thinking flags, schemas validate fixtures, `chapter_draft_<n>` texts equal the validated story, shorts spans index real sentences.
- [ ] Commit `feat(c5): long-form, policy and clipper prompts with coherent fixtures`.

### Task 8: Master text stages
- [ ] Tests (`tests/test_text_stages_master.py`): registration order; full pipeline on fixtures (plan -> story -> seo_long -> shorts -> beats -> shorts SEO); run twice is a no-op; pairwise continuity on `cpu-dev`; continuity issues trigger one `revise`; selection resolver edge cases; `expand_chapter` keeps indices consistent; `finalize_seo_long` rules.
- [ ] Code: `stages_master.py`, `seo.py`.
- [ ] Commit `feat(c5): master-first text stages with expand helper`.

### Task 9: Change request and plan ticks
- [ ] Append `CR` for `plan`, `story`, `shorts_plan`, `seo_long`, `clip_plan`, `policy_check` models/schemas to `contracts/CHANGE_REQUESTS.md`.
- [ ] Commit `docs(c5): change request for master-first text schemas`.

## Done when
CI green on the PR branch; fixtures validate against `PROMPT_SCHEMAS` and the local models; the master pipeline test passes on `FakeLlm` fixtures only.
