"""After Approve the posting checklist stays reachable (boxes F-H143, F-H164; package P14).

The Queue's Ready tab asked the server for `pending` outputs only. Approving a video moved it out of that
answer, the list reloaded when the outbox counts changed, and the video left the screen with its checklist.
"Finish posting" on the posted list switched to the Ready tab, which could not show an approved video, so the
owner could not copy the title, the description or the tags, nor paste the YouTube link, from the dashboard.

The server half is tested through the API the page calls. The page half has no JavaScript test runner in this
repository, so its rules are two small exported functions (`needsOwner`, `pickOutput`) whose text is checked
here; the click-through is the proof on a dev install (the pull request's Proof section).
"""

from __future__ import annotations

import re
from pathlib import Path

from ns_dashboard import fakes
from tests.test_dashboard_api import make_env

ROOT = Path(__file__).resolve().parents[1]
QUEUE = (ROOT / "ns_dashboard" / "ui" / "src" / "pages" / "Queue.tsx").read_text(encoding="utf-8")


def ready_filter() -> str:
    """The status filter the Ready tab sends, read from the page source."""
    m = re.search(r'const STATUS: Record<Tab, string> = \{ ready: "([^"]+)"', QUEUE)
    assert m, "Queue.tsx no longer declares STATUS with a ready filter"
    return m.group(1)


def seed(state, status: str, title: str, profile: str | None = None) -> str:  # noqa: ANN001 - AppState
    oid = fakes.seed_output(state.db, state.paths, channel="after-dark", status=status, title=title)
    if profile:
        state.db.execute("UPDATE outputs SET profile = ? WHERE output_id = ?", (profile, oid))
    return oid


# ---------- the server answers what the page now asks ----------
def test_the_ready_filter_returns_videos_to_review_and_videos_to_finish_posting(tmp_home: Path):
    client, state = make_env(tmp_home)
    fakes.seed_channel(state.db, state.paths, "after-dark")
    to_review = seed(state, "pending", "To review")
    to_post = seed(state, "approved", "Approved, not posted")
    posted = seed(state, "uploaded", "Posted")
    rejected = seed(state, "rejected", "Rejected")
    assert ready_filter() == "pending,approved"
    r = client.get(f"/api/outputs?status={ready_filter()}")
    assert r.status_code == 200
    ids = {x["output_id"] for x in r.json()}
    assert ids == {to_review, to_post} and posted not in ids and rejected not in ids


def test_an_approved_video_can_still_be_opened_and_marked_uploaded(tmp_home: Path):
    """The whole of J2 step 4 through the routes the checklist calls, for a video approved earlier."""
    client, state = make_env(tmp_home)
    fakes.seed_channel(state.db, state.paths, "after-dark")
    oid = seed(state, "pending", "The Doorbell Camera")
    assert client.post(f"/api/outputs/{oid}/approve").status_code == 200
    # the list reload that used to drop it: it is still in the Ready answer, as approved
    listed = {x["output_id"]: x for x in client.get(f"/api/outputs?status={ready_filter()}").json()}
    assert listed[oid]["status"] == "approved"
    detail = client.get(f"/api/outputs/{oid}")
    assert detail.status_code == 200 and detail.json()["title"] == "The Doorbell Camera"
    done = client.post(f"/api/outputs/{oid}/uploaded", json={"url": "https://youtu.be/abc"})
    assert done.status_code == 200
    after = {x["output_id"] for x in client.get(f"/api/outputs?status={ready_filter()}").json()}
    assert oid not in after  # posted: nothing left to do, it leaves Ready
    row = state.db.one("SELECT status FROM outputs WHERE output_id = ?", (oid,))
    assert row is not None and row["status"] == "uploaded"


def test_an_approved_test_output_comes_back_with_its_mark_so_the_page_can_leave_it_out(tmp_home: Path):
    client, state = make_env(tmp_home)
    fakes.seed_channel(state.db, state.paths, "after-dark")
    test_video = seed(state, "approved", "Tiny story", profile="tiny_story")
    rows = {x["output_id"]: x for x in client.get(f"/api/outputs?status={ready_filter()}").json()}
    assert rows[test_video]["profile"] == "tiny_story"  # `needsOwner` reads this field


# ---------- the page's rules ----------
def test_ready_keeps_what_needs_the_owner_and_leaves_out_test_outputs():
    m = re.search(r"export const needsOwner = \(x: [^)]*\) => (.+);", QUEUE)
    assert m, "needsOwner is gone from Queue.tsx"
    rule = m.group(1)
    assert 'x.status === "pending"' in rule
    assert 'x.status === "approved" && !x.profile' in rule
    assert 'tab === "ready" ? list.data.filter(needsOwner) : list.data' in QUEUE


def test_the_video_on_screen_is_the_picked_one_then_the_first_to_review_then_the_first_to_post():
    m = re.search(r"export const pickOutput = .*?\n  (.+);", QUEUE)
    assert m, "pickOutput is gone from Queue.tsx"
    order = m.group(1)
    picked = order.index("x.output_id === current")
    to_review = order.index('x.status === "pending"')
    first = order.index("items[0]")
    assert picked < to_review < first
    assert "const o = pickOutput(items, current);" in QUEUE


def test_approve_keeps_the_approved_video_on_screen():
    """Without the pick, the reloaded list showed the next video to review: the checklist was gone again."""
    m = re.search(r"const approve = async \(\) => \{(.+?)\};\n", QUEUE)
    assert m
    body = m.group(1)
    assert "/approve`)" in body and "onPick(o.output_id)" in body
    assert body.index("/approve`)") < body.index("onPick(o.output_id)")


def test_finish_posting_opens_the_ready_tab_on_that_video_and_the_card_says_so():
    assert 'onResume={(id) => { setTab("ready"); setCurrent(id);' in QUEUE
    assert 'title={o.status === "approved" ? "Finish posting" : "Ready to review"}' in QUEUE
    # the empty Ready tab no longer claims that approved videos are done
    assert "Everything made so far is approved or posted" not in QUEUE
    assert "Everything made so far is posted or rejected" in QUEUE


def test_the_committed_build_carries_the_change():
    built = (ROOT / "ns_dashboard" / "static" / "app.js").read_text(encoding="utf-8")
    assert "pending,approved" in built and "Everything made so far is posted or rejected" in built
