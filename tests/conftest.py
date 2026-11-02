from pathlib import Path

import pytest


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(home))
    return home


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _roomy_outbox_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """pytest's temp folders on the CI runner are about 110 characters deep; the Outbox path guard (200) is
    meant for a real home like C:/Users/Name/Nightshift, so ordinary tests run with a generous limit and the
    guard's own test sets 200 back."""
    from ns_studio.media import outbox

    monkeypatch.setattr(outbox, "MAX_PATH", 400)
