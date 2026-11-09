# ruff: noqa: E501
"""Package P10a: the one-time notice about owner formats (decision D2) and the channel's resolved formats (D1)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from ns_common.paths import Paths
from ns_studio.media import format_notice as N
from ns_studio.media import formats as F
from ns_studio.media import job_formats as JF
from tests.helpers import make_channel, make_ctx

pytest.importorskip("fastapi")

from ns_dashboard.fakes import seed_channel  # noqa: E402
from ns_dashboard.formats import create_format  # noqa: E402
from tests.test_dashboard_api import make_env  # noqa: E402


def shipped(fid: str) -> dict[str, Any]:
    return yaml.safe_load((F.FORMATS_DIR / f"{fid}.yaml").read_text(encoding="utf-8"))


def old_copy_of_short(paths: Paths) -> None:
    """The file Library > Formats wrote on 0.1.14 for an edit of the Short preset: every value copied out."""
    doc = shipped("short_9x16")
    doc["canvas"]["fps"] = 30
    doc["captions"].update(anchor_y_pct=72, words_on_screen=3)
    doc["encoder"] = {"preset": "p4"}
    doc["safe_margins"]["bottom"] = 400
    paths.formats_dir.mkdir(parents=True, exist_ok=True)
    (paths.formats_dir / "short_9x16.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def notes(paths: Paths) -> list[dict[str, str]]:
    from ns_supervisor.notify import read_notes

    return read_notes(paths)


# ---------- the notice (D2) ----------
def test_an_owner_edited_short_gets_one_note_that_says_what_changes(tmp_home: Path) -> None:
    paths = Paths.from_env()
    old_copy_of_short(paths)
    assert N.notify_owner_formats(paths) is True
    (note,) = notes(paths)
    msg = note["message"]
    assert "short_9x16 (edited)" in msg and "72 %" in msg and "at most 3 words" in msg
    assert "400 px from the bottom" in msg and "Library > Formats" in msg
    assert N.marker_path(paths).exists()
    # once only: the same start again, and a second job, leave no second note
    assert N.notify_owner_formats(paths) is False
    assert len(notes(paths)) == 1


def test_an_added_format_is_named_as_usable(tmp_home: Path) -> None:
    paths = Paths.from_env()
    create_format(paths, {"duplicate_of": "short_9x16"})
    assert N.notify_owner_formats(paths) is True
    (note,) = notes(paths)
    assert "short_9x16_copy (yours)" in note["message"]


def test_nothing_to_say_leaves_no_note_but_is_remembered(tmp_home: Path) -> None:
    paths = Paths.from_env()
    assert N.notify_owner_formats(paths) is False
    assert notes(paths) == [] and N.marker_path(paths).exists()
    old_copy_of_short(paths)  # an edit made after the first start is deliberate: no note
    assert N.notify_owner_formats(paths) is False
    assert notes(paths) == []


def test_an_edited_copy_that_matches_the_shipped_file_is_not_news(tmp_home: Path) -> None:
    paths = Paths.from_env()
    paths.formats_dir.mkdir(parents=True)
    (paths.formats_dir / "short_9x16.yaml").write_text(
        yaml.safe_dump(copy.deepcopy(shipped("short_9x16"))), encoding="utf-8"
    )
    assert N.owner_format_changes(paths) == []


def test_the_first_job_after_the_update_leaves_the_note_and_the_second_does_not(tmp_home: Path) -> None:
    paths = Paths.from_env()
    old_copy_of_short(paths)
    first = make_ctx(tmp_home)
    JF.prepare_job_formats(first)
    assert len(notes(paths)) == 1
    second = make_ctx(tmp_home)
    JF.prepare_job_formats(second)
    assert len(notes(paths)) == 1
    # what the note promised is what the job does: the owner's values apply as written
    fmt = JF.job_format(first, "short")
    assert fmt.captions.anchor_y_pct == 72 and fmt.safe_margins.bottom == 400
    assert first.read_json("formats.json")["short"]["source"] == "edited"


def test_a_failing_note_never_stops_a_job(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = Paths.from_env()
    old_copy_of_short(paths)

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("disk")

    monkeypatch.setattr(N, "owner_format_changes", boom)
    assert JF.prepare_job_formats(make_ctx(tmp_home)) is not None


# ---------- the channel's formats (D1) ----------
def test_the_channel_route_names_the_format_each_kind_uses_and_why(tmp_home: Path) -> None:
    client, state = make_env(tmp_home)
    copy_id = create_format(state.paths, {"duplicate_of": "short_9x16"})["id"]
    seed_channel(state.db, state.paths, "after-dark", formats_allowed=[copy_id, "short_9x16", "long_16x9"])
    body = client.get("/api/channels/after-dark/formats").json()
    assert body["short"]["id"] == copy_id and body["short"]["why"] == "first allowed of that kind"
    assert body["short"]["source"] == "yours" and body["short"]["applied"] is True
    assert body["long"]["id"] == "long_16x9" and body["long"]["applied"] is False
    seed_channel(state.db, state.paths, "plain")
    plain = client.get("/api/channels/plain/formats").json()
    assert plain["short"]["id"] == "short_9x16" and plain["short"]["why"].startswith("default")
    assert client.get("/api/channels/none/formats").status_code == 404


def test_the_route_reports_a_problem_instead_of_failing(tmp_home: Path) -> None:
    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths, "after-dark", formats_allowed=["gone", "clip_center_crop"])
    body = client.get("/api/channels/after-dark/formats").json()
    assert body["short"]["id"] == "short_9x16" and "default" in body["short"]["why"]


def test_formats_in_use_matches_what_a_job_would_record(tmp_home: Path) -> None:
    paths = Paths.from_env()
    copy_id = create_format(paths, {"duplicate_of": "short_9x16"})["id"]
    channel = make_channel(formats_allowed=[copy_id])
    shown = JF.formats_in_use(channel, paths)
    ctx = make_ctx(tmp_home, channel=channel)
    recorded = json.loads(json.dumps(JF.prepare_job_formats(ctx)))
    assert (
        shown["short"]["id"] == recorded["short"]["id"] and shown["short"]["why"] == recorded["short"]["why"]
    )
