"""Every user of the language model's context fits the smallest context `ns tune` can choose (D-44).

`ns_supervisor.tune.CONTEXT_USES` lists each user with the largest request it can make (prompt, thinking and
answer together, in tokens), and `tune.min_ctx_for` derives the floor from the largest of them. This file
builds each request the product can send at its largest, from the product's own limits (the longest text a
job can carry, the 720 s clip window, the answer limits), renders it through the real prompt templates, counts
it with the product's estimator (`estimate_tokens`: `approx_tokens` plus 10 %) and fails when:

- a prompt grows past the figure its use declares (so a template or an input that grows later is caught);
- a use declares more than the floor the tune chooses;
- a prompt file, an answer limit or a `max_tokens` override appears that no use accounts for.

The text is English, the language the word budgets are set for (spec 7.0); other scripts take more tokens
per word (`approx_tokens`), which the report states.
"""

from __future__ import annotations

import ast
import inspect
import json
import math
from pathlib import Path
from typing import Any

import pytest

from ns_common.llm_client import LlmClient
from ns_common.models import Runtime
from ns_common.pacing import PACING_WPM, SPEED_MAX
from ns_dashboard.assistant.prompt import BUDGET_16K, BUDGET_SMALL, budget_for, estimate_tokens
from ns_studio.clipper import analyze, select
from ns_studio.clipper import stages_clip as sc
from ns_studio.clipper.models import Analysis, Loudness, LoudPeak, SilenceSpan
from ns_studio.media.qc import SERIES_MAX_S, SHORT_MAX_S
from ns_studio.series.plain import spoken_wpm
from ns_studio.series.workshop import MAX_PARTS
from ns_studio.text import prompts as P
from ns_studio.text.budget import MAX_CHAPTERS, chapter_budget
from ns_studio.text.validators import load_banned
from ns_supervisor import tune
from tests.helpers import make_ctx

ROOT = Path(__file__).resolve().parents[1]
CHAT_OVERHEAD = 64  # the chat template's role markers and the thinking switch around system and user
REPAIR_LINE = "Repair: your previous answer was not valid JSON. Reply with only the JSON."

# ---- the product's limits ----------------------------------------------------------------------------
#: The fastest the narration can be: the brisk pace at the highest voice speed.
FASTEST_WPM = max(spoken_wpm(p, SPEED_MAX) for p in PACING_WPM)
#: The longest text one job carries: a series part at QC's ceiling (25 minutes plus 15 %) at that pace. An
#: owner's long-video script is shorter (`plain.plan_script` refuses past 14:30).
LONG_WORDS = math.ceil(SERIES_MAX_S / 60 * FASTEST_WPM)
#: The longest Short (QC's ceiling) at that pace: a selected moment, a Short's story part.
SHORT_WORDS = math.ceil(SHORT_MAX_S / 60 * FASTEST_WPM)
#: The fastest speech a clip source is assumed to hold: 4 words a second (240 a minute), above any normal
#: talk (conversation is about 2.5 to 3).
CLIP_WORDS_PER_S = 4.0
#: Free text the owner types (a premise, a feedback note, voice notes): no field caps it; this much is
#: assumed (about a page).
OWNER_WORDS = 400

SENTENCES = (
    "The hall light came on by itself at ten past three, and nobody in the house was awake to see it.",
    "Mara stood at the bottom of the stairs and counted the steps twice, because the second time there was "
    "one more.",
    "Her phone showed a message from her own number, sent an hour before she woke up.",
    "Outside, the rain kept a slow and patient rhythm against the kitchen window.",
    "She told herself it was the old wiring again, the way her landlord always said it was.",
    "Then the doorbell camera pinged, and the picture showed the porch, empty, with wet footprints going in.",
)


def prose(words: int) -> str:
    """English narration of exactly `words` words, in sentences of the length the stories use."""
    pool = " ".join(SENTENCES).split()
    out = [pool[i % len(pool)] for i in range(words)]
    if not out[-1].endswith("."):
        out[-1] += "."
    return " ".join(out)


def sentences_of(text: str) -> list[str]:
    parts = [s.strip() for s in text.replace(". ", ".\n").splitlines()]
    return [s for s in parts if s]


def chapters_of(words: int, n: int = MAX_CHAPTERS) -> list[dict[str, Any]]:
    per = math.ceil(words / n)
    return [
        {"n": i, "title": "The Second Staircase", "text": prose(per), "summary": prose(40)}
        for i in range(1, n + 1)
    ]


def max_tokens_default(method: str) -> int:
    return int(inspect.signature(getattr(LlmClient, method)).parameters["max_tokens"].default)


def clip_select_vars(seconds: float) -> dict[str, Any]:
    """`clip_select`'s variables for a window of `seconds` of nonstop speech at `CLIP_WORDS_PER_S`, with the
    analysis at its caps (60 cuts, 40 silences, 20 peaks: `analyze.window_view`)."""
    seg_s = 6.0
    per_seg = round(CLIP_WORDS_PER_S * seg_s)
    start = 7200.0 - seconds
    segments = [
        {"i": i, "start": round(start + i * seg_s, 1), "end": round(start + i * seg_s + seg_s - 0.1, 1),
         "text": prose(per_seg)}
        for i in range(int(seconds / seg_s))
    ]  # fmt: skip
    an = Analysis(
        width=1920, height=1080, aspect=1.78, duration_s=7200.0, fps=59.94,
        scene_cuts=[i * 1.5 for i in range(4800)], scene_cuts_per_min=40.0,
        silences=[SilenceSpan(start=i * 10.0, end=i * 10.0 + 2.0) for i in range(720)],
        loudness=Loudness(
            integrated_lufs=-14.0, lra=7.0, peaks=[LoudPeak(t=i * 30.0, db=-1.5) for i in range(240)]
        ),
        n_stable_faces=2, faces_detected=True,
    )  # fmt: skip
    return {
        "segments": segments, "window_start_s": start, "window_end_s": 7200.0, "clip_min_s": 10,
        "clip_max_s": 180, "max_candidates": 10, "video_kind": "screen recording or slides",
        "analysis": analyze.prompt_view(an, start, 7200.0),
    }  # fmt: skip


def clip_select_answer(seconds: float) -> dict[str, Any]:
    """The largest `clip_select` answer for such a window: ten candidates whose caption text together holds
    every word of the window once."""
    words = round(seconds * CLIP_WORDS_PER_S)
    return {
        "clips": [
            {"k": k, "start_s": 6480.0 + k * 70, "end_s": 6540.0 + k * 70, "title": prose(12),
             "hook_line": prose(14), "why": prose(20), "score": 0.82, "speakers": [0, 1],
             "keywords": ["staircase", "doorbell", "camera", "rain", "kitchen", "footprints"],
             "layout": "podcast_stacked", "layout_reason": prose(14), "caption_style": "punch_yellow",
             "emphasis": ["stairs", "camera", "footprints"], "caption_text": prose(max(1, words // 10))}
            for k in range(1, 11)
        ]
    }  # fmt: skip


# ---- the requests ------------------------------------------------------------------------------------
def worst_vars(name: str) -> dict[str, Any]:
    """The variables of prompt `name` at the largest the product sends."""
    long_text = prose(LONG_WORDS)
    short_text = prose(SHORT_WORDS)
    owner = prose(OWNER_WORDS)
    chapters = chapters_of(LONG_WORDS)
    plan_chapter = {"n": MAX_CHAPTERS, "title": "The Second Staircase", "beats": [prose(25)] * 8,
                    "cliff": prose(25), "word_budget": 900}  # fmt: skip
    idea = {"premise": prose(60), "twist": prose(30), "setting": prose(10), "protagonist": prose(10),
            "why_chapters": prose(30)}  # fmt: skip
    concept = {"title": prose(8), "premise": owner, "cast": [prose(12)] * 8, "setting": prose(30),
               "tone": prose(20), "beats": [prose(25)] * 8, "why_it_works": prose(40),
               "series_potential": prose(40), "feedback": [owner] * 3}  # fmt: skip
    entry = {"n": MAX_PARTS, "title": prose(8), "hook": prose(30), "cliff": prose(30), "reveals": prose(40),
             "recap": prose(30), "minutes": 25}  # fmt: skip
    seo = {"primary_keyword": "scary story", "combo": None}
    by_name: dict[str, dict[str, Any]] = {
        "chapter_draft": {
            "idea": idea,
            "plan": {"chapters": [plan_chapter] * MAX_CHAPTERS},
            "chapter": plan_chapter,
            "summaries": [
                {"n": i, "title": "The Second Staircase", "summary": prose(60)} for i in range(1, 12)
            ],
            "last_sentences": [prose(35), prose(35)],
            "budget": chapter_budget(1200, 175),
        },
        "clip_judge": {
            "clip": {"k": 10, "title": prose(12), "start_s": 7000.0, "end_s": 7180.0, "duration_s": 180.0},
            "clip_min_s": 10,
            "clip_max_s": 180,
            "words_per_s": 4.0,
            "snapped": True,
            "first_sentence": prose(35),
            "last_sentence": prose(35),
            "clip_text": prose(2000)[:3000],
        },
        "clip_select": clip_select_vars(select.WINDOW_S),
        "clip_seo": {
            "source_title": prose(20),
            "primary_keyword": "scary story",
            "clip_title": prose(12),
            "clip_text": prose(1000)[:1500],
        },  # fmt: skip
        "continuity": {"scope": "the whole part", "chapters": chapters},
        "critic": {
            "story": long_text,
            "banned": load_banned(),
            "scope": "chapter 12 of 12 of part 40 of a series",
        },
        "expand": {
            "chapter": plan_chapter,
            "beats": plan_chapter["beats"],
            "add_words": 900,
            "chapter_text": long_text,
        },  # fmt: skip
        "hook": {"part_text": long_text, "next_part_text": long_text, "part_n": 9, "parts_total": 9},
        "ideas": {"n": 8, "budget": {"words": 600}, "avoid_premises": [prose(45)] * 50},
        "judge": {"purpose": prose(30), "constraints": prose(60), "options": [prose(70)] * 10},
        "long_plan": {
            "idea": idea,
            "budget": {
                "target_words": 2600,
                "target_minutes": 14,
                "wpm": 175,
                "chapters": 7,
                "chapter_budgets": [372] * 7,
            },
        },  # fmt: skip
        "long_seo": {
            **seo,
            "working_title": prose(12),
            "opening": prose(150),
            "chapters": [{"n": c["n"], "title": c["title"], "summary": c["summary"]} for c in chapters],
        },
        "policy_check": {
            "story": long_text,
            "span_text": short_text,
            "title": prose(14),
            "hook": prose(12),
            "description": prose(120),
            "tags": ", ".join(["scary story"] * 60),
        },  # fmt: skip
        "revise": {"story": long_text, "fixes": [prose(40)] * 12},
        "seo": {**seo, "part_text": long_text, "part_n": 9, "parts_total": 9, "n_titles": 5},
        "short_hook": {"chapter_title": prose(5), "span_text": short_text, "why_standalone": prose(40)},
        "shorts_select": {
            "count_min": 4,
            "count_max": 12,
            "buckets": [
                {"seconds": s, "body_words": 300, "min_words": 250, "max_words": 350}
                for s in range(30, 181, 15)
            ],
            "chapters": [
                {
                    "n": c["n"],
                    "title": c["title"],
                    "sentences": [
                        {"idx": i, "text": s, "words": len(s.split())}
                        for i, s in enumerate(sentences_of(c["text"]))
                    ],
                }
                for c in chapters
            ],
        },  # fmt: skip
        "split": {"story": long_text, "budget": {"parts": 9, "words_per_part": 600, "wpm": 175}},
        "story_draft": {
            "plan": {"beats": [{"name": prose(4), "summary": prose(30)}] * 12, "ending_line_idea": prose(30)},
            "budget": {"words": 3000, "min_words": 2400, "max_words": 3600},
        },  # fmt: skip
        "story_plan": {"idea": idea, "budget": {"words": 3000, "parts": 9, "words_per_part": 340}},
        "visual_plan": {"sentences": sentences_of(long_text), "setting": prose(20), "chapter_n": 12},
        "workshop_concepts": {"n": 6, "premise": owner, "avoid_titles": [prose(10)] * 26},
        "workshop_map": {"concept": concept, "parts": MAX_PARTS, "minutes": 25},
        "workshop_part_outline": {
            "concept": concept,
            "series": {"title": prose(8), "parts": [entry] * MAX_PARTS},
            "part": entry,
            "previous": [entry] * (MAX_PARTS - 1),
            "budget": {"minutes": 25, "words": 4375, "wpm": 175, "chapters": 12, "chapter_words": 365},
        },
        "workshop_regenerate": {"concept": concept, "feedback": owner, "letter": "F"},
        "workshop_revise": {"chapter": {"n": 12, "title": prose(5), "text": long_text}, "feedback": owner},
    }
    return by_name[name]


#: The answer limit each prompt is sent with: `complete_json`'s or `complete_text`'s default, or the
#: override its caller passes (`OVERRIDES`, checked against the source below).
CLIP_SELECT_MAX_TOKENS = sc.CLIP_SELECT_MAX_TOKENS
OVERRIDES = {"clip_select": CLIP_SELECT_MAX_TOKENS}


def answer_limit(name: str) -> int:
    if name in OVERRIDES:
        return OVERRIDES[name]
    return max_tokens_default("complete_json" if P.load_prompt(name).schema else "complete_text")


def prompt_tokens(name: str, ctx: Any, extra: dict[str, Any] | None = None) -> int:
    p = P.load_prompt(name)
    variables = P._common_vars(ctx) | (extra if extra is not None else worst_vars(name))
    variables = {k: (ctx.combo if v is None and k == "combo" else v) for k, v in variables.items()}
    user = P.render(p, **variables)
    system = P.render_system(p, **variables)
    return estimate_tokens(system) + estimate_tokens(user) + CHAT_OVERHEAD


def prompt_names() -> list[str]:
    return sorted(f.stem for f in (ROOT / "ns_studio" / "prompts").glob("*.md"))


def use_of(name: str) -> tune.ContextUse:
    found = [u for u in tune.CONTEXT_USES if name in u.prompts]
    assert len(found) == 1, (
        f"prompt {name} must belong to exactly one use in tune.CONTEXT_USES, found {found}"
    )
    return found[0]


def measured(tmp_home: Path) -> dict[str, tuple[int, int]]:
    """{prompt: (prompt tokens, answer limit)} for every prompt file."""
    ctx = make_ctx(tmp_home, fmt="master_first")
    return {n: (prompt_tokens(n, ctx), answer_limit(n)) for n in prompt_names()}


def needs_the_table() -> None:
    """The base has no table of uses: say so at an assertion (not an import error)."""
    assert hasattr(tune, "CONTEXT_USES") and hasattr(tune, "largest_use"), (
        "ns_supervisor.tune has no CONTEXT_USES: the floor does not come from the uses of the context"
    )


# ---- the tests ---------------------------------------------------------------------------------------
def test_every_prompt_fits_the_figure_its_use_declares(tmp_home: Path) -> None:
    needs_the_table()
    for name, (prompt, answer) in measured(tmp_home).items():
        use = use_of(name)
        assert prompt + answer <= use.tokens, (
            f"{name}: prompt {prompt} + answer {answer} = {prompt + answer} tokens, more than the "
            f"{use.tokens} that {use.name!r} declares in tune.CONTEXT_USES"
        )


@pytest.mark.parametrize("profile", ["gpu", "cpu-dev"])
def test_the_floor_holds_the_largest_use_of_the_profile(profile: str) -> None:
    needs_the_table()
    rt = Runtime.default(profile)  # type: ignore[arg-type]
    floor = tune.min_ctx_for(rt)
    uses = [u for u in tune.CONTEXT_USES if profile in u.profiles]
    assert max(u.tokens for u in uses) <= floor
    assert floor in tune.CTX_OPTIONS
    smaller = [c for c in tune.CTX_OPTIONS if c < floor]
    assert all(c < tune.largest_use(rt).tokens for c in smaller)  # it is the smallest that holds them
    assert floor == 16384


def repair_tokens(prompt: int, answer: int) -> int:
    """`complete_json`'s repair of a cut answer: the prompt, the cut answer (at most the whole answer), the
    repair line and a new answer without thinking."""
    return prompt + answer + estimate_tokens(REPAIR_LINE) + CHAT_OVERHEAD + answer


def test_clip_selection_repairs_a_cut_answer_inside_the_floor_or_asks_the_halves(tmp_home: Path) -> None:
    """`complete_json` sends a cut answer back once (`REPAIR_LINE`). For a whole window of nonstop fast speech
    that request is larger than the floor; `SelectStage` then asks the window's two halves
    (`stages_clip.split_window`), whose requests, repair included, fit."""
    needs_the_table()
    src = inspect.getsource(LlmClient.complete_json)
    assert REPAIR_LINE in src and "thinking=False" in src and "max_tokens=max_tokens" in src
    ctx = make_ctx(tmp_home, fmt="master_first")
    floor = tune.min_ctx_for(Runtime.default("cpu-dev"))
    whole = select.WINDOW_S
    answer = estimate_tokens(json.dumps(clip_select_answer(whole)))
    assert answer <= CLIP_SELECT_MAX_TOKENS  # the answer itself fits its limit
    full = repair_tokens(prompt_tokens("clip_select", ctx, clip_select_vars(whole)), answer)
    assert full > floor  # why the halves exist: the table in the report says so
    assert hasattr(sc, "split_window"), "a cut window is lost: no split_window (before D-44 small3)"
    halves = sc.split_window(7200.0 - whole, 7200.0)
    assert len(halves) == 2 and whole > sc.SPLIT_MIN_S
    for ws, we in halves:
        seconds = we - ws
        prompt = prompt_tokens("clip_select", ctx, clip_select_vars(seconds))
        half_answer = estimate_tokens(json.dumps(clip_select_answer(seconds)))
        assert prompt + CLIP_SELECT_MAX_TOKENS <= floor
        assert repair_tokens(prompt, half_answer) <= floor, (ws, we)


def test_only_clip_selection_overrides_the_answer_limit() -> None:
    """A new `max_tokens=` on a prompt call must be added to `OVERRIDES` (and so to its use's figure)."""
    found: dict[str, str] = {}
    for path in (ROOT / "ns_studio").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None))
                    in ("call_json", "call_text", "complete_json", "complete_text")):  # fmt: skip
                continue
            kw = {k.arg: k.value for k in node.keywords}
            if "max_tokens" in kw and path.name != "prompts.py":
                name = next((a.value for a in node.args if isinstance(a, ast.Constant)), "?")
                found[str(name)] = ast.unparse(kw["max_tokens"])
    assert found == {"clip_select": "CLIP_SELECT_MAX_TOKENS"}


def test_the_assistant_plans_inside_the_floor_and_names_its_figure() -> None:
    """The Assistant sizes every request to the context it is given (`budget_for`); at the floor it uses its
    16K plan, which leaves room for earlier turns."""
    needs_the_table()
    floor = tune.min_ctx_for(Runtime.default("cpu-dev"))
    plan = budget_for(floor)
    assert plan.history > 0 and plan.ctx == floor
    assert plan.output_reserve == BUDGET_16K.output_reserve  # the 16K plan, not the small one
    assert use_named("the Assistant").tokens == BUDGET_16K.ctx
    assert BUDGET_SMALL.ctx < floor


def test_the_checks_and_probes_outside_the_studio_are_small() -> None:
    """`ns verify` asks for 64 tokens on a one-line prompt; the tune's probe is a 400-token prompt and 300
    generated tokens (`tune.matrix_lines`)."""
    needs_the_table()
    src = (ROOT / "ns_supervisor" / "verify.py").read_text(encoding="utf-8")
    assert "max_tokens=64" in src
    assert use_named("ns verify and ns tune probes").tokens >= 400 + 300
    assert use_named("Nightshift Code").tokens == tune.CODE_MIN_CONTEXT


def use_named(name: str) -> tune.ContextUse:
    return next(u for u in tune.CONTEXT_USES if u.name == name)
