"""Every sentence that promises "the next nightly run" says that one comes only while Automatic runs are on.

Since the nightly run is a switch in Settings, such a promise is untrue for an owner who switched it off,
and a card's words are fixed when it is written (review of block B's integration). This guard reads the
source: a new sentence with the promise and without the condition fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROMISE = "next nightly run"
CONDITION = ("WHILE_NIGHTLY_ON", "Automatic runs are on")
NEAR = 6  # lines after the promise within which the same message must carry the condition


def promises() -> list[tuple[str, int]]:
    found = []
    for path in sorted((ROOT / "ns_studio").rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if PROMISE in line and not line.lstrip().startswith("#"):
                found.append((str(path.relative_to(ROOT)).replace("\\", "/"), i))
    return found


def test_there_are_promises_to_check() -> None:
    assert len(promises()) >= 7, "the sentences moved: point this guard at where they live now"


@pytest.mark.parametrize(("rel", "at"), promises())
def test_a_promised_nightly_run_names_the_switch(rel: str, at: int) -> None:
    lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
    message = "\n".join(lines[at : at + NEAR + 1])
    assert any(c in message for c in CONDITION), f"{rel}:{at + 1} promises a nightly run without the switch"


def test_the_condition_names_the_place_to_change_it() -> None:
    from ns_studio.ownership import WHILE_NIGHTLY_ON

    assert "Automatic runs" in WHILE_NIGHTLY_ON and "Settings" in WHILE_NIGHTLY_ON
