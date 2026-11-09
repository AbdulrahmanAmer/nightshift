"""F-1: a reader only ever sees a complete file.

main went red on `test_a_finished_run_writes_the_report_and_adds_it_to_the_wizards_zip` with
`BadZipFile: Bad magic number for central directory`: the setup window appended the install report to the
wizard's diagnostics zip IN PLACE (`ZipFile(path, "a")` overwrites the old central directory first), so a
reader that opened the zip during that millisecond (the test, a partner double-clicking it, a sync client, an
antivirus scan) found a torn file, and a power cut there left it torn for good. The diagnostics zips were also
written straight to their final name, so a reader met them half built for as long as the build took.

The tests read a file in a loop on another thread while the writer rewrites it many times, and assert that
every read is a whole file. A writer step is stretched (`slow_zip_writes`) so the torn window of an in-place
writer is 20 ms wide instead of one, which makes the old code fail every time and not now and then. Windows
refuses an open for a few milliseconds while a replace is in flight (PermissionError); the readers here retry
that, the way the product's readers do.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import threading
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from ns_common import atomic
from ns_common.paths import Paths

ROUNDS = 40
WINDOW_S = 0.02


class Hammer:
    """Reads in a loop on a thread; `torn` collects every read that did not get a whole file."""

    def __init__(self, read_once: Callable[[], object]) -> None:
        self._read_once = read_once
        self._stop = threading.Event()
        self._started = threading.Event()
        self.reads = 0
        self.torn: list[str] = []
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._read_once()
                self.reads += 1
            except Exception as e:  # noqa: BLE001 - every kind of failure is a torn read here
                self.torn.append(f"{type(e).__name__}: {e}")
            self._started.set()
            time.sleep(0.0005)  # leaves the file closed between reads, so a replace can get in

    def __enter__(self) -> Hammer:
        self._thread.start()
        assert self._started.wait(10), "the reader thread never ran"
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(10)


def open_busy[T](fn: Callable[[], T]) -> T:
    """Retry an open that Windows refused while a replace was in flight (as the product's readers do)."""
    for attempt in range(60):
        try:
            return fn()
        except PermissionError:
            if attempt == 59:
                raise
            time.sleep(0.005)
    raise AssertionError("unreachable")


def read_whole_zip(path: Path) -> list[str]:
    def once() -> list[str]:
        with zipfile.ZipFile(path) as zf:
            assert zf.testzip() is None
            return zf.namelist()

    return open_busy(once)


def read_json(path: Path) -> Any:
    return open_busy(lambda: json.loads(path.read_text(encoding="utf-8")))


def make_zip(path: Path, **members: str) -> Path:
    """A finished zip, put in place without ever being half written (the test's own starting point)."""
    tmp = path.with_name(path.name + ".start")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in members.items():
            zf.writestr(name, text)
    open_busy(lambda: os.replace(tmp, path))
    return path


def swap_in_copy(template: Path, path: Path) -> None:
    """Replace `path` with a copy of `template` the way an atomic writer would."""
    tmp = path.with_name(path.name + ".start")
    shutil.copyfile(template, tmp)
    open_busy(lambda: os.replace(tmp, path))


@contextlib.contextmanager
def slow_zip_writes(monkeypatch: pytest.MonkeyPatch, seconds: float = WINDOW_S) -> Iterator[None]:
    """Every `ZipFile.write` / `writestr` takes `seconds` longer after it wrote.

    The window in which a zip built in place has no central directory is then wide enough for the reader to
    land in it every time."""
    real_write, real_writestr = zipfile.ZipFile.write, zipfile.ZipFile.writestr

    def write(self: zipfile.ZipFile, *a: Any, **k: Any) -> None:
        real_write(self, *a, **k)
        time.sleep(seconds)

    def writestr(self: zipfile.ZipFile, *a: Any, **k: Any) -> None:
        real_writestr(self, *a, **k)
        time.sleep(seconds)

    with monkeypatch.context() as m:
        m.setattr(zipfile.ZipFile, "write", write)
        m.setattr(zipfile.ZipFile, "writestr", writestr)
        yield


def zips_in(*folders: Path) -> Callable[[], None]:
    """A reader that opens every diagnostics zip it can see in `folders`, as a sync client would."""

    def once() -> None:
        for folder in folders:
            for z in folder.glob("nightshift-diagnostics-*.zip"):
                read_whole_zip(z)

    return once


# ------------------------------------------------------------------------ the bug, at the controller
def test_a_reader_never_sees_a_torn_zip_while_the_report_is_added_to_the_wizards_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_installer_web_controller import Setup

    s = Setup(tmp_path, monkeypatch)
    (s.home / "logs").mkdir(parents=True)
    assert s.ctl._write_install_report("stopped") is not None
    bundle = tmp_path / "desk" / "nightshift-diagnostics-2026-11-04-1200.zip"
    bundle.parent.mkdir()
    s.ctl._bundle = str(bundle)
    template = make_zip(tmp_path / "template.zip", VERSION="0.1.7\n", **{"logs/big.txt": "x" * 200_000})
    swap_in_copy(template, bundle)

    with slow_zip_writes(monkeypatch), Hammer(lambda: read_whole_zip(bundle)) as seen:
        for _ in range(ROUNDS):
            swap_in_copy(template, bundle)  # the wizard's finished zip, without the report
            s.ctl._add_report_to_bundle()
            names = read_whole_zip(bundle)
            assert "install-report.json" in names and "VERSION" in names
    assert seen.reads > ROUNDS, f"the reader only got {seen.reads} reads in; the test proves nothing"
    assert seen.torn == [], f"{len(seen.torn)} torn reads out of {seen.reads}: {seen.torn[:3]}"
    assert not list(bundle.parent.glob("*.tmp")), "the temporary zip was left behind"
    with zipfile.ZipFile(bundle) as zf:
        assert json.loads(zf.read("install-report.json"))["outcome"] == "stopped"


# ------------------------------------------------------------------------ the diagnostics zips
def test_the_engines_diagnostics_zip_is_not_visible_until_it_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime

    from ns_supervisor import doctor

    paths = Paths(tmp_path / "home")
    paths.logs.mkdir(parents=True)
    (paths.logs / "installer.jsonl").write_text("{}\n", encoding="utf-8")
    out_dir = tmp_path / "desk"
    out_dir.mkdir()

    def slow_freeze(p: Paths, venv: str) -> str | None:
        time.sleep(0.1)  # uv pip freeze takes seconds in life
        return f"{venv}==1\n"

    with Hammer(zips_in(out_dir)) as seen:
        for minute in range(5):  # a new minute names a new zip; the same minute replaces it
            doctor.build_bundle(
                paths,
                out_dir=out_dir,
                now=datetime(2026, 11, 9, 12, minute),
                freeze=slow_freeze,
                smi=lambda: None,
                setup_dir=tmp_path / "setup",
                temp_dir=tmp_path / "temp",
            )
        doctor.build_bundle(
            paths,
            out_dir=out_dir,
            now=datetime(2026, 11, 9, 12, 4),
            freeze=slow_freeze,
            smi=lambda: None,
            setup_dir=tmp_path / "setup",
            temp_dir=tmp_path / "temp",
        )
    assert seen.reads >= 5
    assert seen.torn == [], f"{len(seen.torn)} torn reads out of {seen.reads}: {seen.torn[:3]}"
    assert len(list(out_dir.glob("nightshift-diagnostics-*.zip"))) == 5
    assert not list(out_dir.glob("*.tmp"))


def test_the_wizards_fallback_diagnostics_zip_is_not_visible_until_it_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_installer_web_controller import Setup

    s = Setup(tmp_path, monkeypatch)
    (s.home / "logs").mkdir(parents=True)
    for i in range(3):
        (s.home / "logs" / f"installer-{i}.jsonl").write_text("{}\n", encoding="utf-8")
    desktop = tmp_path / "desktop"
    path: Path | None = None
    with slow_zip_writes(monkeypatch), Hammer(zips_in(desktop)) as seen:
        for _ in range(5):
            path = s.ctl.write_diagnostics()  # the same minute: it replaces the zip it made before
            assert path is not None
    assert path is not None
    assert seen.torn == [], f"{len(seen.torn)} torn reads out of {seen.reads}: {seen.torn[:3]}"
    assert "install-report.json" in read_whole_zip(path)
    assert not list(desktop.glob("*.tmp"))


def test_the_consoles_fallback_bundle_and_its_desktop_copy_are_not_visible_until_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime

    from installer.stage2.diagzip import write_fallback_bundle

    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    paths = Paths(tmp_path / "home")
    paths.logs.mkdir(parents=True)
    (paths.logs / "installer.jsonl").write_text("{}\n", encoding="utf-8")
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    with slow_zip_writes(monkeypatch), Hammer(zips_in(paths.logs, desktop)) as seen:
        for minute in range(4):
            write_fallback_bundle(paths, when=datetime(2026, 11, 9, 12, minute % 2), desktop=desktop)
    assert seen.torn == [], f"{len(seen.torn)} torn reads out of {seen.reads}: {seen.torn[:3]}"
    assert len(list(desktop.glob("nightshift-diagnostics-*.zip"))) == 2
    assert not list(desktop.glob("*.tmp")) and not list(paths.logs.glob("*.tmp"))


# ------------------------------------------------------------------------ the helpers
def test_a_zip_rewritten_many_times_is_always_a_whole_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = make_zip(tmp_path / "out.zip", old="0")
    src = tmp_path / "member.txt"
    src.write_text("payload", encoding="utf-8")

    def fill(n: int) -> Callable[[zipfile.ZipFile], None]:
        def run(zf: zipfile.ZipFile) -> None:
            zf.write(src, "member.txt")
            zf.writestr("round.txt", str(n))

        return run

    with slow_zip_writes(monkeypatch, 0.01), Hammer(lambda: read_whole_zip(target)) as seen:
        for n in range(ROUNDS):
            atomic.write_zip_atomic(target, fill(n))
    assert seen.reads > ROUNDS and seen.torn == [], seen.torn[:3]
    with zipfile.ZipFile(target) as zf:
        assert zf.read("round.txt") == str(ROUNDS - 1).encode() and "old" not in zf.namelist()
    assert [p.name for p in tmp_path.glob("*.tmp")] == []


def test_a_zip_whose_builder_fails_leaves_the_old_zip_and_no_temp_file(tmp_path: Path) -> None:
    target = make_zip(tmp_path / "out.zip", keep="me")

    def broken(zf: zipfile.ZipFile) -> None:
        zf.writestr("half", "x")
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        atomic.write_zip_atomic(target, broken)
    assert read_whole_zip(target) == ["keep"]
    assert not list(tmp_path.glob("*.tmp"))


def test_members_added_to_a_zip_appear_with_the_old_ones_and_only_once(tmp_path: Path) -> None:
    target = make_zip(tmp_path / "bundle.zip", VERSION="0.1.7\n", report="old")
    extra = tmp_path / "extra.json"
    extra.write_text('{"outcome": "done"}', encoding="utf-8")
    added = atomic.append_to_zip_atomic(
        target, [("extra.json", extra), ("blob.bin", b"\x00\x01"), ("report", b"new")]
    )
    assert added == ["extra.json", "blob.bin"]
    with zipfile.ZipFile(target) as zf:
        assert zf.testzip() is None
        assert zf.namelist() == ["VERSION", "report", "extra.json", "blob.bin"]
        assert zf.read("report") == b"old" and json.loads(zf.read("extra.json")) == {"outcome": "done"}
    before = target.stat().st_mtime_ns
    assert atomic.append_to_zip_atomic(target, [("extra.json", extra)]) == []  # nothing to add: not rewritten
    assert target.stat().st_mtime_ns == before
    assert not list(tmp_path.glob("*.tmp"))


def test_a_file_that_is_not_a_zip_is_left_alone_when_members_are_added(tmp_path: Path) -> None:
    target = tmp_path / "bundle.zip"
    target.write_bytes(b"this is not a zip")
    with pytest.raises(zipfile.BadZipFile):
        atomic.append_to_zip_atomic(target, [("a", b"1")])
    assert target.read_bytes() == b"this is not a zip"
    with pytest.raises(FileNotFoundError):
        atomic.append_to_zip_atomic(tmp_path / "missing.zip", [("a", b"1")])
    assert not list(tmp_path.glob("*.tmp"))


def test_a_crash_in_the_swap_leaves_the_old_zip_whole_and_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = make_zip(tmp_path / "bundle.zip", VERSION="0.1.7\n")

    def die(src: object, dst: object) -> None:
        raise OSError("power lost")

    monkeypatch.setattr(atomic.os, "replace", die)
    with pytest.raises(OSError):
        atomic.append_to_zip_atomic(target, [("report", b"x" * 100_000)])
    with pytest.raises(OSError):
        atomic.write_zip_atomic(target, lambda zf: zf.writestr("other", "y"))
    monkeypatch.undo()
    assert read_whole_zip(target) == ["VERSION"]
    assert not list(tmp_path.glob("*.tmp"))


def test_a_text_file_rewritten_many_times_is_always_whole(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    atomic.write_text_atomic(target, json.dumps({"n": -1, "pad": ""}))
    with Hammer(lambda: read_json(target)) as seen:
        for n in range(ROUNDS * 5):
            atomic.write_text_atomic(target, json.dumps({"n": n, "pad": "x" * 50_000}))
    assert seen.reads > ROUNDS and seen.torn == [], seen.torn[:3]
    assert read_json(target)["n"] == ROUNDS * 5 - 1
    assert not list(tmp_path.glob("*.tmp"))


def test_bytes_streams_and_copies_arrive_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    blob = bytes(range(256)) * 4000
    target = tmp_path / "blob.bin"
    atomic.write_bytes_atomic(target, blob)
    assert target.read_bytes() == blob
    atomic.write_stream_atomic(target, io.BytesIO(blob[:1000]))
    assert target.read_bytes() == blob[:1000]
    copy = atomic.copy_atomic(target, tmp_path / "sub" / "copy.bin")
    assert copy.read_bytes() == blob[:1000]

    def die(src: object, dst: object) -> None:
        raise OSError("power lost")

    monkeypatch.setattr(atomic.os, "replace", die)
    for attempt in (
        lambda: atomic.write_bytes_atomic(target, b"new"),
        lambda: atomic.write_stream_atomic(target, io.BytesIO(b"new")),
        lambda: atomic.copy_atomic(copy, target),
    ):
        with pytest.raises(OSError):
            attempt()
    monkeypatch.undo()
    assert target.read_bytes() == blob[:1000]
    assert not list(tmp_path.rglob("*.tmp"))


def test_a_copy_that_dies_half_way_never_shows_under_the_final_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "video.mp4"
    src.write_bytes(b"v" * 100_000)
    dest = tmp_path / "outbox" / "video.mp4"

    def half_copy(a: Path, b: Path) -> None:
        Path(b).write_bytes(Path(a).read_bytes()[:1000])
        raise OSError("disk full")

    monkeypatch.setattr(atomic.shutil, "copy2", half_copy)
    with pytest.raises(OSError):
        atomic.copy_atomic(src, dest)
    assert not dest.exists() and not list(dest.parent.glob("*"))


def test_a_busy_open_is_retried_and_a_missing_file_is_not() -> None:
    calls = {"n": 0}

    def busy_twice() -> str:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(5, "Access is denied")
        return "ok"

    assert atomic.retry_busy(busy_twice) == "ok" and calls["n"] == 3
    calls["n"] = 0

    def always_busy() -> str:
        calls["n"] += 1
        raise PermissionError(5, "Access is denied")

    with pytest.raises(PermissionError):
        atomic.retry_busy(always_busy, tries=3)
    assert calls["n"] == 3
    calls["n"] = 0

    def missing() -> str:
        calls["n"] += 1
        raise FileNotFoundError("gone")

    with pytest.raises(FileNotFoundError):
        atomic.retry_busy(missing)
    assert calls["n"] == 1


def test_the_update_page_never_reads_idle_while_the_updater_rewrites_its_state(tmp_path: Path) -> None:
    from ns_dashboard.updater import read_update_state
    from ns_supervisor import update_job

    paths = Paths(tmp_path / "home")
    update_job.write_status(paths, "running", "Updating.", pid=os.getpid())

    def once() -> None:
        state = read_update_state(paths)["state"]
        assert state == "running", state

    with Hammer(once) as seen:
        for n in range(ROUNDS * 3):
            update_job.write_status(paths, "running", f"Step {n}.", pid=os.getpid())
    assert seen.reads > ROUNDS and seen.torn == [], seen.torn[:3]


# ------------------------------------------------------------------------ every writer in the audit
def _paths(tmp_path: Path) -> Paths:
    p = Paths(tmp_path / "home")
    p.config.mkdir(parents=True)
    return p


def _answers_view(tmp_path: Path) -> tuple[Path, Callable[[], object]]:
    from installer.stage2.web import answers_view

    f = tmp_path / "home" / "config" / "answers.json"
    f.parent.mkdir(parents=True)
    f.write_text('{"home_path": "old"}', encoding="utf-8")
    return f, lambda: answers_view.write_answers(f, {"home_path": "new"})


def _kept(tmp_path: Path) -> tuple[Path, Callable[[], object]]:
    from installer.stage2.web import answers_view

    f = tmp_path / "config" / "answers.json"
    f.parent.mkdir(parents=True)
    f.write_text('{"home_path": "earlier"}', encoding="utf-8")
    kept = f.with_name(answers_view.KEPT_NAME)
    return kept, lambda: answers_view.keep_earlier_answers(f)


def _blank(tmp_path: Path) -> tuple[Path, Callable[[], object]]:
    from installer.stage2.answers import blank_secrets_in_file

    f = tmp_path / "answers.json"
    f.write_text('{"pexels_api_key": "SECRET", "x": 1}', encoding="utf-8")
    return f, lambda: blank_secrets_in_file(f)


def _flags(tmp_path: Path) -> tuple[Path, Callable[[], object]]:
    from installer.stage2 import state

    p = _paths(tmp_path)
    state.save_flags(p, {"old": True})
    return state.flags_path(p), lambda: state.save_flags(p, {"new": True})


def _schedule(tmp_path: Path) -> tuple[Path, Callable[[], object]]:
    from ns_supervisor import schedule_cfg

    p = _paths(tmp_path)
    schedule_cfg.write_run_time(p, "01:00")
    return schedule_cfg.schedule_file(p), lambda: schedule_cfg.write_run_time(p, "02:30")


def _health(tmp_path: Path) -> tuple[Path, Callable[[], object]]:
    from ns_supervisor import health_cfg

    p = _paths(tmp_path)
    health_cfg.write_health_timeout(p, 300.0)
    return health_cfg.health_file(p), lambda: health_cfg.write_health_timeout(p, 600.0)


def _dashboard_settings(tmp_path: Path) -> tuple[Path, Callable[[], object]]:
    from ns_dashboard import state

    p = _paths(tmp_path)
    state.save_dashboard_settings(p, {"run_time": "01:00"})
    return state.settings_file(p), lambda: state.save_dashboard_settings(p, {"run_time": "03:00"})


def _stage_state(tmp_path: Path) -> tuple[Path, Callable[[], object]]:
    from ns_studio.stages import STATE_FILE, StageState

    tmp_path.mkdir(exist_ok=True)
    st = StageState()
    st.mark_done("a", "h1")
    st.save(tmp_path)
    st.mark_done("b", "h2")
    return tmp_path / STATE_FILE, lambda: st.save(tmp_path)


def _series_text(tmp_path: Path) -> tuple[Path, Callable[[], object]]:
    from ns_studio.series import store

    f = tmp_path / "series.md"
    store.write_text(f, "old\n")
    return f, lambda: store.write_text(f, "new\n")


def _server_files(tmp_path: Path) -> tuple[Path, Callable[[], object]]:
    from installer.stage2.web import launch

    launch.write_server_files(tmp_path, pid=1, port=1, token="t", app_path="a", url="http://x/")
    return tmp_path / launch.SERVER_FILE, lambda: launch.write_server_files(
        tmp_path, pid=2, port=2, token="u", app_path="b", url="http://y/"
    )


def _console_report(tmp_path: Path) -> tuple[Path, Callable[[], object]]:
    from installer.stage2.diagzip import write_console_install_report

    p = _paths(tmp_path)
    write_console_install_report(p, outcome="failed", version="0.1.0")
    return p.logs / "install-report.json", lambda: write_console_install_report(
        p, outcome="done", version="0.1.0"
    )


def _rtf(tmp_path: Path) -> tuple[Path, Callable[[], object]]:
    from ns_common import tts_timeout

    p = _paths(tmp_path)
    tts_timeout.save_rtf(p, 1.0, quick=True)
    return tts_timeout.speed_path(p), lambda: tts_timeout.save_rtf(p, 2.0, quick=False)


def _outbox_copy(tmp_path: Path) -> tuple[Path, Callable[[], object]]:
    from ns_studio.media.outbox import copy_if_changed

    src = tmp_path / "src.mp4"
    src.write_bytes(b"new video, longer than the old one")
    dest = tmp_path / "outbox" / "v.mp4"
    dest.parent.mkdir()
    dest.write_bytes(b"old")
    return dest, lambda: copy_if_changed(src, dest)


def _restore(tmp_path: Path) -> tuple[Path, Callable[[], object]]:
    from ns_supervisor import backup

    p = _paths(tmp_path)
    cfg = p.config / "ui_prefs.json"
    cfg.write_text('{"theme": "old"}', encoding="utf-8")
    z = tmp_path / "b.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr(backup.MANIFEST, "{}")
        zf.writestr("config/ui_prefs.json", '{"theme": "new"}')
    return cfg, lambda: backup.restore_backup(p, z)


WRITERS: dict[str, tuple[Callable[[Path], tuple[Path, Callable[[], object]]], str]] = {
    "answers.json (setup window)": (_answers_view, "old"),
    "answers.kept.json": (_kept, ""),
    "answers.json (key blanking)": (_blank, "SECRET"),
    "wizard_flags.json": (_flags, "old"),
    "schedule.json": (_schedule, "01:00"),
    "supervisor.json (health timeout)": (_health, "300"),
    "dashboard.json": (_dashboard_settings, "01:00"),
    "stage_state.json": (_stage_state, '"a"'),
    "series markdown": (_series_text, "old"),
    "setup-server.json": (_server_files, '"pid": 1'),
    "install-report.json (console)": (_console_report, "failed"),
    "tts_speed.json": (_rtf, '"rtf": 1.0'),
    "outbox video copy": (_outbox_copy, "old"),
    "restored config file": (_restore, "old"),
}


@pytest.mark.parametrize("name", sorted(WRITERS))
def test_a_crash_in_the_swap_leaves_the_old_file_whole(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make, expect_old = WRITERS[name]
    target, write = make(tmp_path)
    before = target.read_bytes() if target.exists() else None
    if name == "answers.kept.json":
        assert before is None  # nothing yet: a crash must leave nothing, not half a copy
    else:
        assert expect_old.encode() in (before or b"")
    swaps: list[Path] = []
    real_replace = os.replace

    def die(src: Any, dst: Any) -> None:
        swaps.append(Path(dst))
        raise OSError("power lost")

    monkeypatch.setattr(atomic.os, "replace", die)
    with contextlib.suppress(Exception):  # some writers report the failure, some swallow it
        write()
    monkeypatch.setattr(atomic.os, "replace", real_replace)
    assert target in [p for p in swaps] or any(p.resolve() == target.resolve() for p in swaps), (
        f"{name}: {target} was not written through the atomic swap (swapped: {swaps})"
    )
    after = target.read_bytes() if target.exists() else None
    assert after == before, f"{name}: the file changed although the swap never happened"
    assert not list(target.parent.rglob("*.tmp")), f"{name}: a temporary file was left behind"
