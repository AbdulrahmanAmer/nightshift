"""A regenerated job must not reproduce what the policy gate rejected (review row 10)."""

import json
from pathlib import Path

from ns_common.tts_client import TtsItem
from ns_studio.media.clips import blocked_for_job, clip_seed
from ns_studio.media.voice import synthesize
from ns_studio.policy.regen import invalidate_from
from ns_studio.stages import StageState
from ns_studio.text.prompts import attempt_temperature, call_json
from ns_studio.text.stages_shorts import ideas_budget
from tests.helpers import make_ctx


def _attempt(ctx, n: int) -> None:
    ctx.write_json("policy_attempts.json", {"regenerations": n, "history": []})


def test_attempt_counts_regenerations(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    assert ctx.attempt == 0
    _attempt(ctx, 1)
    assert ctx.attempt == 1


def test_prompts_run_hotter_on_a_retry(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    llm = ctx.services.llm
    call_json(ctx, "ideas", n=8, budget=ideas_budget(ctx), avoid_premises=[])
    _attempt(ctx, 1)
    call_json(ctx, "ideas", n=8, budget=ideas_budget(ctx), avoid_premises=[])
    first, second = (c["temperature"] for c in llm.calls)  # type: ignore[attr-defined]
    assert first == 0.8 and second == attempt_temperature(1) > first
    assert attempt_temperature(9) == 1.1


def test_invalidate_deletes_the_memo_files_that_made_the_stage_a_no_op(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.write_json("hooks_all.json", [["h"]])
    ctx.write_json("audio/manifest.json", {})
    state = StageState()
    state.mark_done("hooks", "x")
    state.mark_done("voice", "y")
    state.save(ctx.job_dir)
    invalidate_from(ctx, ["ideas", "hooks", "seo", "voice"], "hooks")
    assert not ctx.file("hooks_all.json").exists() and not ctx.file("audio/manifest.json").exists()
    assert StageState.load(ctx.job_dir).entries == {}


def test_hooks_stage_reruns_after_invalidation(tmp_home: Path):
    from ns_studio.text.stages_shorts import HooksStage

    ctx = make_ctx(tmp_home)
    ctx.write_json("parts.json", [{"n": 1, "text": "The porch light flickered. Then it went out."}])
    stage = HooksStage()
    stage.run(ctx)
    calls = len(ctx.services.llm.calls)  # type: ignore[attr-defined]
    stage.run(ctx)
    assert len(ctx.services.llm.calls) == calls  # memo: nothing new
    invalidate_from(ctx, ["hooks"], "hooks")
    stage.run(ctx)
    assert len(ctx.services.llm.calls) > calls  # the retry really asks again


def test_clip_seed_changes_with_the_attempt(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    base = clip_seed(ctx, "s1")
    assert base == clip_seed(ctx, "s1")
    _attempt(ctx, 1)
    assert clip_seed(ctx, "s1") != base


def test_rejected_clips_are_blocked_on_the_retry(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.write_json("clips/shots.json", {"s1": {"shots": [{"source": "pexels", "clip_id": "42"}]}})
    assert blocked_for_job(ctx, ("clips/shots.json",)) == set()
    _attempt(ctx, 1)
    assert ("pexels", "42") in blocked_for_job(ctx, ("clips/shots.json",))


def test_voice_is_resynthesized_with_another_seed_on_a_retry(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    seeds: list[int | None] = []
    real = ctx.services.tts.speak_batch

    def spy(items, out_dir, *, seed=None, timeout_s=None):
        seeds.append(seed)
        return real(items, out_dir, seed=seed, timeout_s=timeout_s)

    ctx.services.tts.speak_batch = spy  # type: ignore[method-assign]
    items = [TtsItem("p1", "One two three four.", "Ryan", 1.0)]
    synthesize(ctx, items)
    ctx.file("audio/manifest.json").write_text(json.dumps({}), encoding="utf-8")
    _attempt(ctx, 1)
    synthesize(ctx, items)
    assert len(seeds) == 2 and seeds[0] != seeds[1]
