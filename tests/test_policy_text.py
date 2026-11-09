from pathlib import Path

import pytest

from ns_common.embed_client import to_blob
from ns_common.errors import NsError
from ns_studio.errors import hint_of
from ns_studio.policy import text_checks as tc
from ns_studio.policy.base import CODES, POLICY_CODES
from ns_studio.policy.gate import run_gate
from ns_studio.policy.meta import collect_items, policy_list, short_moments, story_text
from tests.helpers import add_output, make_ctx

STORY = (
    "The porch light flickered twice before the knocking started again. "
    "Maya counted the knocks and found the fourth one was missing."
)


@pytest.fixture(autouse=True)
def _no_spacy(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(tc._NLP_CACHE, "nlp", None)


def _seo(title: str = "The Porch Light", tags: list[str] | None = None, **kw):
    base = {
        "title": title,
        "description": "A short scare.\nOriginal fiction.",
        "tags": tags if tags is not None else ["horror story", "scary story"],
        "hashtags": ["#Shorts", "#horror"],
    }
    return base | kw


def _job(ctx, seo=None, hook="Someone keeps knocking."):
    ctx.write_json("story.json", {"text": STORY})
    ctx.write_json("parts.json", [{"n": 1, "text": STORY, "hook": hook, "word_count": 20, "est_seconds": 9}])
    ctx.write_json("seo.json", [seo or _seo()])


def stub_judge(answer=None, calls=None):
    def judge(ctx, mode, **vars):
        if calls is not None:
            calls.append((mode, vars))
        return answer or {"verdict": "pass", "reason": "", "flags": [], "intensity": "low"}

    return judge


def test_codes_cover_spec_table():
    assert set(CODES.values()) == POLICY_CODES
    assert {"NS-E601", "NS-E602", "NS-E603", "NS-E604", "NS-E605", "NS-E606", "NS-E607"} <= POLICY_CODES
    assert {"NS-E611", "NS-E612", "NS-E613", "NS-E614"} <= POLICY_CODES


def test_meta_readers(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _job(ctx)
    items = collect_items(ctx)
    assert [i.name for i in items] == ["s1"] and items[0].hook == "Someone keeps knocking."
    assert story_text(ctx) == STORY
    ctx.write_json("shorts_plan.json", {"shorts": [{"sentences": ["a b", "c d"], "hook": "h"}]})
    assert short_moments(ctx) == [{"k": 1, "text": "a b c d", "hook": "h"}]
    ctx.write_json("seo_long.json", _seo("Long Title"))
    assert [i.name for i in collect_items(ctx)] == ["long", "s1"]
    assert "true story" in policy_list("misleading_claims.txt")


def test_misleading_claims(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _job(ctx)
    assert tc.MisleadingClaims().run(ctx).passed
    _job(ctx, _seo("My TRUE STORY of the porch"))
    res = tc.MisleadingClaims().run(ctx)
    assert not res.passed and "true story" in res.detail
    _job(ctx, _seo(tags=["horror", "real footage"]))
    assert not tc.MisleadingClaims().run(ctx).passed
    _job(ctx, hook="Caught on camera tonight")
    assert not tc.MisleadingClaims().run(ctx).passed


def test_real_entities_blocklist_and_fallback_ner(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _job(ctx)
    assert tc.RealEntities().run(ctx).passed
    ctx.write_json("story.json", {"text": STORY + " Then Taylor Swift called."})
    res = tc.RealEntities().run(ctx)
    assert not res.passed and "taylor swift" in res.detail
    ctx.write_json("story.json", {"text": STORY + " Sarah Miller opened the door."})
    res2 = tc.RealEntities().run(ctx)
    assert not res2.passed and "Sarah Miller" in res2.detail
    ctx.write_json("story.json", {"text": STORY + " Sarah opened the door on Maple Street."})
    assert tc.RealEntities().run(ctx).passed


def test_real_entities_with_injected_ner(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _job(ctx)
    assert not tc.RealEntities(ner=lambda t: [("John Smith", "PERSON")]).run(ctx).passed
    assert tc.RealEntities(ner=lambda t: [("Maple Street", "GPE"), ("Sarah", "PERSON")]).run(ctx).passed
    block = policy_list("real_entities_block.txt")[0]
    res = tc.RealEntities(ner=lambda t: [(block.title(), "ORG")]).run(ctx)
    assert not res.passed and block in res.detail.lower()


def test_regex_entities_only_flags_known_first_name_sequences():
    names = frozenset({"sarah"})
    assert tc.regex_entities("Sarah Miller met Dark Hallway.", names) == [("Sarah Miller", "PERSON")]
    assert tc.regex_entities("Maple Street and Dark Hallway", names) == []


def test_unsafe_topics_flags_and_notes(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _job(ctx)
    calls: list = []
    res = tc.UnsafeTopics(stub_judge({"verdict": "pass", "flags": [], "intensity": "high"}, calls)).run(ctx)
    assert res.passed and calls[0][0] == "unsafe_topics" and calls[0][1]["categories"]
    assert ctx.read_json("policy_notes.json")["intensity"] == "high"
    bad = {"verdict": "fail", "flags": [{"category": "minors", "reason": "x"}], "reason": "no"}
    res2 = tc.UnsafeTopics(stub_judge(bad)).run(ctx)
    assert not res2.passed and "minors" in res2.detail


def test_hook_title_honesty(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    assert tc.HookTitleHonesty(stub_judge()).run(ctx).passed  # nothing to judge yet
    _job(ctx)
    calls: list = []
    assert tc.HookTitleHonesty(stub_judge(calls=calls)).run(ctx).passed
    expected = [{"item": "s1", "title": "The Porch Light", "hook": "Someone keeps knocking."}]
    assert calls[0][1]["items"] == expected
    bait = {"verdict": "fail", "failed_items": ["s1"], "reason": "bait"}
    res = tc.HookTitleHonesty(stub_judge(bait)).run(ctx)
    assert not res.passed and "s1" in res.detail


def test_standalone_short(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    assert tc.StandaloneShort(stub_judge()).run(ctx).passed
    ctx.write_json("shorts_plan.json", [{"text": "A moment.", "hook": "h"}])
    assert tc.StandaloneShort(stub_judge()).run(ctx).passed
    res = tc.StandaloneShort(stub_judge({"verdict": "fail", "failed_items": ["1"]})).run(ctx)
    assert not res.passed and "1" in res.detail


def test_metadata_spam_limits_and_judge(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _job(ctx)
    assert tc.MetadataSpam(stub_judge()).run(ctx).passed
    _job(ctx, _seo(tags=[f"tag{i}" for i in range(22)]))
    assert "22 tags" in tc.MetadataSpam(stub_judge()).run(ctx).detail
    _job(ctx, _seo(tags=["x" * 30] * 20))
    assert "500" in tc.MetadataSpam(stub_judge()).run(ctx).detail
    _job(ctx, _seo(tags=["Horror", "horror"]))
    assert "duplicate" in tc.MetadataSpam(stub_judge()).run(ctx).detail
    _job(ctx, _seo(hashtags=["#a"] * 6))
    assert "hashtags" in tc.MetadataSpam(stub_judge()).run(ctx).detail
    _job(ctx)
    ctx.write_json("seo_long.json", _seo())
    assert "#Shorts" in tc.MetadataSpam(stub_judge()).run(ctx).detail
    ctx.file("seo_long.json").unlink()
    res = tc.MetadataSpam(stub_judge({"verdict": "fail", "reason": "celebrity tags"})).run(ctx)
    assert not res.passed and "celebrity" in res.detail


def _insert_story(
    ctx, job_id: str, channel: str, text: str, *, finished: bool = True, status: str = "pending"
) -> None:
    ctx.db.execute(
        "insert or ignore into channels(slug, display_name, created_at) values (?,?,?)",
        (channel, channel, "2026-11-01T00:00:00"),
    )
    vec = ctx.services.embed.embed([text])[0]
    ctx.db.execute(
        "insert into stories(job_id, channel, format, text, word_count, opening_line, embedding, created_at) "
        "values (?,?,?,?,?,?,?,?)",
        (job_id, channel, "shorts_only", text, 10, text[:20], to_blob(vec), "2026-10-01T00:00:00"),
    )
    if finished:  # only a story whose video was made enters the comparison set (D-25)
        add_output(ctx.db, job_id, "long", channel=channel, status=status)


def test_self_similarity(tmp_home: Path):
    ctx = make_ctx(tmp_home, fake={"embed"})  # real (unconnected) LLM client, so the check is active
    _job(ctx)
    assert tc.SelfSimilarity().run(ctx).passed
    _insert_story(ctx, "other-20261001-aaaa", "after-dark", STORY)
    res = tc.SelfSimilarity().run(ctx)
    assert not res.passed and "other-20261001-aaaa" in res.detail
    ctx.db.execute("delete from stories")
    _insert_story(ctx, "x-20261001-bbbb", "other-ch", STORY)
    assert not tc.SelfSimilarity().run(ctx).passed
    ctx.db.execute("delete from stories")
    other = "completely different words about a lighthouse keeper"
    _insert_story(ctx, "y-20261001-cccc", "after-dark", other)
    assert tc.SelfSimilarity().run(ctx).passed


def test_self_similarity_skipped_with_fake_llm(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _job(ctx)
    _insert_story(ctx, "other-20261001-aaaa", "after-dark", STORY)
    assert "skipped" in tc.SelfSimilarity().run(ctx).detail
    assert not tc.SelfSimilarity(skip_when_fake=False).run(ctx).passed


def test_text_checks_order_and_names():
    names = [c.name for c in tc.text_checks()]
    assert names == [
        "unsafe_topics",
        "real_entities",
        "misleading_claims",
        "hook_title_honesty",
        "self_similarity",
        "metadata_spam",
        "standalone_short",
    ]
    assert all(c.gate == "text" and c.code for c in tc.text_checks())


def test_nserror_codes_resolve():
    for code in POLICY_CODES:
        assert NsError(code).definition.message


def test_self_similarity_embeds_only_the_opening_window(tmp_home: Path):
    """Review row 2: a whole master (about 9,000 characters) made the embed server answer HTTP 500."""
    ctx = make_ctx(tmp_home, fake={"embed"})  # FakeEmbed refuses inputs over 512 tokens, like llama-server
    master = " ".join(f"Chapter sentence number {i} walks down the dark corridor." for i in range(200))
    assert len(master) > 9000
    ctx.write_json("story.json", {"chapters": [{"n": 1, "text": master}]})
    _insert_story(ctx, "other-20261001-aaaa", "after-dark", master[:1500])
    res = tc.SelfSimilarity(skip_when_fake=False).run(ctx)  # before the fix: NsError NS-E301
    assert not res.passed and "other-20261001-aaaa" in res.detail


# ------------------------------------------------ D-25: only a finished video is compared with
def _script_ctx(tmp_home: Path):
    """A job made from the owner's own script, its story already imported."""
    ctx = make_ctx(tmp_home, fake={"embed"})
    ctx.write_json(
        "script_source.json", {"kind": "script_import", "owner_script": True, "script": "script.md"}
    )
    assert ctx.owner_script
    _job(ctx)
    return ctx


def test_failed_script_job_does_not_block_the_same_script(tmp_home: Path):
    """The first job stopped at voice: its story row exists, its video does not. The same script passes."""
    ctx = _script_ctx(tmp_home)
    _insert_story(ctx, "engine-test-20261109-576e", "after-dark", STORY, finished=False)
    res = tc.SelfSimilarity(skip_when_fake=False).run(ctx)
    assert res.passed and "compared with 0 stories" in res.detail
    # the effect: the gate raises nothing
    assert run_gate(ctx, "text", [tc.SelfSimilarity(skip_when_fake=False)]).passed


def test_finished_video_with_the_same_script_still_blocks_with_a_script_message(tmp_home: Path):
    ctx = _script_ctx(tmp_home)
    _insert_story(ctx, "engine-test-20261109-576e", "after-dark", STORY)  # finished: has an outputs row
    with pytest.raises(NsError) as ei:
        run_gate(ctx, "text", [tc.SelfSimilarity(skip_when_fake=False)])
    err = ei.value
    assert err.code == "NS-E605"
    assert "engine-test-20261109-576e" in err.detail and "finished video" in err.detail
    hint = hint_of(err)
    assert "new idea" not in hint and "engine-test-20261109-576e" in hint
    assert "change the script" in hint.lower()
    assert "new idea" in err.definition.hint  # the generic text in errors.json is untouched


def test_idea_job_keeps_the_idea_hint(tmp_home: Path):
    ctx = make_ctx(tmp_home, fake={"embed"})  # no script_source.json: the story was drawn from an idea
    assert not ctx.owner_script
    _job(ctx)
    _insert_story(ctx, "other-20261001-aaaa", "after-dark", STORY)
    with pytest.raises(NsError) as ei:
        run_gate(ctx, "text", [tc.SelfSimilarity(skip_when_fake=False)])
    assert ei.value.code == "NS-E605" and "finished video" not in ei.value.detail
    assert hint_of(ei.value) == ei.value.definition.hint
    assert "a new idea will be drawn" in hint_of(ei.value)


def test_a_failed_idea_story_is_not_compared_either(tmp_home: Path):
    ctx = make_ctx(tmp_home, fake={"embed"})
    _job(ctx)
    _insert_story(ctx, "other-20261001-aaaa", "after-dark", STORY, finished=False)
    assert tc.SelfSimilarity(skip_when_fake=False).run(ctx).passed


def test_story_that_fails_after_it_was_recorded_leaves_the_comparison_set(tmp_home: Path):
    """The ledger is read from outputs: a story whose video is gone again is ignored."""
    ctx = _script_ctx(tmp_home)
    _insert_story(ctx, "engine-test-20261109-576e", "after-dark", STORY)
    assert not tc.SelfSimilarity(skip_when_fake=False).run(ctx).passed
    ctx.db.execute("delete from outputs where job_id=?", ("engine-test-20261109-576e",))
    assert tc.SelfSimilarity(skip_when_fake=False).run(ctx).passed


def test_cli_prints_the_script_hint_not_the_idea_hint(capsys: pytest.CaptureFixture[str]):
    import typer

    from ns_studio.cli import guarded

    err = NsError("NS-E605", "self_similarity: x", hint="Change the script, or use that video.")
    with pytest.raises(typer.Exit), guarded():
        raise err
    out = capsys.readouterr().err
    assert "hint: Change the script, or use that video." in out and "new idea" not in out


# ------------------------------------------------ D-25 review: a resumed job meets the ledger again
def _text_stage():
    from ns_studio.policy.gate import PolicyTextStage

    return PolicyTextStage([tc.SelfSimilarity(skip_when_fake=False)])


def test_job_resumed_after_the_same_script_finished_is_blocked_not_finished(tmp_home: Path):
    """A fails after policy_text, B (same script) finishes, A is resumed: A must not make a second copy."""
    from ns_studio.stages import StageState, inputs_hash, run_stage

    ctx = _script_ctx(tmp_home)  # job A
    _insert_story(ctx, ctx.job_id, "after-dark", STORY, finished=False)  # A's story row, no video
    stage = _text_stage()
    assert run_stage(ctx, stage) is True  # A passes the text gate and goes on to voice, where it fails
    assert StageState.load(ctx.job_dir).is_done("policy_text", inputs_hash(ctx, stage))
    _insert_story(ctx, "engine-test-20261109-bbbb", "after-dark", STORY)  # B finished: it has an outputs row
    with pytest.raises(NsError) as ei:  # A is resumed: policy_text would be skipped
        run_stage(ctx, stage)
    err = ei.value
    assert err.code == "NS-E605" and "engine-test-20261109-bbbb" in err.detail
    assert "change the script" in hint_of(err).lower() and "new idea" not in hint_of(err)


def test_resumed_job_is_not_blocked_while_the_other_copy_never_finished(tmp_home: Path):
    from ns_studio.stages import run_stage

    ctx = _script_ctx(tmp_home)
    _insert_story(ctx, ctx.job_id, "after-dark", STORY, finished=False)
    stage = _text_stage()
    assert run_stage(ctx, stage) is True
    _insert_story(ctx, "engine-test-20261109-bbbb", "after-dark", STORY, finished=False)
    assert run_stage(ctx, stage) is False  # skipped as before: nothing finished, nothing to repeat


def test_resumed_idea_job_is_blocked_with_the_idea_hint(tmp_home: Path):
    from ns_studio.stages import run_stage

    ctx = make_ctx(tmp_home, fake={"embed"})
    _job(ctx)
    _insert_story(ctx, ctx.job_id, "after-dark", STORY, finished=False)
    stage = _text_stage()
    assert run_stage(ctx, stage) is True
    _insert_story(ctx, "other-20261001-aaaa", "after-dark", STORY)
    with pytest.raises(NsError) as ei:
        run_stage(ctx, stage)
    assert ei.value.code == "NS-E605" and "a new idea will be drawn" in hint_of(ei.value)


def test_final_gate_refuses_a_second_copy_of_a_finished_story(tmp_home: Path):
    """The last stop before the outbox: B finished while A was rendering (policy_final runs after render)."""
    from ns_studio.policy.gate import PolicyFinalStage

    ctx = _script_ctx(tmp_home)
    _insert_story(ctx, ctx.job_id, "after-dark", STORY, finished=False)
    _insert_story(ctx, "engine-test-20261109-bbbb", "after-dark", STORY)
    with pytest.raises(NsError) as ei:
        PolicyFinalStage([]).run(ctx)
    assert ei.value.code == "NS-E605" and "change the script" in hint_of(ei.value).lower()
    assert ctx.db.query("select 1 from outputs where job_id=?", (ctx.job_id,)) == []


def test_final_gate_meets_the_ledger_again_when_a_resume_skips_it(tmp_home: Path):
    from ns_studio.policy.gate import PolicyFinalStage
    from ns_studio.stages import StageState, inputs_hash, run_stage

    ctx = _script_ctx(tmp_home)
    ctx.file("long").mkdir()
    ctx.file("long/master.mp4").write_bytes(b"x")
    _insert_story(ctx, ctx.job_id, "after-dark", STORY, finished=False)
    stage = PolicyFinalStage([])
    ctx.write_json("policy_text.json", {})
    assert run_stage(ctx, stage) is True
    assert StageState.load(ctx.job_dir).is_done("policy_final", inputs_hash(ctx, stage))
    assert run_stage(ctx, stage) is False  # nothing finished meanwhile: skipped as before
    _insert_story(ctx, "engine-test-20261109-bbbb", "after-dark", STORY)
    with pytest.raises(NsError) as ei:
        run_stage(ctx, stage)
    assert ei.value.code == "NS-E605"


def test_outbox_does_not_read_the_ledger_so_no_policy_runs_can_finish(tmp_home: Path):
    """`studio run --no-policy` installs no gate stage: the outbox must not raise NS-E605 on its own."""
    from ns_studio.media.outbox import OutboxStage

    ctx = _script_ctx(tmp_home)
    _insert_story(ctx, ctx.job_id, "after-dark", STORY, finished=False)
    _insert_story(ctx, "engine-test-20261109-bbbb", "after-dark", STORY)  # a similar finished story
    try:
        OutboxStage().run(ctx)
    except Exception as e:  # no render report in this fixture: any failure but the repeat refusal is fine
        assert getattr(e, "code", None) != "NS-E605"


def test_a_rejected_video_does_not_block_the_same_story(tmp_home: Path):
    """Reject-and-redo: review.reject moved the files out of the outbox, so the story may be made again."""
    ctx = _script_ctx(tmp_home)
    _insert_story(ctx, "engine-test-20261109-576e", "after-dark", STORY, status="rejected")
    res = tc.SelfSimilarity(skip_when_fake=False).run(ctx)
    assert res.passed and "compared with 0 stories" in res.detail
    assert run_gate(ctx, "text", [tc.SelfSimilarity(skip_when_fake=False)]).passed


def test_a_story_with_one_rejected_and_one_kept_video_still_blocks(tmp_home: Path):
    ctx = _script_ctx(tmp_home)
    _insert_story(ctx, "engine-test-20261109-576e", "after-dark", STORY, status="rejected")
    add_output(ctx.db, "engine-test-20261109-576e", "s1", channel="after-dark", status="approved")
    assert not tc.SelfSimilarity(skip_when_fake=False).run(ctx).passed


def test_a_resumed_job_is_not_blocked_by_a_rejected_copy(tmp_home: Path):
    from ns_studio.stages import run_stage

    ctx = _script_ctx(tmp_home)
    _insert_story(ctx, ctx.job_id, "after-dark", STORY, finished=False)
    stage = _text_stage()
    assert run_stage(ctx, stage) is True
    _insert_story(ctx, "engine-test-20261109-bbbb", "after-dark", STORY, status="rejected")
    assert run_stage(ctx, stage) is False
