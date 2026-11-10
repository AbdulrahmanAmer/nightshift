from collections.abc import Iterator
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


@pytest.fixture(autouse=True)
def _network_is_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """The downloader probes the network after a failed transfer (a PC that woke from sleep). Tests use fake
    hosts and fake sleeps, so the probe says "up" unless a test sets its own."""
    from installer.stage2 import downloader

    monkeypatch.setattr(downloader, "network_is_up", lambda url, timeout=5.0: True)


@pytest.fixture(autouse=True)
def _stage_table_is_restored() -> Iterator[None]:
    """Put `FORMAT_STAGES` back after every test.

    `jobs.prepare_plan` writes the policy gates and the Short combos into the module-level stage table
    (`policy/gate.py` `install_gates`, `variation/stages.py` `install_short_combos`), so a test that
    prepares a plan left the table changed for every later test in the same process. Which tests share a
    process depends on the shard split: `test_job_disk_room` then `test_policy_p04` failed on PR 206 and
    not on main. The product fix (card D-stages: the table is never written after the stage modules are
    loaded) is under way; until it lands this keeps one test from failing another.
    """
    import sys

    orch = sys.modules.get("ns_studio.orchestrator")
    before = {k: list(v) for k, v in orch.FORMAT_STAGES.items()} if orch is not None else None
    yield
    orch = sys.modules.get("ns_studio.orchestrator")
    if orch is None:
        return
    if before is None:  # loaded during this test: nothing older to go back to
        return
    orch.FORMAT_STAGES.clear()
    orch.FORMAT_STAGES.update({k: list(v) for k, v in before.items()})
