"""vram-busy: another program holding the graphics card's memory is found BEFORE a model loads.

Entry: the real `Supervisor` with real stub service processes (`ns_supervisor.testing.StubTable`), a starter
that counts the spawns, an injected clock, and H-10's `SmiSource` answered by the simulated card's own
nvidia-smi (`gpu.FakeSmi`) from a scripted list of readings (one per memory query, the last repeats): the
check reads the same text the real tool prints, `[N/A]` per process as under WDDM unless a reading says
otherwise. The night goes through `daily.run_daily` on the fake-services home of
`tests/test_interrupted_nightly.py`, with a run that raises what the engine answers.

Numbers of the card (written out here, so the tests read on a base that has none): margin 512 MiB, the wait
1800 s, a stub service with no model file and no context needs the estimate's 700 MiB.

The rule since review c913351: only a MEASURED need, compared with a SETTLED reading, refuses a load (or makes
a nightly wait), and only for memory OTHER programs hold. So a test of a refusal first gives the model a
measured need (`Engine.measured`, the file `run/vram-need.json` as a load would leave it); an estimate never
refuses (`test_a_first_load_is_tried_with_the_manifests_real_sizes_on_a_16_gb_card`). A reading that does
not fit is read once more to see that it stopped falling, so a refusal takes two readings.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import random
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pytest

from ns_common.errors import NsError
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_supervisor import gpu, procs
from ns_supervisor.core import Supervisor
from ns_supervisor.testing import StubTable, fast_config

#: The most any test of this file may leave in its temp folders (the home, the logs, the need file): a model
#: of a real size is given through the size seam (`vram._mib`), never written (D: filled up, 2026-11-11).
DISK_CAP = 4 * 1024 * 1024


@pytest.fixture(autouse=True)
def _writes_at_most_4_mib(tmp_path: Path) -> Any:
    """After each test: the bytes under its temp folder (the test home lives there too) are at most 4 MiB,
    else the test fails naming the largest file."""
    yield
    files = [(f.stat().st_size, f) for f in tmp_path.rglob("*") if f.is_file()]
    total = sum(n for n, _ in files)
    if total > DISK_CAP:
        size, big = max(files)
        pytest.fail(f"this test left {total} bytes under {tmp_path}; the largest is {big} ({size} bytes)")


MARGIN = 512
WAIT_S = 1800
STUB_NEED = 700  # a stub has no `-m` and no `-c`: the estimate is its load overhead
TOTAL = 16000
Row = tuple[int, str, str, int]  # pid, type (C, G, C+G), process name, MiB


# ------------------------------------------------------------------------------------------ the card
def card(
    free: int, others: Iterable[Row] = (), *, per: str = "na", total: int = TOTAL, **services: int
) -> gpu.Card:
    """A reading with `free` MiB free while nothing of ours is loaded; `others` are the programs on it."""
    rows = list(others)
    base = total - free - sum(r[3] for r in rows)
    return gpu.Card(total_mb=total, base_mb=base, per_process=per, others=rows, services=dict(services))


class Scripted(gpu.FakeSmi):
    """The simulated card's nvidia-smi, answering from a list of readings: the next one on every memory
    query (the check's first read, a re-read while waiting, the measurement after a load)."""

    def __init__(self, paths: Paths, cards: list[gpu.Card], on_read: Callable[[int], None] | None) -> None:
        super().__init__(paths, tenants=lambda: {})
        self.cards, self.on_read, self.reads = cards, on_read, 0
        self.garbage = False  # two compute-apps rows that do not read, among the good ones

    def card(self) -> gpu.Card:
        return self.cards[min(max(self.reads - 1, 0), len(self.cards) - 1)]

    def __call__(self, args: list[str], timeout_s: float) -> Any:
        if args and args[0].startswith("--query-gpu=memory.used"):
            self.reads += 1
            if self.on_read is not None:
                self.on_read(self.reads)
        out = super().__call__(args, timeout_s)
        if self.garbage and args and args[0].startswith("--query-compute-apps"):
            out.stdout = "not a row\nabc, x.exe, [N/A]\n" + out.stdout
        return out


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Engine:
    def __init__(
        self,
        tmp_path: Path,
        cards: list[gpu.Card],
        *,
        on_read: Callable[[int], None] | None = None,
        runtime: Runtime | None = None,
        table: Any = None,
        process_probe: Callable[[int], float | None] | None = None,
        smi_fails: bool = False,
    ) -> None:
        self.paths = Paths(tmp_path / "ns")
        self.clock = Clock()
        self.starts: list[str] = []
        self.events: list[dict[str, Any]] = []

        def starter(d: Any, logs: Path, job: Any = None) -> procs.Proc:
            self.starts.append(d.name)
            return procs.start(d, logs, job=job)

        self.sup = Supervisor(
            self.paths,
            runtime or Runtime.default("gpu"),
            config=fast_config(),
            resolver=table or StubTable(),
            clock=self.clock,
            starter=starter,
            process_probe=process_probe,
        )
        self.smi = Scripted(self.paths, cards, on_read)
        self.smi.tenants = self.sup._gpu_tenants
        if smi_fails:
            self.smi.cards = [gpu.Card(fail="missing")]
        self.sup.smi = gpu.SmiSource(self.smi)

    def run(self, coro: Callable[[], Any]) -> Any:
        async def main() -> Any:
            q = self.sup.subscribe()
            try:
                return await coro()
            finally:
                while not q.empty():
                    self.events.append(q.get_nowait())
                await self.sup.stop_all()
                self.sup.close_job()

        return asyncio.run(main())

    async def acquire(self, service: str, owner: str, **kw: Any) -> dict[str, Any]:
        """`Supervisor.acquire` with the keywords this version knows (the base has no `vram_wait`)."""
        known = inspect.signature(self.sup.acquire).parameters
        return await self.sup.acquire(service, owner, **{k: v for k, v in kw.items() if k in known})

    def log(self, event: str) -> list[dict[str, Any]]:
        out = []
        for f in sorted(self.paths.logs.glob("supervisor-*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                rec = json.loads(line)
                if rec["event"] == event:
                    out.append(rec)
        return out

    def measured(
        self, mb: int, service: str = "llama", *, idle: int | None = None, low: int | None = None
    ) -> None:
        """`run/vram-need.json` as an earlier load of this table's `service` left it: measured at `mb` (a
        failed load's lower bound `low`), and the idle card at `idle` MiB."""
        d = self.sup.resolver(service, "code")
        cmd = list(d.cmd)
        flags: list[str] = []
        skip = False
        for a in cmd[1:]:
            if skip:
                skip = False
            elif a in ("-m", "-c", "--port", "--host", "--alias"):
                skip = True
            else:
                flags.append(a)
        entry: dict[str, Any] = {
            "service": d.name, "variant": d.variant, "model": cmd[cmd.index("-m") + 1] if "-m" in cmd else "",
            "ctx": int(cmd[cmd.index("-c") + 1]) if "-c" in cmd else None, "flags": " ".join(flags),
            "mb": [mb] if mb else [],
        }  # fmt: skip
        if low:
            entry["min_mb"] = low
        f = self.paths.home / "run" / "vram-need.json"
        data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"version": 1, "entries": []}
        data["entries"].append(entry)
        if idle is not None:
            data["idle"] = {"mb": idle}
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data), encoding="utf-8")

    def vram_events(self) -> list[dict[str, Any]]:
        return [e["data"] for e in self.events if e["data"].get("event") == "vram_wait"]


async def refused(eng: Engine, service: str, owner: str, **kw: Any) -> NsError:
    try:
        await eng.acquire(service, owner, **kw)
    except NsError as e:
        return e
    raise AssertionError(f"{owner} was given {service}: the load went ahead")


# ------------------------------------------------------------------------------------------ 1
def test_a_load_with_room_goes_ahead(tmp_path: Path) -> None:
    eng = Engine(tmp_path, [card(STUB_NEED + MARGIN, [(4242, "C", "game.exe", 0)])])
    eng.measured(STUB_NEED)
    got = eng.run(lambda: eng.acquire("llama", "opencode", model_alias="code"))
    assert got["endpoint"].startswith("http://127.0.0.1:")
    assert eng.starts == ["llama"]
    assert eng.smi.reads >= 1, "the card was not read before the load"


def test_a_load_without_room_is_refused_before_the_process_starts(tmp_path: Path) -> None:
    eng = Engine(tmp_path, [card(STUB_NEED + MARGIN - 1, [(4242, "C", "game.exe", 0)], per="mb")])
    eng.measured(STUB_NEED)
    e = eng.run(lambda: refused(eng, "llama", "opencode", model_alias="code"))
    assert e.code == "NS-E208", str(e)
    assert eng.starts == [], "the starter was called for a load that had no room"
    assert "game.exe" in e.detail and "GB" in e.detail, e.detail
    assert eng.sup.status()["gpu_owner"] is None, "the refusal keeps the card"


# ------------------------------------------------------------------------------------------ 2
def test_a_studio_owner_waits_and_loads_when_the_memory_comes_free(tmp_path: Path) -> None:
    busy = card(1000, [(4242, "C", "game.exe", 9000), (5150, "C+G", "chrome.exe", 1200)])
    seen: dict[int, Any] = {}
    eng: Engine

    def on_read(n: int) -> None:
        if n > 2:  # (the second reading is the check's own: it stopped falling)
            eng.clock.t += 120  # the poll
        seen[n] = (eng.sup.status().get("gpu_wait"), list(eng.starts))

    eng = Engine(tmp_path, [busy, busy, busy, card(8000)], on_read=on_read)
    eng.measured(STUB_NEED)
    eng.run(lambda: eng.acquire("llama", "studio:night-1", model_alias="story", vram_wait=True))
    assert eng.starts == ["llama"], "loaded once, after the memory came free"
    assert sorted(seen)[:4] == [1, 2, 3, 4], f"the card was read {len(seen)} times, not busy x3, free"
    assert seen[4][1] == [], "nothing was started before the fourth reading"
    wait = seen[3][0]
    assert wait is not None, "/status says gpu_wait while waiting"
    assert wait["said"].startswith("Waiting for the graphics card's memory: other programs are using"), wait
    assert "game.exe" in wait["said"] and "chrome.exe" in wait["said"]
    assert wait["owner"] == "studio:night-1" and wait["service"] == "llama"
    assert eng.sup.status().get("gpu_wait") is None, "gpu_wait is emitted only while waiting"
    assert len(eng.vram_events()) == 1, eng.events
    (ended,) = eng.log("vram_wait_ended")
    assert ended["level"] == "INFO" and ended["data"]["waited_s"] == 240


# ------------------------------------------------------------------------------------------ 3
@pytest.mark.parametrize("last", [WAIT_S - 1, WAIT_S])
def test_a_studio_owner_gives_up_after_the_bound(tmp_path: Path, last: int) -> None:
    """Busy at every reading; the clock reaches `last` seconds of waiting, then 1800 (both ends)."""
    times = [600, 1200, last, WAIT_S]
    eng: Engine
    reads_at: list[float] = []

    def on_read(n: int) -> None:
        if n > 2:  # (the first two are the check's own)
            eng.clock.t = 1000.0 + times[min(n - 3, len(times) - 1)]
        reads_at.append(eng.clock.t - 1000.0)

    eng = Engine(tmp_path, [card(500, [(4242, "C", "game.exe", 10000)])], on_read=on_read)
    eng.measured(STUB_NEED)
    e = eng.run(lambda: refused(eng, "llama", "studio:night-1", model_alias="story", vram_wait=True))
    assert e.code == "NS-E208", str(e)
    assert eng.clock.t - 1000.0 == WAIT_S, "given up at 1800 s of waiting, not before"
    assert WAIT_S - 1 in reads_at or last == WAIT_S, "the reading at 1799 s did not give up"
    assert "waited 30 minutes" in e.detail, e.detail
    assert eng.starts == []


# ------------------------------------------------------------------------------------------ 4
@pytest.mark.parametrize(
    "owner", ["opencode", "dashboard:assistant", "dashboard:job", "dashboard:preview-1", "studio:dash-job"]
)
def test_an_interactive_owner_is_refused_at_once(tmp_path: Path, owner: str) -> None:
    """Only the nightly says it may wait (`vram_wait`); everyone else, a dashboard job's studio child too, is
    refused at the first reading."""
    eng = Engine(tmp_path, [card(100, [(4242, "C", "game.exe", 12000)])])
    eng.measured(STUB_NEED)
    e = eng.run(lambda: refused(eng, "llama", owner, model_alias="code"))
    assert e.code == "NS-E208" and eng.starts == []
    assert eng.smi.reads == 2, "no wait: the reading and the one that shows it stopped falling"
    assert eng.vram_events() == []


# ------------------------------------------------------------------------------------------ 5
def test_the_owner_vanishing_during_the_wait_releases_the_card(tmp_path: Path) -> None:
    alive = {"on": True}
    eng: Engine

    def probe(pid: int) -> float | None:
        return 123.0 if alive["on"] else None

    def on_read(n: int) -> None:
        eng.clock.t += 120
        if n == 2:
            alive["on"] = False  # the studio's process ends between two readings

    eng = Engine(
        tmp_path, [card(100, [(4242, "C", "game.exe", 12000)])], on_read=on_read, process_probe=probe
    )
    eng.measured(STUB_NEED)
    e = eng.run(
        lambda: refused(eng, "llama", "studio:night-1", model_alias="story", vram_wait=True, pid=4321)
    )
    assert e.code == "NS-E202" and "gone" in e.detail, str(e)
    assert eng.smi.reads == 2, "the wait ended at the next poll, by the guard, before another reading"
    assert eng.starts == [], "nothing was started for a holder that is gone"
    assert eng.sup.status()["gpu_owner"] is None
    assert [r["data"].get("reason") for r in eng.log("owner_released")] == ["holder_gone"]


# ------------------------------------------------------------------------------------------ 6
def test_our_own_processes_are_not_blamed(tmp_path: Path) -> None:
    """The voice service (ours, running beside llama in this table) and the holder's own process hold 600 MiB;
    dwm.exe and csrss.exe count but are not named; game.exe is named. Without taking ours off, it would not
    fit; with it, it does (both told apart by one reading each)."""

    async def scene(eng: Engine, short: int) -> NsError | None:
        eng.measured(STUB_NEED)
        await eng.acquire("tts", "studio:night-1")
        tts_pid = eng.sup.services["tts"].proc.pid  # type: ignore[union-attr]
        rows = [(tts_pid, "C", "python.exe", 300), (4321, "C", "python.exe", 300),
                (88, "C+G", "C:\\Windows\\System32\\dwm.exe", 300), (90, "G", "csrss.exe", 100),
                (4242, "C", "D:\\Games\\game.exe", 500)]  # fmt: skip
        eng.smi.cards = [card(STUB_NEED + MARGIN - 600 - short, rows, per="mb")]
        try:
            await eng.acquire("llama", "studio:night-1", model_alias="story", pid=4321)
        except NsError as e:
            return e
        return None

    eng = Engine(
        tmp_path / "a", [card(9000)], table=StubTable(gpu={"tts": False}), process_probe=lambda p: 1.0
    )
    assert eng.run(lambda: scene(eng, 0)) is None, "our own 600 MiB were held against us"
    assert eng.starts == ["tts", "llama"]

    eng2 = Engine(
        tmp_path / "b", [card(9000)], table=StubTable(gpu={"tts": False}), process_probe=lambda p: 1.0
    )
    e = eng2.run(lambda: scene(eng2, 1))
    assert e is not None and e.code == "NS-E208"
    assert "game.exe" in e.detail and "python.exe" not in e.detail, e.detail
    assert "dwm.exe" not in e.detail and "csrss.exe" not in e.detail and "Games" not in e.detail, e.detail


# ------------------------------------------------------------------------------------------ 7, 8
def test_programs_windows_does_not_measure_are_named_without_numbers(tmp_path: Path) -> None:
    rows = [(4242, "C", "game.exe", 9000), (5150, "C+G", "chrome.exe", 2000)]
    eng = Engine(tmp_path, [card(100, rows)])  # per process `[N/A]`, as under WDDM
    eng.smi.garbage = True
    eng.measured(STUB_NEED)
    e = eng.run(lambda: refused(eng, "llama", "opencode", model_alias="code"))
    assert e.code == "NS-E208"
    assert "game.exe, chrome.exe" in e.detail or "chrome.exe, game.exe" in e.detail, e.detail
    assert "Windows does not say how much each uses" in e.detail, e.detail
    assert "9000" not in e.detail and "8.8 GB" not in e.detail, "no number per program was guessed"


def test_no_program_listed_says_so(tmp_path: Path) -> None:
    eng = Engine(tmp_path, [card(100)])
    eng.measured(STUB_NEED)
    e = eng.run(lambda: refused(eng, "llama", "opencode", model_alias="code"))
    assert e.code == "NS-E208" and "Windows did not say which program" in e.detail, e.detail


# ------------------------------------------------------------------------------------------ 9, 10
def test_the_need_is_measured_after_a_load_and_kept(tmp_path: Path) -> None:
    eng = Engine(tmp_path, [card(TOTAL - 800), card(TOTAL - 800, llama=9400)])

    async def scene() -> list[Any]:
        await eng.acquire("llama", "opencode", model_alias="code")  # used 800 before, 10 200 after
        await eng.sup.stop_all()
        need = json.loads((eng.paths.home / "run" / "vram-need.json").read_text(encoding="utf-8"))
        out: list[Any] = [need]
        eng.clock.t += 60  # a later start: the memory of the model stopped above had its time to come back
        eng.smi.cards, eng.smi.reads = [card(9400 + MARGIN - 1)], 0
        out.append(await refused(eng, "llama", "opencode", model_alias="code"))
        eng.smi.cards, eng.smi.reads = [card(9400 + MARGIN)], 0
        await eng.acquire("llama", "opencode", model_alias="code")
        return out

    need, e = eng.run(scene)
    (entry,) = need["entries"]
    assert entry["mb"] == [9400] and entry["service"] == "llama" and "card" not in entry, entry
    assert {"variant", "model", "ctx"} <= set(entry)
    assert e.code == "NS-E208" and "needs 9.2 GB" in e.detail, "the next check used the measurement"

    # the largest of the last three; another context starts again from the estimate
    eng2 = Engine(tmp_path / "b", [card(TOTAL - 800)])
    loads = [5000, 7000, 6000, 3000]

    async def many() -> NsError:
        for mb in loads:
            eng2.smi.cards, eng2.smi.reads = [card(TOTAL - 800), card(TOTAL - 800, llama=mb)], 0
            await eng2.acquire("llama", "opencode", model_alias="code")
            await eng2.sup.stop_all()
        eng2.clock.t += 60
        eng2.smi.cards, eng2.smi.reads = [card(7000 + MARGIN - 1)], 0
        return await refused(eng2, "llama", "opencode", model_alias="code")

    e2 = eng2.run(many)
    assert "needs 6.8 GB" in e2.detail, e2.detail  # 7000 of [7000, 6000, 3000], not 5000

    eng3 = Engine(
        tmp_path / "c", [card(STUB_NEED + 4 * 64 + MARGIN)], table=StubTable(extra={"llama": ["-c", "4096"]})
    )
    (eng3.paths.home / "run").mkdir(parents=True, exist_ok=True)
    (eng3.paths.home / "run" / "vram-need.json").write_text(
        (tmp_path / "b" / "ns" / "run" / "vram-need.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    eng3.run(lambda: eng3.acquire("llama", "opencode", model_alias="code"))
    assert eng3.starts == ["llama"], "a different context is not held to another context's measurement"


@pytest.mark.parametrize("short", [0, 1])
def test_without_a_measurement_the_estimate_is_the_file_size_plus_the_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, short: int
) -> None:
    """Both ends of the estimate (file + ctx / 1024 x 64 + 700). It decides nothing: one MiB short, the load
    is still tried (an estimate never refuses), and the log says why. The file's 10 MiB come through the size
    seam (`vram._mib`): the file on disk is 4 bytes."""
    from ns_supervisor import vram

    model = tmp_path / "model.gguf"
    model.write_bytes(b"GGUF")
    real_mib = vram._mib
    monkeypatch.setattr(vram, "_mib", lambda p: 10 if Path(p) == model else real_mib(p))
    need = 10 + 8192 * 64 // 1024 + 700  # file + ctx / 1024 x 64 + 700 = 1222
    table = StubTable(extra={"llama": ["-m", str(model), "-c", "8192"]})
    eng = Engine(tmp_path, [card(need + MARGIN - short)], table=table)

    async def scene() -> NsError | None:
        try:
            await eng.acquire("llama", "opencode", model_alias="code")
        except NsError as e:
            return e
        return None

    e = eng.run(scene)
    assert e is None and eng.starts == ["llama"], "an estimate refused a load"
    tried = eng.log("vram_tried")
    if short:
        (line,) = tried
        assert line["data"]["need_mb"] == need and line["data"]["need_measured"] is False, line
        assert "only estimated" in line["msg"], line
    else:
        assert tried == [], "it fits: nothing to say"


# ------------------------------------------------------------------------------------------ 11
@pytest.mark.parametrize("other", [True, False])
def test_a_failed_load_with_others_holding_memory_becomes_ns_e208(tmp_path: Path, other: bool) -> None:
    after = card(200, [(4242, "C", "game.exe", 12000)]) if other else card(9000)
    table = StubTable(extra={"llama": ["--exit-immediately"]})
    eng = Engine(tmp_path, [card(9000), after], table=table)
    e = eng.run(lambda: refused(eng, "llama", "opencode", model_alias="code"))
    if other:
        assert e.code == "NS-E208", str(e)
        assert "game.exe" in e.detail and "exited with code" in e.detail, e.detail
    else:
        assert e.code == "NS-E202" and "exited with code" in e.detail, str(e)


# ------------------------------------------------------------------------------------------ 13
def test_no_check_without_a_reading(tmp_path: Path) -> None:
    eng = Engine(tmp_path / "a", [], smi_fails=True)

    async def twice() -> None:
        await eng.acquire("llama", "opencode", model_alias="code")
        await eng.sup.stop_all()
        await eng.acquire("llama", "opencode", model_alias="code")

    eng.run(twice)
    assert eng.starts == ["llama", "llama"], "no reading: the load goes ahead as before"
    notes = eng.log("vram_not_checked")
    assert len(notes) == 1 and notes[0]["level"] == "INFO", notes
    assert notes[0]["msg"].startswith("graphics memory not checked: "), notes[0]

    cpu = Engine(tmp_path / "b", [card(10)], runtime=Runtime.default("cpu-dev"))
    cpu.run(lambda: cpu.acquire("llama", "opencode", model_alias="code"))
    assert cpu.starts == ["llama"] and cpu.smi.reads == 0
    (note,) = cpu.log("vram_not_checked")
    assert "cpu-dev" in note["msg"], note


# ------------------------------------------------------------------------------------------ the states
@pytest.mark.parametrize("how", ["stop_all", "shutdown", "release"])
def test_stop_all_shutdown_or_release_end_the_wait_at_once(tmp_path: Path, how: str) -> None:
    """Stop All or Release (the dashboard), or the engine leaving, while a nightly waits (polls 30 s apart
    here): the wait ends at once, nothing is started, the card is free."""
    import dataclasses

    eng = Engine(tmp_path, [card(100, [(4242, "C", "game.exe", 12000)])])
    eng.measured(STUB_NEED)
    if "vram_poll_s" in {f.name for f in dataclasses.fields(eng.sup.cfg)}:
        eng.sup.cfg = dataclasses.replace(eng.sup.cfg, vram_poll_s=30.0)

    async def scene() -> tuple[NsError, float]:
        task = asyncio.create_task(
            eng.acquire("llama", "studio:night-1", model_alias="story", vram_wait=True)
        )
        for _ in range(200):
            await asyncio.sleep(0.02)
            if eng.sup.status().get("gpu_wait") or task.done():
                break
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        if how == "stop_all":
            await eng.sup.stop_all()
        elif how == "shutdown":
            await eng.sup.shutdown(0)
        else:
            await eng.sup.release_owner("studio:night-1")
        try:
            await asyncio.wait_for(task, 5)
        except NsError as e:
            return e, loop.time() - t0
        raise AssertionError(f"the wait went on after {how}")

    e, took = eng.run(scene)
    assert e.code == "NS-E202" and took < 5, (str(e), took)
    assert eng.starts == [] and eng.sup.status()["gpu_owner"] is None


def test_the_lease_is_not_reclaimed_during_the_wait(tmp_path: Path) -> None:
    """A nightly waiting for memory sends no heartbeat (its first acquire has not returned): the wait is not
    silence, as a model load is not. The monitor runs every 50 s of the clock (no suspend gap); the memory
    comes free after 700 s, past the 600 s lease."""
    eng: Engine
    free = card(9000)

    def on_read(n: int) -> None:
        if eng.clock.t - 1000.0 > 700:
            eng.smi.cards = [free]

    eng = Engine(tmp_path, [card(100, [(4242, "C", "game.exe", 12000)])], on_read=on_read)
    eng.measured(STUB_NEED)

    async def scene() -> None:
        task = asyncio.create_task(
            eng.acquire("llama", "studio:night-1", model_alias="story", vram_wait=True)
        )
        while not task.done():
            await eng.sup.tick()
            eng.clock.t += 50
            await asyncio.sleep(0.03)
        await task

    eng.run(scene)
    assert eng.vram_events(), "the nightly did not wait for memory"
    assert eng.starts == ["llama"] and eng.log("lease_expired") == []


def test_a_warmup_is_checked_never_waits_and_only_logs(tmp_path: Path) -> None:
    eng = Engine(tmp_path, [card(100, [(4242, "C", "game.exe", 12000)])])
    eng.measured(STUB_NEED)
    state = eng.run(lambda: eng.sup.warmup("llama"))
    assert state == "stopped" and eng.starts == [] and eng.smi.reads == 2
    (line,) = eng.log("vram_busy")
    assert line["data"].get("owner") is None and "game.exe" in line["msg"]


def test_the_engine_route_says_ns_e208_and_takes_vram_wait(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ns_supervisor.app import create_app
    from ns_supervisor.client import SupervisorClient

    eng: Engine

    def tick(n: int) -> None:
        eng.clock.t += 1  # each look at the card is a second later: the 3 s bound passes

    eng = Engine(tmp_path, [card(100, [(4242, "C", "game.exe", 12000)])], on_read=tick)
    eng.measured(STUB_NEED)
    with TestClient(create_app(eng.sup), base_url="http://127.0.0.1:18099") as tc:
        r = tc.post("/acquire", json={"service": "llama", "owner": "opencode", "model_alias": "code"})
        assert r.status_code == 409 and r.json()["code"] == "NS-E208" and "game.exe" in r.json()["detail"]
        c = SupervisorClient("http://127.0.0.1:18099", client=tc)
        with pytest.raises(NsError) as ei:
            c.acquire("llama", "studio:night-1", model_alias="story", timeout_s=3, vram_wait=True)  # type: ignore[call-arg]
        assert ei.value.code == "NS-E208" and "waited" in ei.value.detail, str(ei.value)
    assert eng.starts == []


def test_the_nightly_acquirer_alone_asks_to_wait_and_names_its_process() -> None:
    """Only the nightly asks to wait; the nightly and a dashboard job's studio child name their process, so
    their own context is never "another program" (and the card is freed when the process dies)."""
    import os

    from ns_common.acquire import Endpoint
    from ns_studio.acquirer import StudioAcquirer

    calls: list[dict[str, Any]] = []

    class Client:
        def acquire(self, service: str, owner: str, **kw: Any) -> Endpoint:
            calls.append(kw)
            return Endpoint("http://127.0.0.1:1", None)

        def release(self, service: str, owner: str) -> None: ...

        def warmup(self, service: str) -> str:
            return "ready"

    acq = StudioAcquirer(Runtime.default("gpu"), Paths(Path.cwd() / "unused"), client=Client(),
                         ensure=lambda _c: None, beat_s=3600)  # fmt: skip
    acq.acquire("llama", "studio:a")
    acq.send_pid = True  # what a dashboard job's child sets (`ns_dashboard.studio_job`)
    acq.acquire("llama", "studio:b")
    acq.vram_wait = True  # what the nightly sets (`daily.run_daily`)
    acq.acquire("llama", "studio:c")
    acq.stop_beat()
    assert "vram_wait" not in calls[0] and "pid" not in calls[0], calls
    assert calls[1].get("pid") == os.getpid() and "vram_wait" not in calls[1], calls
    assert calls[2].get("vram_wait") is True and calls[2].get("pid") == os.getpid(), calls


# ------------------------------------------------------------------------------------------ 12: the night
def test_the_night_treats_ns_e208_like_a_busy_card(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime, timedelta

    from ns_studio import daily, jobs, ownership
    from ns_studio.channels import crud
    from tests import d31_driver as driver
    from tests.helpers import make_channel
    from tests.test_interrupted_nightly import _notes, _setup

    paths, db = _setup(tmp_home)
    created = (datetime.now(UTC) - timedelta(weeks=11)).isoformat(timespec="seconds")
    crud.add_channel(
        paths,
        db,
        make_channel(
            slug="tiny-mysteries",
            display_name="Tiny Mysteries",
            format="master_first",
            created_at=created,
            niche="small locked-room puzzles solved in under ten minutes",
            voice_notes="Dry, quick, amused.",
        ),
    )
    driver.install(monkeypatch)
    detail = (
        "other programs were using 15.2 GB of the graphics card's 15.6 GB (game.exe, chrome.exe; Windows "
        "does not say how much each uses), so 0.4 GB were free and the story model needs about 9.3 GB "
        "with 0.5 GB to spare; Nightshift waited 30 minutes for it"
    )
    attempts: list[str] = []

    def busy(ctx: Any) -> list[str]:
        attempts.append(ctx.job_id)
        raise NsError("NS-E208", detail)

    toasts: list[str] = []
    report = daily.run_daily(jobs.open_env(light=True), rng=random.Random(1), run_fn=busy,
                             notifier=lambda title, message: toasts.append(message) or True)  # fmt: skip
    assert len(attempts) == 1, f"{len(attempts)} jobs met the busy card: the night went on after the first"
    job = attempts[0]
    assert report["failures"] == [], "a busy card is not a failure"
    (entry,) = report["interrupted_jobs"]
    assert entry["job_id"] == job and entry["state"] == "waiting" and "game.exe" in entry.get("said", ""), (
        entry
    )
    assert (ownership.read_record(paths, job) or {}).get("state") == "interrupted"
    (skipped,) = report["skipped"]
    assert skipped["reason"] == "vram" and skipped["jobs"] == 1, report["skipped"]
    notes = [n["message"] for n in _notes(tmp_home)]
    vram_notes = [n for n in notes if "NS-E208" in n]
    assert len(vram_notes) == 1, notes
    assert "game.exe, chrome.exe" in vram_notes[0] and "Automatic runs are on" in vram_notes[0], vram_notes
    assert toasts and "NS-E208" in toasts[0], toasts

    made: list[str] = []

    def works(ctx: Any) -> list[str]:
        made.append(ctx.job_id)
        return ["d31_c"]

    daily.run_daily(jobs.open_env(light=True), rng=random.Random(2), run_fn=works)
    assert made and made[0] == job, "the next night continues the job that waited"


@pytest.mark.parametrize(("value", "test_install", "taken"), [
    ("59", True, None), ("60", True, 60.0), ("1800", True, 1800.0), ("1801", True, None), ("abc", True, None),
    ("60", False, None),
])  # fmt: skip
def test_a_proof_shortens_the_wait_only_on_a_test_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, test_install: bool, taken: float | None
) -> None:
    """`NS_TEST_VRAM_WAIT_S` (both ends, 60 and 1800) is read only on a dev or engine-test install; any other
    value, or any other install, keeps the 1800 s and says so in the log."""
    from ns_common import devmode

    monkeypatch.setenv("NS_TEST_VRAM_WAIT_S", value)
    monkeypatch.setattr(devmode, "test_install", lambda paths: test_install)
    eng = Engine(tmp_path, [card(9000)])
    assert getattr(eng.sup, "vram_wait_s", None) == (taken if taken is not None else float(WAIT_S))
    assert bool(eng.log("vram_wait_ignored")) is (taken is None)


def test_a_half_made_job_that_meets_ns_e208_waits_with_the_engines_words(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job an earlier night left half made, continued tonight, meets NS-E208: it stays interrupted at its
    stage (as for NS-E201), its line says what held the memory, nothing is a failure, one note."""
    from ns_studio import daily, jobs, ownership
    from tests import d31_driver as driver
    from tests.test_interrupted_nightly import _dead_daily_job, _notes, _setup

    paths, db = _setup(tmp_home)
    driver.install(monkeypatch)
    dead = _dead_daily_job(paths, db)
    said = "other programs were using 15.0 GB of the graphics card's 15.9 GB (Windows did not say which one)"

    def busy(ctx: Any) -> list[str]:
        raise NsError("NS-E208", said)

    report = daily.run_daily(jobs.open_env(light=True), rng=random.Random(1), run_fn=busy)
    assert report["failures"] == []
    (entry,) = report["interrupted_jobs"]
    assert entry["job_id"] == dead and entry["state"] == "waiting" and entry.get("said") == said, entry
    assert (ownership.read_record(paths, dead) or {}).get("state") == "interrupted"
    assert report.get("vram", {}).get("said") == said
    assert len([n for n in _notes(tmp_home) if "NS-E208" in n["message"]]) == 1


# ------------------------------------------------------------------------------------------ review c913351
@pytest.mark.parametrize("entry", ["story_llm", "llm_balanced", "llm_best"])
def test_a_first_load_is_tried_with_the_manifests_real_sizes_on_a_16_gb_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """Review blocker 1. The GPU PC's card (16,311 MiB, 800 in use idle) and the manifest's real model sizes
    (given where the estimate reads a file's size: nothing is written to disk) at the 16K context: their
    estimates (file + 1 GB of KV + 700) are above what the card has free, the story model's and the best
    tier's above the card itself. Never measured, each is TRIED (the nightly
    does not wait, nobody is refused), measured (13,000 MiB here: what -ngl and --n-cpu-moe keep on the card)
    and the measurement is kept for the next check."""
    manifest = json.loads((Path(__file__).resolve().parents[1] / "manifest.json").read_text(encoding="utf-8"))
    size = int(manifest["profiles"]["gpu"][entry]["bytes"])
    model = tmp_path / f"{entry}.gguf"
    model.write_bytes(b"GGUF")
    from ns_supervisor import vram

    real_mib = vram._mib
    monkeypatch.setattr(vram, "_mib", lambda p: -(-size // 2**20) if Path(p) == model else real_mib(p))
    gpu_pc = gpu.Card(total_mb=16311, base_mb=800, services={"llama": 13000})
    eng = Engine(tmp_path, [gpu_pc], table=StubTable(extra={"llama": ["-m", str(model), "-c", "16384"]}))
    eng.run(lambda: eng.acquire("llama", "studio:night-1", model_alias="story", vram_wait=True))
    assert eng.starts == ["llama"], "the model was not tried"
    assert eng.vram_events() == [] and eng.log("vram_busy") == [], "an estimate held the night back"
    estimate = -(-size // 2**20) + 16384 * 64 // 1024 + 700
    (tried,) = eng.log("vram_tried")
    assert tried["data"]["need_mb"] == estimate and tried["data"]["need_measured"] is False, tried
    kept = json.loads((eng.paths.home / "run" / "vram-need.json").read_text(encoding="utf-8"))
    (mine,) = [e for e in kept["entries"] if e["model"] == str(model)]
    assert mine["mb"] == [13000], mine


@pytest.mark.parametrize("held_reads", [2, 99])
def test_memory_our_stopped_tenant_still_holds_is_not_another_programs(
    tmp_path: Path, held_reads: int
) -> None:
    """Review blocker 2. The voice model is loaded (measured 9,000 MiB); Nightshift Code asks for llama
    (measured 6,000), so the swap stops the voice service. Under WDDM nothing says whose memory the card
    still shows: the reading stays 9.8 GB for `held_reads` polls after the stop. 2: it comes back and llama
    loads, no NS-E208. 99 (never within the 10 s): still no NS-E208 ("other programs" would be false); the
    load is tried and the log says why."""
    idle = card(TOTAL - 800, tts=9000)  # while the voice service runs, the card lists it with 9,000 MiB
    held = card(TOTAL - 9800)  # it has stopped; its memory is still in use, listed under no process
    mark: dict[str, int | None] = {"n": None}
    eng: Engine

    def on_read(n: int) -> None:
        if mark["n"] is not None:
            k = n - mark["n"]  # 1: the reading just before the stop
            eng.smi.cards = [held] if 2 <= k <= held_reads + 1 else [idle]

    eng = Engine(tmp_path, [idle], on_read=on_read)

    async def scene() -> None:
        await eng.acquire("tts", "studio:voice-preview")  # loads; measured 9,000
        await eng.sup.release("tts", "studio:voice-preview")
        eng.measured(6000)
        mark["n"] = eng.smi.reads
        await eng.acquire("llama", "opencode", model_alias="code")

    eng.run(scene)
    assert eng.starts == ["tts", "llama"] and eng.log("vram_busy") == [], eng.log("vram_busy")
    if held_reads > 20:
        (tried,) = eng.log("vram_tried")
        assert "had not come free" in tried["msg"], tried


def test_a_measured_need_larger_than_the_card_is_a_tuning_matter_not_another_program(
    tmp_path: Path,
) -> None:
    """A measured need (17,000 MiB) that no idle card of 16,000 could hold: never a wait nor "another
    program"; it is tried, and when it fails the error is NS-E203, whose hint says ns doctor, then ns tune."""
    table = StubTable(extra={"llama": ["--exit-immediately"]})
    eng = Engine(tmp_path, [card(15000, [(4242, "C", "game.exe", 200)])], table=table)
    eng.measured(17000)
    e = eng.run(lambda: refused(eng, "llama", "studio:night-1", model_alias="story", vram_wait=True))
    assert e.code == "NS-E203", str(e)
    assert "more than the graphics card's" in e.detail and "game.exe" not in e.detail, e.detail
    assert eng.starts == ["llama"] and eng.vram_events() == []


def test_the_studios_own_context_is_not_another_program(tmp_path: Path) -> None:
    """The nightly's studio process (its pid named at the acquire) keeps whisper's CUDA context, 400 MiB that
    WDDM never reports per process. llama is measured at 6,500 on an 8,000 MiB card whose idle is 800: with
    that context it does not fit, without it it would, and nothing else is on the card. Not "another
    program": no wait, no NS-E208; the load is tried."""
    eng = Engine(tmp_path, [card(8000 - 1200, total=8000)], process_probe=lambda p: 1.0)
    eng.measured(6500, idle=800)
    eng.run(lambda: eng.acquire("llama", "studio:night-1", model_alias="story", vram_wait=True, pid=4321))
    assert eng.starts == ["llama"]
    assert eng.vram_events() == [] and eng.log("vram_busy") == []


@pytest.mark.parametrize("code", ["NS-E201", "NS-E208"])
def test_a_new_nightly_job_waits_the_same_on_both_busy_card_codes(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    """One state, one behaviour: a NEW nightly job that meets a busy card (another task, NS-E201, or other
    programs' memory, NS-E208) is not a failure: it stays interrupted at its stage, its line says waiting,
    the rest of the night is skipped, and the next night continues it."""
    from ns_studio import daily, jobs, ownership
    from tests import d31_driver as driver
    from tests.test_interrupted_nightly import _setup

    paths, _db = _setup(tmp_home)
    driver.install(monkeypatch)
    met: list[str] = []

    def busy(ctx: Any) -> list[str]:
        met.append(ctx.job_id)
        raise NsError(code, "held")

    report = daily.run_daily(jobs.open_env(light=True), rng=random.Random(1), run_fn=busy)
    assert report["failures"] == [], report["failures"]
    (entry,) = report["interrupted_jobs"]
    assert entry["job_id"] == met[0] and entry["state"] == "waiting", entry
    rec = ownership.read_record(paths, met[0]) or {}
    assert rec.get("state") == "interrupted" and rec.get("owner") in (
        ownership.CARD_BUSY_OWNER, ownership.VRAM_OWNER
    ), rec
    made: list[str] = []

    def works(ctx: Any) -> list[str]:
        made.append(ctx.job_id)
        return ["d31_c"]

    daily.run_daily(jobs.open_env(light=True), rng=random.Random(2), run_fn=works)
    assert made and made[0] == met[0], "the next night continues the job that waited"


def _lines(eng: Engine) -> int:
    files = eng.paths.logs.glob("supervisor-*.jsonl")
    return sum(len(f.read_text(encoding="utf-8").splitlines()) for f in files)


def test_the_wait_ends_with_a_clock_that_never_moves_and_logs_two_lines(tmp_path: Path) -> None:
    """Firmware rule for every loop: the engine's clock never advances and the card never changes. The
    nightly's wait (3 s of its acquire's time) still ends, on real time and on its count of looks, with
    NS-E208, and it logged exactly two lines (its start and its end), not one per look."""
    import time as real_time

    eng = Engine(tmp_path, [card(100, [(4242, "C", "game.exe", 12000)])])
    eng.measured(STUB_NEED)
    t0 = real_time.monotonic()

    async def scene() -> tuple[NsError, int]:
        before = _lines(eng)
        e = await refused(
            eng, "llama", "studio:night-1", model_alias="story", vram_wait=True, timeout_s=3
        )
        return e, _lines(eng) - before

    e, logged = eng.run(scene)
    took = real_time.monotonic() - t0
    assert e.code == "NS-E208" and took < 20, (str(e), took)
    assert [r["event"] for r in eng.log("vram_wait")] == ["vram_wait"] and len(eng.log("vram_busy")) == 1
    assert logged == 2, f"{logged} lines for one wait"
    assert eng.smi.reads < 400, f"{eng.smi.reads} looks at the card for a 3 s wait"


def test_the_settle_ends_when_our_memory_never_comes_back_and_logs_one_line(tmp_path: Path) -> None:
    """The settle after one of our tenants stopped: the reading never falls and the engine's clock never
    moves. It ends after its 20 readings (or 10 s of real time), the load is tried, one line says why."""
    stuck = card(TOTAL - 9800)
    eng: Engine
    armed = {"on": False}

    def on_read(n: int) -> None:
        if armed["on"]:
            eng.smi.cards = [stuck]

    eng = Engine(tmp_path, [card(TOTAL - 800, tts=9000)], on_read=on_read)

    async def scene() -> int:
        await eng.acquire("tts", "studio:voice-preview")
        await eng.sup.release("tts", "studio:voice-preview")
        eng.measured(6000)
        armed["on"], first = True, eng.smi.reads
        await eng.acquire("llama", "opencode", model_alias="code")
        return eng.smi.reads - first

    reads = eng.run(scene)
    assert eng.starts == ["tts", "llama"]
    assert reads <= 1 + 20 + 1 + 3, f"{reads} readings (the stop's, at most 20 settling, the measurement)"
    assert len(eng.log("vram_tried")) == 1


def test_the_estimate_reads_a_model_files_size_only_through_the_size_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`estimate_mb` learns a file's size from `vram._mib` and nowhere else, so a stubbed size is the only way
    a model of a real size enters a test (none is ever written)."""
    import inspect as insp

    from ns_supervisor import vram
    from ns_supervisor.services import ServiceDef

    asked: list[Path] = []

    def seam(p: Path) -> int:
        asked.append(Path(p))
        return 20186

    monkeypatch.setattr(vram, "_mib", seam)
    huge = Path("Z:/no/such/folder/story.gguf")  # never on disk: only the seam can give its size
    d = ServiceDef("llama", ["llama-server.exe", "-m", str(huge), "-c", "16384"], "127.0.0.1", 1, gpu=True)
    assert vram.estimate_mb(d) == 20186 + 16384 * 64 // 1024 + 700
    assert asked == [huge]
    body = insp.getsource(vram.estimate_mb)
    assert "stat(" not in body and "getsize" not in body and "open(" not in body, body


def test_a_failed_first_start_that_was_not_memory_keeps_no_lower_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A never-measured model (estimated at 20 GB on a 16 GB card, nothing else on it) whose process exits at
    start (a damaged file: NS-E202's ground) twice: both failures stay NS-E202, no lower bound is kept, and
    the second is never told as NS-E203 "too big"."""
    from ns_supervisor import vram

    model = tmp_path / "story.gguf"
    model.write_bytes(b"GGUF")
    real_mib = vram._mib
    monkeypatch.setattr(vram, "_mib", lambda p: 20186 if Path(p) == model else real_mib(p))
    table = StubTable(extra={"llama": ["-m", str(model), "--exit-immediately"]})
    eng = Engine(tmp_path, [card(TOTAL - 800)], table=table)

    async def twice() -> list[NsError]:
        out = [await refused(eng, "llama", "opencode", model_alias="code")]
        eng.clock.t += 60
        out.append(await refused(eng, "llama", "opencode", model_alias="code"))
        return out

    first, second = eng.run(twice)
    assert (first.code, second.code) == ("NS-E202", "NS-E202"), (str(first), str(second))
    kept = eng.paths.home / "run" / "vram-need.json"
    entries = json.loads(kept.read_text(encoding="utf-8")).get("entries", []) if kept.exists() else []
    assert all("min_mb" not in e for e in entries), entries


def test_the_idle_baseline_is_provisional_until_readings_agree_and_never_raised(tmp_path: Path) -> None:
    """A first reading may include a game: it is a candidate, not the baseline. A lower reading replaces the
    candidate; three agreeing readings (within 64 MiB) make it the baseline; a higher one never raises it."""
    from datetime import datetime

    from ns_supervisor import vram

    paths, now = Paths(tmp_path / "ns"), datetime(2026, 11, 11, 3, 0)
    vram.record_idle(paths, 12800, None, now)  # a game was open at the first reading
    assert vram.idle_mb(paths) is None, "one reading made a baseline"
    for used in (800, 810, 790):
        vram.record_idle(paths, used, None, now)
    assert vram.idle_mb(paths) == 800, "three agreeing readings did not make the baseline"
    for used in (5000, 5000, 5000):
        vram.record_idle(paths, used, None, now)
    assert vram.idle_mb(paths) == 800, "a higher reading raised it"
    vram.record_idle(paths, 600, None, now)
    assert vram.idle_mb(paths) == 800, "one lower reading lowered it at once"
    vram.record_idle(paths, 610, None, now)
    vram.record_idle(paths, 600, None, now)
    assert vram.idle_mb(paths) == 600


def test_numbers_of_the_simulated_card_never_stand_for_a_real_card(tmp_path: Path) -> None:
    """A need measured, and an idle card read, on the simulated card (marked `card`) are not a real card's
    numbers, and a real card's are not the simulated one's (H-10)."""
    from datetime import datetime

    from ns_supervisor import vram
    from ns_supervisor.services import ServiceDef

    paths, now, sim = Paths(tmp_path / "ns"), datetime(2026, 11, 11, 3, 0), gpu.SIMULATED
    d = ServiceDef("llama", ["llama-server.exe", "--alias", "story"], "127.0.0.1", 1, gpu=True)
    vram.record(paths, d, 9000, sim, now)
    for _ in range(3):
        vram.record_idle(paths, 800, sim, now)
    assert vram.need(paths, d, mark=sim) == vram.Need(9000, True)
    assert vram.need(paths, d).measured is False, "a simulated measurement was used for a real card"
    assert vram.idle_mb(paths, sim) == 800 and vram.idle_mb(paths) is None
    vram.record(paths, d, 7000, None, now)
    assert vram.need(paths, d) == vram.Need(7000, True) and vram.need(paths, d, mark=sim).mb == 9000
