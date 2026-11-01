import pytest

from ns_common.textstats import fk_grade, word_count
from ns_studio.text.exemplars import EXEMPLARS_DIR, FOLDERS, load_exemplars, niche_folder

BANNED_FRAGMENTS = ("true story", "based on")


def test_niche_mapping():
    assert niche_folder("original short horror fiction") == "horror"
    assert niche_folder("small-town mystery") == "mystery"
    assert niche_folder("petty revenge stories") == "revenge"
    assert niche_folder("heartwarming tales") == "wholesome_twist"
    assert niche_folder("original short horror fiction set in everyday places") == "horror"


@pytest.mark.parametrize("folder", FOLDERS)
def test_each_niche_has_three_quality_exemplars(folder: str):
    ex = load_exemplars(folder)
    assert len(ex) == 3, folder
    for e in ex:
        n = word_count(e["text"])
        assert 600 <= n <= 900, (folder, e["title"], n)
        assert 4.0 <= fk_grade(e["text"]) <= 9.0, (folder, e["title"])
        assert e["title"] and e["structure"] and e["pov_tone"] and e["why"]
        low = e["text"].lower()
        assert not any(b in low for b in BANNED_FRAGMENTS), (folder, e["title"])


@pytest.mark.parametrize("folder", FOLDERS)
def test_exemplars_cover_three_structures_and_two_voices(folder: str):
    ex = load_exemplars(folder)
    assert len({e["structure"] for e in ex}) == 3
    assert len({e["pov_tone"] for e in ex}) >= 2


def test_free_text_niche_resolves_to_folder_and_returns_copies():
    a = load_exemplars("original short horror fiction set in everyday places")
    b = load_exemplars("horror")
    assert [e["title"] for e in a] == [e["title"] for e in b]
    a[0]["title"] = "changed"
    assert load_exemplars("horror")[0]["title"] != "changed"


def test_exemplar_files_are_ascii_and_referenced_by_meta():
    for folder in FOLDERS:
        files = sorted(p.name for p in (EXEMPLARS_DIR / folder).glob("*.md"))
        assert len(files) == 3, folder
        for name in files:
            text = (EXEMPLARS_DIR / folder / name).read_text(encoding="utf-8")
            assert text.isascii(), (folder, name)


def test_exemplars_avoid_banned_phrases():
    from ns_studio.text.validators import check_banned, load_banned

    for folder in FOLDERS:
        for e in load_exemplars(folder):
            assert check_banned(e["text"], load_banned()) is None, (folder, e["title"])
