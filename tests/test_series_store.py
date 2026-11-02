from pathlib import Path

import pytest
from pydantic import ValidationError

from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_studio.series import store as S

CH1 = (
    "The camera pings at three every night.\n\nI work nights at the hospital. My phone stays loud.\n\n"
    "Tonight the porch was wet."
)
CH2 = "She called her neighbor. He did not answer! Was he asleep?\n\nThe light in his hall was on."
CH3 = "The recording ended. Nothing moved."


def script_doc() -> S.ScriptDoc:
    return S.ScriptDoc(
        part=2,
        title="Wet Footprints",
        critic_avg=8.456,
        chapters=[
            S.ScriptChapter(
                n=1, title="The Porch", text=CH1, summary="Wet porch.", beats=["a", "b"], cliff="c"
            ),
            S.ScriptChapter(n=2, title="The Hall Light", text=CH2),
            S.ScriptChapter(n=3, title="Tomorrow", text=CH3),
        ],
    )


def test_front_matter_round_trip_and_crlf():
    text = S.join_front_matter({"a": 1, "span": [3, 9], "name": "x: y"}, "\nbody line\n")
    meta, body = S.split_front_matter(text)
    assert meta == {"a": 1, "span": [3, 9], "name": "x: y"} and body.strip() == "body line"
    assert "span: [3, 9]" in text
    meta2, body2 = S.split_front_matter(text.replace("\n", "\r\n"))
    assert meta2 == meta and body2.strip() == "body line"
    assert S.split_front_matter("no front matter\n") == ({}, "no front matter\n")


def test_front_matter_must_be_a_mapping_and_valid_yaml():
    with pytest.raises(S.WorkshopError):
        S.split_front_matter("---\n- a\n- b\n---\nbody\n")
    with pytest.raises(S.WorkshopError):
        S.split_front_matter("---\na: [unclosed\n---\nbody\n")


def test_write_text_is_lf_and_atomic(tmp_path: Path):
    p = tmp_path / "x" / "y.md"
    S.write_text(p, "a\r\nb\n")
    assert p.read_bytes() == b"a\nb\n"
    assert not (tmp_path / "x" / "y.md.tmp").exists()


def test_sections_and_bullets():
    body = "# T\n\n## One\n\ntext a\n\n## Two\n\n- x\n- y\n3. z\nplain\n"
    sec = S.parse_sections(body)
    assert list(sec) == ["One", "Two"] and sec["One"] == "text a"
    assert S.bullets(sec["Two"]) == ["x", "y", "z"]
    assert S.parse_sections(S.render_sections("T", sec)) == sec


def test_concept_round_trip():
    doc = S.ConceptDoc(
        letter="B",
        title="The Last Ferry Light",
        premise="A pilot sees names.",
        cast=["Ines: pilot", "Dov: keeper"],
        setting="Island",
        tone="Eerie",
        beats=["one", "two", "three", "four", "five"],
        why_it_works="Puzzle.",
        series_potential="Many parts.",
        audience="Mystery fans.",
        risks=["Fog"],
        feedback=["warmer"],
        revision=2,
    )
    assert S.parse_concept(S.render_concept(doc)) == doc


def test_concept_skeleton_and_hand_written_file():
    skel = S.parse_concept(S.render_concept(S.ConceptDoc(title="Untitled", premise="An idea.")))
    assert skel.premise == "An idea." and skel.cast == [] and skel.beats == []
    hand = S.parse_concept("# My Story\n\n## premise\n\nA hand-written premise.\n\n## Cast\n\n* Ann: lead\n")
    assert hand.title == "My Story" and hand.premise == "A hand-written premise."
    assert hand.cast == ["Ann: lead"]


def test_part_entry_enforces_12_to_25_minutes():
    base = {"n": 1, "title": "t", "hook": "h", "cliff": "c"}
    assert S.PartEntry(**base, minutes=12).minutes == 12 and S.PartEntry(**base, minutes=25).minutes == 25
    for bad in (11, 26, 0):
        with pytest.raises(ValidationError):
            S.PartEntry(**base, minutes=bad)


def test_series_round_trip_ignores_generated_body():
    doc = S.SeriesDoc(
        title="The Doorbell Camera",
        channel="after-dark",
        minutes_per_part=15,
        arc_payoff="It pays off.",
        warnings=["Part 2 may not stand alone"],
        parts=[
            S.PartEntry(n=1, title="A", hook="h1", cliff="c1", reveals="r1", minutes=15),
            S.PartEntry(n=2, title="B", hook="h2", cliff="c2", minutes=20, recap="Recap.", status="written"),
        ],
    )
    text = S.render_series(doc)
    assert "parts_total: 2" in text and "## Part 2: B (20 min, written)" in text
    assert S.parse_series(text) == doc
    assert doc.entry(2).title == "B"
    with pytest.raises(S.WorkshopError):
        doc.entry(3)
    with pytest.raises(S.WorkshopError):
        S.parse_series("---\ntitle: x\n---\n")


def test_status_recompute_and_yaml_round_trip(tmp_home: Path):
    sd = S.StoryDir.open(Paths.from_env(), "demo", must_exist=False)
    st = S.StatusDoc(slug="demo", channel="after-dark", parts={1: S.PartState(), 2: S.PartState()})
    st.recompute()
    assert st.status == "drafting"
    st.parts[1].status = st.parts[2].status = "approved"
    st.recompute()
    assert st.status == "approved"
    st.parts[2].status = "producing"
    st.recompute()
    assert st.status == "producing"
    st.parts[1].status = st.parts[2].status = "published"
    st.recompute()
    assert st.status == "published"
    sd.root.mkdir(parents=True)
    sd.write_status(st)
    assert sd.read_status() == st and sorted(sd.read_status().parts) == [1, 2]


def test_design_defaults_and_cadence_bounds(tmp_home: Path):
    d = S.DesignDoc()
    assert d.format == "long_16x9" and d.publish_cadence_days == 7 and d.voice is None  # no pick: the channel voice
    with pytest.raises(ValidationError):
        S.DesignDoc(publish_cadence_days=0)
    sd = S.StoryDir.open(Paths.from_env(), "demo", must_exist=False)
    assert sd.read_design() is None
    sd.write_design(d.model_copy(update={"caption_style": "boxed_dark"}))
    assert sd.read_design() == d.model_copy(update={"caption_style": "boxed_dark"})


def test_normalize_text_adds_terminal_punctuation():
    assert S.normalize_text("One line\nstill one.\n\n\n  Second   paragraph") == (
        "One line still one.\n\nSecond paragraph."
    )
    assert S.normalize_text('He said "go."') == 'He said "go."'


def test_script_render_numbers_sentences_and_marks_breaks():
    text = S.render_script(script_doc())
    meta, body = S.split_front_matter(text)
    assert meta["part"] == 2 and meta["critic_avg"] == 8.46 and meta["chapters"][0]["cliff"] == "c"
    assert body.count(S.BREAK) == 2 and body.rstrip().endswith("Nothing moved.")
    assert "## 1. The Porch" in body and "## 3. Tomorrow" in body
    n_sentences = len(script_doc().sentences())
    assert n_sentences == 10
    for i in range(1, n_sentences + 1):
        assert f"<!-- s{i} -->" in body
    assert f"<!-- s{n_sentences + 1} -->" not in body
    assert "<!-- s1 -->The camera pings at three every night." in body
    # break sits between a chapter's last sentence and the next heading
    assert "Tonight the porch was wet.\n\n[BREAK]\n\n## 2. The Hall Light" in body


def test_script_round_trip_keeps_text_and_outline():
    doc = script_doc()
    back = S.parse_script(S.render_script(doc))
    assert [c.text for c in back.chapters] == [S.normalize_text(c.text) for c in doc.chapters]
    assert back.part == 2 and back.title == "Wet Footprints" and back.critic_avg == 8.46
    assert back.chapters[0].beats == ["a", "b"] and back.chapters[0].summary == "Wet porch."
    assert [t for _, _, t in back.sentences()] == [t for _, _, t in doc.sentences()]
    # chapters without front-matter facts get defaults from the text
    assert back.chapters[2].cliff == "Nothing moved." and back.chapters[2].beats == ["The recording ended."]


def test_parse_script_accepts_hand_edits():
    raw = (
        "## Chapter 1: Start\n\n<!-- note to self -->Hello there.   It is late.\n\n[BREAK]\n\n"
        "## 2. Next\n\n<!-- s1 -->A stale number.\nSecond line without punctuation\n"
    )
    doc = S.parse_script(raw)
    assert [c.title for c in doc.chapters] == ["Start", "Next"]
    assert doc.chapters[0].text == "Hello there. It is late."
    assert doc.chapters[1].text == "A stale number. Second line without punctuation."
    assert doc.part == 1 and doc.title == "Untitled"


def test_parse_script_errors():
    with pytest.raises(NsError) as e1:
        S.parse_script("Just text, no headings.\n")
    assert e1.value.code == "NS-E303"
    with pytest.raises(NsError) as e2:
        S.parse_script("## 1. A\n\nText.\n\n## 3. C\n\nText.\n")
    assert e2.value.code == "NS-E303"
    with pytest.raises(NsError):
        S.parse_script("## 1. A\n\n[BREAK]\n\n## 2. B\n\nText.\n")


def test_short_round_trip_and_resolve_span():
    doc = S.ShortDoc(
        k=3,
        hook="I came home to wet footprints.",
        span=[4, 6],
        closing_line="The full story is on the channel.",
        length_bucket=35,
        chapter=2,
        why_standalone="Complete moment.",
        hooks=["I came home to wet footprints.", "Another hook."],
        start_text="The light in his hall",
        text="The light in his hall was on. More.",
    )
    text = S.render_short(doc)
    assert "span: [4, 6]" in text and "Hook: I came home" in text and "Closing: The full story" in text
    back = S.parse_short(text)
    assert back.model_copy(update={"text": doc.text}) == doc
    sentences = ["a one.", "b two.", "c three.", "The light in his hall was on.", "e.", "f.", "g."]
    assert S.resolve_span(doc, sentences) == (3, 5)
    # an inserted sentence moves the moment; the anchor finds it again and keeps the length
    shifted = ["x new.", *sentences]
    assert S.resolve_span(doc, shifted) == (4, 6)
    # the start sentence was edited: trust the numbers while they fit
    edited = [*sentences[:3], "Edited start.", *sentences[4:]]
    assert S.resolve_span(doc, edited) == (3, 5)
    with pytest.raises(NsError) as err:
        S.resolve_span(doc.model_copy(update={"span": [40, 50], "start_text": ""}), sentences)
    assert err.value.code == "NS-E303"


def test_parse_short_rejects_invalid_files():
    with pytest.raises(NsError) as err:
        S.parse_short("---\nk: 1\nhook: only a hook\n---\nbody\n")
    assert err.value.code == "NS-E303"


def test_story_dir_layout_matches_the_spec(tmp_home: Path):
    paths = Paths.from_env()
    sd = S.StoryDir.open(paths, "demo", must_exist=False)
    rel = lambda p: p.relative_to(sd.root).as_posix()  # noqa: E731
    assert rel(sd.concept) == "concept.md" and rel(sd.draft("b")) == "concept-B.md"
    assert rel(sd.series) == "series.md" and rel(sd.design) == "design.yaml"
    assert rel(sd.status) == "status.yaml"
    assert rel(sd.script(3)) == "parts/part-03/full/script.md"
    assert rel(sd.short_file(3, 7)) == "parts/part-03/shorts/s07.md"
    assert rel(sd.notes(12)) == "parts/part-12/notes.md"
    assert sd.root == paths.home / "stories" / "demo"
    assert S.list_slugs(paths) == []


def test_story_dir_open_validates_slug_and_existence(tmp_home: Path):
    paths = Paths.from_env()
    for bad in ("Bad Slug", "../x", ""):
        with pytest.raises(S.WorkshopError):
            S.StoryDir.open(paths, bad, must_exist=False)
    with pytest.raises(S.WorkshopError, match="no story"):
        S.StoryDir.open(paths, "missing")


def test_story_dir_typed_files(tmp_home: Path):
    paths = Paths.from_env()
    sd = S.StoryDir.open(paths, "demo", must_exist=False)
    sd.write_script(1, script_doc())
    assert sd.read_script(1).chapters[1].title == "The Hall Light"
    for k in (2, 1):
        sd.write_short(
            1,
            S.ShortDoc(k=k, hook=f"Hook {k}.", span=[1, 2], closing_line="Closing.", length_bucket=35),
        )
    assert [s.k for s in sd.read_shorts(1)] == [1, 2]
    sd.clear_shorts(1)
    assert sd.read_shorts(1) == []
    sd.write_notes(1, {"Critic scores": "- ok", "Owner feedback": "- more"})
    assert sd.read_notes(1) == {"Critic scores": "- ok", "Owner feedback": "- more"}
    assert S.list_slugs(paths) == ["demo"] and sd.draft_letters() == []
    with pytest.raises(S.WorkshopError, match="no script"):
        sd.read_script(9)
