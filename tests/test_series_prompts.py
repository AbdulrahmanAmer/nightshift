import json
from pathlib import Path

import pytest
from jsonschema import ValidationError, validate

import ns_studio.series.schemas as W
from ns_studio.text import prompts as P
from tests.helpers import make_ctx

PROMPTS = {
    "workshop_concepts": True,
    "workshop_regenerate": True,
    "workshop_map": True,
    "workshop_part_outline": True,
    "workshop_revise": False,
}
FIXTURE_SCHEMAS = {
    "workshop_concepts": "workshop_concepts",
    "workshop_concepts_2": "workshop_concepts",
    "workshop_regenerate": "workshop_concepts",
    "workshop_map": "workshop_map",
    "workshop_part_outline": "workshop_part_outline",
}
CONCEPT = {
    "title": "The Doorbell Camera",
    "premise": "A nurse's camera shows a figure.",
    "cast": ["Mara: nurse"],
    "setting": "A street",
    "tone": "Dread",
    "beats": ["one", "two", "three", "four", "five"],
    "why_it_works": "Simple.",
    "series_potential": "Many parts.",
    "feedback": ["earlier note"],
}
ENTRY = {"n": 2, "title": "Wet Footprints", "hook": "A ping.", "cliff": "A door.", "reveals": "Tomorrow.",
         "recap": "Recap.", "minutes": 15}  # fmt: skip


def test_prompts_load_with_front_matter_and_registered_schemas():
    for name, thinking in PROMPTS.items():
        p = P.load_prompt(name)
        assert p.version >= 1 and p.thinking is thinking and p.system.strip(), name
        assert p.schema is None or p.schema in P.PROMPT_SCHEMAS, name
    assert P.load_prompt("workshop_revise").schema is None
    assert set(W.WORKSHOP_SCHEMAS) <= set(P.PROMPT_SCHEMAS)


def test_every_workshop_json_fixture_validates_against_its_schema():
    files = sorted((P.FIXTURES).glob("workshop_*.json"))
    assert {f.stem for f in files} == set(FIXTURE_SCHEMAS)
    for f in files:
        validate(json.loads(f.read_text(encoding="utf-8")), P.PROMPT_SCHEMAS[FIXTURE_SCHEMAS[f.stem]])


def test_concept_fixtures_have_five_beats_and_letters():
    data = json.loads((P.FIXTURES / "workshop_concepts.json").read_text(encoding="utf-8"))
    assert [c["letter"] for c in data["concepts"]] == ["A", "B", "C"]
    assert all(len(c["beats"]) == 5 for c in data["concepts"])
    regen = json.loads((P.FIXTURES / "workshop_regenerate.json").read_text(encoding="utf-8"))
    assert len(regen["concepts"]) == 1 and regen["concepts"][0]["letter"] == "B"


def test_map_fixture_respects_runtime_bounds():
    data = json.loads((P.FIXTURES / "workshop_map.json").read_text(encoding="utf-8"))
    assert all(12 <= p["minutes"] <= 25 for p in data["parts"])
    assert [p["n"] for p in data["parts"]] == [1, 2, 3]
    bad = {"parts": [{**data["parts"][0], "minutes": 30}]}
    with pytest.raises(ValidationError):
        validate(bad, P.PROMPT_SCHEMAS["workshop_map"])


def test_outline_schema_allows_5_to_12_chapters():
    s = P.PROMPT_SCHEMAS["workshop_part_outline"]["properties"]["chapters"]
    assert (s["minItems"], s["maxItems"]) == (5, 12)


def test_text_fixtures_exist():
    assert (P.FIXTURES / "workshop_revise.txt").read_text(encoding="utf-8").strip() == "UNCHANGED"
    assert (P.FIXTURES / "workshop_revise_fix.txt").read_text(encoding="utf-8").strip()


def test_workshop_prompts_render_with_their_variables(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    common = P._common_vars(ctx)

    def render(name: str, **vars) -> str:
        return P.render(P.load_prompt(name), **(common | vars))

    out = render("workshop_concepts", n=3, premise="A hooded figure.", avoid_titles=["Old Title"])
    assert "exactly 3 items" in out and "A hooded figure." in out and "- Old Title" in out
    assert "Real people, brands or events as fact" in out
    assert "invent the premises" in render("workshop_concepts", n=3, premise="", avoid_titles=[])

    out = render("workshop_regenerate", concept=CONCEPT, feedback="Make it warmer.", letter="B")
    assert 'letter "B"' in out and "Make it warmer." in out and "- earlier note" in out and "4. four" in out

    out = render("workshop_map", concept=CONCEPT, parts=6, minutes=18)
    assert "exactly 6 parts of about 18 minutes" in out and "between 12 and 25" in out

    series = {"title": "The Doorbell Camera", "parts": [ENTRY, ENTRY]}
    budget = {"minutes": 12, "words": 1860, "wpm": 155, "chapters": 5, "chapter_words": 372}
    out = render(
        "workshop_part_outline", concept=CONCEPT, series=series, part=ENTRY,
        previous=[{**ENTRY, "n": 1}], budget=budget,
    )  # fmt: skip
    assert "part 2" in out and "1860 spoken words" in out and "Part 1, Wet Footprints" in out
    assert "must end on this cliff: A door." in out and "Recap." in out

    out = render("workshop_revise", chapter={"n": 3, "title": "T", "text": "Body text."}, feedback="Cut it.")
    assert "Cut it." in out and "Chapter 3: T" in out and "Body text." in out
