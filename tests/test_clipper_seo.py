"""C8 Clipper: clip SEO normalisation and metadata-only policy checks."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ns_common.errors import NsError
from ns_common.llm_client import FakeLlm
from ns_common.models import ClipPlanItem, Seo
from ns_studio.clipper import seo as sp
from tests.helpers import make_ctx

CLIP = ClipPlanItem(
    k=1, start_s=1.0, end_s=31.0, title="The mountain route in one take", why="Clear promise and payoff.",
    score=0.9, keywords=["route", "mountain"],
)
GOOD: dict[str, Any] = {
    "title": "The mountain level route explained fast",
    "description": "A quick walkthrough of the safest route.\nIt shows the ledge trick.\nA third line.",
    "tags": [f"tag number {i}" for i in range(30)],
    "hashtags": ["#Shorts", "#gaming", "walkthrough", "#extra one", "#a", "#b", "#c"],
    "keywords": ["mountain level", "route"],
}


def seo_of(**kw: Any) -> Seo:
    base: dict[str, Any] = {
        "title": "A fair title", "description": "Plain words.\n\nClip from: x", "tags": ["a", "b"],
        "hashtags": ["#Shorts"], "keywords": [],
    }
    return Seo(**(base | kw))


# ---------- normalisation ----------
def test_finalize_title_description_hashtags_tags() -> None:
    s = sp.finalize_seo(GOOD, clip=CLIP, source_title="vod_2026", seed_keywords=["gaming"])
    assert len(s.title) <= 70 and "mountain level" in s.title.lower()[:40]
    lines = s.description.split("\n")
    assert lines[0].startswith("A quick walkthrough") and lines[1].startswith("It shows")
    assert lines[-1] == "Clip from: vod_2026" and "A third line." not in s.description
    assert s.hashtags[0] == "#Shorts" and 3 <= len(s.hashtags) <= 5
    assert all(h.startswith("#") and " " not in h for h in s.hashtags)
    assert 10 <= len(s.tags) <= 20 and sum(len(t) + 1 for t in s.tags) <= 500


def test_long_title_is_cut_at_a_word_boundary() -> None:
    raw = dict(GOOD, title="word " * 30)
    s = sp.finalize_seo(raw, clip=CLIP, source_title="x", seed_keywords=[])
    assert len(s.title) <= 70 and not s.title.endswith(" ")


def test_missing_title_and_description_fall_back_to_the_clip() -> None:
    s = sp.finalize_seo({"tags": ["x"], "hashtags": []}, clip=CLIP, source_title="x", seed_keywords=[])
    assert "mountain" in s.title.lower() and "Clear promise" in s.description
    assert s.hashtags[0] == "#Shorts" and len(s.hashtags) >= 3


def test_tags_are_padded_when_the_model_returns_few() -> None:
    raw = dict(GOOD, tags=["one"])
    s = sp.finalize_seo(raw, clip=CLIP, source_title="x", seed_keywords=["alpha", "beta"])
    assert "one" in s.tags and len(s.tags) >= 5


def test_tag_budget_is_500_characters() -> None:
    long_tags = [f"{'x' * 29}{i}" for i in range(40)]
    s = sp.finalize_seo(dict(GOOD, tags=long_tags), clip=CLIP, source_title="x", seed_keywords=[])
    assert sum(len(t) + 1 for t in s.tags) <= 500 and len(s.tags) <= 20


# ---------- checks ----------
def test_clean_metadata_passes() -> None:
    s = sp.finalize_seo(GOOD, clip=CLIP, source_title="vod", seed_keywords=[])
    res = sp.check_metadata(s)
    assert res.passed and res.stage == "text" and set(res.checks) == {
        "misleading_claims", "metadata_spam", "real_entities"}


def test_misleading_claim_fails() -> None:
    res = sp.check_misleading_claims(seo_of(title="This is 100% REAL, not clickbait"))
    assert not res.passed and "100% real" in res.detail
    # the fiction list does not apply to the owner's own footage
    assert sp.check_misleading_claims(seo_of(title="Caught on camera: my true story")).passed


def test_real_entities_only_flags_links_and_handles() -> None:
    assert not sp.check_real_entities(seo_of(description="follow @someone\n\nClip from: x")).passed
    assert not sp.check_real_entities(seo_of(description="see https://example.com")).passed
    # the owner's guests and games may be named; the source line is never checked
    assert sp.check_real_entities(seo_of(description="Elon Musk plays it\n\nClip from: x")).passed
    assert sp.check_real_entities(seo_of(title="Dr. Smith reacts")).passed
    assert sp.check_real_entities(seo_of(title="Mountain Level Route")).passed


def test_a_source_title_with_honorifics_brands_and_handles_never_blocks_the_clip() -> None:
    """Review clipper row 6: "Interview with Dr. Smith" / "Podcast #12 @mychannel" failed every attempt."""
    for source in ("Interview with Dr. Smith", "My Tesla review", "Podcast #12 @mychannel https://x.tv"):
        s = sp.finalize_seo(GOOD, clip=CLIP, source_title=source, seed_keywords=[])
        res = sp.check_metadata(s)
        assert res.passed, (source, res.model_dump())
        assert f"Clip from: {source}" in s.description


def test_creator_note_follows_the_source_line_and_is_not_checked() -> None:
    note = "More at https://me.tv"
    s = sp.finalize_seo(GOOD, clip=CLIP, source_title="vod", seed_keywords=[], creator_note=note)
    assert s.description.splitlines()[-2:] == ["Clip from: vod", "More at https://me.tv"]
    assert sp.check_metadata(s).passed


def test_metadata_spam_rules() -> None:
    assert not sp.check_metadata_spam(seo_of(tags=[f"{'x' * 29}{i}" for i in range(20)])).passed
    assert not sp.check_metadata_spam(seo_of(title="THIS IS ALL CAPITAL LETTERS")).passed
    assert not sp.check_metadata_spam(seo_of(description="route " * 8)).passed
    assert sp.check_metadata_spam(seo_of()).passed


# ---------- generation ----------
def with_llm(ctx: Any, tmp_path: Path, answers: list[dict[str, Any]]) -> FakeLlm:
    llm = FakeLlm(tmp_path, overrides={"clip_seo": answers})
    ctx.services.llm = llm
    return llm


def test_generate_seo_uses_the_llm_once_when_clean(tmp_home: Path, tmp_path: Path) -> None:
    ctx = make_ctx(tmp_home, fmt="clip")
    llm = with_llm(ctx, tmp_path, [GOOD])
    s, policy = sp.generate_seo(ctx, CLIP, excerpt="text", source_title="vod")
    assert policy.passed and s.hashtags[0] == "#Shorts"
    assert [c["prompt_name"] for c in llm.calls] == ["clip_seo"]


def test_generate_seo_retries_once_then_passes(tmp_home: Path, tmp_path: Path) -> None:
    ctx = make_ctx(tmp_home, fmt="clip")
    llm = with_llm(ctx, tmp_path, [dict(GOOD, title="Leaked footage from the mountain"), GOOD])
    s, policy = sp.generate_seo(ctx, CLIP, excerpt="text", source_title="vod")
    assert policy.passed and "leaked" not in s.title.lower() and len(llm.calls) == 2


def test_generate_seo_fails_with_the_check_code(tmp_home: Path, tmp_path: Path) -> None:
    ctx = make_ctx(tmp_home, fmt="clip")
    with_llm(ctx, tmp_path, [dict(GOOD, title="Banned video: the route")])
    with pytest.raises(NsError) as ei:
        sp.generate_seo(ctx, CLIP, excerpt="text", source_title="vod")
    assert ei.value.code == "NS-E603"
