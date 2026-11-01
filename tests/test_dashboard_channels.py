from typing import Any

from ns_common.models import Channel
from ns_dashboard.channels import validate_profile


def profile(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"slug": "after-dark", "display_name": "After Dark", "niche": "scary stories"}
    return base | kw


def fields(items: list[dict[str, str]]) -> set[str]:
    return {i["field"] for i in items}


def test_minimal_profile_is_ok_with_pool_size_warnings():
    out = validate_profile(profile())
    assert out["ok"] is True and out["errors"] == []
    # the default pacings, caption styles and visual treatments pools have only two options
    assert {"pools.pacings", "pools.caption_styles", "pools.visual_treatments"} <= fields(out["warnings"])
    assert "pools.hooks" not in fields(out["warnings"])


def test_errors_are_plain_english():
    out = validate_profile(
        {"slug": "Bad Slug!", "critic_bar": 12, "long_minutes": [5, 8], "voice": {"speed": 3}, "bogus": 1}
    )
    assert out["ok"] is False
    by_field = {e["field"]: e["message"] for e in out["errors"]}
    assert "lowercase" in by_field["slug"]
    assert by_field["display_name"] == "This field is required."
    assert by_field["niche"] == "This field is required."
    assert by_field["critic_bar"] == "Must be at most 10.0."
    assert by_field["voice.speed"] == "Must be at most 1.3."
    assert by_field["bogus"] == "This setting is not known."
    assert "long_minutes" in by_field and "Value error" not in by_field["long_minutes"]


def test_literal_error_lists_choices():
    out = validate_profile(profile(format="weekly"))
    msg = next(e["message"] for e in out["errors"] if e["field"] == "format")
    assert msg.startswith("Pick one of:") and "shorts_only" in msg


def test_duplicate_options_and_publish_window():
    out = validate_profile(profile(pools={"hooks": ["a", "A", "b"]}, publish_window="21:00-18:00"))
    assert out["ok"] is True
    msgs = {(w["field"], w["message"]) for w in out["warnings"]}
    assert ("pools.hooks", "Some options are listed twice.") in msgs
    assert ("publish_window", "The window should end after it starts.") in msgs


def test_invalid_clock_time_is_an_error():
    out = validate_profile(profile(publish_window="25:00-26:00"))
    assert out["ok"] is False and out["errors"][0]["field"] == "publish_window"


def test_distinctness_against_other_channels():
    other = Channel.model_validate(profile(slug="night-owl", display_name="Night Owl"))
    out = validate_profile(profile(), [other])
    assert out["ok"] is True
    assert {"pools", "niche"} <= fields(out["warnings"])
    distinct = validate_profile(
        profile(
            niche="cosy cooking",
            seed_keywords=["recipe"],
            pools={
                "hooks": ["h1", "h2", "h3"], "structures": ["s1", "s2", "s3"], "pov_tones": ["p1", "p2", "p3"],
                "pacings": ["k1", "k2", "k3"], "caption_styles": ["c1", "c2", "c3"],
                "visual_treatments": ["v1", "v2", "v3"], "title_formulas": ["t1", "t2", "t3"],
            },
        ),
        [other],
    )
    assert distinct["ok"] and distinct["warnings"] == []


def test_same_slug_is_not_compared_with_itself():
    same = Channel.model_validate(profile())
    out = validate_profile(profile(), [same])
    assert "niche" not in fields(out["warnings"]) and "pools" not in fields(out["warnings"])


def test_shared_seed_keywords_warn():
    other = Channel.model_validate(profile(slug="other", seed_keywords=["horror", "ghost", "night"]))
    out = validate_profile(profile(niche="different", seed_keywords=["horror", "ghost", "night", "x"]), [other])
    assert "seed_keywords" in fields(out["warnings"])
