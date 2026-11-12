"""Routes that had no caller (system-gaps.md #21, #22, #23, #26, #27, #28, #30) are called by the pages.

The built bundle is minified, so this reads `ns_dashboard/ui/src`; the `ui-build` job keeps `static/` equal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "ns_dashboard" / "ui" / "src"
PAGES = {p.name: p.read_text(encoding="utf-8") for p in sorted((SRC / "pages").glob("*.tsx"))}
APP = (SRC / "lib" / "app.tsx").read_text(encoding="utf-8")

CALLS = [
    ("Queue.tsx", '"/runs?limit=1"'),  # 26 Last night
    ("Assistant.tsx", '"/assistant/conversations"'),  # 21
    ("Assistant.tsx", "/assistant/history?conversation_id="),  # 21
    ("Assistant.tsx", '"/assistant/queue/run"'),  # 21
    ("Settings.tsx", "download_url"),  # 22 diagnostics download (GET /diagnostics/latest)
    ("Library.tsx", "/library/formats/${encodeURIComponent(f.id)}"),  # 23
    ("Library.tsx", '"/library/series"'),  # 8
    ("Create.tsx", '"/jobs/clip/rerun"'),  # 18
    ("Create.tsx", "script: ownText.trim(), script_title:"),  # G-43 a: the owner's own story text
    ("Library.tsx", "api.del(`/voices/${encodeURIComponent(v.name)}`)"),  # G-38 remove a voice
    ("Library.tsx", "api.del(`/library/ideas/${encodeURIComponent(String(i.idea_id))}`)"),  # G-40
    ("Library.tsx", "api.del(`/library/stories/${encodeURIComponent(s.job_id)}`)"),  # G-39
    ("Library.tsx", "api.del(`/library/footage/${encodeURIComponent(c.source)}"),  # G-42 (moves aside)
    ("Settings.tsx", '"/engine/warmup"'),  # 27
    ("Settings.tsx", '"/engine/stop_all"'),  # 27
    ("Settings.tsx", '"/jobs/verify"'),  # 27
    ("Settings.tsx", '"/logs/components"'),  # 28
    ("Settings.tsx", '"/settings/export"'),  # 17
    ("Settings.tsx", '"/settings/import"'),  # 17
    ("Settings.tsx", '"/settings/uninstall"'),  # 17
]


@pytest.mark.parametrize(("page", "needle"), CALLS)
def test_page_calls_the_route(page: str, needle: str):
    assert needle in PAGES[page], f"{page} no longer calls {needle}"


def test_first_start_reads_the_setup_windows_choices():
    assert "owner_name" in APP and "ui_theme" in APP
    assert "ownerName" in PAGES["Queue.tsx"]


def test_the_series_tab_is_no_longer_a_stub():
    assert "arrive with the next update" not in PAGES["Library.tsx"]


TYPES = (SRC / "lib" / "types.ts").read_text(encoding="utf-8")


def test_f_e7_the_update_card_shows_the_release_notes_after_an_update():
    """F-E7: Settings reads `release_notes` (the new version's CHANGELOG section) from GET /update/state."""
    assert "release_notes?: string" in TYPES
    settings = PAGES["Settings.tsx"]
    assert "release_notes" in settings
    assert "What is new in" in settings


def test_f_e7_the_update_state_carries_the_release_notes_the_card_reads(tmp_home: Path):
    """The state file the updater wrote after the swap reaches the browser with `release_notes` untouched."""
    from ns_common.paths import Paths
    from ns_dashboard import updater

    paths = Paths.from_env()
    notes = "## [0.2.0] - 2026-11-10\n\n- Faster captions\n  that wrap onto a second line"
    state = {"state": "done", "old": "0.1.0", "new": "0.2.0", "message": "Now on", "release_notes": notes}
    f = paths.home / "run" / "update_state.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(state), encoding="utf-8")
    got = updater.read_update_state(paths, alive=lambda _pid: False)
    assert got["state"] == "done" and got["new"] == "0.2.0" and got["release_notes"] == notes


def test_f_d2_the_browser_shows_update_and_run_events():
    """F-D2: the events stream keeps the `update` and `run` kinds and app.tsx turns them into toasts."""
    assert 'm.kind === "update"' in APP
    assert 'm.kind === "run"' in APP
    # the supervisor envelope {ts, kind, data} reaches the browser as payload, so the toasts read payload.data
    assert "notifyUpdate(m.payload?.data)" in APP
    assert "notifyRun(m.payload?.data)" in APP


def test_f_d2_the_run_toast_does_not_claim_new_videos():
    """`run_finished` fires after every run (failed or empty too), so the toast must not promise videos."""
    assert "New videos wait" not in APP
    assert "The nightly run has finished" in APP


def test_f_d2_the_browser_facing_payload_has_the_fields_the_toasts_read(tmp_home: Path):
    """FakeSupervisor -> SupervisorLink -> EventRelay -> EventHub -> iter_sse: the frame the browser parses
    has `update_available` / `latest` / `event` under `payload.data` (the supervisor envelope)."""
    import threading

    from ns_common.logging import NsLogger
    from ns_common.paths import Paths
    from ns_dashboard.events import EventHub, iter_sse
    from ns_dashboard.fakes import FakeSupervisor
    from ns_dashboard.supervisor_link import EventRelay, SupervisorLink

    with FakeSupervisor() as sup:
        sup.events = [
            {
                "ts": 1,
                "kind": "update",
                "data": {"update_available": True, "latest": "0.2.0", "current": "0.1.0"},
            },
            {"ts": 2, "kind": "run", "data": {"event": "finished"}},
        ]
        hub = EventHub()
        sub = hub.subscribe()
        log = NsLogger("dashboard", Paths.from_env().logs)
        relay = EventRelay(SupervisorLink(sup.url), hub, log, retry_s=0.01)
        assert relay.relay_once() == 2
    frames = [
        json.loads(f[len("data:") :])
        for f in iter_sse(sub, lambda: {"state": "idle"}, heartbeat_s=0.01, limit=5, stop=threading.Event())
    ]
    by_kind = {f["kind"]: f["payload"] for f in frames if f["kind"] != "status"}
    assert by_kind["update"]["data"] == {"update_available": True, "latest": "0.2.0", "current": "0.1.0"}
    assert by_kind["run"]["data"]["event"] == "finished"
    # nothing sits at the top level of the payload, which is why app.tsx must read `.data`
    assert "update_available" not in by_kind["update"] and "event" not in by_kind["run"]
