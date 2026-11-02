"""Routes that had no caller (system-gaps.md #21, #22, #23, #26, #27, #28, #30) are called by the pages.

The built bundle is minified, so this reads `ns_dashboard/ui/src`; the `ui-build` job keeps `static/` equal.
"""

from __future__ import annotations

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
