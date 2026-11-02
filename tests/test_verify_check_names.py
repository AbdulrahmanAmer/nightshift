"""C1/C2: the installer reads `ns verify` check names that really exist.

s08_smoke once matched `llm` and `mtp` while `ns verify` reports `llama.start`, `llama.health`,
`llama.json_completion` and has no MTP check; the fake runner faked the wrong names too, so nothing failed.
"""

from pathlib import Path
from typing import Any

import pytest

from installer.stage2.simulate import VERIFY_CHECKS, FakeRunner
from installer.stage2.steps.s08_smoke import failing
from installer.stage2.web import labels
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_supervisor import check_names as names
from ns_supervisor import verify
from ns_supervisor.testing import StubTable, SyncSupervisor
from tests.test_ns_verify import good_deps


def real_names(tmp_path: Path, *, quick: bool) -> list[str]:
    paths = Paths(tmp_path / "ns")
    rt = Runtime.default("gpu")
    sync = SyncSupervisor(paths, rt, resolver=StubTable())
    try:
        report = verify.run_verify(paths, rt, sync, quick=quick, deps=good_deps())
    finally:
        sync.close()
    return [c.name for c in report.checks if not c.name.startswith("bench.")]


def test_the_fake_runner_reports_exactly_the_checks_the_real_quick_verify_records(tmp_path: Path) -> None:
    assert list(VERIFY_CHECKS) == real_names(tmp_path, quick=True)
    rows = FakeRunner(tmp_path / "d")._verify_report()["checks"]
    assert [r["name"] for r in rows] == real_names(tmp_path / "again", quick=True)


def test_every_constant_is_a_check_the_full_verify_can_record(tmp_path: Path) -> None:
    full = set(real_names(tmp_path, quick=False))
    constants = {
        v for k, v in vars(names).items() if k.isupper() and isinstance(v, str) and not k.endswith("PREFIX")
    }
    # tts.engine is recorded only when the voice service reports the reference engine (NS-W301)
    assert constants - {names.TTS_MODE_PREFIX, names.TTS_ENGINE} <= full, constants - full


def test_there_is_no_mtp_check_in_verify(tmp_path: Path) -> None:
    assert not any("mtp" in n for n in real_names(tmp_path, quick=False))


@pytest.mark.parametrize(
    ("name", "prefix"),
    [
        (names.LLAMA_START, names.LLAMA_PREFIX),
        (names.LLAMA_HEALTH, names.LLAMA_PREFIX),
        (names.LLAMA_JSON, names.LLAMA_PREFIX),
        (names.EMBED_VECTOR, names.EMBED_PREFIX),
        (names.TTS_PRESET, names.TTS_PREFIX),
        (names.WHISPER_LOAD, names.WHISPER_PREFIX),
        (names.NVENC_PROBE, names.NVENC_PREFIX),
    ],
)
def test_a_failing_real_check_is_seen_by_its_area(name: str, prefix: str) -> None:
    report = {n: n != name for n in VERIFY_CHECKS}
    assert failing(report, prefix)
    assert not failing({n: True for n in VERIFY_CHECKS}, prefix)


def test_every_real_check_gets_a_setup_row() -> None:
    checks: list[dict[str, Any]] = [{"name": n, "passed": True, "value": "1"} for n in VERIFY_CHECKS]
    rows = labels.verify_rows(checks)
    assert [r["name"] for r in rows] == ["engine", "llm", "embed", "tts", "whisper", "nvenc", "vram"]
    assert all(r["label"] != r["name"] and r["status"] == "passed" for r in rows)


def test_the_setup_labels_use_the_same_names_as_check_names() -> None:
    # labels.py runs on the bare Python of the setup server and cannot import check_names, so it repeats them
    for const in (
        "LLAMA_PREFIX", "EMBED_PREFIX", "TTS_PREFIX", "WHISPER_PREFIX", "NVENC_PREFIX",
        "LLAMA_TOK_S", "EMBED_VECTOR", "TTS_START", "TTS_VOICES", "WHISPER_LOAD", "NVENC_PROBE",
    ):  # fmt: skip
        assert getattr(labels, const) == getattr(names, const), const
    assert names.SUPERVISOR_STATUS.startswith("supervisor.") and names.VRAM_IDLE.startswith("vram.")
    assert names.JOB_FAKE_SERVICES.startswith("job.")
    prefixes = {p for p, _, _ in labels.VERIFY_AREAS}
    assert {"supervisor.", "vram.", "job."} <= prefixes
