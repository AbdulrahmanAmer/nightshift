"""C1: opening the setup window: host, then Edge or Chrome app mode, then the default browser."""

import json
from pathlib import Path

from installer.stage2.web import launch

APP = Path("D:/ns-app")
URL = "http://127.0.0.1:53817/?t=abc"
ENV = {"ProgramFiles": "C:\\Program Files", "ProgramFiles(x86)": "C:\\Program Files (x86)",
       "LOCALAPPDATA": "C:\\Users\\x\\AppData\\Local"}
HOST_SCRIPT = APP / "launchers" / "nightshift-host.ps1"
EDGE = Path("C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe")
CHROME = Path("C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe")


class Proc:
    def __init__(self, code: int | None) -> None:
        self.code = code

    def poll(self):
        return self.code


class Popen:
    """Records every launch; ``codes`` is what each one's poll() answers (None = still running)."""

    def __init__(self, *codes: int | None, fail_first: bool = False) -> None:
        self.codes = list(codes)
        self.calls: list[list[str]] = []
        self.fail_first = fail_first

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if self.fail_first and len(self.calls) == 1:
            raise OSError("cannot start")
        return Proc(self.codes.pop(0) if self.codes else None)


DLL = Path(ENV["LOCALAPPDATA"]) / "Nightshift" / "bin" / "webview2" / "Microsoft.Web.WebView2.Core.dll"


def go(popen: Popen, present: set[Path], startfile=None, *, ready: bool = True, dlls: bool = True, **kw):
    opened: list[str] = []
    if dlls and HOST_SCRIPT in present:
        present = present | {DLL}

    def default_browser(url: str) -> None:
        opened.append(url)

    handle = launch.open_window(
        URL, app_path=APP, env=ENV, popen=popen, exists=lambda p: p in present,
        startfile=default_browser if startfile is None else startfile, sleep=lambda _s: None, wait_s=0.0,
        ready=lambda: ready, **kw,
    )
    return handle, opened


def test_the_webview2_host_is_tried_first_and_a_window_that_stays_up_wins():
    popen = Popen(None)
    handle, opened = go(popen, {HOST_SCRIPT, EDGE})
    assert handle.host == "webview2" and handle.proc is not None and not opened
    assert len(popen.calls) == 1
    argv = popen.calls[0]
    assert argv[0] == "powershell.exe" and "-STA" in argv and str(HOST_SCRIPT) in argv
    assert argv[argv.index("-Url") + 1] == URL and argv[argv.index("-AppPath") + 1] == str(APP)
    assert argv[argv.index("-Role") + 1] == "setup"      # own mutex and state file, apart from the dashboard


def test_a_host_that_exits_zero_quickly_raised_an_open_window_and_counts_as_opened():
    handle, _ = go(Popen(0), {HOST_SCRIPT, EDGE})
    assert handle.host == "webview2"


def test_exit_3_means_webview2_is_missing_so_edge_app_mode_is_next():
    popen = Popen(3, None)
    handle, opened = go(popen, {HOST_SCRIPT, EDGE, CHROME})
    assert handle.host == "browser-app" and handle.browser == "Edge" and not opened
    assert launch.host_label(handle) == "opened in Edge"
    edge = popen.calls[1]
    assert edge[0] == str(EDGE) and f"--app={URL}" in edge and "--window-size=1366,768" in edge
    assert any(a.startswith("--user-data-dir=") and a.endswith("browser") for a in edge)


def test_chrome_follows_edge_and_the_default_browser_follows_chrome():
    handle, _ = go(Popen(3, None), {HOST_SCRIPT, CHROME})
    assert handle.host == "browser-app" and handle.browser == "Chrome"
    handle, opened = go(Popen(3), {HOST_SCRIPT})
    assert handle.host == "browser" and opened == [URL]
    assert launch.host_label(handle) == "opened in your browser"
    assert handle.tried == ["webview2", "default browser"]


def test_without_the_host_script_nothing_starts_it():
    popen = Popen(None)
    handle, _ = go(popen, {EDGE})
    assert handle.host == "browser-app" and [c[0] for c in popen.calls] == [str(EDGE)]


def test_a_host_that_cannot_even_start_falls_through():
    handle, _ = go(Popen(None, fail_first=True), {HOST_SCRIPT, EDGE})
    assert handle.host == "browser-app"


def test_when_everything_fails_the_host_is_none_and_the_label_says_what_to_do():
    def broken(_url: str) -> None:
        raise OSError("no browser")

    handle, _ = go(Popen(3), {HOST_SCRIPT}, startfile=broken)
    assert handle.host == "none" and handle.proc is None
    assert launch.host_label(handle) == "open the address below in Edge or Chrome"
    handle = launch.open_window(URL, app_path=APP, env=ENV, popen=Popen(), exists=lambda p: False,
                                startfile=None, sleep=lambda _s: None, wait_s=0.0)
    assert handle.host == "none"


def test_browser_candidates_follow_the_spec_order():
    got = launch.browser_candidates(ENV)
    assert got[0] == ("Edge", EDGE)
    assert got[1][0] == "Edge" and got[2] == ("Chrome", CHROME) and got[3][0] == "Chrome"
    assert str(got[3][1]).startswith("C:\\Users\\x\\AppData\\Local")


def test_server_files_let_a_second_install_cmd_find_the_first(tmp_path: Path):
    folder = tmp_path / "Nightshift"
    server, address = launch.write_server_files(
        folder, pid=1234, port=53817, token="tok", app_path="D:\\NightShift", url=URL)
    info = json.loads(server.read_text(encoding="utf-8"))
    assert info["pid"] == 1234 and info["port"] == 53817 and info["token"] == "tok"
    assert info["app_path"] == "D:\\NightShift" and info["started"]
    assert address.read_text(encoding="utf-8").strip() == URL
    launch.update_server_info(folder, host="webview2", host_text="opened")
    again = launch.read_server_info(folder)
    assert again["host"] == "webview2" and again["host_text"] == "opened" and again["pid"] == 1234
    launch.remove_server_files(folder)
    assert launch.read_server_info(folder) is None and address.is_file()      # the address stays for a look
    assert launch.read_server_info(tmp_path / "none") is None


def test_data_dir_is_under_local_app_data():
    assert launch.data_dir({"LOCALAPPDATA": "C:\\Users\\x\\AppData\\Local"}) == Path(
        "C:\\Users\\x\\AppData\\Local") / "Nightshift"


# ------------------------------------------------------------- review rows 8 and P4
def test_without_the_webview2_dlls_the_host_is_not_even_started():
    """First run: the DLLs arrive in step s02, so the host would only exit 3 after a slow PowerShell start."""
    popen = Popen(None)
    handle, _ = go(popen, {HOST_SCRIPT, EDGE}, dlls=False)
    assert handle.host == "browser-app" and [c[0] for c in popen.calls] == [str(EDGE)]
    assert "webview2" not in handle.tried


def test_a_host_that_is_alive_but_never_shows_a_window_is_replaced_by_the_browser():
    class Hanging(Proc):
        killed = False

        def kill(self):
            Hanging.killed = True

    class P(Popen):
        def __call__(self, argv, **kw):
            self.calls.append(list(argv))
            return Hanging(None) if len(self.calls) == 1 else Proc(None)

    popen = P()
    handle, _ = go(popen, {HOST_SCRIPT, EDGE}, ready=False)
    assert handle.host == "browser-app" and Hanging.killed


def test_a_slow_cold_start_gets_twenty_seconds_to_show_the_window():
    assert launch.HOST_WAIT_S == 20.0
    clock = {"t": 0.0}
    state = {"ready": False}

    def sleep(s: float) -> None:
        clock["t"] += s
        if clock["t"] >= 5.0:                      # the host shows its window after five seconds
            state["ready"] = True

    popen = Popen(None)
    handle = launch.open_window(
        URL, app_path=APP, env=ENV, popen=popen, exists=lambda p: p in {HOST_SCRIPT, DLL, EDGE},
        startfile=None, sleep=sleep, ready=lambda: state["ready"],
    )
    assert handle.host == "webview2" and len(popen.calls) == 1


def test_a_host_that_exits_with_an_error_falls_back_to_the_browser():
    handle, _ = go(Popen(1, None), {HOST_SCRIPT, EDGE})
    assert handle.host == "browser-app"


def test_browser_windows_skip_the_first_run_and_default_browser_pages():
    popen = Popen(None)
    go(popen, {EDGE})
    assert "--no-first-run" in popen.calls[0] and "--no-default-browser-check" in popen.calls[0]
    handle, _ = go(Popen(None), {HOST_SCRIPT, EDGE}, skip_host=True)
    assert handle.host == "browser-app" and "webview2" not in handle.tried


class Waiting:
    def __init__(self, code: int) -> None:
        self.code = code

    def wait(self) -> int:
        return self.code


class Ctl:
    def __init__(self, run: str) -> None:
        self.run = run

    def snapshot(self) -> dict:
        return {"run": self.run}


def watched(code: int, run: str, host: str = "webview2"):
    import importlib
    import threading

    m = importlib.import_module("installer.stage2.web.__main__")
    said: list[str] = []
    fell_back = threading.Event()
    done = threading.Event()
    real_say = m.say
    m.say = lambda text, level="info": (said.append(text), done.set())
    try:
        handle = launch.WindowHandle(host, proc=Waiting(code))
        m.watch_window(handle, Ctl(run), fallback=lambda: (fell_back.set(), done.set()))
        assert done.wait(5)
        done.wait(0.1)
    finally:
        m.say = real_say
    return said, fell_back.is_set()


def test_a_host_that_dies_during_setup_is_replaced_by_the_browser_window():
    said, fell_back = watched(3, "running")
    assert fell_back and "opening it in the browser" in said[0]


def test_closing_the_window_after_stop_tells_the_console_how_to_come_back():
    said, fell_back = watched(0, "stopped")
    assert not fell_back and "closed after Stop" in said[0] and "INSTALL.cmd" in said[0]


def test_closing_the_window_while_setup_runs_says_it_keeps_running():
    said, fell_back = watched(0, "running")
    assert not fell_back and "keeps running" in said[0]
