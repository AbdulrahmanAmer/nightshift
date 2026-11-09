# ruff: noqa: E501
"""Package P10b: a channel's `formats_allowed` is checked where it is saved, and the one-time notice is honest.

A format id that is new in a save and does not load, or cannot be used for any video, is refused with NS-E320
naming the field and the problem (decision D3: the studio's own check, `definition_problems`, so the editor,
the Workshop and the channel save refuse the same things). An id the channel already listed stays, with a
warning: an old channel whose format was deleted must still be editable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ns_common.paths import Paths
from ns_studio.media import format_notice as N
from ns_studio.media import formats as F
from tests.test_p10_formats_core import put, shipped

pytest.importorskip("fastapi")

from ns_dashboard.fakes import seed_channel  # noqa: E402
from ns_dashboard.formats import create_format  # noqa: E402
from ns_dashboard.svc_channels import ChannelsService  # noqa: E402
from tests.test_dashboard_api import make_env  # noqa: E402


def profile(client: Any, slug: str = "after-dark") -> dict[str, Any]:
    return client.get(f"/api/channels/{slug}").json()["profile"]


def save(client: Any, slug: str, formats_allowed: list[str]) -> Any:
    body = {**profile(client, slug), "formats_allowed": formats_allowed}
    return client.put(f"/api/channels/{slug}", json=body)


def off_canvas() -> dict[str, Any]:
    layout = shipped("short_9x16")["layout"]
    layout["regions"][0]["w"] = 5000
    return layout


def test_channel_save_refuses_a_new_unknown_format_id_with_ns_e320(tmp_home: Path) -> None:
    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths, "after-dark")
    resp = save(client, "after-dark", ["short_9x16", "short_vanished"])
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "NS-E320" and "short_vanished" in err["message"]
    (detail,) = err["details"]
    assert (
        detail["field"] == "formats_allowed"
        and detail["code"] == "NS-E320"
        and "unknown format" in detail["message"]
    )
    assert profile(client)["formats_allowed"] == []  # nothing was saved


@pytest.mark.parametrize(
    ("kind", "needle"), [("region", "leaves the"), ("shots", "shots.min_s"), ("broken", "canvas is required")]
)
def test_channel_save_refuses_a_new_format_the_studio_would_refuse(
    tmp_home: Path, kind: str, needle: str
) -> None:
    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths, "after-dark")
    paths = state.paths
    if kind == "region":
        put(paths, "short_bad", layout=off_canvas())
    elif kind == "shots":
        put(paths, "short_bad", shots={"min_s": 5, "max_s": 2})
    else:
        paths.formats_dir.mkdir(parents=True, exist_ok=True)
        (paths.formats_dir / "short_bad.yaml").write_text("id: short_bad\n", encoding="utf-8")
    resp = save(client, "after-dark", ["short_bad"])
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "NS-E320"
    assert needle in resp.json()["error"]["details"][0]["message"]


def test_channel_save_accepts_the_shipped_formats_a_duplicate_and_a_clip_layout(tmp_home: Path) -> None:
    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths, "after-dark")
    copy_id = create_format(state.paths, {"duplicate_of": "long_16x9"})["id"]
    clip_copy = create_format(state.paths, {"duplicate_of": "clip_center_crop"})["id"]
    wanted = ["short_9x16", "long_16x9", "square_1x1", copy_id, clip_copy, "clip_gaming_facecam"]
    resp = save(client, "after-dark", wanted)
    assert resp.status_code == 200, resp.text
    assert profile(client)["formats_allowed"] == wanted


def test_channel_create_checks_every_listed_id(tmp_home: Path) -> None:
    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths, "after-dark")
    body = {**profile(client), "slug": "second", "display_name": "Second", "formats_allowed": ["nope"]}
    resp = client.post("/api/channels", json=body)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "NS-E320"
    body["formats_allowed"] = ["short_9x16"]
    assert client.post("/api/channels", json=body).status_code in (200, 201)


def test_channel_save_keeps_an_old_unknown_id_with_a_warning(tmp_home: Path) -> None:
    """An old channel whose listed format was deleted can still be edited; the dead id stays and is named."""
    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths, "after-dark", formats_allowed=["short_9x16", "gone_since"])
    body = {**profile(client), "display_name": "Renamed", "formats_allowed": ["short_9x16", "gone_since"]}
    resp = client.put("/api/channels/after-dark", json=body)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["profile"]["display_name"] == "Renamed" and out["profile"]["formats_allowed"] == [
        "short_9x16",
        "gone_since",
    ]
    fields = [w for w in out["warning_fields"] if w["field"] == "formats_allowed"]
    assert fields and "gone_since" in fields[0]["message"]
    # adding one more unknown id in the same save is still refused
    again = {**body, "formats_allowed": ["short_9x16", "gone_since", "also_gone"]}
    resp = client.put("/api/channels/after-dark", json=again)
    assert resp.status_code == 422 and "also_gone" in resp.json()["error"]["message"]


def test_the_assistants_dry_run_refuses_what_the_save_refuses(tmp_home: Path) -> None:
    _client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths, "after-dark")
    svc = ChannelsService(state)
    body = {**svc.load("after-dark").model_dump(mode="json"), "formats_allowed": ["nope"]}
    from ns_dashboard.errors import ApiError

    with pytest.raises(ApiError) as e1:
        svc.check_update("after-dark", body)
    assert e1.value.status == 422 and e1.value.code == "NS-E320"
    with pytest.raises(ApiError) as e2:
        svc.check_create({**body, "slug": "third"})
    assert e2.value.code == "NS-E320"


def test_a_format_made_a_moment_ago_is_seen_by_the_save(tmp_home: Path) -> None:
    """The dashboard outlives its edits: a duplicate made after the first listing is accepted at once."""
    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths, "after-dark")
    assert save(client, "after-dark", ["short_later"]).status_code == 422
    create_format(state.paths, {"duplicate_of": "short_9x16", "id": "short_later"})
    assert save(client, "after-dark", ["short_later"]).status_code == 200


# ---------- the one-time notice never names a setting that changes nothing ----------
def test_the_notice_skips_a_format_the_studio_will_not_draw(tmp_home: Path) -> None:
    paths = Paths.from_env()
    put(paths, "short_off", layout=off_canvas(), captions={"anchor_y_pct": 40})
    put(paths, "short_shots", shots={"min_s": 5, "max_s": 2}, captions={"anchor_y_pct": 40})
    put(paths, "short_ok", captions={"anchor_y_pct": 40})
    changes = N.owner_format_changes(paths)
    assert [c[0] for c in changes] == ["short_ok"]


def test_the_notice_skips_an_edited_preset_the_studio_refuses(tmp_home: Path) -> None:
    paths = Paths.from_env()
    put(paths, "short_9x16", captions={"anchor_y_pct": 40}, shots={"min_s": 5, "max_s": 2})
    assert N.owner_format_changes(paths) == []


def test_the_notice_covers_full_video_formats_now_that_they_are_drawn(tmp_home: Path) -> None:
    paths = Paths.from_env()
    put(paths, "long_mine", base="long_16x9")
    put(paths, "long_16x9", base="long_16x9", safe_margins={"top": 54, "bottom": 300})
    assert "long" in N.NOTICE_KINDS
    got = {fid: how for fid, how, _ in N.owner_format_changes(paths)}
    assert got == {"long_mine": "yours", "long_16x9": "edited"}
    # an owner who was told about Shorts and squares under P10a is told once more, about full videos only
    N.marker_path(paths).parent.mkdir(parents=True, exist_ok=True)
    N.marker_path(paths).write_text('{"version": 1, "kinds": ["short", "square"]}\n', encoding="utf-8")
    assert N.notify_owner_formats(paths) is True
    from ns_supervisor.notify import read_notes

    (note,) = read_notes(paths)
    assert "long_mine" in note["message"] and "short_9x16" not in note["message"]


def test_the_notice_skips_a_kind_no_render_path_uses(tmp_home: Path) -> None:
    paths = Paths.from_env()
    put(paths, "clip_mine", base="clip_center_crop", captions={"anchor_y_pct": 40})
    assert N.owner_format_changes(paths) == []
    assert F.get_format("clip_mine").kind == "clip"
