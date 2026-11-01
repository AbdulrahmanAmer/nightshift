import json
from pathlib import Path

import pytest
from jsonschema import validate

from ns_common.llm_client import FakeLlm
from ns_studio.text import prompts as P
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

# fixture file stem (without a trailing _<n>) -> schema name when it differs from the prompt's front-matter
FIXTURE_SCHEMA_OVERRIDES: dict[str, str] = {}


def test_all_shorts_prompts_load_with_frontmatter():
    for n in SHORTS_PROMPTS:
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


def test_missing_frontmatter_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "bad.md").write_text("no front matter\n---USER---\nx\n", encoding="utf-8")
    (tmp_path / "half.md").write_text("---\nversion: 1\n---\nsys\n---USER---\nx\n", encoding="utf-8")
    monkeypatch.setattr(P, "PROMPTS_DIR", tmp_path)
    P.load_prompt.cache_clear()
    try:
        with pytest.raises(ValueError, match="missing front-matter"):
            P.load_prompt("bad")
        with pytest.raises(ValueError, match="needs version, thinking, schema"):
            P.load_prompt("half")
    finally:
        P.load_prompt.cache_clear()


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


def _fixture_schema(stem: str) -> str | None:
    base = stem.rsplit("_", 1)[0] if stem.rsplit("_", 1)[-1].isdigit() else stem
    if stem in FIXTURE_SCHEMA_OVERRIDES:
        return FIXTURE_SCHEMA_OVERRIDES[stem]
    try:
        return P.load_prompt(base).schema
    except FileNotFoundError:
        return None


def test_every_json_fixture_validates_against_its_prompt_schema():
    checked = []
    for f in sorted(P.FIXTURES.glob("*.json")):
        if f.stem.startswith("_"):
            continue
        schema = _fixture_schema(f.stem)
        assert schema, f"{f.name}: no prompt/schema found for this fixture"
        validate(json.loads(f.read_text(encoding="utf-8")), P.PROMPT_SCHEMAS[schema])
        checked.append(f.stem)
    for n in SHORTS_PROMPTS:
        if P.load_prompt(n).schema:
            assert n in checked, f"missing fixture for {n}"


def test_text_fixtures_exist_for_text_prompts():
    for n in ("story_draft", "revise"):
        assert (P.FIXTURES / f"{n}.txt").read_text(encoding="utf-8").strip()
