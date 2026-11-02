"""C2: the end-of-run message reaches the owner when the nightly session has no desktop (review P3)."""

import json
from datetime import datetime
from pathlib import Path

from ns_common.paths import Paths
from ns_supervisor import notify


def test_a_session_without_a_desktop_leaves_a_morning_note_and_shows_no_toast(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    shown: list[str] = []
    ok = notify.toast_or_note(
        "Nightshift", "2 Shorts ready for review", paths=paths, interactive=lambda: False,
        show=lambda t, m: shown.append(m) or True,
    )
    assert ok is False and shown == []
    notes = notify.read_notes(paths)
    assert [n["message"] for n in notes] == ["2 Shorts ready for review"]
    raw = json.loads(notify.note_path(paths).read_text(encoding="utf-8"))
    assert raw["notes"][0]["title"] == "Nightshift"


def test_a_toast_that_shows_leaves_no_note(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    assert notify.toast_or_note("T", "M", paths=paths, interactive=lambda: True, show=lambda t, m: True)
    assert notify.read_notes(paths) == []


def test_a_toast_that_fails_falls_back_to_the_note(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    assert not notify.toast_or_note("T", "M", paths=paths, interactive=lambda: True, show=lambda t, m: False)
    assert len(notify.read_notes(paths)) == 1


def test_notes_pile_up_in_order_are_capped_and_can_be_cleared(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    for i in range(25):
        notify.leave_note(paths, "Nightshift", f"note {i}", now=datetime(2026, 11, 3, 3, 0, i % 60))
    notes = notify.read_notes(paths)
    assert len(notes) == notify.NOTE_KEEP
    assert notes[0]["message"] == "note 5" and notes[-1]["message"] == "note 24"
    notify.clear_notes(paths)
    assert notify.read_notes(paths) == []
    notify.clear_notes(paths)                                       # idempotent


def test_a_damaged_note_file_reads_as_empty_and_is_replaced(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    notify.note_path(paths).parent.mkdir(parents=True)
    notify.note_path(paths).write_text("{torn", encoding="utf-8")
    assert notify.read_notes(paths) == []
    notify.leave_note(paths, "T", "after the crash")
    assert [n["message"] for n in notify.read_notes(paths)] == ["after the crash"]


def test_the_real_session_check_answers_without_raising() -> None:
    assert isinstance(notify.session_is_interactive(), bool)
