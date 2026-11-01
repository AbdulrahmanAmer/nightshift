from pathlib import Path

import pytest
import yaml

from ns_common.db import Db
from ns_common.models import Pools, Voice
from ns_common.paths import Paths
from ns_studio.channels import crud
from ns_studio.errors import StudioError
from tests.helpers import make_channel


def _env(tmp_home: Path) -> tuple[Paths, Db]:
    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    return paths, db


def _other(slug: str, **kw):
    base = {"slug": slug, "display_name": slug.title(), "niche": f"niche of {slug}"}
    return make_channel(**(base | kw))


def test_default_pools_have_no_problems_or_warnings():
    assert crud.pool_problems(Pools()) == []
    assert crud.pool_warnings(Pools()) == []


def test_each_minimum_is_reported():
    assert any("hooks" in p for p in crud.pool_problems(Pools(hooks=["a", "b"])))
    assert any("structures" in p for p in crud.pool_problems(Pools(structures=["a", "b"])))
    assert any("caption styles" in p for p in crud.pool_problems(Pools(caption_styles=["a"])))
    assert any("visual treatments" in p for p in crud.pool_problems(Pools(visual_treatments=["a"])))
    assert any("duplicate" in p for p in crud.pool_problems(Pools(hooks=["a", "a", "b", "c"])))
    assert any("empty" in p for p in crud.pool_problems(Pools(pacings=[])))


def test_three_by_three_passes_with_warning():
    pools = Pools(hooks=["a", "b", "c"], structures=["x", "y", "z"])
    assert crud.pool_problems(pools) == []
    assert len(crud.pool_warnings(pools)) == 2


def test_add_and_load_roundtrip(tmp_home: Path):
    paths, db = _env(tmp_home)
    f = crud.add_channel(paths, db, make_channel())
    assert f == paths.channel_file("after-dark")
    ch = crud.get_channel(paths, "after-dark")
    assert ch.created_at is not None
    row = db.one("select status, display_name from channels where slug=?", ("after-dark",))
    assert row is not None and row["status"] == "active"
    assert [c.slug for c in crud.all_channels(paths)] == ["after-dark"]


def test_pool_minimum_blocks_add(tmp_home: Path):
    paths, db = _env(tmp_home)
    bad = make_channel(pools=Pools(hooks=["a", "b"]))
    with pytest.raises(StudioError) as ei:
        crud.add_channel(paths, db, bad)
    assert ei.value.code == "NS-E320"
    assert not paths.channel_file("after-dark").exists()


def test_distinctness(tmp_home: Path):
    paths, db = _env(tmp_home)
    crud.add_channel(paths, db, make_channel())
    niche = "  Original SHORT horror fiction set in everyday places"
    clone = make_channel(slug="clone", display_name="Clone", niche=niche)
    with pytest.raises(StudioError) as ei:
        crud.add_channel(paths, db, clone)
    assert ei.value.code == "NS-E321"
    different_voice = clone.model_copy(update={"voice": Voice(speaker="Aiden")})
    crud.add_channel(paths, db, different_voice)
    assert {c.slug for c in crud.all_channels(paths)} == {"after-dark", "clone"}


def test_duplicate_slug_and_max_channels(tmp_home: Path):
    paths, db = _env(tmp_home)
    crud.add_channel(paths, db, make_channel())
    with pytest.raises(StudioError):
        crud.add_channel(paths, db, make_channel(niche="something else entirely"))
    crud.add_channel(paths, db, _other("two"))
    crud.add_channel(paths, db, _other("three"))
    with pytest.raises(StudioError) as ei:
        crud.add_channel(paths, db, _other("four"))
    assert "at most 3" in ei.value.detail


def test_edit_persists_and_validates(tmp_home: Path):
    paths, db = _env(tmp_home)
    crud.add_channel(paths, db, make_channel())
    edited = crud.edit_channel(paths, db, "after-dark", {"critic_bar": 8.5, "voice.speed": 1.1})
    assert edited.critic_bar == 8.5
    assert crud.get_channel(paths, "after-dark").voice.speed == 1.1
    before = paths.channel_file("after-dark").read_text(encoding="utf-8")
    with pytest.raises(StudioError) as ei:
        crud.edit_channel(paths, db, "after-dark", {"pools.hooks": ["a"]})
    assert ei.value.code == "NS-E320"
    assert paths.channel_file("after-dark").read_text(encoding="utf-8") == before
    with pytest.raises(StudioError):
        crud.edit_channel(paths, db, "after-dark", {"no.such": 1})
    with pytest.raises(StudioError):
        crud.edit_channel(paths, db, "after-dark", {"critic_bar": 99})
    with pytest.raises(StudioError):
        crud.edit_channel(paths, db, "after-dark", {"slug": "renamed"})


def test_edit_status_syncs_db(tmp_home: Path):
    paths, db = _env(tmp_home)
    crud.add_channel(paths, db, make_channel())
    crud.edit_channel(paths, db, "after-dark", {"status": "paused"})
    row = db.one("select status from channels where slug=?", ("after-dark",))
    assert row is not None and row["status"] == "paused"


def test_unknown_channel_is_e309(tmp_home: Path):
    paths, _ = _env(tmp_home)
    with pytest.raises(StudioError) as ei:
        crud.get_channel(paths, "nope")
    assert ei.value.code == "NS-E322"
    assert "Unknown channel" in str(ei.value)


def test_validate_channel_reports_both_lists():
    ch = make_channel(pools=Pools(hooks=["a", "b", "c"], structures=["x", "y", "z"]))
    problems, warnings = crud.validate_channel(ch, [])
    assert problems == [] and len(warnings) == 2


def test_broken_yaml_is_e307(tmp_home: Path):
    paths, _ = _env(tmp_home)
    paths.channel_file("bad").write_text("slug: Bad Slug\n", encoding="utf-8")
    with pytest.raises(StudioError) as ei:
        crud.all_channels(paths)
    assert ei.value.code == "NS-E320"


def test_load_channel_yaml(tmp_path: Path):
    f = tmp_path / "c.yaml"
    f.write_text(yaml.safe_dump(make_channel().model_dump(mode="json")), encoding="utf-8")
    assert crud.load_channel_yaml(f).slug == "after-dark"
    f.write_text("slug: [", encoding="utf-8")
    with pytest.raises(StudioError):
        crud.load_channel_yaml(f)
