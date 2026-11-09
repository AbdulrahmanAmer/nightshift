"""BUILD_SPEC.md says what the runtime does: the /acquire wait, the /events kinds, the voice library files.

F-H30, F-H34 and F-H35 were spec text that disagreed with the code (120 s against 300 s / 600 s, job progress
on the supervisor stream, `speaker.bin` for every voice). Each test reads the spec lines and checks them
against the constant or the source that decides the behaviour, so text and code cannot drift apart again.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ns_supervisor import health_cfg
from ns_tts import engine as tts_engine

ROOT = Path(__file__).resolve().parents[1]
SPEC = (ROOT / "BUILD_SPEC.md").read_text(encoding="utf-8")


def _line(prefix: str) -> str:
    return next(line for line in SPEC.splitlines() if line.startswith(prefix))


def _section(start: str, end: str) -> str:
    a = SPEC.index(start)
    return SPEC[a : SPEC.index(end, a)]


def _d_rows() -> dict[int, str]:
    rows = _section("## 13. Decisions", "## 14.")
    return {int(m.group(1)): m.group(0) for m in re.finditer(r"^\| D(\d+) \|.*$", rows, re.MULTILINE)}


def test_acquire_line_states_the_health_wait_the_engine_uses():
    """F-H30: 6.1 /acquire says 300 s on an SSD, 600 s on an HDD or unknown drive."""
    line = _line("- **`/acquire`**")
    assert "120 s" not in line
    assert health_cfg.timeout_for_drive("SSD") == health_cfg.DEFAULT_HEALTH_TIMEOUT_S == 300.0
    assert health_cfg.timeout_for_drive("HDD") == health_cfg.timeout_for_drive("Unknown") == 600.0
    assert "300 s on an SSD" in line and "600 s on an HDD or an unknown drive" in line
    assert "config\\supervisor.json" in line and "health_timeout_s" in line and "`NS-E203`" in line
    assert "every 500 ms" in line


def test_events_text_names_the_kinds_the_supervisor_publishes():
    """F-H34: 15.3 /events lists the kinds core.py publishes; job progress is the dashboard's /api/events."""
    core = (ROOT / "ns_supervisor" / "core.py").read_text(encoding="utf-8")
    published = set(re.findall(r'self\.publish\(\s*"(\w+)"', core))
    assert published == {"service", "update", "run"}
    row = _line("| GET | `/events`")
    assert 'kind:"service"|"update"|"run"' in row and '"job"' not in row
    assert "no product code posts" in _line("| POST | `/events`")
    section = _section("### 15.3", "### 15.4")
    assert "`GET /api/events`" in section and "`job` frames" in section and "F-B19" in section
    note = _line("Supervisor API additions")
    assert "job progress" in note and "`GET /api/events`" in note
    assert "service state changes, job progress)" not in note


def test_no_product_code_posts_job_progress_to_the_supervisor_stream():
    """The claim in 15.3 ("no product code posts job progress") holds: `publish_event` has no caller."""
    callers: list[str] = []
    packages = ("ns_common", "ns_studio", "ns_dashboard", "ns_supervisor", "ns_tts", "installer", "launchers")
    for pkg in packages:
        for path in (ROOT / pkg).rglob("*.py"):
            for n, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if ".publish_event(" in text:
                    callers.append(f"{path.relative_to(ROOT)}:{n}")
    assert callers == []


def test_voice_library_text_lists_the_files_each_engine_writes():
    """F-H35: 15.4 lists speaker.spk, speaker.rvq and ref.txt for qwentts and speaker.bin for the others."""
    section = _section("### 15.4", "### 15.5")
    para = next(p for p in section.split("\n\n") if p.startswith("**Voice library"))
    assert (tts_engine.VOICE_SPK, tts_engine.VOICE_RVQ, tts_engine.VOICE_REF_TEXT) == (
        "speaker.spk",
        "speaker.rvq",
        "ref.txt",
    )
    for name in ("speaker.spk", "speaker.rvq", "ref.txt", "speaker.bin"):
        assert f"`{name}`" in para
    assert "{voice.json, speaker.bin, sample.wav}" not in para
    assert "`files`" in para and "CR-C4-6" in para
    assert "reference" in para and "fake" in para and "qwentts" in para
    assert "<name>.voice.zip" in section


def test_decision_rows_for_the_runtime_text_are_numbered_after_the_last_row():
    """Section 13 gains one row per decision: the /acquire wait, the /events kinds, the voice files."""
    rows = _d_rows()
    numbers = sorted(rows)
    assert numbers == list(range(1, numbers[-1] + 1))  # no gap, no repeat
    wanted = {
        "`/acquire` health wait": ("300 s", "600 s", "config\\supervisor.json"),
        "Supervisor `/events` kinds": ("`service`", "`update`", "`run`", "`GET /api/events`", "F-B19"),
        "Voice library files": ("`speaker.spk`", "`speaker.bin`", "`files`", "CR-C4-6"),
    }
    for title, needles in wanted.items():
        hits = [row for row in rows.values() if f"| {title}" in row]
        assert len(hits) == 1, title
        assert len(hits[0].split(" | ")) == 4, title  # | # | Question | Default | Why |
        for needle in needles:
            assert needle in hits[0], (title, needle)


@pytest.mark.parametrize("anchor", ["§6.1", "§15.3", "§15.4"])
def test_each_new_decision_row_names_its_spec_section(anchor: str):
    assert any(anchor in row for n, row in _d_rows().items() if n >= 23)
