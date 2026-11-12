"""Card verify-limits: the model test measures what it claims, and the kit goes on after a missed target.

T1 to T8 of the card (refs/cards/verify-limits.md). The voice service is a stand-in at `ns verify`'s own
boundary (`verify.TtsClient`, with a fake clock in `Deps.clock`) where the numbers of the studio PC are needed
(12 s for 5.5 s of audio cannot be waited out in a test); the stream and its stamps run through the product's
own engine, service and client against a stand-in `qwen-tts` program (T6). Box 3's stderr lines are the
fixture of the parser.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.models import GateCheck, GateReport, Runtime
from ns_common.paths import Paths
from ns_common.tts_client import TtsBatch, TtsClient, TtsFile, TtsItem, TtsStamp, read_stamps
from ns_supervisor import check_names as names
from ns_supervisor import verify
from ns_supervisor.testing import StubTable, SyncSupervisor
from ns_tts.engine import QwenttsEngine, tool_words
from tests.test_ns_verify import good_deps
from tests.test_tts_qwentts import BASE, CUSTOM, DESIGN, RYAN, TOKENIZER_FILE

# ------------------------------------------------------------------------------- the voice at verify's door
SINGLE_AUDIO_S = 5.510375  # box 1: "voice=Ryan audio_s=5.510375", 12.0 s for the whole call


def stamps_at(
    rate: float, *, start_s: float = 3.0, n: int = 6, audio_s: float = 4.0
) -> list[tuple[float, float]]:
    """Six utterances of `audio_s` each: the first complete at `start_s`, each next `rate * audio_s` later."""
    return [(start_s + k * rate * audio_s, audio_s) for k in range(n)]


class Clock:
    """`Deps.clock`: moves only when the stand-in voice works."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def voice_service(
    clock: Clock,
    *,
    single_s: float = 12.0,
    single_audio_s: float = SINGLE_AUDIO_S,
    stamps: list[tuple[float, float]] | None = None,
    batch_s: float = 9.0,
    batch_error: Exception | None = None,
    record: list[Any] | None = None,
) -> type:
    """A stand-in for `TtsClient` as `ns verify` makes it: one sentence takes `single_s` and gives
    `single_audio_s` (0: no sound); the batch takes `batch_s` and answers with `stamps` (None: no stream)."""
    seen = record if record is not None else []

    class Voice:
        def __init__(self, base_url: str, *, timeout_s: float = 1800, client: Any = None) -> None:
            self.client = SimpleNamespace(close=lambda: None)

        def voices(self) -> list[dict[str, str]]:
            return [{"name": "Ryan", "mode": "preset", "source": "builtin"}]

        def speak(self, text: str, voice: str, speed: float, out_path: Path, **kw: Any) -> float:
            seen.append(("speak", voice))
            clock.t += single_s
            return single_audio_s

        def speak_batch_stamped(self, items: list[TtsItem], out_dir: Path, **kw: Any) -> TtsBatch:
            seen.append(("batch", [i.voice for i in items], [i.text for i in items]))
            clock.t += batch_s
            if batch_error is not None:
                raise batch_error
            if stamps is None:
                files = [TtsFile(i.id, out_dir / f"{i.id}.wav", 4.0) for i in items]
                return TtsBatch(files, None)
            files = [
                TtsFile(i.id, out_dir / f"{i.id}.wav", a) for i, (_at, a) in zip(items, stamps, strict=False)
            ]
            return TtsBatch(files, tuple(TtsStamp(at, a) for at, a in stamps))

    return Voice


@pytest.fixture
def sup(tmp_path: Path) -> Iterator[tuple[Paths, Runtime, SyncSupervisor]]:
    paths = Paths(tmp_path / "ns")
    rt = Runtime.default("gpu")
    sync = SyncSupervisor(paths, rt, resolver=StubTable())
    yield paths, rt, sync
    sync.close()


def verify_with(
    sup: tuple[Paths, Runtime, SyncSupervisor], monkeypatch: pytest.MonkeyPatch, *, quick: bool = False,
    **voice: Any,
) -> GateReport:
    """`run_verify` on the stub services (story model, memory index all green) with the stand-in voice."""
    paths, rt, sync = sup
    clock = Clock()
    monkeypatch.setattr(verify, "TtsClient", voice_service(clock, **voice))
    deps = good_deps(clock=clock, tts_engine=lambda url: "qwentts")
    return verify.run_verify(paths, rt, sync, quick=quick, deps=deps)


def by_name(report: GateReport) -> dict[str, GateCheck]:
    return {c.name: c for c in report.checks}


# --------------------------------------------------------------------------------------------------------- T1
def test_t1_a_slow_single_call_and_a_fast_batch_pass(sup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Box 1's voice: one sentence 12.0 s for 5.51 s of audio (one start of the voice program), speaking at
    0.17 in one batch. The talker row says it spoke, the rate row passes, the start is a bench: exit 0."""
    calls: list[Any] = []
    report = verify_with(sup, monkeypatch, stamps=stamps_at(0.17), record=calls)
    rows = by_name(report)
    preset = rows[names.TTS_PRESET]
    assert preset.passed and preset.value == 12.0 and preset.limit is None, preset
    assert preset.log == f"voice=Ryan audio_s={SINGLE_AUDIO_S}"
    rate = rows[names.TTS_RATE]
    assert rate.passed and rate.value == 0.17 and rate.limit == "<= 0.5 RTF", rate
    assert "speaking only" in (rate.log or "")
    start = rows[names.TTS_START_S]
    assert start.passed and start.value == 3.0 and start.limit is None, start
    bench = rows["bench.tts_start_s"]
    assert bench.passed and bench.value == 3.0 and bench.limit == "<= 15 s (soft)"
    assert report.benchmarks["tts_rtf"] == 0.17 and report.benchmarks["tts_start_s"] == 3.0
    assert report.passed, [(c.name, c.log) for c in report.checks if not c.passed]  # `ns verify` exits 0
    assert verify.split_failures(report.checks) == ([], [])
    # the speed was measured on ONE batch of six sentences with the first talker's voice
    batches = [c for c in calls if c[0] == "batch"]
    assert len(batches) == 1 and batches[0][1] == ["Ryan"] * 6 and len(set(batches[0][2])) == 6


def test_t1_a_slow_start_is_logged_never_a_failure(sup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    report = verify_with(sup, monkeypatch, stamps=stamps_at(0.17, start_s=40.0))
    bench = by_name(report)["bench.tts_start_s"]
    assert bench.passed and bench.value == 40.0 and "soft" in (bench.log or "")
    assert report.passed


# --------------------------------------------------------------------------------------------------------- T2
def test_t2_a_slow_speaker_is_one_missed_target(sup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    report = verify_with(sup, monkeypatch, stamps=stamps_at(0.9))
    rows = by_name(report)
    rate = rows[names.TTS_RATE]
    assert not rate.passed and rate.value == 0.9 and rate.limit == "<= 0.5 RTF"
    assert rows[names.TTS_PRESET].passed  # the voice spoke
    assert not rows["bench.tts_rtf"].passed  # the bench records the same figure ...
    missed, broken = verify.split_failures(report.checks)
    assert [c.name for c in missed] == [names.TTS_RATE] and broken == []  # ... and it is ONE target
    assert report.passed is False  # the exit code rule is unchanged: a failed row is exit 1
    last = verify.format_lines(report)[-1]
    assert last == "Result: Everything works. 1 target missed: tts.rate 0.9 (limit <= 0.5 RTF).", last


# --------------------------------------------------------------------------------------------------------- T3
def test_t3_a_voice_that_does_not_speak_is_a_part_that_did_not_work(
    sup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    report = verify_with(sup, monkeypatch, single_audio_s=0.0, stamps=stamps_at(0.17), record=calls)
    rows = by_name(report)
    preset = rows[names.TTS_PRESET]
    assert preset.passed is False and preset.limit is None
    missed, broken = verify.split_failures(report.checks)
    assert missed == [] and [c.name for c in broken] == [names.TTS_PRESET]
    assert names.TTS_RATE not in rows and not any(c[0] == "batch" for c in calls)  # nothing to time
    assert verify.format_lines(report)[-1] == "Result: 1 part did not work: tts.preset."


def test_t3_a_batch_that_fails_is_a_part_that_did_not_work(sup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    report = verify_with(sup, monkeypatch, batch_error=NsError("NS-E310", "qwen-tts exited 3"))
    rate = by_name(report)[names.TTS_RATE]
    assert rate.passed is False and rate.limit is None and "NS-E310" in (rate.log or "")
    missed, broken = verify.split_failures(report.checks)
    assert missed == [] and [c.name for c in broken] == [names.TTS_RATE]
    assert "tts_rtf" not in report.benchmarks


def test_every_failed_row_of_verify_is_said_as_one_kind() -> None:
    """L6 on rows of every shape verify writes: a failed row with a limit is a target, without one a part."""
    checks = [
        GateCheck(name="llama.json_completion", passed=False, value="{}"),
        GateCheck(name="bench.code_tok_s", passed=False, value=10.0, limit=">= 40 tok/s", log="missed"),
        GateCheck(name="vram.idle", passed=False, value=700.0, limit="<= 300 MB"),
        GateCheck(name="bench.idle_vram_mb", passed=False, value=700.0, limit="<= 300 MB"),
        GateCheck(name="bench.model_load_s", passed=True, value=99.0, limit="<= 30 s (soft)"),
        GateCheck(name="tts.clone", passed=True, value=names.TALKER_WARNING_VALUE, log="did not speak"),
    ]
    missed, broken = verify.split_failures(checks)
    assert [c.name for c in missed] == ["bench.code_tok_s", "vram.idle"]
    assert [c.name for c in broken] == ["llama.json_completion"]
    assert verify.verdict(checks) == (
        "1 part did not work: llama.json_completion. 2 targets missed: bench.code_tok_s 10.0 (limit >= 40 "
        "tok/s); vram.idle 700.0 (limit <= 300 MB)."
    )


# --------------------------------------------------------------------------------------------------------- T4
class CardCtl:
    """The supervisor as the idle row sees it: one service that is loaded until `stop_all` (or not at all)."""

    def __init__(self, *, loaded: bool, status_fails: bool = False) -> None:
        self.loaded = loaded
        self.status_fails = status_fails

    def acquire(
        self, service: Any, owner: str, *, model_alias: str | None = None, timeout_s: int = 7200
    ) -> Any:
        raise NsError("NS-E202", f"{service} is not part of this test")

    def release(self, service: Any, owner: str) -> None:
        return None

    def status(self) -> dict[str, Any]:
        if self.status_fails:
            raise NsError("NS-E202", "supervisor unreachable")
        state = {"state": "ready", "pid": 4242} if self.loaded else {"state": "stopped", "pid": None}
        up = {"state": "ready", "pid": 77}  # the Dashboard and the memory index: on the processor, always up
        services = {"llama": state, "tts": {"state": "stopped", "pid": None}, "dashboard": up, "embed": up}
        return {"services": services, "gpu_owner": None}

    def stop_all(self) -> None:
        self.loaded = False


def idle_row(
    tmp_path: Path, ctl: CardCtl, reads: list[int], *, quick: bool = True
) -> tuple[GateReport, list[int]]:
    """The report and the reads of the card's memory left unused (the first read is the baseline; the last
    repeats for every read after it)."""
    left = list(reads)

    def used() -> int:
        return left.pop(0) if len(left) > 1 else left[0]

    deps = good_deps(vram_used=used)
    paths = Paths(tmp_path / "ns")
    return verify.run_verify(paths, Runtime.default("gpu"), ctl, quick=quick, deps=deps), left


def test_t4_the_idle_row_counts_what_nightshift_left(tmp_path: Path) -> None:
    report, _ = idle_row(tmp_path, CardCtl(loaded=False), [800, 806])
    row = by_name(report)[names.VRAM_IDLE]
    assert row.passed and row.value == 6.0 and row.limit == "<= 300 MB"
    # (the Head's ruling on finding 5 of the review: the log also says how long the baseline was waited for)
    assert row.log == (
        "806 MB in use, 800 MB of it held by other programs before the test (read once it stopped falling, "
        "after 0.5 s)"
    )
    assert report.benchmarks["idle_vram_mb"] == 6.0


def test_t4_the_baseline_is_read_once_the_card_stopped_falling(tmp_path: Path) -> None:
    """Review of block E2, finding 5: a model stopped a moment before still counted 2000 MB, then 1200, then
    the card settled at 800 (the other programs). A baseline of 2000 would make 806 after the test look like
    nothing left (`max(0, 806 - 2000)`) and hide any leak; the baseline is 800, read after 1.5 s."""
    report, _ = idle_row(tmp_path, CardCtl(loaded=False), [2000, 1200, 800, 800, 806])
    row = by_name(report)[names.VRAM_IDLE]
    assert row.passed and row.value == 6.0, row
    assert "800 MB of it held by other programs" in (row.log or "") and "after 1.5 s" in (row.log or "")


def test_t4_more_left_than_the_limit_is_a_missed_target(tmp_path: Path) -> None:
    report, _ = idle_row(tmp_path / "a", CardCtl(loaded=False), [800, 1500])
    row = by_name(report)[names.VRAM_IDLE]
    assert row.passed is False and row.value == 700.0 and row.limit == "<= 300 MB"
    assert "1500 MB in use, 800 MB" in (row.log or "")
    missed, _broken = verify.split_failures(report.checks)
    assert [c.name for c in missed] == [names.VRAM_IDLE]  # its bench row is the same target


@pytest.mark.parametrize("status_fails", [False, True])
def test_t4_no_moment_without_our_services_is_na_and_passes(tmp_path: Path, status_fails: bool) -> None:
    report, _ = idle_row(tmp_path, CardCtl(loaded=True, status_fails=status_fails), [5000, 5000])
    row = by_name(report)[names.VRAM_IDLE]
    assert row.passed and row.value == "n/a" and row.limit is None, row
    assert "5000 MB in use" in (row.log or "")
    assert ("could not be read" if status_fails else "llama was loaded") in (row.log or "")
    assert "idle_vram_mb" not in report.benchmarks  # never a guess


def test_t4_the_full_run_reads_the_baseline_after_its_cold_start(tmp_path: Path) -> None:
    """Full: the status shows the story model loaded until the cold start stops it; the baseline is read
    after that, so the row compares."""
    report, _ = idle_row(tmp_path, CardCtl(loaded=True), [900, 950], quick=False)
    row = by_name(report)[names.VRAM_IDLE]
    assert row.passed and row.value == 50.0 and "900 MB of it held by other programs" in (row.log or "")


# --------------------------------------------------------------------------------------------------------- T5
@pytest.fixture
def kit_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    from tests import gpukit_cases

    w = gpukit_cases.World(tmp_path, monkeypatch)
    try:
        yield w
    finally:
        w.close()


def verify_answers(
    world: Any, monkeypatch: pytest.MonkeyPatch, rc: int, checks: list[GateCheck] | None
) -> None:
    """Step 3's `ns verify` ends `rc` after writing a report of `checks` (None: a report it cannot read)."""
    real = world.product.cli

    def cli(program: str, args: list[str], timeout: float) -> tuple[int | None, str]:
        if program == "ns" and args == ["verify"]:
            world.product.calls.append(("cli", program, *args))
            if checks is None:
                f = verify.report_path(world.paths)
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("{not json", encoding="utf-8")
                return rc, "half a report"
            report = GateReport(slice="verify", date="2026-11-12", machine={}, checks=checks)
            verify.write_report(world.paths, report)
            return rc, "\n".join(verify.format_lines(report))
        return real(program, args, timeout)

    monkeypatch.setattr(world.product, "cli", cli)


GREEN = GateCheck(name="llama.tok_s", passed=True, value=137.39)


def test_t5_only_missed_targets_fail_step_3_and_the_kit_goes_on(
    kit_world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = [
        GREEN,
        GateCheck(name="tts.rate", passed=False, value=0.62, limit="<= 0.5 RTF"),
        GateCheck(name="bench.tts_rtf", passed=False, value=0.62, limit="<= 0.5"),
        GateCheck(name="vram.idle", passed=False, value=506.0, limit="<= 300 MB"),
    ]
    verify_answers(kit_world, monkeypatch, 1, checks)
    kit_world.run()
    results = kit_world.results()
    assert results["K2"] == "FAIL", results
    line = kit_world.state()["steps"]["K2"]["line"]
    assert "every model answered" in line, line
    assert "tts.rate 0.62 (limit <= 0.5 RTF)" in line and "vram.idle 506.0 (limit <= 300 MB)" in line, line
    assert "bench.tts_rtf" not in line  # the same target, said once
    assert results["KS"] != "NOT RUN" and results["K3"] == "PASS", results  # the kit went on


@pytest.mark.parametrize(
    ("rc", "checks", "named"),
    [
        (1, [GREEN, GateCheck(name="tts.preset", passed=False, log="NS-E310"),
             GateCheck(name="tts.rate", passed=False, value=0.9, limit="<= 0.5 RTF")], "tts.preset"),
        (1, [GREEN], "none of its rows failed"),
        (1, None, "could not be read"),
    ],
)  # fmt: skip
def test_t5_a_part_that_did_not_work_stops_the_kit(
    kit_world: Any, monkeypatch: pytest.MonkeyPatch, rc: int, checks: list[GateCheck] | None, named: str
) -> None:
    verify_answers(kit_world, monkeypatch, rc, checks)
    kit_world.run()
    results = kit_world.results()
    line = kit_world.state()["steps"]["K2"]["line"]
    assert results["K2"] == "FAIL" and named in line, (results, line)
    assert "every model answered" not in line
    assert results["KS"] == "NOT RUN" and results["K3"] == "NOT RUN", results


def test_the_kit_reads_the_rows_by_the_products_own_rule(tmp_path: Path) -> None:
    from dev.kit import gpukit

    assert gpukit.VERIFY_MIRRORS == verify.MIRRORS
    checks = [
        GateCheck(name="a", passed=False, value=1.0, limit="<= 0"),
        GateCheck(name="b", passed=False),
        GateCheck(name="vram.idle", passed=False, value=9.0, limit="<= 1 MB"),
        GateCheck(name="bench.idle_vram_mb", passed=False, value=9.0, limit="<= 1 MB"),
    ]
    report = GateReport(slice="verify", date="2026-11-12", machine={}, checks=checks)
    f = verify.write_report(Paths(tmp_path / "ns"), report)
    got = gpukit.verify_failures(f)
    missed, broken = verify.split_failures(checks)
    assert got is not None
    assert [r["name"] for r in got[0]] == [c.name for c in missed] == ["a", "vram.idle"]
    assert [r["name"] for r in got[1]] == [c.name for c in broken] == ["b"]


# --------------------------------------------------------------------------------------------------------- T6
# Box 3 of the card, word for word where the studio PC gave the words. The five later [Perf] lines of B were
# reported as their RTF only; their totals here are RTF x 4.00 s (the shape is the program's own).
BOX3_A = (
    "[Load] Talker backend: CUDA0 (CPU threads: 6)\n"
    "[Perf] Total 1222.5 ms (50 frames, 13.68 ms/frame AR, audio 4.00 s, RTF 0.306)\n"
)
BOX3_B_RTF = [0.315, 0.164, 0.177, 0.173, 0.175, 0.159]
BOX3_B = (
    "[Load] Talker backend: CUDA0 (CPU threads: 6)\n"
    "[Perf] Total 1261.2 ms (50 frames, audio 4.00 s, RTF 0.315)\n"
    + "".join(f"[Perf] Total {r * 4000:.1f} ms (50 frames, audio 4.00 s, RTF {r})\n" for r in BOX3_B_RTF[1:])
    + "[Pipeline] Streamed 6 lines to <stdout>.\n"
)

# A stand-in `qwen-tts --stream-by-line -o -`: reads every line, then per line waits PAUSE seconds and writes
# one utterance (a live 44-byte header and 1 s of 24 kHz audio) and its [Perf] line, flushing each.
STAND_IN = """
import os, struct, sys, time
PAUSE = 0.4
HEAD = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 0x7FFFFFFF, b"WAVE", b"fmt ", 16, 1, 1, 24000, 48000, 2, 16,
                   b"data", 0x7FFFFFFF)
err = sys.stderr.buffer
garbage = os.environ.get("NS_STAND_IN_GARBAGE") == "1"
LOAD = b"[Load] Talker backend: CUDA0 (CPU threads: 6)\\n"
err.write(b"\\xff\\xfe[Perf] Total abc ms\\n" if garbage else LOAD)
err.flush()
lines = [ln for ln in sys.stdin.buffer.read().decode("utf-8").splitlines() if ln.strip()]
out = sys.stdout.buffer
for _ in lines:
    time.sleep(PAUSE)
    out.write(HEAD + b"\\x00\\x40" * 24000)
    out.flush()
    if not garbage:
        err.write(b"[Perf] Total 1261.2 ms (50 frames, audio 1.00 s, RTF 0.315)\\n")
        err.flush()
err.write(("[Pipeline] Streamed %d lines to <stdout>.\\n" % len(lines)).encode())
"""


@pytest.fixture
def stand_in_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> QwenttsEngine:
    monkeypatch.delenv("NS_STAND_IN_GARBAGE", raising=False)
    models = tmp_path / "models"
    models.mkdir()
    for name in (CUSTOM, BASE, DESIGN, TOKENIZER_FILE):
        (models / name).write_bytes(b"gguf")
    script = tmp_path / "stand_in_qwen_tts.py"
    script.write_text(STAND_IN, encoding="utf-8")
    run = [sys.executable, str(script)]
    eng = QwenttsEngine(
        tmp_path / "bin", models, timeout_s=60, launcher={"qwen-tts": run, "qwen-codec": run},
        log=NsLogger("tts", tmp_path / "logs"),
    )  # fmt: skip
    eng.load()
    return eng


def said_events(tmp_path: Path) -> list[dict[str, Any]]:
    out = []
    for f in sorted((tmp_path / "logs").glob("tts-*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec["event"] == "qwentts_said":
                out.append(rec["data"])
    return out


def test_t6_the_stream_is_stamped_as_it_arrives(tmp_path: Path, stand_in_engine: QwenttsEngine) -> None:
    audio = stand_in_engine.synth_batch(["One here.", "Two here.", "Three here."], RYAN)
    assert [round(len(x) / 24000, 3) for x in audio] == [1.0, 1.0, 1.0]
    stamps = stand_in_engine.last_stamps
    assert stamps is not None and len(stamps) == 3, stamps
    times = [at for at, _a in stamps]
    assert all(b - a >= 0.2 for a, b in zip(times, times[1:], strict=False)), times  # PAUSE apart, in order
    assert [a for _at, a in stamps] == [1.0, 1.0, 1.0]
    events = said_events(tmp_path)
    assert len(events) == 1, events  # ONE event for the run
    ev = events[0]
    assert ev["tool"] == "qwen-tts" and ev["outcome"] == "ok" and ev["backend"] == "CUDA0"
    assert ev["lines"] == 3 and ev["audio_s"] == 3.0 and ev["wall_s"] >= 1.2
    assert ev["perf_total_ms"] == [1261.2] * 3 and ev["perf_rtf"] == [0.315] * 3


def test_t6_the_stamps_travel_through_the_service_and_the_client(
    tmp_path: Path, stand_in_engine: QwenttsEngine
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ns_tts.server import create_app
    from ns_tts.voices import VoiceLibrary

    url = "http://127.0.0.1:18099"
    app = create_app(stand_in_engine, VoiceLibrary(tmp_path / "voices"))
    client = TtsClient(url, client=TestClient(app, base_url=url))
    items = [TtsItem(k, f"Sentence {k} here.", "Ryan") for k in ("a", "b", "c")]
    batch = client.speak_batch_stamped(items, tmp_path / "out")
    assert [f.id for f in batch.files] == ["a", "b", "c"]
    assert batch.stamps is not None and len(batch.stamps) == 3, batch
    ats = [s.at_s for s in batch.stamps]
    assert ats[0] >= 0.3 and all(b - a >= 0.2 for a, b in zip(ats, ats[1:], strict=False)), ats
    assert [s.audio_s for s in batch.stamps] == [1.0, 1.0, 1.0]
    got = verify.speaking_rate(batch.stamps)
    assert got is not None and got[0] >= 0.2 and got[1] == ats[0], got  # gaps of >= 0.2 s per 1 s of audio
    # the plain call still answers with the files only (every other caller of the batch route)
    assert [f.id for f in client.speak_batch(items, tmp_path / "out2")] == ["a", "b", "c"]


def test_t6_each_utterance_is_stamped_when_its_last_byte_came_whatever_the_way_it_is_written() -> None:
    """Review of block E2, finding 1: three utterances made at 1, 2 and 3 s. Written whole once made, or
    header first and samples when made: both give the stamps 1, 2, 3 (the next header's arrival gave 2, 3 and
    the exit for the first way: one utterance late, a rate half the truth with three)."""
    import struct

    from ns_tts.engine import StreamClock

    head = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 0x7FFFFFFF, b"WAVE", b"fmt ", 16, 1, 1, 24000, 48000, 2,
                       16, b"data", 0x7FFFFFFF)  # fmt: skip
    body = b"\x00\x40" * 24000
    whole = StreamClock()
    for k in range(3):
        whole.feed(head + body, k + 1.0)
    whole.end(3.05)
    first = StreamClock()
    for k in range(3):
        first.feed(head, float(k))
        first.feed(body[:1000], k + 0.5)
        first.feed(body[1000:], k + 1.0)
    first.end(3.05)
    for clock in (whole, first):
        assert len(clock.heads) == 3 and clock.arrivals(ended_whole=True) == [1.0, 2.0, 3.0]
        assert clock.arrivals(ended_whole=False) == [1.0, 2.0]  # after a kill the last may be cut
    stamps = tuple(TtsStamp(at, 1.0) for at in whole.arrivals(ended_whole=True))
    assert verify.speaking_rate(stamps) == (1.0, 1.0, 2.0)


def test_t6_box_3s_lines_give_the_backend_and_the_totals() -> None:
    a = tool_words(BOX3_A)
    assert a == {"backend": "CUDA0", "perf_total_ms": [1222.5], "perf_audio_s": [4.0], "perf_rtf": [0.306]}
    b = tool_words(BOX3_B.encode("utf-8"))
    assert b["backend"] == "CUDA0" and len(b["perf_total_ms"]) == 6 and b["perf_total_ms"][0] == 1261.2
    assert b["perf_rtf"] == BOX3_B_RTF and b["perf_audio_s"] == [4.0] * 6


def test_t6_garbage_on_stderr_gives_an_event_with_empty_figures(
    tmp_path: Path, stand_in_engine: QwenttsEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert tool_words(b"\xff\xfe\x00 [Perf] Total abc ms [Load] backend") == {
        "backend": None, "perf_total_ms": [], "perf_audio_s": [], "perf_rtf": [],
    }  # fmt: skip
    monkeypatch.setenv("NS_STAND_IN_GARBAGE", "1")
    audio = stand_in_engine.synth_batch(["One here.", "Two here."], RYAN)  # no error
    assert len(audio) == 2 and stand_in_engine.last_stamps is not None
    ev = said_events(tmp_path)[-1]
    assert ev["outcome"] == "ok" and ev["backend"] is None and ev["perf_total_ms"] == [] and ev["lines"] == 2


def test_t6_a_stamp_list_that_is_not_well_formed_is_none() -> None:
    assert read_stamps(None) is None and read_stamps([]) is None
    assert read_stamps([{"at_s": 1.0}]) is None and read_stamps([{"at_s": "x", "audio_s": 1}]) is None
    assert read_stamps([{"at_s": float("nan"), "audio_s": 1.0}]) is None
    assert read_stamps([{"at_s": 1, "audio_s": 2}]) == (TtsStamp(1.0, 2.0),)
    # stamps that did not arrive one after the other measure nothing: no rate from them
    assert verify.speaking_rate((TtsStamp(5.0, 4.0), TtsStamp(5.0, 4.0), TtsStamp(5.0, 4.0))) is None
    assert verify.speaking_rate((TtsStamp(5.0, 4.0),)) is None


# --------------------------------------------------------------------------------------------------------- T7
#: `ns verify --quick` on a healthy PC with the stub services (a preset and a clone voice), as before the
#: card: the row names in order, the kind of each value and each limit. Setup (s08) and the updater read them.
QUICK_BEFORE: list[tuple[str, type | None, str | None]] = [
    ("supervisor.status", str, None),
    ("llama.start", float, None),
    ("llama.health", None, None),
    ("llama.json_completion", str, None),
    ("embed.start", float, None),
    ("embed.vector", float, None),
    ("tts.start", float, None),
    ("tts.preset", float, None),
    ("tts.clone", float, None),
    ("tts.voices", str, None),
    ("whisper.load", float, None),
    ("nvenc.probe", str, None),
    ("bench.idle_vram_mb", float, "<= 300 MB"),
    ("vram.idle", float, "<= 300 MB"),
]


def test_t7_quick_mode_keeps_its_rows_kinds_and_exit_code(sup: Any) -> None:
    paths, rt, sync = sup
    report = verify.run_verify(paths, rt, sync, quick=True, deps=good_deps())
    got = [(c.name, None if c.value is None else type(c.value), c.limit) for c in report.checks]
    assert got == QUICK_BEFORE, got
    assert report.passed and report.slice == "verify-quick"  # exit 0
    assert set(report.benchmarks) == {"idle_vram_mb"}
    assert verify.format_lines(report)[-1] == "Result: ALL CHECKS PASSED"


# --------------------------------------------------------------------------------------------------------- T8
@pytest.mark.parametrize("quick", [False, True])
def test_t8_save_rtf_gets_the_single_calls_whole_ratio(
    sup: Any, monkeypatch: pytest.MonkeyPatch, quick: bool
) -> None:
    saved: list[tuple[float, bool]] = []
    monkeypatch.setattr(verify, "save_rtf", lambda paths, rtf, *, quick: saved.append((rtf, quick)))
    verify_with(sup, monkeypatch, quick=quick, stamps=stamps_at(0.17))
    assert saved == [(pytest.approx(12.0 / SINGLE_AUDIO_S), quick)], saved  # 2.18, never the batch's 0.17
