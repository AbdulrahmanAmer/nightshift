"""C8 Clipper: SelectStage tells the model the kind of video, caps titles at 70 characters and writes the
cleaned caption words beside the plan (spec 19.5, 17.2). Synthetic job files, fake model, no FFmpeg."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ns_common.llm_client import FakeLlm
from ns_common.models import Rect, Transcript
from ns_studio.clipper import select
from ns_studio.clipper import stages_clip as sc
from ns_studio.clipper.models import Analysis, ClipParams
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
