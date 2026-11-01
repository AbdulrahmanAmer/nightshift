# M2 C7 Studio core: policy gate, learning loop, cadence, review, daily run

> **For agentic workers:** execute task by task. "Run: pytest" means: commit, push, read CI (`gh run list --branch feat/c7-core --limit 1`, `gh run view <id> --log-failed`), fix until green. Nothing runs locally. Branch `feat/c7-core`, scope `c7`.

**Goal:** the safety and growth half of the studio: a policy gate with one class per check (`NS-E601` to `NS-E614`) and regenerate-once, the analytics learning loop, the cadence governor, the review commands, pause/resume, `studio status --milestone`, and `studio run --daily` with a database backup.

**Spec:** `BUILD_SPEC.md` §10 (review), §15.9 (CLI), §18.3-18.12 (safety, retention, cadence, disclosure, gate, learning, incidents, go/no-go).

**Owned paths:** `ns_studio/policy/`, `ns_studio/learn/`, `ns_studio/channels/cadence.py`, `ns_studio/review.py`, `ns_studio/daily.py`, `ns_studio/jobs.py`, `ns_studio/cli.py`, `tests/test_policy_{text,final,gate}.py`, `tests/test_{learn,cadence,review}.py`, `tests/helpers.py`.

## Global constraints for this plan

- Same as `m1-c7-studio-core.md`: C5/C6 modules only through `importlib`; no `imagehash`, Pillow or spaCy needed by CI; codes not in `errors.json` go through `StudioError` (CR-C7-1).
- **Prompt names called** (CR-C7-2, C5 writes the files): `policy_check` with `mode` = `unsafe_topics | hook_title_honesty | standalone_short | tag_relevance`. Until `ns_studio/prompts/policy_check.md` exists the CLI leaves the gate stages out (`policy.gate.available()`), so `studio run --fake-services` keeps working. `--policy/--no-policy` overrides.
- Gate stages are inserted into `FORMAT_STAGES[fmt]` by `install_gates`: `policy_text` after `visual_plan` (else after `shorts_plan`/`seo_shorts`/`seo`/`split`, else before `voice`), `policy_final` before `outbox` (else after `qc`, else last).
- Fake-service runs skip the two checks that cannot work with fixtures that serve the same story and the same footage to every job: `self_similarity` (skipped when the LLM is `FakeLlm`) and `render_uniqueness` (skipped when every clip source is `fake`). Direct calls with `skip_when_fake=False` exercise them (seeded negatives).
- Job-directory inputs read by the checks (tolerant of both shapes): `story.json {text | chapters[]}`, `parts.json`, `seo.json` (Shorts-only list) or `seo_long.json` + `seo_shorts.json`, `shorts_plan.json`, `long/master.mp4`, `shorts/s<k>/short.mp4`, `long/chapters.json` (`midroll_suggestions_s`), `clip_usage` rows.

## File structure

```
ns_studio/policy/base.py          Check ABC, CODES, POLICY_CODES, ok()/fail()
ns_studio/policy/meta.py          job-file readers, policy lists, unsafe categories
ns_studio/policy/judge.py         JudgeFn, default_judge (C5 prompt via importlib), available()
ns_studio/policy/text_checks.py   unsafe_topics, real_entities (spaCy or regex), misleading_claims, hook_title_honesty, self_similarity, metadata_spam, standalone_short
ns_studio/policy/phash.py         FFmpeg + numpy pHash (10 frames), distance, encode/decode
ns_studio/policy/final_checks.py  render_uniqueness, long_runtime, clip_reuse, audio_only_voice, disclosure_advice
ns_studio/policy/gate.py          run_gate, PolicyTextStage, PolicyFinalStage, install_gates
ns_studio/policy/regen.py         run_job_guarded: reject, restart from the earliest affected stage once
ns_studio/learn/importer.py       CSV column mapping, URL then exact-title matching, analytics rows
ns_studio/learn/reweight.py       viewed % x avg % viewed (Shorts), avg duration / runtime (masters), smoothing, 5 % floor
ns_studio/learn/status.py         performers, rejections, incidents, week-8 milestone
ns_studio/channels/cadence.py     §18.6 governor, quality drop, jobs due today, interleave + jitter
ns_studio/review.py               pending/show/approve/reject, mark-uploaded, pause/resume
ns_studio/daily.py                run_daily: plan, run, publish times, backup, nightly report
```

---

### Task 8: Policy gate framework and text checks (`opus`)

**Interfaces:**
```python
class Check(ABC): name: str; gate: Literal["text","final"]; code -> str | None; def run(self, ctx) -> PolicyCheck
CODES = {"unsafe_topics": "NS-E601", "real_entities": "NS-E602", "misleading_claims": "NS-E603",
         "hook_title_honesty": "NS-E604", "self_similarity": "NS-E605", "metadata_spam": "NS-E606",
         "standalone_short": "NS-E607", "render_uniqueness": "NS-E611", "long_runtime": "NS-E612",
         "clip_reuse": "NS-E613", "audio_only_voice": "NS-E614"}
def run_gate(ctx, gate, checks) -> PolicyResult   # runs ALL checks, writes policy_<gate>.json, raises NsError(first failing code)
```
Rules: `real_entities` matches the `real_entities_block.txt` phrases anywhere in story and metadata, and flags a multi-token PERSON not in the allow-list; spaCy (`PERSON/ORG/GPE/EVENT`) is used when importable, else a regex fallback treats capitalised multi-word sequences starting with a known first name as PERSON, so CI without spaCy stays deterministic. `misleading_claims` uses word-boundary matches of `misleading_claims.txt` over title, description, tags, hashtags and hook of every item. `metadata_spam`: more than 20 tags, over 500 tag characters, duplicate tags, more than 5 hashtags, `#Shorts` on a master, then the judge's tag-relevance verdict. `self_similarity`: cosine over `stories.embedding`, > 0.85 same channel, > 0.80 other channel.

- [x] **Step 1:** tests `tests/test_policy_text.py` (every check pass/fail, stub judges, injected NER, DB-seeded similar story, fake-LLM skip).
- [x] **Step 2:** implement `base.py`, `meta.py`, `judge.py`, `text_checks.py`, `gate.py`, `install_gates` with fallbacks.
- [x] **Step 3:** Run (commit, push, CI); `git commit -m "feat(c7): policy gate framework and text checks"`.

### Task 9: Final checks, pHash and regenerate-once (`opus`)

`phash.py`: FFmpeg writes 10 mid-slice frames as 32x32 gray raw bytes (`-ss d/20`, `fps=10/d`), numpy DCT-II, top-left 8x8 against the median without the DC term, 64-bit ints; `distance` is the mean Hamming distance over aligned frames; threshold 8 bits. `render_uniqueness` compares each render with the other renders of the same job (a Short must differ from its master), with `phash_index.jsonl` rows of earlier jobs and with `outputs.phash`; it records its hashes only when it passes. `long_runtime`: master >= 510 s and each `midroll_suggestions_s` between 120 s and `duration - 120 s`. `clip_reuse`: no clip twice in one `(job, item)`, 14 days per channel, 30 days across channels, same-job reuse across items allowed. `audio_only_voice`: exactly one audio stream and the quietest 2 % of 50 ms frames below -55 dBFS (real silence between sentences; a music bed has no silence). `disclosure_advice` (spec 18.7): `yes` when a known real place or a "really happened" claim appears; age restriction suggested when the audience is `mature` and the text gate noted medium or high intensity.

`regen.py`: on a policy code, copy the text artifacts and move `long/` and `shorts/` to `rejected\<slug>\<date>\<job>\attempt<N>` with `reason.json`, drop the stage state from the earliest affected stage (`REGEN_FROM`), run once more; a second policy failure re-raises. The attempts file `policy_attempts.json` survives `--resume`.

- [x] **Step 1:** tests `tests/test_policy_final.py` (real FFmpeg clips: identical vs different content, pause audio vs tone bed vs two audio streams) and `tests/test_policy_gate.py` (gate result files, install positions, regenerate once, second failure, pass-through of non-policy errors, media moved).
- [x] **Step 2:** implement.
- [x] **Step 3:** Run; `git commit -m "feat(c7): final policy checks, ffmpeg pHash and regenerate-once"`.

### Task 10: Learning loop and status (`sonnet`)

`importer.py` aliases (normalised header text) for the YouTube Studio columns, `--map field=column` overrides, `parse_duration` for `m:ss`, URL or video id first, unique exact title second, unmatched rows listed, rows stored per `(output_id, date)`. `reweight.py` as in the module docstring: relative weights `(1 - 0.3) * old + 0.3 * option_mean / overall_mean`, then `apply_floor` (5 %), stored as probabilities in `pool_weights`; a snapshot of what changed goes to `data/learn_last.json`. `status.py`: top/bottom 3 performers, last rejection reasons, last incidents, last import changes, and `milestone()` per spec 18.12 (go on Shorts 7-day viewed rate >= 55 % with subscribers growing week over week, or masters' average view >= 3:00 with a 12-month pace >= 4,000 hours; `too-early` before week 8; `no-data` without analytics).

- [x] **Step 1:** tests `tests/test_learn.py`. **Step 2:** implement. **Step 3:** Run; `git commit -m "feat(c7): analytics import, pool reweighting and status"`.

### Task 11: Cadence governor, review, pause/resume, daily run (`sonnet`)

`cadence.plan_channel` implements the §18.6 table (weeks 1-2: 1 master/week and 1 Short/day; weeks 3-6: 2 masters, 2 Shorts/day only when the 14-day viewed rate is >= 60 %; later the channel's own limits; never above 3/3) and the quality drop (7-day viewed rate < 50 % or at least 40 % of the last 10 reviewed outputs rejected, with at least 5 reviewed): 1 Short/day and `critic_bar` + 0.5 once per 7 days via `apply_quality_drop` (an incident of kind `warning`). `plan_daily` skips paused channels. `assign_publish_times` puts masters first, interleaves Shorts across stories and jitters each slot by +/-45 minutes inside `publish_window`. `review.py`: `approve`, `reject` (reason required, files moved to `rejected\`), `mark_uploaded` (approved only, YouTube URL only), `pause`/`resume` with incidents. `daily.run_daily`: runs the planned jobs through `run_job_guarded`, isolates failures, rewrites `publish_after` in existing sheets, `Db.backup`, `logs/nightly-<date>.json`, `runs` row.

CLI: `run [--daily] [--policy/--no-policy]`, `review [--channel] | show | approve | reject`, `mark-uploaded`, `pause`, `resume [--yes]`, `status [--channel] [--milestone]`, `learn --import <csv> [--date] [--map f=c]`.

- [x] **Step 1:** tests `tests/test_cadence.py`, `tests/test_review.py`, extended `tests/test_cli.py`. **Step 2:** implement. **Step 3:** Run; `git commit -m "feat(c7): cadence governor, review, pause/resume, daily run"`.
