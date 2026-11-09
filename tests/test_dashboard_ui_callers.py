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
