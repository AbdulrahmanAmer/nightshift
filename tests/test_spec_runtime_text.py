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


def _d_lines() -> list[tuple[int, str]]:
    rows = _section("## 13. Decisions", "## 14.")
    return [(int(m.group(1)), m.group(0)) for m in re.finditer(r"^\| D(\d+) \|.*$", rows, re.MULTILINE)]


def _d_rows() -> dict[int, str]:
    return dict(_d_lines())


def _cites(text: str, ident: str) -> bool:
    """True when `text` names the id as a whole word ("D3" is not found in "D31")."""
    return re.search(rf"(?<!\w){ident}(?!\w)", text) is not None


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
    """The claim in 15.3 ("no product code posts job progress") holds: nothing calls `publish_event`.

    A raw POST would dodge a grep for the method, so the only files that may name the supervisor
    `/events` path are the route itself, the client that wraps it, the dashboard's GET relay and the
    dashboard's fake supervisor.
    """
    callers: list[str] = []
    mentions: set[str] = set()
    packages = ("ns_common", "ns_studio", "ns_dashboard", "ns_supervisor", "ns_tts", "installer", "launchers")
    for pkg in packages:
        for path in (ROOT / pkg).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            for n, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if ".publish_event(" in text:
                    callers.append(f"{rel}:{n}")
                if "/events" in text and "api/events" not in text:
                    mentions.add(rel)
    assert callers == []
    assert mentions <= {
        "ns_dashboard/fakes.py",
        "ns_dashboard/supervisor_link.py",
        "ns_supervisor/app.py",
        "ns_supervisor/client.py",
    }, mentions


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


RUNTIME_TEXT_ROWS = {
    "`/acquire` health wait": ("300 s", "600 s", "config\\supervisor.json"),
    "Supervisor `/events` kinds": ("`service`", "`update`", "`run`", "`GET /api/events`", "F-B19"),
    "Voice library files": ("`speaker.spk`", "`speaker.bin`", "`files`", "CR-C4-6"),
}
# D23 to D30 belong to the installer spec text (P37); the three rows of this file come after them.
FIRST_RUNTIME_TEXT_ID = 31


def _runtime_text_ids() -> dict[str, int]:
    ids: dict[str, int] = {}
    for number, row in _d_rows().items():
        for title in RUNTIME_TEXT_ROWS:
            if f"| {title}" in row:
                ids[title] = number
    return ids


def test_decision_rows_for_the_runtime_text_are_numbered_after_the_installer_rows():
    """Section 13 gains one row per decision; the ids do not reuse D23 to D30 (installer spec text, P37)."""
    rows = _d_rows()
    numbers = [n for n, _ in _d_lines()]
    assert len(numbers) == len(set(numbers))  # no id twice
    assert numbers == sorted(numbers)
    ids = _runtime_text_ids()
    assert sorted(ids.values()) == [FIRST_RUNTIME_TEXT_ID + i for i in range(3)], ids
    for title, needles in RUNTIME_TEXT_ROWS.items():
        row = rows[ids[title]]
        assert len(row.split(" | ")) == 4, title  # | # | Question | Default | Why |
        for needle in needles:
            assert needle in row, (title, needle)


def test_every_decision_id_cited_in_the_runtime_text_is_its_own_row():
    """The lines that cite a decision (6.1, 6.2, 15.3, 15.4, the status rows) cite the row's own id."""
    ids = _runtime_text_ids()
    status = (ROOT / "docs" / "FEATURE_STATUS.md").read_text(encoding="utf-8")
    voice = next(p for p in SPEC.split("\n\n") if p.startswith("**Voice library"))
    cases = [
        (_line("- **`/acquire`**"), "`/acquire` health wait"),
        (_line("Supervisor API additions"), "Supervisor `/events` kinds"),
        (_line("| POST | `/events`"), "Supervisor `/events` kinds"),
        (voice, "Voice library files"),
    ]
    for text, title in cases:
        assert _cites(text, f"D{ids[title]}"), (title, text[:80])
        others = {f"D{n}" for t, n in ids.items() if t != title}
        assert not any(_cites(text, d) for d in others), (title, text[:80])
        assert not any(_cites(text, f"D{n}") for n in (23, 24, 25)), (title, text[:80])
    for needle, title in (
        ("/acquire: poll /health", "`/acquire` health wait"),
        ("GET /events server-sent events", "Supervisor `/events` kinds"),
        ("Voice library layout", "Voice library files"),
    ):
        row = next(line for line in status.splitlines() if needle in line and line.startswith("| built"))
        assert f"13 D{ids[title]} " in row, (title, row[:80])
        assert not any(_cites(row, f"D{n}") for n in (23, 24, 25)), (title, row[:80])


def test_job_frames_sentence_names_the_shutdown_frame():
    """The engine's shutdown publishes hub kind `job` with {draining: true}: not a job record."""
    engine = (ROOT / "ns_dashboard" / "svc_engine.py").read_text(encoding="utf-8")
    assert 'hub.publish("job", {"draining": True})' in engine
    para = next(
        p for p in _section("### 15.3", "### 15.4").split("\n\n") if p.startswith("Job progress is not")
    )
    assert "{draining: true}" in para and "job record" in para


@pytest.mark.parametrize("anchor", ["§6.1", "§15.3", "§15.4"])
def test_each_new_decision_row_names_its_spec_section(anchor: str):
    rows = _d_rows()
    assert any(anchor in rows[n] for n in _runtime_text_ids().values())
