"""C8 Clipper: SelectStage tells the model the kind of video, caps titles at 70 characters and writes the
cleaned caption words beside the plan (spec 19.5, 17.2). Synthetic job files, fake model, no FFmpeg."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ns_common.errors import NsError
from ns_common.llm_client import FakeLlm
from ns_common.models import Rect, Transcript
from ns_studio.clipper import select
from ns_studio.clipper import stages_clip as sc
from ns_studio.clipper.layout import plan_layout
from ns_studio.clipper.models import Analysis, ClipLayout, ClipParams
from ns_studio.context import JobContext
from tests.fixtures.clipper.make_sample import build_transcript
from tests.helpers import make_ctx

LLM_FIXTURES = Path(__file__).parent / "fixtures" / "clipper" / "llm"
CAM = Rect(x=944, y=16, w=320, h=180)
LONG_TITLE = "How a single forgotten ledge saves forty seconds on every run of the mountain level today"


def _job(
    tmp_home: Path, clips: list[dict[str, Any]], *, analysis: dict[str, Any] | None = None, **param_kw: Any
) -> JobContext:
    ctx = make_ctx(tmp_home, fmt="clip")
    ctx.write_json("transcript.json", build_transcript())
    base = {"width": 1280, "height": 720, "aspect": 1.778, "duration_s": 90.0, "fps": 30.0}
    ctx.write_json(
        "analysis.json", Analysis.model_validate({**base, **(analysis or {})}).model_dump(mode="json")
    )
    ClipParams(source="sample.mp4", channel=ctx.channel.slug, rights_confirmed=True, **param_kw).save(
        ctx.job_dir
    )
    ok = {"ok": True, "reasons": []}
    ctx.services.llm = FakeLlm(LLM_FIXTURES, overrides={"clip_select": {"clips": clips}, "clip_judge": ok})
    return ctx


def _clip(**kw: Any) -> dict[str, Any]:
    return {"k": 1, "start_s": 0.5, "end_s": 50.0, "title": "Mountain route", "score": 0.9, **kw}


def _asked(ctx: JobContext) -> str:
    calls = [c for c in ctx.services.llm.calls if c["prompt_name"] == "clip_select"]  # type: ignore[attr-defined]
    assert calls
    return str(calls[0]["user"])


@pytest.mark.parametrize(
    ("analysis", "param_kw", "kind"),
    [
        (
            {"facecam": CAM.model_dump(), "n_stable_faces": 1, "faces_detected": True},
            {},
            "stream with facecam",
        ),
        ({"n_stable_faces": 2, "faces_detected": True}, {}, "podcast"),
        ({"n_stable_faces": 1, "faces_detected": True}, {}, "talking head"),
        ({"faces_detected": True, "scene_cuts_per_min": 7.0}, {}, "screen recording or slides"),
        ({"n_stable_faces": 2, "faces_detected": True}, {"video_kind": "lecture"}, "lecture"),  # owner's pick
    ],
)
def test_the_prompt_names_the_kind_of_video_instead_of_unknown(
    tmp_home: Path, analysis: dict[str, Any], param_kw: dict[str, Any], kind: str
) -> None:
    ctx = _job(tmp_home, [_clip()], analysis=analysis, **param_kw)
    sc.SelectStage().run(ctx)
    asked = _asked(ctx)
    assert f"Kind of video: {kind}." in asked and "Kind of video: unknown" not in asked


def test_the_prompt_says_unknown_only_when_the_analysis_cannot_tell(tmp_home: Path) -> None:
    ctx = _job(tmp_home, [_clip()])
    sc.SelectStage().run(ctx)
    assert "Kind of video: unknown." in _asked(ctx)


def test_the_clip_plan_title_is_at_most_70_characters(tmp_home: Path) -> None:
    ctx = _job(tmp_home, [_clip(title=LONG_TITLE)])
    sc.SelectStage().run(ctx)
    titles = [c["title"] for c in ctx.read_json("clip_plan.json")]
    assert titles and all(0 < len(t) <= 70 for t in titles)
    assert LONG_TITLE.startswith(titles[0])


def test_cleaned_caption_words_are_written_beside_the_plan(tmp_home: Path) -> None:
    tr = Transcript.model_validate(build_transcript())
    heard = select.words_in(tr.words, 0.5, 50.0)
    cleaned = " ".join(w.w.upper() for w in heard)
    ctx = _job(
        tmp_home,
        [_clip(caption_text=cleaned), _clip(k=2, start_s=64.0, end_s=118.0, title="No text", score=0.8)],
    )
    sc.SelectStage().run(ctx)
    plan = {c["title"]: c for c in ctx.read_json("clip_plan.json")}
    sidecar = ctx.read_json(sc.CAPTION_WORDS_FILE)
    assert list(sidecar) == [str(plan["Mountain route"]["k"])]
    words = select.words_in(tr.words, plan["Mountain route"]["start_s"], plan["Mountain route"]["end_s"])
    assert len(sidecar[str(plan["Mountain route"]["k"])]) == len(words)
    assert any(s != w.w for s, w in zip(sidecar[str(plan["Mountain route"]["k"])], words, strict=True))


def test_a_model_without_caption_text_leaves_an_empty_sidecar(tmp_home: Path) -> None:
    ctx = _job(tmp_home, [_clip()])
    sc.SelectStage().run(ctx)
    assert ctx.read_json(sc.CAPTION_WORDS_FILE) == {}


# ---------- break-it round ----------
class _RecordingLlm(FakeLlm):
    """A FakeLlm that also keeps the `max_tokens` each call asked for."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.limits: dict[str, list[int]] = {}

    def complete_json(self, **kw: Any) -> dict[str, Any]:
        self.limits.setdefault(kw["prompt_name"], []).append(kw.get("max_tokens", 4096))
        return super().complete_json(**kw)


def test_the_selection_call_asks_for_room_for_the_spoken_text_of_every_candidate(tmp_home: Path) -> None:
    ctx = _job(tmp_home, [_clip()])
    llm = _RecordingLlm(
        LLM_FIXTURES,
        overrides={"clip_select": {"clips": [_clip()]}, "clip_judge": {"ok": True, "reasons": []}},
    )
    ctx.services.llm = llm
    sc.SelectStage().run(ctx)
    assert llm.limits["clip_select"] == [sc.CLIP_SELECT_MAX_TOKENS]
    assert sc.CLIP_SELECT_MAX_TOKENS > 4096  # complete_json's default, which thinking and JSON share
    assert llm.limits["clip_judge"] == [4096]  # the other prompts keep the client's default


def test_cleaned_words_go_from_the_model_answer_through_both_stages_to_the_ass_file(tmp_home: Path) -> None:
    tr = Transcript.model_validate(build_transcript())
    heard = select.words_in(tr.words, 0.5, 50.0)
    text = " ".join(f"{w.w.capitalize()}\u2026" for w in heard)  # an ellipsis marks the cleaned words
    ctx = _job(
        tmp_home, [_clip(caption_text=text), _clip(k=2, start_s=64.0, end_s=118.0, title="Plain", score=0.8)]
    )
    sc.SelectStage().run(ctx)
    plan = {c["title"]: c for c in ctx.read_json("clip_plan.json")}
    k = plan["Mountain route"]["k"]
    k_plain = plan["Plain"]["k"]
    ctx.write_json(
        "source.json",
        {
            "path": str(ctx.job_dir / "missing.mp4"),
            "width": 1280,
            "height": 720,
            "duration_s": 90.0,
            "fps": 30.0,
            "source_id": "src-test",
        },
    )
    base = ClipLayout(template="center_crop")
    crops = plan_layout("center_crop", 1280, 720, base, t_start=0.0, t_end=90.0)
    ctx.write_json(
        "clip_layout.json",
        ClipLayout(
            template="center_crop",
            crops=crops,
            per_clip={str(k): "center_crop", str(k_plain): "center_crop"},
        ).model_dump(mode="json"),
    )
    sc.CaptionsStage().run(ctx)
    marked = ctx.file(f"captions/c{k}.ass").read_text(encoding="utf-8")
    unmarked = ctx.file(f"captions/c{k_plain}.ass").read_text(encoding="utf-8")
    assert "\u2026" in marked and "\u2026" not in unmarked  # the sidecar key is the clip's k in the plan
    assert ctx.read_json("captions/report.json")[f"c{k}"]["cleaned"] is True
    assert "cleaned" not in ctx.read_json("captions/report.json")[f"c{k_plain}"]


def test_the_fixture_answer_carries_caption_text_and_the_stage_cleans_with_it(tmp_home: Path) -> None:
    """The fake model's fixture (prompt v4) holds `caption_text`, so the fake path cleans like a real run."""
    ctx = _job(tmp_home, [])
    ctx.services.llm = FakeLlm(LLM_FIXTURES, overrides={"clip_judge": {"ok": True, "reasons": []}})
    sc.SelectStage().run(ctx)
    plan = {c["title"]: c for c in ctx.read_json("clip_plan.json")}
    boss = plan["Beat the boss with four laps"]
    cleaned = ctx.read_json(sc.CAPTION_WORDS_FILE)
    assert list(cleaned) == [str(boss["k"])]  # the other fixture moments have no text: they are not listed
    tr = Transcript.model_validate(build_transcript())
    heard = select.words_in(tr.words, boss["start_s"], boss["end_s"])
    shown = cleaned[str(boss["k"])]
    assert len(shown) == len(heard)  # one string per ASR word
    assert shown != [w.w for w in heard]  # "circles!" and "laps," differ from what the recogniser wrote
    assert "circles!" in shown and "laps," in shown


# ---------- D-44: a window whose answer is cut is asked again as two halves ----------
SPEECH = "We took the long way round the mountain and it paid off in the end."


def _long_transcript(seconds: float) -> dict[str, Any]:
    """Nonstop speech for `seconds`: a segment every 6 s, its words spread over it."""
    segments: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []
    tokens = SPEECH.split()
    for i in range(int(seconds / 6.0)):
        start = i * 6.0
        slot = 5.8 / len(tokens)
        for j, tok in enumerate(tokens):
            w_start = round(start + j * slot, 3)
            words.append({"w": tok, "start": w_start, "end": round(start + (j + 0.8) * slot, 3), "seg": i})
        segments.append({"i": i, "start": start, "end": words[-1]["end"], "text": SPEECH})
    return {"source": "long.mp4", "duration_s": seconds, "segments": segments, "words": words}


class _CutsLongWindows(FakeLlm):
    """`clip_select` answers are cut (NS-E302, as `complete_json` raises after its repair) for any window
    longer than `limit_s`; a shorter one gets one moment inside it."""

    def __init__(self, limit_s: float) -> None:
        super().__init__(LLM_FIXTURES, overrides={"clip_judge": {"ok": True, "reasons": []}})
        self.limit_s = limit_s
        self.windows: list[tuple[float, float]] = []

    def complete_json(self, **kw: Any) -> dict[str, Any]:
        if kw["prompt_name"] != "clip_select":
            return super().complete_json(**kw)
        import re

        found = re.search(r"Window: ([\d.]+) s to ([\d.]+) s", kw["user"])
        assert found is not None
        ws, we = float(found.group(1)), float(found.group(2))
        self.windows.append((ws, we))
        if we - ws > self.limit_s:
            raise NsError("NS-E302", f"clip_select: {{\"clips\": [{{\"k\": 1, \"start_s\": {ws}")
        return {"clips": [_clip(start_s=ws + 30.0, end_s=ws + 80.0, title=f"At {ws:.0f}")]}


def _long_job(tmp_home: Path, llm: FakeLlm, seconds: float = 720.0) -> JobContext:
    ctx = make_ctx(tmp_home, fmt="clip")
    ctx.write_json("transcript.json", _long_transcript(seconds))
    base = {"width": 1280, "height": 720, "aspect": 1.778, "duration_s": seconds, "fps": 30.0}
    ctx.write_json("analysis.json", Analysis.model_validate(base).model_dump(mode="json"))
    ClipParams(source="long.mp4", channel=ctx.channel.slug, rights_confirmed=True).save(ctx.job_dir)
    ctx.services.llm = llm
    return ctx


def test_a_window_whose_answer_is_cut_is_asked_again_as_two_halves(tmp_home: Path) -> None:
    llm = _CutsLongWindows(limit_s=sc.select.WINDOW_S - 1)
    ctx = _long_job(tmp_home, llm)
    try:
        sc.SelectStage().run(ctx)
        lost = ""
    except NsError as e:
        lost = e.code
    assert lost == "", f"the window was lost with {lost}"
    assert llm.windows == [(0.0, 720.0), (0.0, 420.0), (300.0, 720.0)]
    assert sc.split_window(0.0, 720.0) == [(0.0, 420.0), (300.0, 720.0)]
    titles = {c["title"] for c in ctx.read_json("clip_plan.json") if c["status"] == "kept"}
    assert titles == {"At 0", "At 300"}  # the window is not lost: both halves' moments are in the plan
    cached = json.loads(ctx.file("clip_candidates.json").read_text(encoding="utf-8"))
    assert [len(v["clips"]) for v in cached.values()] == [2]  # the halves are cached as the window's answer
    sc.SelectStage().run(ctx)  # a second run reads the cache: nothing is asked again
    assert len(llm.windows) == 3


def test_a_half_that_is_cut_again_fails_the_stage_and_a_short_window_is_not_split(tmp_home: Path) -> None:
    llm = _CutsLongWindows(limit_s=100.0)
    ctx = _long_job(tmp_home, llm)
    with pytest.raises(NsError) as e:
        sc.SelectStage().run(ctx)
    assert e.value.code == "NS-E302"
    assert llm.windows == [(0.0, 720.0), (0.0, 420.0)]  # one split only; the first half fails it
    short = _CutsLongWindows(limit_s=100.0)
    ctx2 = _long_job(tmp_home, short, seconds=sc.SPLIT_MIN_S)
    with pytest.raises(NsError):
        sc.SelectStage().run(ctx2)
    assert short.windows == [(0.0, sc.SPLIT_MIN_S)]  # at or below SPLIT_MIN_S the window is not split
