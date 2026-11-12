"""Card download-outage (block E2): a download of hours survives an outage of minutes, and says what it does.

Found on the studio PC's first real install (2026-11-12, G-8 to G-10): a six-minute loss of the internet cost
the 35B model file its three attempts, nothing was said or logged, and an unattended setup printed nothing for
the whole download. Every test uses a fake transport, a fake sleep and (where ``download`` takes one) a fake
clock, so ten simulated minutes take no time. T1 to T10 are the card's acceptance tests.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import threading
from pathlib import Path

import httpx
import pytest
from rich.console import Console

from installer.stage2 import downloader as dl
from installer.stage2.ui import PLAIN_INTERVAL_S, RichReporter
from ns_common.errors import NsError
from ns_common.logging import NsLogger

MIB = 1024 * 1024
BIG = bytes(range(256)) * 4096 * 6          # 6 MiB
BIG_SHA = hashlib.sha256(BIG).hexdigest()
SMALL = bytes(range(256)) * 1024            # 256 KiB
SMALL_SHA = hashlib.sha256(SMALL).hexdigest()
STALL = "stall"                             # no bytes at all: 60 s pass, then the read times out
DROP = "drop"                               # the connection is refused or reset at once


class Boom(BaseException):
    """Stands for Stop (``SetupStopped``): not an Exception, so nothing may swallow it."""


class FakeTime:
    """A clock the fake sleep moves; a stall moves it by the 60 s it really takes."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.lock = threading.Lock()

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        with self.lock:
            self.sleeps.append(seconds)
            self.now += seconds


class _Gives(httpx.SyncByteStream):
    """A body that sends ``n`` bytes, then goes silent for 60 s and times out (``n`` None: all of it)."""

    def __init__(self, body: bytes, n: int | None, t: FakeTime) -> None:
        self.body, self.n, self.t = body, n, t

    def __iter__(self):
        if self.n is None:
            yield self.body
            return
        if self.n:
            yield self.body[: self.n]
        self.t.now += 60.0
        raise httpx.ReadTimeout("no data")


class Link:
    """A fake site for one or more files. ``step(name, n)`` decides the n-th GET of a file (1-based):
    ``STALL``, ``DROP``, an int (that many bytes, then a stall) or None (the rest of the file). HEAD always
    answers."""

    def __init__(self, files: dict[str, bytes], step, t: FakeTime) -> None:
        self.files, self.step, self.t = files, step, t
        self.gets: list[tuple[str, str | None]] = []     # (file, Range header)
        self.lock = threading.Lock()

    def count(self, name: str) -> int:
        return sum(1 for n, _r in self.gets if n == name)

    def handler(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        data = self.files[name]
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(data))})
        rng = request.headers.get("range")
        with self.lock:
            self.gets.append((name, rng))
            n = self.count(name)
        what = self.step(name, n)
        if what == STALL:
            self.t.now += 60.0
            raise httpx.ReadTimeout("no data", request=request)
        if what == DROP:
            raise httpx.ConnectError("connection reset", request=request)
        start = int(rng.split("=")[1].split("-")[0]) if rng else 0
        body = data[start:]
        headers = {"content-length": str(len(body))}
        if rng:
            headers["content-range"] = f"bytes {start}-{len(data) - 1}/{len(data)}"
        return httpx.Response(206 if rng else 200, headers=headers, stream=_Gives(body, what, self.t))

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def url(name: str) -> str:
    return f"https://cdn.test/files/{name}"


class Rec:
    """A reporter that keeps everything it is told."""

    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.lock = threading.Lock()

    def start(self, name, total):
        with self.lock:
            self.events.append(("start", name))

    def update(self, name, done, total):
        return None

    def finish(self, name, outcome):
        with self.lock:
            self.events.append(("finish", name, outcome))

    def retrying(self, name, attempt, reason, wait_s):
        with self.lock:
            self.events.append(("retry", name, attempt, reason, wait_s))

    def retries(self, name: str) -> list[tuple]:
        return [e for e in self.events if e[0] == "retry" and e[1] == name]


def log_records(folder: Path) -> list[dict]:
    return [json.loads(line) for p in sorted(folder.glob("*.jsonl"))
            for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------------------------- N1 download
def test_t1_a_link_that_is_up_but_dead_for_five_minutes_then_alive_finishes_the_file(tmp_path: Path):
    t = FakeTime()
    began = t.now
    link = Link({"a.bin": BIG}, lambda name, n: STALL if t.now - began < 300 else None, t)
    dest = tmp_path / "a.bin"
    dl.download(url("a.bin"), dest, BIG_SHA, client=link.client(), sleep=t.sleep, clock=t.clock)
    assert dest.read_bytes() == BIG and dl.is_verified(dest, BIG_SHA)
    assert link.count("a.bin") > dl.DEFAULT_RETRIES + 1, "more tries than rc5's three: the window held it"
    assert t.now - began >= 300


def test_t2_a_link_dead_past_the_window_is_ns_e105_side_network_not_before_ten_minutes(tmp_path: Path):
    t = FakeTime()
    first_failure: list[float] = []

    def step(name: str, n: int) -> str:
        if not first_failure:
            first_failure.append(t.now + 60.0)          # the first stall ends 60 s from now
        return STALL

    link = Link({"a.bin": BIG}, step, t)
    with pytest.raises(NsError) as ei:
        dl.download(url("a.bin"), tmp_path / "a.bin", BIG_SHA, client=link.client(), sleep=t.sleep,
                    clock=t.clock)
    err = ei.value
    assert err.code == "NS-E105" and err.data["side"] == "network" and err.data["host"] == "cdn.test"
    assert t.now - first_failure[0] >= dl.NETWORK_WINDOW_S == 600.0, "given up before the window"
    assert t.now - first_failure[0] < dl.NETWORK_WINDOW_S + 60 + dl.BACKOFF_CAP_S + 1, "and not much after"
    assert err.data["waited_s"] >= 600 and "No data came from cdn.test for 1" in err.detail   # 10 or 11 min
    assert "kept" in err.detail and "INSTALL.cmd" not in err.detail


def test_t2_a_link_that_is_down_uses_the_window_too_and_its_offline_waits_count(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "network_is_up", lambda u, timeout=5.0: False)
    t = FakeTime()
    link = Link({"a.bin": BIG}, lambda name, n: DROP, t)
    with pytest.raises(NsError) as ei:
        dl.download(url("a.bin"), tmp_path / "a.bin", BIG_SHA, client=link.client(), sleep=t.sleep,
                    clock=t.clock)
    assert ei.value.data["side"] == "network"
    assert sum(t.sleeps) >= dl.NETWORK_WINDOW_S
    # every failure waited the two offline minutes first, so far fewer tries than a window of backoffs alone
    assert dl.DEFAULT_RETRIES <= link.count("a.bin") <= 6


def test_t3_three_stalls_each_after_more_than_one_mib_gained_finish_the_file(tmp_path: Path):
    t = FakeTime()
    gift = MIB + MIB // 2
    link = Link({"a.bin": BIG}, lambda name, n: gift if n <= 3 else None, t)
    dest = tmp_path / "a.bin"
    dl.download(url("a.bin"), dest, BIG_SHA, client=link.client(), sleep=t.sleep, clock=t.clock)
    assert dest.read_bytes() == BIG
    assert [r for _n, r in link.gets] == [None, f"bytes={gift}-", f"bytes={2 * gift}-", f"bytes={3 * gift}-"]
    assert t.sleeps == [2, 2, 2], "each gain started the attempts anew: the wait stays the first one"


def test_t4_a_trickle_below_one_mib_per_try_fails_when_the_window_is_used_up(tmp_path: Path):
    t = FakeTime()
    link = Link({"a.bin": BIG}, lambda name, n: 100 * 1024, t)
    dest = tmp_path / "a.bin"
    with pytest.raises(NsError) as ei:
        dl.download(url("a.bin"), dest, BIG_SHA, client=link.client(), sleep=t.sleep, clock=t.clock)
    assert ei.value.code == "NS-E105" and ei.value.data["side"] == "network"
    assert ei.value.data["waited_s"] >= dl.NETWORK_WINDOW_S
    part = dest.with_name("a.bin.part")
    assert part.stat().st_size == 100 * 1024 * link.count("a.bin"), "the .part is kept for the next run"
    assert link.count("a.bin") > dl.DEFAULT_RETRIES, "a trickle restarts nothing, but the window still runs"


# ---------------------------------------------------------------------------------------------- N3 passes
def test_t5_a_file_the_network_stopped_is_continued_in_a_second_pass_from_its_part(tmp_path: Path):
    t = FakeTime()
    healed = threading.Event()

    def step(name: str, n: int):
        if name != "a.bin":
            return None
        if healed.is_set():
            return None
        return 200 * 1024 if n == 1 else STALL

    class Healing(Rec):
        def retrying(self, name, attempt, reason, wait_s):
            super().retrying(name, attempt, reason, wait_s)
            if reason == "later":
                healed.set()                            # the network comes back while the other files run

    link = Link({"a.bin": BIG, "b.bin": SMALL, "c.bin": SMALL}, step, t)
    items = [dl.DownloadItem(name=n, url=url(n), dest=tmp_path / n, sha256=s)
             for n, s in (("a.bin", BIG_SHA), ("b.bin", SMALL_SHA), ("c.bin", SMALL_SHA))]
    rec = Healing()
    res = dl.run_plan(items, client=link.client(), reporter=rec, sleep=t.sleep)
    assert sorted(res.downloaded) == ["a.bin", "b.bin", "c.bin"] and res.failed == {}
    assert (tmp_path / "a.bin").read_bytes() == BIG
    assert link.gets[-1] == ("a.bin", f"bytes={200 * 1024}-"), "pass 2 continued from the .part"
    assert link.count("b.bin") == link.count("c.bin") == 1
    assert ("retry", "a.bin", 0, "later", 0.0) in rec.events
    finished = [e for e in rec.events if e[0] == "finish" and e[1] == "a.bin"]
    assert finished == [("finish", "a.bin", "downloaded")], "no 'failed' in between: it was only held back"
    assert sum(w for *_x, w in rec.retries("a.bin")) >= dl.NETWORK_WINDOW_S, "pass 1 gave it its window"


def test_t6_a_second_pass_that_gains_nothing_ends_in_one_ns_e105_and_keeps_the_part(tmp_path: Path):
    t = FakeTime()
    link = Link({"a.bin": BIG}, lambda name, n: 200 * 1024 if n == 1 else STALL, t)
    item = dl.DownloadItem(name="a.bin", url=url("a.bin"), dest=tmp_path / "a.bin", sha256=BIG_SHA)
    lines: list[str] = []
    rec = Rec()
    log = NsLogger("installer", tmp_path / "logs", echo=False)
    with pytest.raises(NsError) as ei:
        dl.run_plan([item], client=link.client(), reporter=rec, sleep=t.sleep, say=lines.append, log=log)
    err = ei.value
    assert err.code == "NS-E105" and err.data["side"] == "network"
    assert "1 file is still to come" in err.detail and "kept" in err.detail
    assert (tmp_path / "a.bin.part").stat().st_size == 200 * 1024
    assert [x for x in lines if x.startswith("Trying again")] == [
        "Trying again: 1 file(s) the internet outage interrupted"
    ], "exactly one more pass, no third"
    passes = [r for r in log_records(tmp_path / "logs") if r["event"] == "download_pass"]
    assert [r["data"].get("state") for r in passes] == ["start", "end"]
    finished = [e for e in rec.events if e[0] == "finish"]
    assert finished == [("finish", "a.bin", "failed")], "told once, at the end"


def test_t7_hash_refusal_and_site_window_failures_are_never_retried_in_a_pass(tmp_path: Path):
    t = FakeTime()
    phase = {"n": 1}
    seen: list[tuple[int, str]] = []
    files = {"hash.bin": SMALL, "refused.bin": SMALL, "site.bin": SMALL, "net.bin": SMALL}

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(SMALL))})
        seen.append((phase["n"], name))
        if name == "hash.bin":
            return httpx.Response(200, content=b"x" * len(SMALL))         # never the pinned file
        if name == "refused.bin":
            return httpx.Response(403)
        if name == "site.bin":
            return httpx.Response(503)
        t.now += 60.0
        raise httpx.ReadTimeout("no data", request=request)

    def say(line: str) -> None:
        if line.startswith("Trying again"):
            phase["n"] = 2

    items = [dl.DownloadItem(name=n, url=url(n), dest=tmp_path / n, sha256=SMALL_SHA) for n in files]
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(NsError) as ei:
        dl.run_plan(items, client=client, sleep=t.sleep, say=say)
    # which of the three is raised depends on the order the threads ended in; never the network's
    assert not dl.is_network_failure(ei.value), "a hash, refusal or site failure wins over the network one"
    assert ei.value.code == "NS-E109" or ei.value.data.get("side") in ("request", "site")
    assert phase["n"] == 2 and (2, "net.bin") in seen, "the network's file had its pass"
    assert [name for p, name in seen if p == 2 and name != "net.bin"] == [], "nothing else was retried"
    assert sum(1 for _p, name in seen if name == "hash.bin") == dl.HASH_ATTEMPTS
    assert sum(1 for _p, name in seen if name == "refused.bin") == 1


def test_t8_after_one_file_used_its_window_the_later_files_of_that_pass_get_one_try(tmp_path: Path):
    t = FakeTime()
    link = Link({"a.bin": BIG, "b.bin": BIG, "c.bin": BIG}, lambda name, n: STALL, t)
    items = [dl.DownloadItem(name=n, url=url(n), dest=tmp_path / n, sha256=BIG_SHA)
             for n in ("a.bin", "b.bin", "c.bin")]
    rec = Rec()
    lines: list[str] = []
    with pytest.raises(NsError):
        dl.run_plan(items, client=link.client(), reporter=rec, sleep=t.sleep, concurrency=1, say=lines.append)
    for name in ("b.bin", "c.bin"):
        assert [r[3] for r in rec.retries(name)] == ["later", "later"], "no backoff, no offline wait"
        assert link.count(name) == 2, "one try in each pass"
    assert sum(r[4] for r in rec.retries("a.bin")) >= 2 * dl.NETWORK_WINDOW_S, "a.bin had its window twice"
    assert sum(t.sleeps) < 2 * (dl.NETWORK_WINDOW_S + dl.BACKOFF_CAP_S + 1), "nobody else waited a window"
    assert "Trying again: 3 file(s) the internet outage interrupted" in lines


# ---------------------------------------------------------------------------------------------- N4, N5 said
def test_t9_every_wait_is_logged_with_its_reason_and_reaches_the_rich_reporter(tmp_path, monkeypatch):
    answers = [True, False, True]                       # the stall: up; the drop: down, then back

    def probe(u: str, timeout: float = 5.0) -> bool:
        return answers.pop(0) if answers else True

    monkeypatch.setattr(dl, "network_is_up", probe)
    t = FakeTime()
    link = Link({"a.bin": SMALL}, lambda name, n: {1: STALL, 2: DROP}.get(n), t)
    item = dl.DownloadItem(name="a.bin", url=url("a.bin"), dest=tmp_path / "a.bin", sha256=SMALL_SHA,
                           size=len(SMALL))
    buf = io.StringIO()
    log = NsLogger("installer", tmp_path / "logs", echo=False)
    with RichReporter(Console(file=buf, force_terminal=False), len(SMALL), files=1, clock=t.clock) as rep:
        res = dl.run_plan([item], client=link.client(), reporter=rep, sleep=t.sleep, log=log)
    assert res.downloaded == ["a.bin"]
    waits = [r for r in log_records(tmp_path / "logs") if r["event"] == "download_retry"]
    assert [r["data"]["reason"] for r in waits] == ["stall", "offline"]
    assert all(r["data"]["file"] == "a.bin" and r["data"]["wait_s"] == 2.0 for r in waits)
    out = buf.getvalue()
    assert "a.bin: No data came for a while. Trying again in 2 s" in out
    assert "a.bin: No internet right now. Checking again in 2 s" in out
    assert "a.bin: starting, 0.0 GB" in out and "a.bin: downloaded, 0.0 GB" in out
    assert "\x1b" not in out, "no control sequences in a file"


def test_t9_without_a_terminal_a_start_line_an_end_line_and_at_most_one_total_line_per_interval():
    t = FakeTime()
    buf = io.StringIO()
    total = 2 * 600 * MIB
    with RichReporter(Console(file=buf, force_terminal=False), total, files=3, clock=t.clock) as rep:
        rep.start("a.gguf", 600 * MIB)
        rep.start("b.gguf", 600 * MIB)
        for second in range(0, 301, 10):                # 5 min: both files get 10 MiB every 10 s
            t.now = 1000.0 + second
            rep.update("a.gguf", second * MIB, 600 * MIB)
            rep.update("b.gguf", second * MIB, 600 * MIB)
        rep.finish("a.gguf", "downloaded")
    lines = buf.getvalue().splitlines()
    assert lines[0] == "a.gguf: starting, 0.6 GB" and lines[1] == "b.gguf: starting, 0.6 GB"
    totals = [x for x in lines if x.startswith("Downloaded ")]
    assert 1 <= len(totals) <= 300 // PLAIN_INTERVAL_S
    # at 60 s: a.gguf 60 MiB, b.gguf 50 MiB (its 60 s report comes after the line); 110 MiB moved in 60 s
    assert totals[0] == "Downloaded 0.1 GB of 1.2 GB (9%), 1.8 MB/s, 0 of 3 files done"
    assert len(totals) == 5, "one per minute, never two"
    assert lines[-1].startswith("a.gguf: downloaded, 0.6 GB, 5 min 0 s")
    assert "\x1b" not in buf.getvalue()


def test_t9_plain_lines_stay_whole_with_two_files_at_a_time():
    t = FakeTime()
    buf = io.StringIO()
    rep = RichReporter(Console(file=buf, force_terminal=False), 10 * MIB, files=2, clock=lambda: t.now)

    def run(name: str) -> None:
        rep.start(name, 5 * MIB)
        for i in range(400):
            t.now += 1.0
            rep.update(name, i * 1024, 5 * MIB)
            if i % 50 == 0:
                rep.retrying(name, 1, "stall", 2.0)
        rep.finish(name, "downloaded")

    workers = [threading.Thread(target=run, args=(n,)) for n in ("x.bin", "y.bin")]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=10)
    shape = re.compile(r"^((x|y)\.bin: .+|Downloaded .+ files done)$")
    lines = buf.getvalue().splitlines()
    assert lines and all(shape.match(x) for x in lines), [x for x in lines if not shape.match(x)]


def test_the_windows_row_note_reads_a_later_retry_in_plain_words():
    from installer.stage2.web.controller import retry_note

    note = retry_note("later", 0.0, 0, 0)
    assert note == "The internet stopped this file; it is tried again when the other files are done"


def test_ns_models_get_says_every_wait_and_the_later_line_on_screen():
    """Ruling 6c: ``ns models get`` has its own reporter; the waits and the "later" line reach the screen."""
    from ns_supervisor.models_get import EchoReporter

    said: list[str] = []
    rep = EchoReporter(said.append)
    rep.retrying("m.gguf", 1, "stall", 2.0)
    rep.retrying("m.gguf", 0, "later", 0.0)
    assert said == [
        "m.gguf: No data came for a while. Trying again in 2 s",
        "m.gguf: The internet stopped this file. The other files go on, and this one is tried again"
        " at the end",
    ]


# ---------------------------------------------------------------------------------------------- setup's end
def test_an_unattended_setup_on_a_dead_line_says_each_round_and_ends_with_run_install_again(
    tmp_path, monkeypatch
):
    """Rulings 6a and 6b: the network give-up ends with "run INSTALL.cmd again"; each unattended round is
    said and logged before it starts (which of how many, and why); the whole run stays inside the bound the
    wizard's docstring gives (about 80 to 100 minutes of waiting on a dead line)."""
    from installer.stage2.testing import make_test_context
    from installer.stage2.wizard import EXIT_FAILED, Wizard
    from tests.test_downloader_site_outage import DownloadStep

    monkeypatch.setattr(dl, "network_is_up", lambda u, timeout=5.0: False)

    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    ctx = make_test_context(tmp_path, unattended=True, simulate=False,
                            http=lambda: httpx.Client(transport=httpx.MockTransport(dead)))
    t = FakeTime()
    ctx.sleep = t.sleep
    assert Wizard(ctx, [DownloadStep(tmp_path / "bin" / "a.bin")]).run() == EXIT_FAILED
    shown = ctx.ui.text
    way_out = "What to do: Run INSTALL.cmd again when the internet is back: setup continues where it stopped."
    assert way_out in shown
    assert "firewall" not in shown, "the generic hint is not the way out of an outage"
    for n in (1, 2, 3):
        assert f"Retry {n} of 3 in " in shown
    assert shown.count("because this step stopped with NS-E105") == 3
    records = log_records(ctx.paths.logs)
    order = [r["event"] for r in records if r["event"] in ("stage_start", "stage_error", "stage_retry")]
    assert order == ["stage_start", "stage_error", "stage_retry"] * 3 + ["stage_start", "stage_error"]
    rounds = [r["data"] for r in records if r["event"] == "stage_retry"]
    assert [(d["round"], d["of"], d["code"]) for d in rounds] == [(n, 3, "NS-E105") for n in (1, 2, 3)]
    # four rounds of (a ten-minute window, then ten minutes waiting for the network), plus 5 + 15 + 45 s
    assert 4 * 2 * 600 <= sum(t.sleeps) <= 100 * 60


def test_the_setup_windows_failed_screen_says_run_install_again_after_an_outage(tmp_path, monkeypatch):
    """Ruling 6a on the window's route: the Failed screen of a network give-up names Retry and INSTALL.cmd."""
    from tests.test_installer_web_controller import Setup

    s = Setup(tmp_path, monkeypatch)
    s.accept()
    s.ctl._ev_failure({"type": "failure", "step": "s03_download_models", "code": "NS-E105", "message": "m",
                       "hint": "", "detail": "1 file is still to come.", "optional": False, "attempt": 1,
                       "host": "huggingface.co", "side": "network"})
    f = s.snap()["failure"]
    assert "Retry" in f["whatToDo"]
    assert "run INSTALL.cmd again: it continues where it stopped" in f["whatToDo"]
    assert "firewall" not in f["whatToDo"]


# ---------------------------------------------------------------------------------------------- stop
def test_t10_stop_during_the_wait_before_a_pass_is_seen_within_one_slice(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "network_is_up", lambda u, timeout=5.0: False)
    t = FakeTime()
    between = threading.Event()
    stop = threading.Event()
    pressed: list[float] = []                           # the sleep Stop was pressed in
    after: list[float] = []                             # what was slept after Stop: must be nothing
    waited = [0.0]

    def sleep(seconds: float) -> None:
        t.sleep(seconds)
        if stop.is_set():
            after.append(seconds)
            return
        if between.is_set():
            waited[0] += seconds
            # deep in the wait, inside its 16 s step (2 + 4 + 8 + 16 > 20): a step slept whole shows 16 here
            if waited[0] >= 20.0:
                pressed.append(seconds)
                stop.set()

    link = Link({"a.bin": BIG}, lambda name, n: STALL, t)
    gets_then: list[int] = []

    class Stoppable(Rec):
        def retrying(self, name, attempt, reason, wait_s):
            super().retrying(name, attempt, reason, wait_s)
            if reason == "later":
                gets_then.append(link.count("a.bin"))
                between.set()

        def check_stop(self) -> None:
            if stop.is_set():
                raise Boom()

    item = dl.DownloadItem(name="a.bin", url=url("a.bin"), dest=tmp_path / "a.bin", sha256=BIG_SHA)
    with pytest.raises(Boom):
        dl.run_plan([item], client=link.client(), reporter=Stoppable(), sleep=sleep, network_window_s=10.0)
    assert len(pressed) == 1 and pressed[0] <= dl.SITE_SLICE_S, "the wait is slept in slices of 5 s at most"
    assert after == [], "seen at the end of the slice it was pressed in"
    assert gets_then == [link.count("a.bin")], "no second pass began"
