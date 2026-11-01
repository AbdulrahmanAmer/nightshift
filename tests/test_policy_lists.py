import yaml

from ns_common.paths import contracts_dir


def _lines(name: str) -> list[str]:
    p = contracts_dir() / "policy" / name
    text = p.read_text(encoding="utf-8")
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]


def test_lists_non_empty_and_lowercase():
    names = (
        "banned_phrases.txt", "misleading_claims.txt", "real_entities_block.txt", "real_entities_allow.txt"
    )
    for name in names:
        lines = _lines(name)
        assert len(lines) >= 10, name
        assert all(ln == ln.lower() for ln in lines), name


def test_unsafe_topics_yaml_shape():
    text = (contracts_dir() / "policy" / "unsafe_topics.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    ids = [c["id"] for c in data["categories"]]
    assert {"real_people", "minors", "graphic_harm", "advice", "fake_news"} <= set(ids)
    for c in data["categories"]:
        assert c["examples"] and c["description"]
