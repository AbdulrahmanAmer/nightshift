"""F-H95 / D-38: a clip with a hesitation is captioned when the channel's `remove_fillers` is on.

`CaptionsStage.run` used to give `speaker_plan` the speakers of every spoken word and the words with the
hesitations taken out, so `speaker_plan` raised "speaker_of must have one entry per word". The words and the
speakers now come from one list of kept words.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from ns_studio.clipper import layout as lay
from ns_studio.clipper import select
from ns_studio.clipper import stages_clip as sc
from ns_studio.clipper.models import ClipItem, ClipLayout, Rect, Transcript
from tests.helpers import make_channel, make_combo, make_ctx

SPOKEN = "So um this uh game is like really er good"
KEPT = ["So", "this", "game", "is", "like", "really", "good"]
CLIP_END_S = 20.0
BS = chr(92)
ASS_BREAK = BS + "N"  # the ASS line break
BRACES = re.compile("[{][^}]*[}]")  # an ASS override block


def _transcript(parts: list[tuple[int, str]]) -> Transcript:
    """One segment per (speaker, text); three words a second, in order."""
    words: list[dict[str, Any]] = []
    segs: list[dict[str, Any]] = []
    t = 0.0
    for i, (speaker, text) in enumerate(parts):
        first = t
        for tok in text.split():
            words.append({"w": tok, "start": round(t, 3), "end": round(t + 0.3, 3), "seg": i})
            t += 1 / 3.0
        segs.append({"i": i, "start": first, "end": t, "text": text, "speaker": speaker})
    return Transcript.model_validate(
        {"source": "v.mp4", "duration_s": 30.0, "segments": segs, "words": words}
    )


def _run(
    tmp_home: Path,
    parts: list[tuple[int, str]],
    *,
    remove: bool = True,
    cleaned: list[str] | None = None,
    spy: list | None = None,
) -> tuple[Any, dict | None]:
    """Set up one clip job as the dashboard's would be and run CaptionsStage; (ctx, report or None).

    A ValueError out of the stage (the defect) gives report None, so a test fails at its assertion.
    """
    ctx = make_ctx(
        tmp_home,
        fmt="clip",
        combo=make_combo(caption_style="clean_white"),
        channel=make_channel(captions={"remove_fillers": remove}),
    )
    ctx.write_json("transcript.json", _transcript(parts).model_dump(mode="json"))
    ctx.write_json(
        "source.json",
        {
            "path": str(ctx.job_dir / "missing.mp4"),
            "width": 1280,
            "height": 720,
            "duration_s": 30.0,
            "fps": 30.0,
            "source_id": "src-test",
        },
    )
    clip = ClipItem(k=1, start_s=0.0, end_s=CLIP_END_S, title="t", score=0.9)
    ctx.write_json("clip_plan.json", [clip.model_dump(mode="json")])
    cam = Rect(x=944, y=16, w=320, h=180)
    base = ClipLayout(template="gaming_facecam", facecam=cam)
    crops = lay.plan_layout("gaming_facecam", 1280, 720, base, t_start=0.0, t_end=CLIP_END_S)
    out = ClipLayout(template="gaming_facecam", facecam=cam, crops=crops, per_clip={"1": "gaming_facecam"})
    ctx.write_json("clip_layout.json", out.model_dump(mode="json"))
    if cleaned is not None:
        ctx.write_json(sc.CAPTION_WORDS_FILE, {"1": cleaned})
    if spy is not None:
        real = sc.speaker_plan

        def recording(words, speaker_of, style):
            result = real(words, speaker_of, style)
            spy.append(([w.w for w in words], list(speaker_of), result))
            return result

        sc.speaker_plan = recording
    try:
        sc.CaptionsStage().run(ctx)
    except ValueError:
        return ctx, None
    finally:
        if spy is not None:
            sc.speaker_plan = real
    return ctx, ctx.read_json("captions/report.json")


def _groups(ctx) -> list[tuple[str, str]]:
    """(text, base colour) of each caption group on screen; the word-by-word events of a group count once."""
    ass = ctx.file("captions/c1.ass").read_text(encoding="utf-8")
    groups: list[tuple[str, str]] = []
    for ln in ass.splitlines():
        if ln.startswith("Dialogue:"):
            raw = ln.split(",", 9)[9]
            text = re.sub(BRACES, "", raw).replace(ASS_BREAK, " ")
            colour = re.search(re.escape(BS) + "c&H([0-9A-Fa-f]+)&", raw)
            entry = (text, colour.group(1) if colour else "")
            if not groups or groups[-1] != entry:
                groups.append(entry)
    return groups


def _dialogue(ctx) -> list[str]:
    return [w for text, _ in _groups(ctx) for w in re.findall(r"[A-Za-z']+", text)]


def test_a_clip_with_a_hesitation_is_captioned_with_the_setting_on(tmp_home: Path):
    ctx, report = _run(tmp_home, [(0, SPOKEN)])
    assert report is not None, "the stage raised on a hesitation"
    assert ctx.file("captions/c1.ass").exists()
    spoken = _dialogue(ctx)
    assert not {w.lower() for w in spoken} & select.HESITATIONS
    assert "like" in [w.lower() for w in spoken]
    assert spoken == KEPT
    assert report["c1"]["words"] == len(KEPT)
    # the second run, on the job folder the first one left, gives the same files
    first = ctx.file("captions/c1.ass").read_text(encoding="utf-8")
    sc.CaptionsStage().run(ctx)
    assert ctx.file("captions/c1.ass").read_text(encoding="utf-8") == first


def test_each_kept_word_keeps_its_speaker_after_a_hesitation_is_removed(tmp_home: Path):
    spy: list = []
    parts = [(0, "hello there everyone"), (1, "um now we begin")]
    ctx, report = _run(tmp_home, parts, spy=spy)
    assert report is not None, "the stage raised on a hesitation"
    words, speaker_of, (mapping, breaks) = spy[0]
    assert words == ["hello", "there", "everyone", "now", "we", "begin"]
    assert speaker_of == [0, 0, 0, 1, 1, 1]
    # the break lands on "now", the first kept word of segment 1: not on "everyone" (one early) nor "we"
    assert breaks == [3]
    assert sorted(set(mapping.values())) == [0, 1]
    assert report["c1"]["speakers"] == 2
    # the same break reaches the ASS: "now" opens the second speaker's group and has its colour
    groups = _groups(ctx)
    assert [t for t, _ in groups] == ["hello there everyone", "now we begin"]
    assert groups[0][1] != groups[1][1]


def test_a_clip_that_is_only_hesitations_gets_no_dialogue_and_does_not_fail(tmp_home: Path):
    ctx, report = _run(tmp_home, [(0, "um uh er")])
    assert report is not None, "the stage raised on a clip of hesitations"
    assert report["c1"]["words"] == 0
    assert _dialogue(ctx) == []


def test_the_cleaned_spelling_still_lines_up_after_the_filter(tmp_home: Path):
    cleaned = ["Sooo", "um", "Thiss", "uh", "Gamee", "iss", "likee", "reallyy", "er", "goodd"]
    assert len(cleaned) == len(SPOKEN.split())
    ctx, report = _run(tmp_home, [(0, SPOKEN)], cleaned=cleaned)
    assert report is not None, "the stage raised on a hesitation"
    pairs = zip(cleaned, SPOKEN.split(), strict=True)
    expected = [shown for shown, spoken in pairs if not select.is_filler_word(spoken)]
    assert [w.lower() for w in _dialogue(ctx)] == [w.lower() for w in expected]
    assert report["c1"]["words"] == len(expected) == 7
    assert report["c1"].get("cleaned") is True


def test_a_clip_without_hesitations_is_unchanged_with_the_setting_on(tmp_home: Path):
    plain = "So this game is like really good"
    ctx_on, rep_on = _run(tmp_home, [(0, plain)], remove=True)
    assert rep_on is not None
    on = ctx_on.file("captions/c1.ass").read_text(encoding="utf-8")
    ctx_off, rep_off = _run(tmp_home / "off", [(0, plain)], remove=False)
    assert rep_off is not None
    assert on == ctx_off.file("captions/c1.ass").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _restore_speaker_plan():
    real = sc.speaker_plan
    yield
    sc.speaker_plan = real
