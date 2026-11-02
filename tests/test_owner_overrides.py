"""The owner's own exemplars and prompt tweaks win over the shipped ones (system gap 17)."""

from pathlib import Path

from ns_common.paths import Paths
from ns_studio.text import prompts as P
from ns_studio.text.exemplars import load_exemplars


def test_owner_exemplars_replace_the_shipped_ones(tmp_home: Path):
    shipped = load_exemplars("horror")
    assert shipped and shipped[0]["title"] != "my_story"
    folder = Paths.from_env().exemplars
    folder.mkdir(parents=True)
    story = "I count the stairs every night.\n\nThere is always one more."
    (folder / "my_story.md").write_text(story, encoding="utf-8")
    (folder / "empty.txt").write_text("   ", encoding="utf-8")
    mine = load_exemplars("horror")
    assert [e["title"] for e in mine] == ["my_story"] and "one more" in mine[0]["text"]


def test_owner_exemplar_folder_with_meta_yaml_is_used_per_niche(tmp_home: Path):
    folder = Paths.from_env().exemplars / "mystery"
    folder.mkdir(parents=True)
    (folder / "a.md").write_text("The key was never lost.", encoding="utf-8")
    (folder / "meta.yaml").write_text(
        "exemplars:\n  - {file: a.md, title: The Key, structure: twist, pov_tone: calm, why: tight}\n",
        encoding="utf-8",
    )
    assert load_exemplars("mystery")[0]["title"] == "The Key"
    assert load_exemplars("horror")[0]["title"] != "The Key"  # other niches keep the shipped set


OVERRIDE = (
    "---\nversion: 7\nthinking: false\nschema: ideas\n---\nSystem tweak.\n---USER---\n"
    "Give {{ n }} ideas, owner style.\n"
)


def test_prompt_override_wins_and_a_broken_one_falls_back(tmp_home: Path):
    P.clear_prompt_cache()
    shipped = P.load_prompt("ideas")
    own = Paths.from_env().prompt_overrides
    own.mkdir(parents=True)
    (own / "ideas.md").write_text(OVERRIDE, encoding="utf-8")
    p = P.load_prompt("ideas")
    assert p.version == 7 and P.render(p, n=3) == "Give 3 ideas, owner style."
    (own / "ideas.md").write_text("no front matter at all", encoding="utf-8")
    assert P.load_prompt("ideas").version == shipped.version  # the nightly run must not die on a typo
    P.clear_prompt_cache()
