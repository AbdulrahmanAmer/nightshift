"""Dev mode: the fake model's clip_select answer fits the window in the prompt (first dev-mode clip job
failed with NS-E407 because the fixture's moments lie at 62-262 s of a test recording)."""

from pathlib import Path

from ns_common.llm_client import FakeLlm, fit_clip_fixture

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "llm"


def _prompt(start: float, end: float, n: int = 6, head: bool = True) -> str:
    """The shape of prompts/clip_select.md: a Window line, then "[i] start - end: text" segment lines."""
    step = (end - start) / n
    lines = [f"Window: {start:.1f} s to {end:.1f} s. Clip length: 5 to 30 seconds."] if head else []
    lines += [f"[{i}] {start + i * step:.1f} - {start + (i + 1) * step:.1f}: sentence {i}" for i in range(n)]
    return "\n".join(lines)


def test_a_short_video_gets_two_made_up_moments_inside_its_window():
    llm = FakeLlm(FIXTURES)
    out = llm.complete_json(prompt_name="clip_select", system="", user=_prompt(0.0, 53.6), schema={})
    clips = out["clips"]
    assert len(clips) == 2 and all(0.0 <= c["start_s"] < c["end_s"] <= 53.6 for c in clips)
    assert clips[0]["k"] == 1 and clips[1]["k"] == 2 and clips[0]["score"] > clips[1]["score"]


def test_the_fixture_is_kept_when_its_moments_fit_the_window():
    llm = FakeLlm(FIXTURES)
    out = llm.complete_json(prompt_name="clip_select", system="", user=_prompt(0.0, 600.0), schema={})
    assert out["clips"][0]["start_s"] == 62.4                       # the recorded fixture, untouched


def test_segment_lines_alone_give_the_window_too():
    llm = FakeLlm(FIXTURES)
    user = _prompt(0.0, 53.6, head=False)
    out = llm.complete_json(prompt_name="clip_select", system="", user=user, schema={})
    assert len(out["clips"]) == 2 and out["clips"][1]["end_s"] <= 53.6


def test_no_window_lines_or_a_tiny_window_leave_the_fixture_alone():
    data = {"clips": [{"start_s": 62.4, "end_s": 101.9}]}
    assert fit_clip_fixture(data, "no timings here") is data
    assert fit_clip_fixture(data, _prompt(0.0, 6.0)) is data


def test_the_series_map_fixture_is_fitted_to_the_requested_part_count():
    from ns_common.llm_client import fit_map_fixture

    data = {"parts": [{"n": 1, "title": "A", "recap": ""}, {"n": 2, "title": "B", "recap": "x"},
                      {"n": 3, "title": "C", "recap": "y"}], "arc_payoff": "p"}
    two = fit_map_fixture(data, "Plan exactly 2 parts of about 12 minutes each.")
    assert [p["n"] for p in two["parts"]] == [1, 2] and two["arc_payoff"] == "p"
    four = fit_map_fixture(data, "Plan exactly 4 parts of about 12 minutes each.")
    assert [p["n"] for p in four["parts"]] == [1, 2, 3, 4] and four["parts"][3]["title"].startswith("C")
    assert fit_map_fixture(data, "Plan exactly 3 parts") is data and fit_map_fixture(data, "no count") is data
