"""Package P10b, Review round: the command line refuses a bad `formats_allowed` where it is chosen too.

`studio channel edit <slug> --set formats_allowed=[...]`, `channel add --from-yaml` and the library call
`crud.add_channel` / `crud.edit_channel` use the same rule as the dashboard save (`crud.unusable_formats`):
an id that is new in the save and that nothing can use is refused with NS-E320 naming the field; an id the
saved channel already listed stays so an old channel stays editable.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from ns_common.db import Db
from ns_common.paths import Paths
from ns_studio.channels import crud
from ns_studio.cli import app
from ns_studio.errors import StudioError
from tests.helpers import make_channel
from tests.test_p10_formats_core import put

runner = CliRunner()


def _env(tmp_home: Path) -> tuple[Paths, Db]:
    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    return paths, db


def _allowed(paths: Paths) -> list[str]:
    return yaml.safe_load(paths.channel_file("after-dark").read_text(encoding="utf-8"))["formats_allowed"]


def test_channel_edit_refuses_an_unknown_format_id(tmp_home: Path) -> None:
    paths, db = _env(tmp_home)
    crud.add_channel(paths, db, make_channel())
    res = runner.invoke(app, ["channel", "edit", "after-dark", "--set", 'formats_allowed=["nope"]'])
    assert res.exit_code == 1
    assert "NS-E320" in res.output and "formats_allowed" in res.output and "nope" in res.output
    assert _allowed(paths) == []  # nothing was saved


def test_channel_edit_refuses_a_format_the_studio_would_refuse(tmp_home: Path) -> None:
    paths, db = _env(tmp_home)
    crud.add_channel(paths, db, make_channel())
    put(paths, "short_bad", shots={"min_s": 5, "max_s": 2})
    res = runner.invoke(app, ["channel", "edit", "after-dark", "--set", 'formats_allowed=["short_bad"]'])
    assert res.exit_code == 1 and "NS-E320" in res.output and "shots.min_s" in res.output
    assert _allowed(paths) == []


def test_channel_edit_accepts_a_shipped_format_and_a_good_duplicate(tmp_home: Path) -> None:
    paths, db = _env(tmp_home)
    crud.add_channel(paths, db, make_channel())
    put(paths, "short_copy")
    res = runner.invoke(
        app, ["channel", "edit", "after-dark", "--set", 'formats_allowed=["short_9x16","short_copy"]']
    )
    assert res.exit_code == 0, res.output
    assert _allowed(paths) == ["short_9x16", "short_copy"]


def test_channel_edit_keeps_an_old_unknown_id_the_channel_already_listed(tmp_home: Path) -> None:
    paths, db = _env(tmp_home)
    crud.add_channel(paths, db, make_channel(formats_allowed=[]))
    path = paths.channel_file("after-dark")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["formats_allowed"] = ["short_gone"]  # a format deleted after it was chosen
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    res = runner.invoke(
        app, ["channel", "edit", "after-dark", "--set", 'formats_allowed=["short_gone","short_9x16"]']
    )
    assert res.exit_code == 0, res.output
    assert _allowed(paths) == ["short_gone", "short_9x16"]
    # a second unknown id added in the same save is still refused
    again = runner.invoke(
        app, ["channel", "edit", "after-dark", "--set", 'formats_allowed=["short_gone","short_new_gone"]']
    )
    assert again.exit_code == 1 and "short_new_gone" in again.output


def test_channel_add_from_yaml_refuses_a_bad_format(tmp_home: Path, tmp_path: Path) -> None:
    paths, _db = _env(tmp_home)
    data = make_channel(formats_allowed=["nope"]).model_dump(mode="json")
    f = tmp_path / "ch.yaml"
    f.write_text(yaml.safe_dump(data), encoding="utf-8")
    res = runner.invoke(app, ["channel", "add", "--from-yaml", str(f)])
    assert res.exit_code == 1 and "NS-E320" in res.output and "nope" in res.output
    assert not paths.channel_file("after-dark").exists()


def test_add_channel_library_call_refuses_and_accepts(tmp_home: Path) -> None:
    paths, db = _env(tmp_home)
    try:
        crud.add_channel(paths, db, make_channel(formats_allowed=["short_vanished"]))
    except StudioError as e:
        assert e.code == "NS-E320" and "short_vanished" in e.detail
    else:
        raise AssertionError("a channel listing an unknown format was saved")
    crud.add_channel(paths, db, make_channel(formats_allowed=["short_9x16", "long_16x9"]))
    assert _allowed(paths) == ["short_9x16", "long_16x9"]
