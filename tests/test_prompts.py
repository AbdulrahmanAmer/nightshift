import json
from pathlib import Path

import pytest
from jsonschema import validate

from ns_common.llm_client import FakeLlm
from ns_studio.text import prompts as P
from ns_studio.text.budget import chapter_budget
from tests.helpers import make_channel, make_combo, make_ctx

SHORTS_PROMPTS = [
    "ideas",
    "story_plan",
    "story_draft",
    "critic",
    "revise",
    "split",
    "hook",
    "judge",
    "seo",
    "visual_plan",
]

MASTER_PROMPTS = [
    "long_plan",
    "chapter_draft",
    "continuity",
    "expand",
    "long_seo",
    "shorts_select",
    "short_hook",
    "policy_check",
    "clip_select",
    "clip_seo",
]
ALL_PROMPTS = SHORTS_PROMPTS + MASTER_PROMPTS

# fixture stem without a trailing _<n> -> schema name, for fixtures that are not named after a prompt
FIXTURE_SCHEMA_OVERRIDES: dict[str, str] = {"seo_short": "seo_titles"}


def test_all_prompts_load_with_frontmatter():
    for n in ALL_PROMPTS:
        p = P.load_prompt(n)
        assert p.version >= 1 and isinstance(p.thinking, bool), n
        assert p.system.strip() and p.user_template, n
        assert p.schema is None or p.schema in P.PROMPT_SCHEMAS, n


def test_thinking_flags_match_spec():
    flags = {n: P.load_prompt(n).thinking for n in SHORTS_PROMPTS}
    assert flags == {
        "ideas": False,
        "story_plan": True,
        "story_draft": False,
        "critic": True,
        "revise": False,
        "split": False,
        "hook": False,
        "judge": True,
        "seo": False,
        "visual_plan": False,
    }


def test_text_prompts_have_no_schema():
    assert P.load_prompt("story_draft").schema is None
    assert P.load_prompt("revise").schema is None


def test_render_includes_channel_rules_and_budget():
    p = P.load_prompt("ideas")
    out = P.render(
        p,
        channel=make_channel(),
        combo=make_combo(),
        exemplars=[],
        policy_rules=P.policy_rules_text(),
        budget={"words": 117},
        n=8,
        avoid_premises=["An earlier premise about a lighthouse."],
    )
    assert "horror fiction" in out
    assert "about 117 words" in out
    assert "Real people, brands or events as fact" in out
    assert "An earlier premise about a lighthouse." in out
    assert "Give 8 distinct premises" in out


def test_render_rejects_missing_variables():
    import jinja2

    with pytest.raises(jinja2.UndefinedError):
        P.render(P.load_prompt("judge"), purpose="p", constraints="c")


def test_policy_rules_text_lists_every_category_and_claims():
    text = P.policy_rules_text()
    for c in P.unsafe_categories():
        assert c["label"] in text
    assert "true story" in text


def test_clip_select_prompt_asks_for_one_to_three_emphasis_words():
    p = P.load_prompt("clip_select")
    text = p.system + (P.PROMPTS_DIR / "clip_select.md").read_text(encoding="utf-8")
    assert "emphasis" in text and "1 to 3" in text
    assert "appear" in text and "transcript" in text  # words must be taken from the clip's own words
    assert p.version >= 3


def test_clip_select_prompt_asks_for_cleaned_caption_text_and_the_schema_allows_it():
    p = P.load_prompt("clip_select")
    text = (P.PROMPTS_DIR / "clip_select.md").read_text(encoding="utf-8")
    assert "caption_text" in text and "casing and punctuation" in text and "Do not add" in text
    assert p.version >= 4
    clip = P.PROMPT_SCHEMAS["clip_plan"]["properties"]["clips"]["items"]
    assert clip["properties"]["caption_text"] == {"type": "string"} and "emphasis" in clip["properties"]


def test_missing_frontmatter_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "bad.md").write_text("no front matter\n---USER---\nx\n", encoding="utf-8")
    (tmp_path / "half.md").write_text("---\nversion: 1\n---\nsys\n---USER---\nx\n", encoding="utf-8")
    monkeypatch.setattr(P, "PROMPTS_DIR", tmp_path)
    P.clear_prompt_cache()
    try:
        with pytest.raises(ValueError, match="missing front-matter"):
            P.load_prompt("bad")
        with pytest.raises(ValueError, match="needs version, thinking, schema"):
            P.load_prompt("half")
    finally:
        P.clear_prompt_cache()


def test_call_json_records_version_and_forwards_schema(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    out = P.call_json(ctx, "judge", purpose="pick", constraints="none", options=["a", "b"])
    assert out["index"] == 0
    call = ctx.services.llm.calls[-1]  # type: ignore[attr-defined]
    assert call["prompt_name"] == "judge" and call["thinking"] is True
    assert call["schema"] == P.PROMPT_SCHEMAS["judge"]
    assert "0. a" in call["user"] and "1. b" in call["user"]
    assert ctx.prompt_versions["judge"] == P.load_prompt("judge").version


def test_call_json_fixture_override_selects_fixture_file(tmp_home: Path, tmp_path: Path):
    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "judge_special.json").write_text('{"index": 3, "reason": "special"}', encoding="utf-8")
    ctx = make_ctx(tmp_home)
    ctx.services.llm = FakeLlm(fx)
    out = P.call_json(ctx, "judge", fixture="judge_special", purpose="p", constraints="c", options=["a"])
    assert out["index"] == 3
    assert ctx.prompt_versions["judge"] == 1


def test_call_json_without_schema_is_an_error(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    with pytest.raises(ValueError, match="no schema"):
        P.call_json(ctx, "revise", fixes=[], story="x")


def test_call_text_returns_fixture_text(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    text = P.call_text(ctx, "revise", fixes=["Cut it."], story="old")
    assert text.startswith("The doorbell camera pings at 3:12 every night.")
    assert "- Cut it." in ctx.services.llm.calls[-1]["user"]  # type: ignore[attr-defined]


def test_parse_model_wraps_validation_errors():
    from ns_common.errors import NsError
    from ns_common.models import Idea

    with pytest.raises(NsError) as ei:
        P.parse_model(Idea, {"premise": "p"}, "idea")
    assert ei.value.code == "NS-E302"


def _base(stem: str) -> str:
    head, _, tail = stem.rpartition("_")
    return head if head and tail.isdigit() else stem


def _fixture_schema(stem: str) -> str | None:
    base = _base(stem)
    if base in FIXTURE_SCHEMA_OVERRIDES:
        return FIXTURE_SCHEMA_OVERRIDES[base]
    try:
        return P.load_prompt(base).schema
    except FileNotFoundError:
        return None


def test_every_json_fixture_validates_against_its_prompt_schema():
    checked = set()
    for f in sorted(P.FIXTURES.glob("*.json")):
        if f.stem.startswith("_"):
            continue
        schema = _fixture_schema(f.stem)
        assert schema, f"{f.name}: no prompt/schema found for this fixture"
        validate(json.loads(f.read_text(encoding="utf-8")), P.PROMPT_SCHEMAS[schema])
        checked.add(_base(f.stem))
    for n in ALL_PROMPTS:
        if P.load_prompt(n).schema:
            assert n in checked, f"missing fixture for {n}"


def test_text_fixtures_exist_for_text_prompts():
    for n in ("story_draft", "revise", "expand"):
        assert (P.FIXTURES / f"{n}.txt").read_text(encoding="utf-8").strip()


def test_master_prompts_render_with_their_variables(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    common = P._common_vars(ctx)
    chapter = {"n": 2, "title": "The Hall Light", "beats": ["a", "b"], "cliff": "A wrong detail."}
    plan = {"chapters": [chapter] * 5}
    out = P.render(
        P.load_prompt("chapter_draft"),
        **common,
        idea={"premise": "P.", "twist": "T."},
        plan=plan,
        chapter=chapter,
        summaries=[{"n": 1, "title": "One", "summary": "Fact."}],
        last_sentences=["Last one.", "Very last."],
        budget=chapter_budget(310, 155),
    )
    assert 'chapter 2 of 5: "The Hall Light"' in out and "Last one. Very last." in out
    assert "Chapter 1, One: Fact." in out and "between 248 and 372)" in out
    assert "final chapter" not in out
    sel = P.render(
        P.load_prompt("shorts_select"),
        **common,
        count_min=4,
        count_max=6,
        buckets=[{"seconds": 35, "body_words": 66, "min_words": 53, "max_words": 79}],
        chapters=[{"n": 1, "title": "T", "sentences": [{"idx": 0, "text": "Hi.", "words": 1}]}],
    )
    assert "[0] (1 words) Hi." in sel and "35 seconds: about 66 words" in sel
    pol = P.render(P.load_prompt("policy_check"), **common, story="Text.", title="Ti")
    assert "real_people:" in pol and "Title: Ti" in pol and "Hook:" not in pol
    exp = P.render(
        P.load_prompt("expand"),
        **common,
        chapter=chapter,
        beats=["x"],
        add_words=160,
        chapter_text="Body.",
    )
    assert "Add about 160 words" in exp and "never fewer than 120" in exp


def test_schema_name_override_is_used(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    kw = {"primary_keyword": "horror story", "part_text": "x", "part_n": None, "parts_total": 1}
    P.call_json(ctx, "seo", fixture="seo_short_1", schema_name="seo_titles", n_titles=5, **kw)
    call = ctx.services.llm.calls[-1]  # type: ignore[attr-defined]
    assert call["prompt_name"] == "seo_short_1"
    assert call["schema"] is P.PROMPT_SCHEMAS["seo_titles"]
    assert "5 different title candidates" in call["user"]


def test_master_thinking_flags_match_spec():
    flags = {n: P.load_prompt(n).thinking for n in MASTER_PROMPTS}
    assert flags == {
        "long_plan": True,
        "chapter_draft": False,
        "continuity": True,
        "expand": False,
        "long_seo": False,
        "shorts_select": True,
        "short_hook": False,
        "policy_check": True,
        "clip_select": True,
        "clip_seo": False,
    }


def test_master_prompt_schemas():
    assert P.load_prompt("expand").schema is None
    expected = {
        "long_plan": "long_plan",
        "chapter_draft": "chapter_draft",
        "continuity": "continuity",
        "long_seo": "long_seo",
        "shorts_select": "shorts_select",
        "short_hook": "short_hook",
        "policy_check": "policy_check",
        "clip_select": "clip_plan",
        "clip_seo": "seo",
    }
    assert {n: P.load_prompt(n).schema for n in expected} == expected
    required = set(P.PROMPT_SCHEMAS["policy_check"]["required"])
    assert required == {
        "unsafe_categories",
        "hook_honest",
        "title_honest",
        "standalone",
        "notes",
        "intensity",
        "real_setting",
    }  # the last two are studio-side keys (P04); ns_common's PolicyCheckResult keeps the five
    assert P.PROMPT_SCHEMAS["policy_check"]["properties"]["standalone"]["anyOf"]
