"""BUILD_SPEC.md says what the Clipper does: facecam limits, caption filler words, metadata policy.

F-H125, F-H127 and F-H128 were spec text that disagreed with the code (8 % against 5 % of the frame for the
facecam, `um/uh/like` against hesitations only, a story-video `real_entities` check against links and handles
only). Each test reads the spec lines and checks them against the constant or the behaviour that decides the
result, so text and code cannot drift apart again.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ns_common.models import ClipPlanItem, Seo, Word
from ns_studio.clipper import detect as det
from ns_studio.clipper import layout as lay
from ns_studio.clipper import select
from ns_studio.clipper import seo as clip_seo
from ns_studio.clipper import stages_clip as sc
from ns_studio.clipper.models import ClipItem, ClipLayout, Rect, Transcript
from tests.helpers import make_channel, make_combo, make_ctx
from tests.test_clipper_detect import synth_frames

ROOT = Path(__file__).resolve().parents[1]
SPEC = (ROOT / "BUILD_SPEC.md").read_text(encoding="utf-8")
STATUS = (ROOT / "docs" / "FEATURE_STATUS.md").read_text(encoding="utf-8")


def _line(prefix: str) -> str:
    return next(line for line in SPEC.splitlines() if line.startswith(prefix))


def _d_rows() -> dict[int, str]:
    a = SPEC.index("## 13. Decisions")
    rows = SPEC[a : SPEC.index("## 14.", a)]
    return {int(m.group(1)): m.group(0) for m in re.finditer(r"^\| D(\d+) \|.*$", rows, re.MULTILINE)}


def _decision(title: str) -> tuple[int, str]:
    """The (number, row) of the section 13 row whose question starts with `title`."""
    found = [(n, row) for n, row in _d_rows().items() if row.startswith(f"| D{n} | {title}")]
    assert len(found) == 1, title
    return found[0]


def _cites(text: str, ident: str) -> bool:
    """True when `text` names the id as a whole word ("D3" is not found in "D31")."""
    return re.search(rf"(?<!\w){ident}(?!\w)", text) is not None


def _status_row(feature_start: str) -> list[str]:
    row = next(ln for ln in STATUS.splitlines() if ln.startswith("| ") and f"| {feature_start}" in ln[:200])
    return [c.strip() for c in row.strip().strip("|").split(" | ")]


def _box(box_id: str) -> str:
    line = next(ln for ln in STATUS.splitlines() if ln.startswith((f"- [ ] {box_id} ", f"- [x] {box_id} ")))
    assert line.startswith("- [ ] ") or " evidence: " in line, f"{box_id} is ticked without evidence"
    return line


FACECAM_QUESTION = "Facecam candidate limits"
FILLER_QUESTION = "Caption filler words in clips"
POLICY_QUESTION = "Clip metadata policy"


# ---------------------------------------------------------------------------------------------------------
# F-H125: facecam area and aspect (CR-C8-7)
# ---------------------------------------------------------------------------------------------------------
def test_facecam_text_states_the_area_and_aspect_limits_the_scan_uses():
    """19.2 C5 and 19.4 say 5-30 % of the frame and an aspect of 4:3 to 16:9 with a +-0.06 tolerance."""
    p = det.DEFAULT_FACECAM
    lo, hi = round(p.min_area_frac * 100), round(p.max_area_frac * 100)
    assert (lo, hi) == (5, 30)
    assert 4 / 3 - p.min_aspect == pytest.approx(0.06) and p.max_aspect - 16 / 9 == pytest.approx(0.06)
    c5 = _line("| C5 |")
    bullet = _line("- **Facecam:**")
    for text in (c5, bullet):
        assert f"{lo}–{hi} % of" in text and "8–30" not in text
        assert "±0.06" in text and "4:3" in text and "16:9" in text
        assert "corner" in text
    assert _cites(c5, f"D{_decision(FACECAM_QUESTION)[0]}")
    assert "CR-C8-7" in bullet


@pytest.mark.parametrize(
    ("cam", "found", "why"),
    [
        ((232, 10, 64, 36), False, "4 % of the frame is below the 5 % floor"),
        ((232, 10, 73, 41), True, "5.2 % of the frame is above the floor (the old text said 8 %)"),
        ((232, 10, 80, 45), True, "the prescribed fixture: 6.25 %, 16:9"),
        ((232, 10, 80, 75), False, "aspect 1.07 is far below 4:3"),
        ((212, 10, 100, 40), False, "aspect 2.5 is far above 16:9"),
    ],
)
def test_the_scan_accepts_and_rejects_what_the_spec_text_states(cam, found, why):
    got = det.scan_facecam(synth_frames(cam=cam), use_cv2=False)
    assert (got is not None) == found, why


# ---------------------------------------------------------------------------------------------------------
# F-H127: caption filler words in clips (17.2)
# ---------------------------------------------------------------------------------------------------------
def _text_source() -> str:
    return next(ln for ln in SPEC.splitlines() if ln.startswith("- **Text source**"))


def test_text_source_lists_the_hesitations_and_keeps_like_on_purpose():
    """17.2: `captions.remove_fillers` leaves out the hesitations of select.HESITATIONS; `like` stays."""
    line = _text_source()
    assert "um/uh/like" not in line
    assert "`captions.remove_fillers`" in line and "default off" in line
    for word in sorted(select.HESITATIONS):
        assert f"`{word}`" in line, word
    assert "`like`" in line and "never removed" in line and "never silently change meaning" in line.lower()
    assert "like" not in select.HESITATIONS and not select.is_filler_word("like")
    assert "like" in select.FILLER_TOKENS  # the C4 candidate filter counts it, and removes nothing
    assert "C4" in line and "removes no word" in line


def test_text_source_says_how_the_cleaned_clip_text_reaches_the_captions():
    """17.2 describes what the merged clip_select v4 and the captions stage do with `caption_text` (F-H95)."""
    line = _text_source()
    assert "not asked to restore" not in line and "restored by the model" not in line
    prompt = (ROOT / "ns_studio" / "prompts" / "clip_select.md").read_text(encoding="utf-8")
    assert "caption_text" in prompt and "caption_text" in line  # the model is asked, and 17.2 names the field
    version = re.search(r"^version: (\d+)$", prompt, re.MULTILINE)
    assert version is not None and f"version {version.group(1)}" in line
    assert "caption_text" in select.Cand.__dataclass_fields__
    # the sidecar: its file name is the constant the stages use, and the plan is not where it lives
    assert sc.CAPTION_WORDS_FILE == "clip_caption_words.json"
    assert f"`{sc.CAPTION_WORDS_FILE}`" in line and "`CAPTION_WORDS_FILE`" in line
    assert "not in `clip_plan.json`" in line
    assert "caption_text" not in ClipPlanItem.model_fields  # nothing to put it in: the plan has no such field
    # not a listed output: a missing file would make --resume ask the model for the selection again
    assert sc.CAPTION_WORDS_FILE not in sc.SelectStage.outputs
    assert sc.CAPTION_WORDS_FILE in sc.CaptionsStage.optional_inputs
    # the acceptance rule: ASR words stay, a model word is taken on its letters, the share is the constant
    share = round(select.MIN_CLEANED_SHARE * 100)
    assert f"{share} % of its spoken words" in line and "`MIN_CLEANED_SHARE`" in line
    for symbol in ("align_caption_text", "_display", "_is_spoken_digits"):
        assert callable(getattr(select, symbol)) and f"`{symbol}`" in line, symbol
    for banned in ("sign", "decimal point", "currency", "percent", "hash", "emoji"):
        assert banned in line, banned
    assert "stays uncleaned with the ASR spelling" in line
    assert "hesitation removal stays the captions stage's job" in line.lower()


def test_the_acceptance_rule_17_2_states_is_the_rule_align_caption_text_applies():
    """Letters must match; digits only for a spoken number; a symbol, sign, decimal or poor match: ASR."""
    spoken = "so we got twenty dollars and then five more of them to the old house".split()
    words = [Word(w=w, start=i / 3.0, end=i / 3.0 + 0.3, seg=0) for i, w in enumerate(spoken)]
    text = "So we got 20 dollars and then 5 more of them to the old house."
    got = select.align_caption_text(words, text)
    assert got is not None and got[0] == "So" and got[3] == "20" and got[-1] == "house."
    assert select._is_spoken_digits('"20",') and select._is_spoken_digits("(5)")
    assert select._display("$20") is None and select._display("20%") is None and select._display("#1") is None
    assert select._display("20" + chr(0x1F600)) == "20"  # an emoji is taken off, never shown
    assert not select._is_spoken_digits("-20") and not select._is_spoken_digits("3.5")
    dec = select.align_caption_text(words, "So we got 20 dollars and then 3.5 more of them to the old house.")
    assert dec is not None and dec[7] == "five" and dec[3] == "20"  # the decimal keeps the ASR word
    assert select.align_caption_text(words, "completely different words that matched nothing at all") is None


def test_c4_row_lists_the_filler_tokens_the_candidate_filter_counts():
    """C4: the "more than 40 %" filter names every token of select.FILLER_TOKENS, not 'um/uh/like'."""
    row = _line("| C4 |")
    assert '"um/uh/like"' not in row and "40 %" in row and select.MAX_FILLER_RATIO == 0.4
    for word in sorted(select.FILLER_TOKENS):
        assert f"`{word}`" in row, word
    assert "hmm" not in row and "hmm" not in select.FILLER_TOKENS


def test_c8_row_names_where_a_handle_is_caught():
    """C8/D41: `@x` is caught at the start of a text or after a space, not inside `(@x)` or `a@b`."""
    d41 = _decision(POLICY_QUESTION)[1]
    assert "_HANDLE_OR_URL" in d41 and "after a space" in d41
    for text, caught in (("@x", True), ("hi @x", True), ("(@x)", False), ("a@b.com", False)):
        assert bool(clip_seo._HANDLE_OR_URL.search(text)) is caught, text


def _clip_words(text: str, per_s: float = 3.0) -> tuple[list[dict], list[dict]]:
    words, t = [], 0.0
    for tok in text.split():
        words.append({"w": tok, "start": round(t, 3), "end": round(t + 0.3, 3), "seg": 0})
        t += 1 / per_s
    seg = {"i": 0, "start": 0.0, "end": t, "text": text, "speaker": 0}
    return words, [seg]


def _caption_words(tmp_home: Path, text: str, *, remove: bool) -> list[str]:
    """Run the clip CaptionsStage on `text` and return the words of the ASS dialogue lines."""
    ctx = make_ctx(
        tmp_home,
        fmt="clip",
        combo=make_combo(caption_style="clean_white"),
        channel=make_channel(captions={"remove_fillers": remove}),
    )
    words, segs = _clip_words(text)
    tr = Transcript.model_validate({"source": "v.mp4", "duration_s": 20.0, "segments": segs, "words": words})
    ctx.write_json("transcript.json", tr.model_dump(mode="json"))
    ctx.write_json(
        "source.json",
        {
            "path": str(ctx.job_dir / "missing.mp4"),
            "width": 1280,
            "height": 720,
            "duration_s": 20.0,
            "fps": 30.0,
            "source_id": "src-test",
        },
    )
    clip = ClipItem(k=1, start_s=0.0, end_s=9.0, title="t", score=0.9)
    ctx.write_json("clip_plan.json", [clip.model_dump(mode="json")])
    cam = Rect(x=944, y=16, w=320, h=180)
    base = ClipLayout(template="gaming_facecam", facecam=cam)
    crops = lay.plan_layout("gaming_facecam", 1280, 720, base, t_start=0.0, t_end=9.0)
    out = ClipLayout(template="gaming_facecam", facecam=cam, crops=crops, per_clip={"1": "gaming_facecam"})
    ctx.write_json("clip_layout.json", out.model_dump(mode="json"))
    sc.CaptionsStage().run(ctx)
    ass = ctx.file("captions/c1.ass").read_text(encoding="utf-8")
    spoken: list[str] = []
    for ln in ass.splitlines():
        if ln.startswith("Dialogue:"):
            body = re.sub(r"\{[^}]*\}", "", ln.split(",", 9)[9]).replace("\\N", " ")
            spoken.extend(re.findall(r"[A-Za-z']+", body))
    return spoken


SPOKEN = "So um this uh game is like really er good hmm and like it works"


def test_remove_fillers_drops_the_hesitations_and_keeps_like_in_the_captions(tmp_home: Path):
    on = {w.lower() for w in _caption_words(tmp_home, SPOKEN, remove=True)}
    assert "like" in on  # kept on purpose: removing it would change the meaning
    assert not on & select.HESITATIONS
    assert {"game", "really", "good", "works"} <= on


def test_the_spec_names_the_remove_fillers_failure_exactly_while_the_stage_fails(tmp_home: Path):
    """17.2 and D40 must not state the setting works while CaptionsStage raises on a hesitation (and the
    reverse: when the product is fixed this test asks for the 'Known defect' sentences to go)."""
    try:
        _caption_words(tmp_home, SPOKEN, remove=True)
        fails = False
    except ValueError as e:
        assert "one entry per word" in str(e)
        fails = True
    d40 = _decision(FILLER_QUESTION)[1]
    for text in (_text_source(), d40):
        assert ("Known defect" in text or "known defect" in text) is fails
    if fails:
        assert "fails" in _text_source() and "hesitation" in _text_source()
        assert "speaker_plan" in d40 and "CaptionsStage.run" in d40 and "stages_clip.py" in d40


def test_the_captions_keep_every_word_when_the_channel_does_not_ask(tmp_home: Path):
    off = {w.lower() for w in _caption_words(tmp_home, SPOKEN, remove=False)}
    assert {"um", "uh", "er", "hmm", "like"} <= off


def test_the_captions_of_a_clip_without_hesitations_are_the_same_with_the_setting_on(tmp_home: Path):
    plain = "So this game is like really good and it works"
    on = {w.lower() for w in _caption_words(tmp_home, plain, remove=True)}
    assert on == {w.lower() for w in plain.split()}


# ---------------------------------------------------------------------------------------------------------
# F-H128: the clip policy check real_entities (19.2 C8)
# ---------------------------------------------------------------------------------------------------------
def _seo(**kw) -> Seo:
    base = {
        "title": "A fair title",
        "description": "Plain words.\n\nClip from: x",
        "tags": ["a", "b"],
        "hashtags": ["#Shorts"],
        "keywords": [],
    }
    return Seo(**(base | kw))


def test_c8_row_says_real_entities_flags_links_and_handles_only():
    row = _line("| C8 |")
    assert "`real_entities`" in row and "**on metadata only**" in row
    for needle in ("links", "@handles", "guests", "games", "`Clip from:`"):
        assert needle in row, needle
    assert "real names" in row and "allowed" in row
    assert _cites(row, f"D{_decision(POLICY_QUESTION)[0]}")


def test_real_entities_flags_links_and_handles_and_lets_real_names_through():
    """The effect behind the C8 sentence: the check fails on a link or handle and passes on real names."""
    for bad in (
        _seo(description="see https://example.com\n\nClip from: x"),
        _seo(description="see www.example.com\n\nClip from: x"),
        _seo(description="follow @someone\n\nClip from: x"),
        _seo(tags=["@someone", "b"]),
        _seo(title="DM @someone for the run"),
    ):
        assert not clip_seo.check_real_entities(bad).passed, bad
    for good in (
        _seo(title="Elon Musk reacts to Elden Ring"),
        _seo(description="Dr. Smith plays Nintendo's new game.\n\nClip from: x"),
        _seo(hashtags=["#Shorts", "#Minecraft"]),
        _seo(description="Plain words.\n\nClip from: Interview @host https://example.com"),  # the fixed line
    ):
        assert clip_seo.check_real_entities(good).passed, good
    checks = clip_seo.check_metadata(_seo()).checks
    assert set(checks) == {"misleading_claims", "metadata_spam", "real_entities"}


# ---------------------------------------------------------------------------------------------------------
# section 13 rows and the audit file
# ---------------------------------------------------------------------------------------------------------
def test_three_new_decision_rows_follow_each_other_after_the_earlier_ones():
    ids = [_decision(q)[0] for q in (FACECAM_QUESTION, FILLER_QUESTION, POLICY_QUESTION)]
    assert ids == [ids[0], ids[0] + 1, ids[0] + 2] and ids[0] >= 34
    for q, needles in (
        (FACECAM_QUESTION, ("CR-C8-7", "5–30 %", "±0.06", "§19.2", "§19.4", "detect.py")),
        (FILLER_QUESTION, ("captions.remove_fillers", "`like`", "§17.2", "select.py")),
        (POLICY_QUESTION, ("real_entities", "links", "@handles", "guests", "§19.2", "seo.py")),
    ):
        row = _decision(q)[1]
        assert len(row.split(" | ")) == 4, q  # | # | Question | Default | Why |
        for needle in needles:
            assert needle in row, (q, needle)


def test_decision_ids_cited_in_the_clipper_text_are_the_rows_own_ids():
    n_cam, n_fill, n_pol = (_decision(q)[0] for q in (FACECAM_QUESTION, FILLER_QUESTION, POLICY_QUESTION))
    assert _cites(_line("| C5 |"), f"D{n_cam}") and _cites(_line("- **Facecam:**"), f"D{n_cam}")
    assert _cites(_text_source(), f"D{n_fill}")
    assert _cites(_line("| C8 |"), f"D{n_pol}")
    for line, others in (
        (_line("| C5 |"), (n_fill, n_pol)),
        (_text_source(), (n_cam, n_pol)),
        (_line("| C8 |"), (n_cam, n_fill)),
    ):
        assert not any(_cites(line, f"D{n}") for n in others), line[:60]


def test_audit_rows_follow_the_spec_text_and_the_boxes_stay_open():
    n_cam, n_fill, n_pol = (_decision(q)[0] for q in (FACECAM_QUESTION, FILLER_QUESTION, POLICY_QUESTION))
    for start, number in (
        ("19.4 Facecam candidate", n_cam),
        ("C8 policy check real_entities", n_pol),
    ):
        cells = _status_row(start)
        assert cells[0] == "built", start
        assert f"13 D{number}" in cells[2], start
        assert cells[4] == "", start  # nothing missing
    # the text matches the code and the setting captions a clip with a hesitation (D-38 is fixed)
    cells = _status_row("C6 optional hesitation removal")
    assert cells[0] == "built" and f"13 D{n_fill}" in cells[2]
    assert cells[4] == "" and "stages_clip.py" in cells[3] and "test_clip_captions_fillers.py" in cells[5]
    for line in (_status_row("Captions 17.2: text source")[4], _box("F-H95")):
        assert "), (" not in line and "restor" in line and f"D{n_fill}" in line  # no dangling comma
    for box_id, number in (("F-H125", n_cam), ("F-H127", n_fill), ("F-H128", n_pol)):
        assert f"Decision: section 13 D{number}." in _box(box_id), box_id  # QA ticks with evidence
