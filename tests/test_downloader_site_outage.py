"""D-13: a download site that answers 5xx or 429 is waited out for about ten minutes, and the end says whose
side it is.

Found in the GitHub incident of 2026-11-09: s02 retried for 2.5 minutes, then stopped with "A required
download site is unreachable" and no word that the fault was the site's or that INSTALL.cmd continues.
Every test uses a fake transport and an injected sleep and clock, so ten simulated minutes take no time, and
each ends at the effect: the text a person reads and the exit code the console run returns.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from rich.console import Console

from installer.stage2 import downloader as dl
from installer.stage2.base import Step
from installer.stage2.bridge import BridgeReporter, BridgeUi
from installer.stage2.errors import make_error
from installer.stage2.testing import make_test_context
from installer.stage2.ui import RichReporter
from installer.stage2.web import controller
from installer.stage2.wizard import EXIT_FAILED, Wizard, describe_failure
from ns_common.errors import NsError
from tests.test_downloader import DATA, SHA

URL = "https://github.com/AbdulrahmanAmer/nightshift/releases/download/v1/a.bin"


def scripted(script: list, *, forever: object | None = None):
    """A site whose GET answers follow ``script`` (an int, or ``(int, headers)``); then the file, or
    ``forever`` repeated. HEAD always answers 200 so only the GET decides. Returns ``(client, gets)``."""
    gets: list[int] = []
    queue = list(script)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(DATA))})
        step = queue.pop(0) if queue else forever
        if step is None:
            return httpx.Response(200, content=DATA)
        status, headers = step if isinstance(step, tuple) else (step, {})
        gets.append(status)
        return httpx.Response(status, headers=headers)

    return httpx.Client(transport=httpx.MockTransport(handler)), gets


class FakeTime:
    """A clock the fake sleep moves, so the window is measured the way a real run measures it."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# ---------------------------------------------------------------------------------------------- the wait
def test_503_503_200_succeeds_after_two_waits_and_reports_them(tmp_path: Path):
    client, gets = scripted([503, 503])
    t, waits = FakeTime(), []
    dest = tmp_path / "a.bin"
    dl.download(URL, dest, SHA, client=client, sleep=t.sleep, clock=t.clock, on_site_wait=waits.append)
    assert dest.read_bytes() == DATA and dl.is_verified(dest, SHA)
    assert gets == [503, 503]
    assert t.sleeps == [2, 4]
    assert [(w.host, w.status, w.wait_s) for w in waits] == [
        ("github.com", 503, 2.0),
        ("github.com", 503, 4.0),
    ]
    assert [w.left_s for w in waits] == [598.0, 594.0]  # the ten minutes shrink by what was waited


def test_a_site_that_stays_down_for_the_whole_window_ends_in_e105_naming_host_and_status(tmp_path: Path):
    client, gets = scripted([], forever=504)
    t, waits = FakeTime(), []
    with pytest.raises(NsError) as ei:
        dl.download(
            URL,
            tmp_path / "a.bin",
            SHA,
            client=client,
            sleep=t.sleep,
            clock=t.clock,
            on_site_wait=waits.append,
        )
    err = ei.value
    text = str(err)
    assert err.code == "NS-E105"
    assert "github.com" in text and "504" in text  # whose answer it was
    assert "site's side" in text and "not this PC" in text  # whose fault it is
    assert "INSTALL.cmd" in text and "continues" in text  # what to do
    assert err.data["host"] == "github.com" and err.data["status"] == 504 and err.data["side"] == "site"
    assert sum(t.sleeps) == dl.SITE_WINDOW_S == 600.0  # about ten minutes, never more
    assert len(gets) == len(waits) + 1 and len(waits) >= 8
    assert max(t.sleeps) <= dl.SITE_BACKOFF_CAP_S  # backoff 2, 4, 8 ... 60 s


def test_the_window_is_measured_by_the_clock_too_so_slow_answers_use_it_up(tmp_path: Path):
    client, gets = scripted([], forever=502)
    t = FakeTime()

    def slow_sleep(seconds: float) -> None:
        t.sleep(seconds)
        t.now += 100.0  # every answer also took 100 s to come back

    with pytest.raises(NsError):
        dl.download(URL, tmp_path / "a.bin", SHA, client=client, sleep=slow_sleep, clock=t.clock)
    assert t.now - 1000.0 <= 600 + 100  # stopped at the window, not after ten more minutes of waits
    assert len(gets) < 15


def test_429_with_retry_after_waits_as_long_as_the_site_asked(tmp_path: Path):
    client, _ = scripted([(429, {"retry-after": "45"})])
    t, waits = FakeTime(), []
    dl.download(
        URL, tmp_path / "a.bin", SHA, client=client, sleep=t.sleep, clock=t.clock, on_site_wait=waits.append
    )
    assert t.sleeps == [45] and waits[0].status == 429 and waits[0].wait_s == 45.0


def test_a_huge_retry_after_is_capped_and_a_small_one_does_not_shorten_the_backoff(tmp_path: Path):
    client, _ = scripted([(503, {"retry-after": "99999"}), (503, {"retry-after": "1"}), 503])
    t = FakeTime()
    dl.download(URL, tmp_path / "a.bin", SHA, client=client, sleep=t.sleep, clock=t.clock)
    assert t.sleeps == [dl.RETRY_AFTER_CAP_S, 4, 8]


def test_retry_after_as_seconds_or_a_date():
    now = datetime(2026, 11, 9, 7, 0, 0, tzinfo=UTC)
    assert dl.retry_after_seconds("45") == 45.0
    assert dl.retry_after_seconds(" 3 ") == 3.0
    assert dl.retry_after_seconds("Mon, 09 Nov 2026 07:01:30 GMT", now=now) == 90.0
    assert dl.retry_after_seconds("Mon, 09 Nov 2026 06:00:00 GMT", now=now) == 0.0  # in the past
    assert dl.retry_after_seconds("soon") is None and dl.retry_after_seconds("") is None
    assert dl.retry_after_seconds("-5") is None


def test_the_wait_line_says_host_status_their_side_and_the_time_left():
    assert dl.site_wait_text("github.com", 504, 40, 420) == (
        "github.com answers 504 (their side). Trying again in 40 s, 7 min left"
    )
    assert dl.site_wait_text("github.com", 429, 2, 30) == (
        "github.com answers 429 (their side). Trying again in 2 s, 30 s left"
    )


# ---------------------------------------------------------------------------------------------- stops at once
def test_404_stops_at_once_without_waiting(tmp_path: Path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(404)

    t = FakeTime()
    with pytest.raises(NsError) as ei:
        dl.download(
            URL,
            tmp_path / "a.bin",
            SHA,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=t.sleep,
            clock=t.clock,
        )
    assert ei.value.code == "NS-E105" and ei.value.data["status"] == 404
    assert calls == ["HEAD"] and t.sleeps == []


@pytest.mark.parametrize("status", [401, 403, 410])
def test_other_4xx_stop_at_once_and_do_not_blame_the_site(tmp_path: Path, status: int):
    client, gets = scripted([], forever=status)
    t = FakeTime()
    with pytest.raises(NsError) as ei:
        dl.download(URL, tmp_path / "a.bin", SHA, client=client, sleep=t.sleep, clock=t.clock)
    text = str(ei.value)
    assert ei.value.code == "NS-E105" and ei.value.data["status"] == status
    assert "github.com" in text and str(status) in text
    assert "site's side" not in text and "INSTALL.cmd" not in text  # waiting will not help here
    assert gets == [status] and t.sleeps == []


def test_a_wrong_hash_still_stops_with_e109_and_never_enters_the_window(tmp_path: Path):
    client, _ = scripted([])
    t = FakeTime()
    with pytest.raises(NsError) as ei:
        dl.download(URL, tmp_path / "a.bin", "0" * 64, client=client, sleep=t.sleep, clock=t.clock)
    assert ei.value.code == "NS-E109" and t.sleeps == [2]  # the one re-download, no outage wait


def test_a_wrong_size_is_not_an_outage(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(DATA))})
        return httpx.Response(200, content=DATA[:100], headers={"content-length": str(len(DATA))})

    waits: list[dl.SiteWait] = []
    t = FakeTime()
    with pytest.raises(NsError) as ei:
        dl.download(
            URL,
            tmp_path / "a.bin",
            SHA,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=t.sleep,
            clock=t.clock,
            on_site_wait=waits.append,
        )
    assert ei.value.code == "NS-E105" and waits == [] and "site's side" not in str(ei.value)


# ---------------------------------------------------------------------------------------------- resume
def test_after_the_window_ran_out_running_it_again_continues_and_finishes(tmp_path: Path):
    item = dl.DownloadItem(name="llama", url=URL, dest=tmp_path / "bin" / "a.bin", sha256=SHA)
    down, _ = scripted([], forever=503)
    t = FakeTime()
    with pytest.raises(NsError) as ei:
        dl.run_plan([item], client=down, sleep=t.sleep, site_window_s=60.0)
    assert ei.value.code == "NS-E105" and not item.dest.exists()
    back, _ = scripted([])  # INSTALL.cmd again, the site is back
    res = dl.run_plan([item], client=back, sleep=t.sleep)
    assert res.downloaded == ["llama"] and item.dest.read_bytes() == DATA
    again = dl.run_plan([item], client=down, sleep=t.sleep)  # a third run has nothing to do
    assert again.skipped == ["llama"]


# ---------------------------------------------------------------------------------------------- what is shown
def test_run_plan_hands_each_wait_to_the_reporter_and_logs_it(tmp_path: Path):
    seen: list[tuple] = []

    class Rec:
        def start(self, name, total): ...
        def update(self, name, done, total): ...
        def finish(self, name, outcome): ...

        def site_wait(self, name, host, status, wait_s, left_s):
            seen.append((name, host, status, wait_s, left_s))

    logged: list[tuple] = []

    class Log:
        def warn(self, event, msg="", **data):
            logged.append((event, msg, data))

    item = dl.DownloadItem(name="llama", url=URL, dest=tmp_path / "a.bin", sha256=SHA)
    client, _ = scripted([502, 503])
    res = dl.run_plan([item], client=client, reporter=Rec(), sleep=FakeTime().sleep, log=Log())
    assert res.downloaded == ["llama"]
    assert seen == [("llama", "github.com", 502, 2.0, 598.0), ("llama", "github.com", 503, 4.0, 594.0)]
    assert [e for e, _m, _d in logged] == ["download_site_down"] * 2
    assert logged[0][1] == "github.com answers 502 (their side). Trying again in 2 s, 10 min left"
    assert logged[0][2]["host"] == "github.com" and logged[0][2]["status"] == 502


def test_a_reporter_without_site_wait_is_told_through_retrying(tmp_path: Path):
    told: list[tuple] = []

    class Old:
        def start(self, name, total): ...
        def update(self, name, done, total): ...
        def finish(self, name, outcome): ...

        def retrying(self, name, attempt, reason, wait_s):
            told.append((name, attempt, reason, wait_s))

    item = dl.DownloadItem(name="f", url=URL, dest=tmp_path / "a.bin", sha256=SHA)
    dl.run_plan([item], client=scripted([500])[0], reporter=Old(), sleep=FakeTime().sleep)
    assert told == [("f", 1, "site", 2.0)]


def test_the_console_progress_prints_the_wait_line():
    buf = io.StringIO()
    rep = RichReporter(Console(file=buf, width=140, force_terminal=False))
    with rep:
        rep.site_wait("llama", "github.com", 504, 40.0, 420.0)
    assert "github.com answers 504 (their side). Trying again in 40 s, 7 min left" in buf.getvalue()


def test_the_setup_window_gets_a_retry_event_with_host_status_and_time_left():
    out = io.StringIO()
    ui = BridgeUi(out=out)
    BridgeReporter(ui).site_wait("llama", "github.com", 504, 40.0, 420.0)
    ev = [json.loads(x) for x in out.getvalue().splitlines()][0]
    assert ev["event"] == "retry" and ev["reason"] == "site" and ev["key"] == "llama"
    assert (ev["host"], ev["status"], ev["waitS"], ev["leftS"]) == ("github.com", 504, 40.0, 420.0)


def test_the_setup_servers_copy_of_the_wait_line_equals_the_downloaders():
    for wait_s, left_s in [(2, 598), (40, 420), (60, 61), (5, 30), (45, 0)]:
        assert controller.site_wait_text("github.com", 504, wait_s, left_s) == dl.site_wait_text(
            "github.com", 504, wait_s, left_s
        )


def test_the_row_note_in_the_window_reads_like_the_console_line():
    note = controller.retry_note("site", 40.0, host="github.com", status=504, left_s=420.0)
    assert note == "github.com answers 504 (their side). Trying again in 40 s, 7 min left"


# ---------------------------------------------------------------------------------------------- the end
class DownloadStep(Step):
    """A step that downloads one file the way s02 does."""

    id = "s02_download_bins"
    title = "Download programs"

    def __init__(self, dest: Path) -> None:
        self.item = dl.DownloadItem(name="llama", url=URL, dest=dest, sha256=SHA)

    def check(self, ctx) -> bool:
        return dl.is_done(self.item)

    def verify(self, ctx) -> None:
        assert dl.is_done(self.item)

    def run(self, ctx) -> None:
        res = ctx.download([self.item])
        if res.failed:
            raise next(iter(res.failed.values()))


def test_console_run_exit_code_3_and_the_screen_says_their_side_and_to_run_install_again(tmp_path: Path):
    ctx = make_test_context(
        tmp_path, unattended=True, simulate=False, http=lambda: scripted([], forever=504)[0]
    )
    t = FakeTime()
    ctx.sleep = t.sleep
    step = DownloadStep(tmp_path / "bin" / "a.bin")
    code = Wizard(ctx, [step]).run()
    assert code == EXIT_FAILED == 3
    shown = ctx.ui.text
    assert "NS-E105" in shown and "github.com" in shown and "504" in shown
    assert "site's side" in shown and "INSTALL.cmd" in shown
    assert sum(t.sleeps) == 600.0, "one window, not one per unattended retry (that would be 40 minutes)"
    assert ctx.state.status("s02_download_bins") == "failed"


def test_the_console_run_that_follows_continues_at_the_failed_step(tmp_path: Path):
    step = DownloadStep(tmp_path / "bin" / "a.bin")
    down = make_test_context(
        tmp_path, unattended=True, simulate=False, http=lambda: scripted([], forever=503)[0]
    )
    down.sleep = FakeTime().sleep
    assert Wizard(down, [step]).run() == EXIT_FAILED
    up = make_test_context(tmp_path, unattended=True, simulate=False, http=lambda: scripted([])[0])
    assert Wizard(up, [step]).run() == 0
    assert step.item.dest.read_bytes() == DATA


def test_the_failure_event_carries_host_status_and_side_to_the_setup_window():
    err = make_error("NS-E105", "x", status=504, host="github.com", side="site")
    failure = describe_failure(err)
    assert failure.data["host"] == "github.com"
    out = io.StringIO()
    ui = BridgeUi(out=out)
    ui.stop.set()  # decide_failure returns at once: "quit"
    ui.decide_failure("s02_download_bins", failure, False, 1)
    ev = [json.loads(x) for x in out.getvalue().splitlines()][0]
    assert ev["type"] == "failure" and ev["code"] == "NS-E105"
    assert (ev["host"], ev["status"], ev["side"]) == ("github.com", 504, "site")


def test_the_failed_screen_names_the_host_and_says_it_is_their_side(tmp_path, monkeypatch):
    from tests.test_installer_web_controller import Setup

    s = Setup(tmp_path, monkeypatch)
    s.accept()
    s.ctl._ev_failure(
        {
            "type": "failure",
            "step": "s02_download_bins",
            "code": "NS-E105",
            "message": "A download site",
            "hint": "",
            "detail": "github.com answered 504",
            "optional": False,
            "attempt": 1,
            "host": "github.com",
            "status": 504,
            "side": "site",
        }
    )
    f = s.snap()["failure"]
    assert f["code"] == "NS-E105" and "github.com" in f["title"] and "504" in f["title"]
    assert "their side" in f["whatToDo"] and "INSTALL.cmd" in f["whatToDo"] and "Retry" in f["whatToDo"]
    s.ctl._ev_failure(
        {
            "type": "failure",
            "step": "s02_download_bins",
            "code": "NS-E105",
            "message": "m",
            "hint": "",
            "detail": "no route",
            "optional": False,
            "attempt": 2,
        }
    )
    g = s.snap()["failure"]
    assert g["title"] == "A download site cannot be reached" and "internet" in g["whatToDo"]


def test_ns_e105_text_in_errors_json_says_the_site_side_and_the_rerun():
    from ns_common.errors import describe

    d = describe("NS-E105")
    assert "site" in d.message
    assert "their side" in d.hint and "INSTALL.cmd" in d.hint and "internet" in d.hint
