import json
import re
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ns_common.config import save_channel
from ns_common.db import Db
from ns_common.errors import NsError
from ns_common.llm_client import FakeLlm
from ns_common.logging import NsLogger
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_common.textstats import split_sentences
from ns_studio.context import FAKEABLE, FIXTURES_LLM, build_services
from ns_studio.series import workshop as W
from ns_studio.series.store import (
    PartEntry,
    ScriptDoc,
    StoryDir,
    WorkshopError,
    anchor_of,
    list_slugs,
)
from ns_studio.text.validators import load_banned
from tests.helpers import make_channel

PREMISE = "A night nurse's doorbell camera shows a hooded figure"
RUBRIC = ("hook", "clarity", "pacing", "payoff", "cliffhangers", "originality", "voice_fit")
BAD_CRITIC = {"scores": {k: 5 for k in RUBRIC}, "fixes": ["Cut the filler."]}
GOOD_CRITIC = {"scores": {k: 9 for k in RUBRIC}, "fixes": []}
UNSAFE = {
    "unsafe_categories": ["graphic_harm"],
    "hook_honest": True,
    "title_honest": True,
    "standalone": True,
    "notes": "Explicit gore.",
}


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_LLM / f"{name}.json").read_text(encoding="utf-8"))


class TextLlm(FakeLlm):
    """FakeLlm with scripted plain-text answers for the revise prompts."""

    def __init__(self, *args: Any, edits: dict[int, Callable[[str], str]] | None = None, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.edits = edits or {}

    def complete_text(self, *, prompt_name: str, system: str, user: str, thinking: bool = False,
                      temperature: float = 0.8, max_tokens: int = 2048) -> str:
        if prompt_name == "workshop_revise":
            self._record(prompt_name=prompt_name, system=system, user=user, thinking=thinking)
            n = int(re.search(r"Chapter (\d+): ", user).group(1))  # type: ignore[union-attr]
            text = user.split("Text:\n", 1)[1].strip()
            return self.edits[n](text) if n in self.edits else "UNCHANGED"
        if prompt_name == "workshop_revise_fix":
            self._record(prompt_name=prompt_name, system=system, user=user, thinking=thinking)
            return user.split("Text:\n", 1)[1].strip() + " One more sentence."
        return super().complete_text(prompt_name=prompt_name, system=system, user=user, thinking=thinking)


def make_workshop(
    tmp_home: Path, *, llm: FakeLlm | None = None, enqueue: W.EnqueueFn | None = None, **channel_kw: Any
) -> W.Workshop:
    paths = Paths.from_env()
    paths.ensure()
    runtime = Runtime.default("cpu-dev")
    services = build_services(runtime, fake=set(FAKEABLE), paths=paths)
    if llm is not None:
        services.llm = llm
    db = Db(paths.db_path)
    db.migrate()
    save_channel(paths, make_channel(format="master_first", **channel_kw))
    return W.Workshop(paths, services, runtime, db, NsLogger("studio", paths.logs, echo=False), enqueue)


def built(ws: W.Workshop, write: tuple[int, ...] = (1,)) -> str:
    slug = ws.start("after-dark", PREMISE)
    ws.concepts(slug)
    ws.choose(slug, "A")
    ws.map_parts(slug, 3, 12)
    for n in write:
        ws.write_part(slug, n)
    return slug


def calls_of(ws: W.Workshop, name: str) -> list[dict[str, Any]]:
    return [c for c in ws.services.llm.calls if c["prompt_name"] == name]  # type: ignore[attr-defined]


def story_files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


# --- helpers ---------------------------------------------------------------------------------------


def test_slugify_and_chapter_count():
    assert W.slugify("A night nurse's doorbell camera shows") == "a-night-nurse-s-doorbell"
    assert W.slugify("!!!") == "story" and W.slugify("") == "story"
    assert [W.chapter_count(m * 155) for m in (12, 15, 20, 25)] == [5, 7, 9, 11]
    assert W.chapter_count(10) == 5 and W.chapter_count(10**6) == 12


def test_design_options_follow_formats_allowed():
    channel = SimpleNamespace(formats_allowed=["long_16x9"])
    opts = W.design_options(channel)  # type: ignore[arg-type]
    assert opts["format"] == ["long_16x9"]
    assert "dark_slowzoom" in opts["visual_treatment"] and "clean_white" in opts["caption_style"]
    assert W.design_options(make_channel())["format"] == W.available_formats()


# --- steps 1-3 -------------------------------------------------------------------------------------


def test_full_run_produces_the_folder_tree_of_the_spec(tmp_home: Path):
    ws = make_workshop(tmp_home)
    slug = ws.start("after-dark", PREMISE)
    root = tmp_home / "stories" / slug
    assert slug == "a-night-nurse-s-doorbell" and story_files(root) == ["concept.md", "status.yaml"]
    assert ws.concepts(slug) == ["A", "B", "C"]
    assert ws.choose(slug, "A") == "The Doorbell Camera"
    series = ws.map_parts(slug, 3, 12)
    assert [p.title for p in series.parts] == ["Wet Footprints", "Her Own Handwriting", "The Porch"]
    summary = ws.write_part(slug, 1)

    files = story_files(root)
    shorts = [f for f in files if "/shorts/" in f]
    assert len(shorts) >= 4
    assert shorts == [f"parts/part-01/shorts/s{k:02d}.md" for k in range(1, len(shorts) + 1)]
    assert [f for f in files if "/shorts/" not in f] == [
        "concept-A.md",
        "concept-B.md",
        "concept-C.md",
        "concept.md",
        "parts/part-01/full/script.md",
        "parts/part-01/notes.md",
        "series.md",
        "status.yaml",
    ]
    assert summary["script"] == str(root / "parts" / "part-01" / "full" / "script.md")
    assert summary["chapters"] == 5 and summary["words"] >= 1400
    assert not list(tmp_home.rglob("*.tmp"))
    assert StoryDir.open(ws.paths, slug).read_concept().title == "The Doorbell Camera"
    assert ws.status(slug)["chosen"] == "A"


def test_write_part_script_has_breaks_numbering_and_review_notes(tmp_home: Path):
    ws = make_workshop(tmp_home)
    slug = built(ws)
    sd = StoryDir.open(ws.paths, slug)
    raw = sd.script(1).read_text(encoding="utf-8")
    assert raw.count("[BREAK]") == 4
    script = sd.read_script(1)
    assert [c.title for c in script.chapters] == ["Wet Footprints", "The Hall Light", "Tomorrow's Date",
                                                   "White Gauze", "The Porch"]  # fmt: skip
    total = len(script.sentences())
    numbers = [int(x) for x in re.findall(r"<!-- s(\d+) -->", raw)]
    assert numbers == list(range(1, total + 1))
    assert script.chapters[0].text.startswith("The doorbell camera pings at 3:12 every night.")
    assert script.chapters[0].cliff.startswith("The footprints cross the porch")
    notes = sd.read_notes(1)
    assert list(notes) == ["Critic scores", "Continuity", "Validator warnings", "Shorts", "Owner feedback"]
    assert notes["Critic scores"].count("- Chapter") == 5 and "avg 8.6" in notes["Critic scores"]
    assert "no contradictions" in notes["Continuity"]
    assert 8.5 < script.critic_avg < 8.7  # type: ignore[operator]
    st = sd.read_status()
    assert st.parts[1].status == "written" and st.parts[1].words == script.words()
    assert st.parts[2].status == "planned" and sd.read_series().entry(1).status == "written"
    prompts = {c["prompt_name"] for c in ws.services.llm.calls}  # type: ignore[attr-defined]
    assert {"workshop_part_outline", "chapter_draft_1", "critic", "continuity", "shorts_select"} <= prompts


def test_write_part_shorts_are_standalone_files_with_distinct_hooks(tmp_home: Path):
    ws = make_workshop(tmp_home)
    slug = built(ws)
    sd = StoryDir.open(ws.paths, slug)
    shorts = sd.read_shorts(1)
    flat = [t for _, _, t in sd.read_script(1).sentences()]
    assert len(shorts) >= 4
    hooks = [s.hook.lower() for s in shorts]
    assert len(set(hooks)) == len(hooks)
    for s in shorts:
        assert 0 < len(s.hook.split()) <= 12 and 0 < len(s.closing_line.split()) <= 15
        a, b = s.span
        assert 1 <= a <= b <= len(flat) and s.start_text == anchor_of(flat[a - 1])
        assert s.length_bucket in (35, 50, 75) and s.chapter and s.hooks[0] == s.hook
        assert s.why_standalone
    assert [s.chapter for s in shorts] == sorted(s.chapter for s in shorts if s.chapter)
    first = sd.short_file(1, 1).read_text(encoding="utf-8")
    assert first.startswith("---\nk: 1\nhook:") and "span: [" in first and "closing_line:" in first


def test_write_part_uses_the_part_runtime_budget(tmp_home: Path):
    ws = make_workshop(tmp_home)
    built(ws)
    outline = calls_of(ws, "workshop_part_outline")[0]
    assert "1860 spoken words at 155 words per minute" in outline["user"]
    assert "in about 5 chapters of roughly 372 words" in outline["user"]
    draft = calls_of(ws, "chapter_draft_2")[0]
    assert "Chapter 1, Wet Footprints" in draft["user"] and "part 1 of 3" in draft["user"]


def test_later_parts_get_the_earlier_parts_as_context(tmp_home: Path):
    ws = make_workshop(tmp_home)
    slug = built(ws, write=(1, 2))
    calls = calls_of(ws, "workshop_part_outline")
    assert len(calls) == 2 and "Part 1, Wet Footprints" not in calls[0]["user"]
    assert "Part 1, Wet Footprints" in calls[1]["user"] and "Her Own Handwriting" in calls[1]["user"]
    sd = StoryDir.open(ws.paths, slug)
    assert sd.read_script(2).title == "Her Own Handwriting"


def test_concepts_replace_the_previous_drafts(tmp_home: Path):
    ws = make_workshop(tmp_home)
    slug = ws.start("after-dark")
    assert slug == "after-dark-story" and ws.start("after-dark") == "after-dark-story-2"
    assert ws.concepts(slug) == ["A", "B", "C"]
    assert ws.concepts(slug, n=2) == ["A", "B"]
    sd = StoryDir.open(ws.paths, slug)
    assert sd.draft_letters() == ["A", "B"]
    avoid = calls_of(ws, "workshop_concepts")[-1]
    assert "- The Doorbell Camera" in avoid["user"] and "exactly 2 items" in avoid["user"]
    for bad in (0, 7):
        with pytest.raises(WorkshopError):
            ws.concepts(slug, bad)


def test_regenerate_replaces_exactly_one_draft(tmp_home: Path):
    ws = make_workshop(tmp_home)
    slug = ws.start("after-dark", PREMISE)
    ws.concepts(slug)
    sd = StoryDir.open(ws.paths, slug)
    before = {x: sd.draft(x).read_bytes() for x in "ABC"}
    assert ws.regenerate(slug, "b", "Make the tone warmer.") == "B"
    after = {x: sd.draft(x).read_bytes() for x in "ABC"}
    assert after["A"] == before["A"] and after["C"] == before["C"] and after["B"] != before["B"]
    doc = sd.read_concept(sd.draft("B"))
    assert doc.revision == 1 and doc.feedback == ["Make the tone warmer."] and "Eerie but hopeful" == doc.tone
    prompt = calls_of(ws, "workshop_regenerate")[0]
    assert "Make the tone warmer." in prompt["user"] and "Ines Moreau" in prompt["user"]
    with pytest.raises(WorkshopError, match="no draft Z"):
        ws.regenerate(slug, "Z", "x")
    with pytest.raises(WorkshopError, match="feedback is empty"):
        ws.regenerate(slug, "A", "  ")


def test_choose_promotes_the_draft_and_records_it(tmp_home: Path):
    ws = make_workshop(tmp_home)
    slug = ws.start("after-dark", PREMISE)
    ws.concepts(slug)
    assert ws.choose(slug, "b") == "The Last Ferry Light"
    sd = StoryDir.open(ws.paths, slug)
    assert sd.read_concept().title == "The Last Ferry Light" and sd.read_concept().letter == "B"
    assert sd.read_status().chosen == "B"
    with pytest.raises(WorkshopError, match="no draft"):
        ws.choose(slug, "F")


def test_map_enforces_the_runtime_and_parts_bounds(tmp_home: Path):
    ws = make_workshop(tmp_home)
    slug = ws.start("after-dark", PREMISE)
    ws.concepts(slug)
    with pytest.raises(WorkshopError, match="choose a concept first"):
        ws.map_parts(slug, 3, 12)
    ws.choose(slug, "A")
    for minutes in (11, 26, 0):
        with pytest.raises(WorkshopError, match="12-25"):
            ws.map_parts(slug, 3, minutes)
    for parts in (0, 41):
        with pytest.raises(WorkshopError, match="parts must be"):
            ws.map_parts(slug, parts, 15)
    with pytest.raises(NsError) as err:
        ws.map_parts(slug, 4, 15)  # the fixture has 3 parts
    assert err.value.code == "NS-E302"
    assert not StoryDir.open(ws.paths, slug).series.exists()
    doc = ws.map_parts(slug, 3, 25)
    assert doc.minutes_per_part == 25 and [p.minutes for p in doc.parts] == [12, 12, 12]
    with pytest.raises(ValueError):
        PartEntry(n=1, title="t", hook="h", cliff="c", minutes=26)


def test_map_replaces_out_of_range_model_minutes_and_lists_standalone_problems(tmp_home: Path):
    data = fixture("workshop_map")
    data["parts"][1]["minutes"] = 40
    data["parts"][1]["standalone"] = False
    data["issues"] = ["The payoff is thin."]
    ws = make_workshop(tmp_home, llm=FakeLlm(FIXTURES_LLM, overrides={"workshop_map": data}))
    slug = ws.start("after-dark", PREMISE)
    ws.concepts(slug)
    ws.choose(slug, "A")
    doc = ws.map_parts(slug, 3, 18)
    assert [p.minutes for p in doc.parts] == [12, 18, 12]
    assert doc.warnings == [
        "Part 2 may not stand alone for a new viewer; sharpen its recap",
        "The payoff is thin.",
    ]
    assert doc.parts[0].recap == "" and doc.arc_payoff.startswith("Part 3 shows")
    text = StoryDir.open(ws.paths, slug).series.read_text(encoding="utf-8")
    assert "## Needs attention" in text and "The payoff is thin." in text
    ws.write_part(slug, 1)
    with pytest.raises(WorkshopError, match="already written"):
        ws.map_parts(slug, 3, 18)


# --- policy gate -----------------------------------------------------------------------------------


def test_concept_with_a_real_person_is_rejected_before_anything_is_written(tmp_home: Path):
    llm = FakeLlm(FIXTURES_LLM, overrides={"workshop_concepts": fixture("workshop_concepts_2")})
    ws = make_workshop(tmp_home, llm=llm)
    slug = ws.start("after-dark", PREMISE)
    with pytest.raises(NsError) as err:
        ws.concepts(slug)
    assert err.value.code == "NS-E602" and "taylor swift" in err.value.detail
    sd = StoryDir.open(ws.paths, slug)
    assert sd.draft_letters() == [] and story_files(sd.root) == ["concept.md", "status.yaml"]


def test_unsafe_premise_creates_no_folder(tmp_home: Path):
    ws = make_workshop(tmp_home)
    with pytest.raises(NsError) as err:
        ws.start("after-dark", "Taylor Swift is hunted by a doorbell camera")
    assert err.value.code == "NS-E602" and list_slugs(ws.paths) == []
    ws.services.llm = FakeLlm(FIXTURES_LLM, overrides={"policy_check": UNSAFE})
    with pytest.raises(NsError) as err2:
        ws.start("after-dark", PREMISE)
    assert err2.value.code == "NS-E601" and list_slugs(ws.paths) == []


def test_misleading_claim_in_the_map_is_rejected(tmp_home: Path):
    data = fixture("workshop_map")
    data["parts"][0]["title"] = "A True Story"
    ws = make_workshop(tmp_home, llm=FakeLlm(FIXTURES_LLM, overrides={"workshop_map": data}))
    slug = ws.start("after-dark", PREMISE)
    ws.concepts(slug)
    ws.choose(slug, "A")
    with pytest.raises(NsError) as err:
        ws.map_parts(slug, 3, 12)
    assert err.value.code == "NS-E603" and not StoryDir.open(ws.paths, slug).series.exists()


def test_unsafe_script_is_rejected_and_nothing_is_written(tmp_home: Path):
    ws = make_workshop(tmp_home)
    slug = built(ws, write=())
    ws.services.llm = FakeLlm(FIXTURES_LLM, overrides={"policy_check": UNSAFE})
    with pytest.raises(NsError) as err:
        ws.write_part(slug, 1)
    assert err.value.code == "NS-E601"
    sd = StoryDir.open(ws.paths, slug)
    assert not sd.script(1).exists() and not sd.shorts_dir(1).exists()
    assert sd.read_status().parts[1].status == "planned"


def test_banned_phrase_in_a_drafted_chapter_fails_validation(tmp_home: Path):
    banned = load_banned()[0]
    chapter = fixture("chapter_draft_1")
    chapter["text"] += f"\n\nShe said {banned} and left."
    ws = make_workshop(tmp_home, llm=FakeLlm(FIXTURES_LLM, overrides={"chapter_draft_1": chapter}))
    slug = built(ws, write=())
    with pytest.raises(NsError) as err:
        ws.write_part(slug, 1)
    assert err.value.code == "NS-E303" and "chapter 1" in err.value.detail
    assert "banned_phrase" in err.value.detail


def test_too_short_part_is_rejected(tmp_home: Path):
    short = {"text": "The camera pings at three. Nobody comes.", "summary": "Pings."}
    overrides = {f"chapter_draft_{n}": short for n in range(1, 6)}
    ws = make_workshop(tmp_home, llm=FakeLlm(FIXTURES_LLM, overrides=overrides))
    slug = built(ws, write=())
    with pytest.raises(NsError) as err:
        ws.write_part(slug, 1)
    assert err.value.code == "NS-E303" and "target is 1860" in err.value.detail


# --- critic loop -----------------------------------------------------------------------------------


def test_chapters_below_the_bar_are_revised_once_and_noted(tmp_home: Path):
    llm = TextLlm(FIXTURES_LLM, overrides={"critic": [BAD_CRITIC, GOOD_CRITIC]})
    ws = make_workshop(tmp_home, llm=llm)
    slug = built(ws)
    revise = [c for c in llm.calls if c["prompt_name"] == "workshop_revise_fix"]
    assert len(revise) == 5 and "- Cut the filler." in revise[0]["user"]
    sd = StoryDir.open(ws.paths, slug)
    assert all(c.text.endswith("One more sentence.") for c in sd.read_script(1).chapters)
    assert sd.read_notes(1)["Critic scores"].count("1 revision(s)") == 5


def test_chapters_that_never_reach_the_bar_are_flagged_not_dropped(tmp_home: Path):
    llm = TextLlm(FIXTURES_LLM, overrides={"critic": BAD_CRITIC})
    ws = make_workshop(tmp_home, llm=llm)
    slug = built(ws)
    assert len([c for c in llm.calls if c["prompt_name"] == "workshop_revise_fix"]) == 10  # two per chapter
    sd = StoryDir.open(ws.paths, slug)
    warnings = sd.read_notes(1)["Validator warnings"]
    assert warnings.count("critic below the bar after 2 revisions") == 5
    assert sd.read_status().parts[1].status == "written"


def test_continuity_issues_trigger_one_revise_of_the_affected_chapter(tmp_home: Path):
    found = {"chapter": 3, "problem": "Handwriting is unclear.", "fix": "Say careful."}
    issue = {"ok": False, "issues": [found]}
    llm = TextLlm(FIXTURES_LLM, overrides={"continuity": [issue, *[{"ok": True, "issues": []}] * 3]})
    ws = make_workshop(tmp_home, llm=llm)
    slug = built(ws)
    fixes = [c for c in llm.calls if c["prompt_name"] == "workshop_revise_fix"]
    assert len(fixes) == 1 and "- Say careful." in fixes[0]["user"]
    notes = StoryDir.open(ws.paths, slug).read_notes(1)
    assert "Chapter 3: Handwriting is unclear. Fix: Say careful." in notes["Continuity"]


def test_write_part_errors(tmp_home: Path):
    ws = make_workshop(tmp_home)
    slug = ws.start("after-dark", PREMISE)
    with pytest.raises(WorkshopError, match="no series.md"):
        ws.write_part(slug, 1)
    ws.concepts(slug)
    ws.choose(slug, "A")
    ws.map_parts(slug, 3, 12)
    with pytest.raises(WorkshopError, match="no part 9"):
        ws.write_part(slug, 9)
    with pytest.raises(WorkshopError, match="no story"):
        ws.write_part("nope", 1)
    with pytest.raises(WorkshopError, match="unknown channel"):
        ws.start("missing-channel")


# --- revise ----------------------------------------------------------------------------------------


def add_cold_sentence(text: str) -> str:
    first = split_sentences(text)[0]
    return text.replace(first, first + " It was cold.", 1)


def test_revise_part_changes_only_the_noted_chapter_and_moves_the_shorts(tmp_home: Path):
    llm = TextLlm(FIXTURES_LLM, edits={2: add_cold_sentence})
    ws = make_workshop(tmp_home, llm=llm)
    slug = built(ws)
    sd = StoryDir.open(ws.paths, slug)
    ws.approve(slug, 1)
    old_script, old_shorts = sd.read_script(1), sd.read_shorts(1)
    old_flat = [t for _, _, t in old_script.sentences()]

    out = ws.revise_part(slug, 1, "Add a cold detail to chapter two.")
    assert out == {"slug": slug, "part": 1, "changed_chapters": [2]}
    new_script, new_shorts = sd.read_script(1), sd.read_shorts(1)
    unchanged = [c.text for c in old_script.chapters if c.n != 2]
    assert [c.text for c in new_script.chapters if c.n != 2] == unchanged
    assert "It was cold." in new_script.chapters[1].text and "It was cold." not in old_script.chapters[1].text
    assert len(calls_of(ws, "workshop_revise")) == 5
    new_flat = [t for _, _, t in new_script.sentences()]
    assert len(new_flat) == len(old_flat) + 1
    assert len(old_shorts) == len(new_shorts)
    for before, after in zip(old_shorts, new_shorts, strict=True):
        # the new sentence sits after the first sentence of chapter 2: later moments move by one
        shift = 1 if (before.chapter or 0) >= 2 else 0
        assert after.span[0] == before.span[0] + shift and after.hook == before.hook
        assert after.start_text == before.start_text and after.chapter == before.chapter
        assert anchor_of(new_flat[after.span[0] - 1]) == after.start_text
    notes = sd.read_notes(1)
    assert "Add a cold detail to chapter two. (chapters changed: 2)" in notes["Owner feedback"]
    assert sd.read_status().parts[1].status == "written"  # needs approving again
    assert sd.read_series().entry(1).status == "written"


def test_revise_part_without_matching_notes_changes_nothing(tmp_home: Path):
    ws = make_workshop(tmp_home, llm=TextLlm(FIXTURES_LLM))
    slug = built(ws)
    sd = StoryDir.open(ws.paths, slug)
    before = sd.script(1).read_bytes()
    shorts_before = [sd.short_file(1, s.k).read_bytes() for s in sd.read_shorts(1)]
    assert ws.revise_part(slug, 1, "Nothing applies.")["changed_chapters"] == []
    assert sd.script(1).read_bytes() == before
    assert [sd.short_file(1, s.k).read_bytes() for s in sd.read_shorts(1)] == shorts_before
    assert "Nothing applies." in sd.read_notes(1)["Owner feedback"]
    with pytest.raises(WorkshopError, match="feedback is empty"):
        ws.revise_part(slug, 1, " ")


def test_revise_part_rejects_banned_phrases_and_keeps_the_script(tmp_home: Path):
    banned = load_banned()[0]
    llm = TextLlm(FIXTURES_LLM, edits={1: lambda t: f"{t} {banned}."})
    ws = make_workshop(tmp_home, llm=llm)
    slug = built(ws)
    sd = StoryDir.open(ws.paths, slug)
    before = sd.script(1).read_bytes()
    with pytest.raises(NsError) as err:
        ws.revise_part(slug, 1, "Add something.")
    assert err.value.code == "NS-E303" and sd.script(1).read_bytes() == before


def test_hand_edited_script_survives_revise_unchanged(tmp_home: Path):
    ws = make_workshop(tmp_home, llm=TextLlm(FIXTURES_LLM))
    slug = built(ws)
    sd = StoryDir.open(ws.paths, slug)
    raw = sd.script(1).read_text(encoding="utf-8")
    sd.script(1).write_text(raw.replace("The Hall Light", "The Hall Lamp"), encoding="utf-8")
    assert sd.read_script(1).chapters[1].title == "The Hall Lamp"
    ws.revise_part(slug, 1, "No-op.")
    assert "The Hall Lamp" in sd.script(1).read_text(encoding="utf-8")


# --- approve, status, design, produce --------------------------------------------------------------


def test_status_walks_through_the_workflow(tmp_home: Path):
    ws = make_workshop(tmp_home)
    slug = ws.start("after-dark", PREMISE)
    assert ws.status(slug)["next_step"] == "concepts"
    ws.concepts(slug)
    assert ws.status(slug)["next_step"] == "choose" and ws.status(slug)["drafts"] == ["A", "B", "C"]
    ws.choose(slug, "A")
    assert ws.status(slug)["next_step"] == "map"
    ws.map_parts(slug, 3, 12)
    st = ws.status(slug)
    assert st["next_step"] == "write part 1" and st["title"] == "The Doorbell Camera"
    assert [p["status"] for p in st["parts"]] == ["planned"] * 3
    ws.write_part(slug, 1)
    assert ws.status(slug)["next_step"] == "write part 2"
    assert ws.status(slug)["parts"][0]["shorts"] >= 4
    ws.write_part(slug, 2)
    ws.write_part(slug, 3)
    assert ws.status(slug)["next_step"] == "approve part 1"
    assert [(s["slug"], s["status"], s["parts"]) for s in ws.list_stories()] == [(slug, "drafting", 3)]


def test_approve_transitions(tmp_home: Path):
    ws = make_workshop(tmp_home)
    slug = built(ws, write=(1,))
    sd = StoryDir.open(ws.paths, slug)
    with pytest.raises(WorkshopError, match="part 2 cannot approve"):
        ws.approve(slug, 2)
    with pytest.raises(WorkshopError, match="part must be"):
        ws.approve(slug, "soon")
    with pytest.raises(WorkshopError, match="no part 7"):
        ws.approve(slug, 7)
    st = ws.approve(slug, "all")  # only part 1 is written
    assert st.parts[1].status == "approved" and st.parts[2].status == "planned" and st.status == "drafting"
    ws.write_part(slug, 2)
    ws.write_part(slug, 3)
    ws.approve(slug, 2)
    st = ws.approve(slug, "all")
    assert [st.parts[n].status for n in (1, 2, 3)] == ["approved"] * 3 and st.status == "approved"
    assert [p.status for p in sd.read_series().parts] == ["approved"] * 3
    assert ws.status(slug)["next_step"] == "design"
    # approving again is harmless; rewriting an approved part sends it back to written
    ws.approve(slug, 1)
    ws.write_part(slug, 1)
    assert sd.read_status().parts[1].status == "written" and sd.read_status().status == "drafting"


def test_approve_without_written_parts_and_with_broken_shorts(tmp_home: Path):
    ws = make_workshop(tmp_home)
    slug = built(ws, write=())
    with pytest.raises(WorkshopError, match="no part can approve"):
        ws.approve(slug)
    ws.write_part(slug, 1)
    sd = StoryDir.open(ws.paths, slug)
    f = sd.short_file(1, 1)
    text = re.sub(r"closing_line: .*", "closing_line: ''", f.read_text(encoding="utf-8"))
    f.write_text(text, encoding="utf-8")
    with pytest.raises(NsError) as err:
        ws.approve(slug, 1)
    assert err.value.code == "NS-E303" and sd.read_status().parts[1].status == "written"


def test_approve_reruns_the_policy_gate_on_hand_edited_scripts(tmp_home: Path):
    ws = make_workshop(tmp_home)
    slug = built(ws)
    sd = StoryDir.open(ws.paths, slug)
    raw = sd.script(1).read_text(encoding="utf-8")
    sd.script(1).write_text(raw.replace("<!-- s1 -->The doorbell", "<!-- s1 -->Elon Musk's doorbell", 1),
                            encoding="utf-8")
    with pytest.raises(NsError) as err:
        ws.approve(slug, 1)
    assert err.value.code == "NS-E602" and sd.read_status().parts[1].status == "written"


def test_design_validates_and_merges(tmp_home: Path):
    ws = make_workshop(tmp_home)
    slug = built(ws, write=())
    sd = StoryDir.open(ws.paths, slug)
    assert sd.read_design() is None
    d = ws.design(slug, format="long_16x9", visual_treatment="cinematic_teal", caption_style="boxed_dark",
                  voice="Aiden", thumbnail_style="bold", publish_cadence_days=5)  # fmt: skip
    assert d.visual_treatment == "cinematic_teal" and d.voice.speaker == "Aiden"
    assert d.publish_cadence_days == 5
    d2 = ws.design(slug, caption_style="clean_white", voice={"speed": 1.1}, thumbnail_style=None)
    assert d2.caption_style == "clean_white" and d2.visual_treatment == "cinematic_teal"
    assert d2.voice.speaker == "Aiden" and d2.voice.speed == 1.1 and d2.thumbnail_style == "bold"
    assert sd.read_design() == d2
    text = sd.design.read_text(encoding="utf-8")
    assert "format: long_16x9" in text and "visual_treatment: cinematic_teal" in text
    choices = ws.design_choices(slug)
    assert set(choices) == {"format", "visual_treatment", "caption_style"}
    assert "long_16x9" in choices["format"]


def test_design_rejects_unknown_and_disallowed_values(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    ws = make_workshop(tmp_home)
    slug = built(ws, write=())
    sd = StoryDir.open(ws.paths, slug)
    cases = [
        ({"visual_treatment": "neon"}, "visual_treatment 'neon'"),
        ({"caption_style": "comic"}, "caption_style 'comic'"),
        ({"format": "hologram"}, "format 'hologram'"),
        ({"publish_cadence_days": 0}, "invalid design"),
        ({"voice": {"speed": 3}}, "invalid design"),
        ({"colour": "red"}, "unknown design field"),
    ]
    for fields, message in cases:
        with pytest.raises(WorkshopError, match=message):
            ws.design(slug, **fields)
    assert not sd.design.exists()
    monkeypatch.setattr(W, "available_formats", lambda: ["long_16x9"])
    with pytest.raises(WorkshopError, match="format 'short_9x16'"):
        ws.design(slug, format="short_9x16")
    assert ws.design(slug, format="long_16x9").format == "long_16x9"


class Recorder:
    """A fake enqueue callback."""

    def __init__(self) -> None:
        self.requests: list[W.ProduceRequest] = []

    def __call__(self, req: W.ProduceRequest) -> str:
        self.requests.append(req)
        return req.job_id


def ready_to_produce(tmp_home: Path, rec: Recorder | None) -> tuple[W.Workshop, str]:
    ws = make_workshop(tmp_home, enqueue=rec)
    slug = built(ws, write=(1, 2, 3))
    ws.approve(slug)
    ws.design(slug, format="long_16x9", publish_cadence_days=7)
    return ws, slug


def test_produce_enqueues_one_job_per_approved_part(tmp_home: Path):
    rec = Recorder()
    ws, slug = ready_to_produce(tmp_home, rec)
    sd = StoryDir.open(ws.paths, slug)
    results = ws.produce(slug, "all")
    assert [r["part"] for r in results] == [1, 2, 3] and [r.part_n for r in rec.requests] == [1, 2, 3]
    assert len({r.job_id for r in rec.requests}) == 3
    assert [r["job_id"] for r in results] == [r.job_id for r in rec.requests]

    first = rec.requests[0]
    assert first.title == "The Doorbell Camera - Part 01"
    assert first.short_titles[:2] == ["The Doorbell Camera - Part 01 - Short 01",
                                      "The Doorbell Camera - Part 01 - Short 02"]  # fmt: skip
    assert (first.fmt, first.plan, first.channel, first.parts_total) == ("master_first", "master_from_script",
                                                                          "after-dark", 3)  # fmt: skip
    assert first.series_slug == "the-doorbell-camera" and first.story_slug == slug
    assert [r.publish_not_before_days for r in rec.requests] == [0, 7, 14]
    assert first.end_card == "Next: Part 2, Her Own Handwriting."
    assert rec.requests[2].end_card.startswith("That was")
    assert first.design == sd.read_design().model_dump()  # type: ignore[union-attr]

    for req in rec.requests:
        job = ws.paths.job_dir(req.job_id)
        assert json.loads((job / "series_context.json").read_text(encoding="utf-8")) == {
            "series_slug": "the-doorbell-camera",
            "series_title": "The Doorbell Camera",
            "part_n": req.part_n,
            "parts_total": 3,
            "title": req.title,
            "short_titles": req.short_titles,
            "end_card": req.end_card,
            "publish_not_before_days": req.publish_not_before_days,
            "minutes": req.minutes,
            "design": req.design,
        }
        src = json.loads((job / "script_source.json").read_text(encoding="utf-8"))
        assert src["kind"] == "script_import" and src["job_id"] == req.job_id
        assert Path(src["script"]) == sd.script(req.part_n) and Path(src["script"]).is_file()
        expected = [sd.short_file(req.part_n, s.k) for s in sd.read_shorts(req.part_n)]
        assert [Path(p) for p in src["shorts"]] == expected
        assert Path(src["concept"]) == sd.concept and src["design"] == first.design

    st = sd.read_status()
    assert [st.parts[n].status for n in (1, 2, 3)] == ["producing"] * 3 and st.status == "producing"
    assert [st.parts[n].job_id for n in (1, 2, 3)] == [r.job_id for r in rec.requests]
    assert [p.status for p in sd.read_series().parts] == ["producing"] * 3
    assert ws.status(slug)["next_step"] == "wait for production"
    with pytest.raises(WorkshopError, match="already in production"):
        ws.write_part(slug, 1)
    with pytest.raises(WorkshopError, match="already in production"):
        ws.revise_part(slug, 1, "More.")
    with pytest.raises(WorkshopError, match="no part can produce"):
        ws.produce(slug, "all")


def test_produce_one_part_and_explicit_callback(tmp_home: Path):
    ws, slug = ready_to_produce(tmp_home, None)
    rec = Recorder()
    out = ws.produce(slug, 2, enqueue=rec)
    assert [r["part"] for r in out] == [2] and rec.requests[0].publish_not_before_days == 0
    assert rec.requests[0].title == "The Doorbell Camera - Part 02"
    st = StoryDir.open(ws.paths, slug).read_status()
    assert [st.parts[n].status for n in (1, 2, 3)] == ["approved", "producing", "approved"]
    assert st.status == "producing"
    with pytest.raises(WorkshopError, match="part 2 cannot produce"):
        ws.produce(slug, 2, enqueue=rec)


def test_default_enqueue_queues_a_dashboard_job(tmp_home: Path):
    ws, slug = ready_to_produce(tmp_home, None)
    job_id = ws.produce(slug, 1)[0]["job_id"]
    rows = ws.db.query("select job_id, kind, channel, status, message, params_json from dashboard_jobs")
    assert len(rows) == 1 and rows[0]["job_id"] == job_id
    assert (rows[0]["kind"], rows[0]["channel"], rows[0]["status"]) == ("series_part", "after-dark", "queued")
    assert rows[0]["message"] == "The Doorbell Camera - Part 01"
    assert json.loads(rows[0]["params_json"])["part_n"] == 1


def test_produce_preconditions(tmp_home: Path):
    rec = Recorder()
    ws = make_workshop(tmp_home, enqueue=rec)
    slug = built(ws, write=(1,))
    with pytest.raises(WorkshopError, match="no design yet"):
        ws.produce(slug)
    ws.design(slug)
    with pytest.raises(WorkshopError, match="no part can produce"):
        ws.produce(slug)
    with pytest.raises(WorkshopError, match="part 1 cannot produce"):
        ws.produce(slug, 1)
    ws.approve(slug, 1)
    # a Short that was hand-edited into a bad state blocks the whole batch before any job exists
    sd = StoryDir.open(ws.paths, slug)
    f = sd.short_file(1, 1)
    f.write_text(f.read_text(encoding="utf-8").replace("span: [", "span: [900, ", 1),
                 encoding="utf-8")
    with pytest.raises(NsError):
        ws.produce(slug, 1)
    assert rec.requests == [] and not list(ws.paths.jobs_dir.glob("*"))


def test_mark_published_completes_the_story(tmp_home: Path):
    ws, slug = ready_to_produce(tmp_home, Recorder())
    ws.produce(slug)
    for n in (1, 2):
        assert ws.mark_published(slug, n).status == "producing"
    assert ws.mark_published(slug, 3).status == "published"
    assert ws.status(slug)["next_step"] == "done"
    with pytest.raises(WorkshopError, match="no part 4"):
        ws.mark_published(slug, 4)


def test_script_doc_roundtrip_is_stable_through_a_workshop_write(tmp_home: Path):
    ws = make_workshop(tmp_home)
    slug = built(ws)
    sd = StoryDir.open(ws.paths, slug)
    doc = sd.read_script(1)
    sd.write_script(1, doc)
    assert sd.read_script(1) == ScriptDoc.model_validate(doc.model_dump())
