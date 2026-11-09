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
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import threading
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from ns_common import atomic
from ns_common.errors import NsError
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
    rounds = ROUNDS * 2  # 200 fsynced rewrites took 14 s and proved no more than 80 do
    atomic.write_text_atomic(target, json.dumps({"n": -1, "pad": ""}))
    with Hammer(lambda: read_json(target)) as seen:
        for n in range(rounds):
            atomic.write_text_atomic(target, json.dumps({"n": n, "pad": "x" * 50_000}))
    assert seen.reads > ROUNDS and seen.torn == [], seen.torn[:3]
    assert read_json(target)["n"] == rounds - 1
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


def _clip_sidecars(tmp_path: Path) -> tuple[Path, Callable[[], object]]:
    from ns_studio.clipper.models import ClipMetadata
    from ns_studio.clipper.stages_clip import write_clip_sidecars

    def meta(title: str) -> ClipMetadata:
        return ClipMetadata(
            channel="after-dark",
            job_id="after-dark-20261101-ab12",
            k=1,
            title=title,
            description="D",
            tags=["a"],
            hashtags=["#Shorts"],
            publish_after="2026-11-02T18:00",
            source_video="v.mp4",
            template="center_crop",
        )

    j, t = tmp_path / "out" / "c1.json", tmp_path / "out" / "c1.txt"
    j.parent.mkdir()
    write_clip_sidecars(j, t, meta("old"), "c1.mp4")
    return j, lambda: write_clip_sidecars(j, t, meta("new"), "c1.mp4")


# Every case is in this literal: `parametrize` below reads the dict once, when the decorator runs, so a case
# added to `WRITERS` further down the file is silently never collected (the clip sidecar case was).
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
    "clip sidecar (.json)": (_clip_sidecars, '"title": "old"'),
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


def test_every_writer_in_the_table_is_a_crash_case_that_gets_collected() -> None:
    """`parametrize` copies `sorted(WRITERS)` when the decorator runs: a case added to the dict after that is
    never collected, and the run still looks green (14 cases ran where the table held 15)."""
    marks = [
        m for m in test_a_crash_in_the_swap_leaves_the_old_file_whole.pytestmark if m.name == "parametrize"
    ]
    assert len(marks) == 1 and marks[0].args[0] == "name"
    assert list(marks[0].args[1]) == sorted(WRITERS)
    assert "clip sidecar (.json)" in marks[0].args[1]


def test_a_clip_rewritten_in_the_outbox_has_both_sidecars_and_no_temp_file(tmp_path: Path) -> None:
    j, rewrite = _clip_sidecars(tmp_path)
    rewrite()
    assert '"title": "new"' in j.read_text(encoding="utf-8")
    assert "TITLE: new" in j.with_suffix(".txt").read_text(encoding="utf-8")
    assert sorted(p.name for p in j.parent.iterdir()) == ["c1.json", "c1.txt"]


# ------------------------------------------------------------------ a reader that holds the file open
# Windows refuses `os.replace` over a file another program has open (the dashboard playing an outbox
# video, a mail client holding a zip, a virus scan, a sync client). The writer waits for the program to
# let go, for at most `REPLACE_WAIT_S`; after that it fails with a coded error and the old file is
# exactly as it was. It never writes the new content over the old file in place: a reader (or a crash)
# would meet it torn.
OLD = b"old"
NEW = b"new content, longer than the old"
windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="only Windows refuses os.replace over a file another program has open; "
    "elsewhere there is nothing to wait out",
)


class FakeClock:
    """Stands in for `time` inside `atomic`: a sleep moves the clock and takes (almost) no real time.

    `real_per_fake` lets a test keep a little real time passing, so a reader thread runs while the writer
    waits.
    """

    def __init__(self, real_per_fake: float = 0.0) -> None:
        self.now = 1000.0
        self.slept = 0.0
        self._scale = real_per_fake

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.now += seconds
        if self._scale:
            time.sleep(seconds * self._scale)

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return time.time()


def refuse_every_swap(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every `os.replace` is refused as Windows refuses it over a file that is open; returns the targets."""
    tried: list[Path] = []

    def refuse(src: Any, dst: Any) -> None:
        tried.append(Path(dst))
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(atomic.os, "replace", refuse)
    return tried


def writes_in_place(monkeypatch: pytest.MonkeyPatch, target: Path, *, stretch_s: float = 0.0) -> list[Path]:
    """Spy on `shutil.copyfile` over `target` (the old fallback). With `stretch_s` the copy leaves the file
    empty for that long first, so a reader of the old code meets it torn every time and not now and then."""
    calls: list[Path] = []
    real = shutil.copyfile

    def copyfile(src: Any, dst: Any, **k: Any) -> Any:
        if Path(dst) == target:
            calls.append(Path(dst))
            if stretch_s:
                Path(dst).write_bytes(b"")
                time.sleep(stretch_s)
        return real(src, dst, **k)

    monkeypatch.setattr(atomic.shutil, "copyfile", copyfile)
    return calls


def _copy(src: Path, dst: Path) -> None:
    src.write_bytes(NEW)
    atomic.copy_atomic(src, dst)


def _text(src: Path, dst: Path) -> None:
    atomic.write_text_atomic(dst, NEW.decode())


def _bytes(src: Path, dst: Path) -> None:
    atomic.write_bytes_atomic(dst, NEW)


def _chunks(src: Path, dst: Path) -> None:
    atomic.write_chunks_atomic(dst, iter([NEW[:5], NEW[5:]]))


def _stream(src: Path, dst: Path) -> None:
    atomic.write_stream_atomic(dst, io.BytesIO(NEW))


def _outbox(src: Path, dst: Path) -> None:
    from ns_studio.media.outbox import copy_if_changed

    src.write_bytes(NEW)
    copy_if_changed(src, dst)


WRITE_KINDS = [_copy, _text, _bytes, _chunks, _stream, _outbox]


@pytest.mark.parametrize("write", WRITE_KINDS)
def test_a_file_held_open_past_the_wait_stays_as_it_was_and_the_error_is_coded(
    write: Callable[[Path, Path], None], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dst = tmp_path / "v.mp4"
    dst.write_bytes(OLD)
    clock = FakeClock()
    in_place = writes_in_place(monkeypatch, dst)
    tried = refuse_every_swap(monkeypatch)
    monkeypatch.setattr(atomic, "time", clock)
    with pytest.raises(NsError) as ei:
        write(tmp_path / "src.bin", dst)
    err = ei.value
    assert err.code == "NS-E119" and "v.mp4" in err.detail
    # callers that catch OSError / PermissionError around a write (the updater's progress lines, the restore's
    # roll back, the key store) still catch it
    assert isinstance(err, PermissionError)
    assert dst.read_bytes() == OLD, "the old file must be left whole"
    assert in_place == [], "the content must never be written over the old file"
    assert len(tried) > 5 and 10 <= clock.slept <= 20, (
        f"{len(tried)} tries over {clock.slept:.1f} s of waiting"
    )
    assert not list(tmp_path.glob("*.tmp"))


@windows_only
@pytest.mark.parametrize("write", WRITE_KINDS)
def test_a_file_another_program_holds_open_is_replaced_as_soon_as_it_lets_go(
    write: Callable[[Path, Path], None], tmp_path: Path
) -> None:
    dst = tmp_path / "v.mp4"
    dst.write_bytes(OLD)
    holder = dst.open("rb")  # a plain open: Windows refuses to replace the file under it
    release = threading.Timer(0.4, holder.close)
    release.start()
    began = time.monotonic()
    try:
        write(tmp_path / "src.bin", dst)
    finally:
        release.join()
        holder.close()
    assert time.monotonic() - began >= 0.3, "the writer did not wait for the reader"
    assert dst.read_bytes() == NEW
    assert not list(tmp_path.glob("*.tmp"))


@windows_only
def test_a_real_reader_that_never_lets_go_is_not_disturbed_and_the_writer_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dst = tmp_path / "v.mp4"
    dst.write_bytes(OLD)
    clock = FakeClock()
    in_place = writes_in_place(monkeypatch, dst)
    monkeypatch.setattr(atomic, "time", clock)  # the wait passes in a blink; the refusals are Windows' own
    with dst.open("rb") as holder:
        with pytest.raises(NsError) as ei:
            atomic.write_bytes_atomic(dst, NEW)
        assert holder.read() == OLD, "the reader's file changed under it"
    assert ei.value.code == "NS-E119" and in_place == [] and clock.slept >= 10
    assert dst.read_bytes() == OLD
    assert not list(tmp_path.glob("*.tmp"))


def test_a_zip_held_open_past_the_wait_stays_whole_and_the_error_is_coded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    z = make_zip(tmp_path / "d.zip", **{"a.txt": "old"})
    clock = FakeClock()
    in_place = writes_in_place(monkeypatch, z)
    refuse_every_swap(monkeypatch)
    monkeypatch.setattr(atomic, "time", clock)
    with pytest.raises(NsError) as rebuilt:
        atomic.write_zip_atomic(z, lambda zf: zf.writestr("b.txt", "new"))
    with pytest.raises(NsError) as appended:
        atomic.append_to_zip_atomic(z, [("c.txt", b"new")])
    monkeypatch.undo()
    assert rebuilt.value.code == appended.value.code == "NS-E119"
    assert read_whole_zip(z) == ["a.txt"] and in_place == []
    assert not list(tmp_path.glob("*.tmp"))


@windows_only
def test_a_zip_a_mail_client_holds_open_is_rebuilt_once_it_lets_go(tmp_path: Path) -> None:
    z = make_zip(tmp_path / "d.zip", **{"a.txt": "old"})
    holder = z.open("rb")
    release = threading.Timer(0.4, holder.close)
    release.start()
    try:
        atomic.write_zip_atomic(z, lambda zf: zf.writestr("b.txt", "new"))
    finally:
        release.join()
        holder.close()
    assert read_whole_zip(z) == ["b.txt"]
    assert not list(tmp_path.glob("*.tmp"))


def test_a_target_that_is_a_folder_fails_at_once_and_is_not_waited_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(atomic, "time", clock)
    dst = tmp_path / "v.mp4"
    dst.mkdir()
    with pytest.raises(OSError) as ei:
        atomic.write_bytes_atomic(dst, b"new")
    assert not isinstance(ei.value, NsError), "a folder in the way is not 'another program holds the file'"
    assert clock.slept == 0
    assert not list(tmp_path.glob("*.tmp"))


@windows_only
def test_a_read_only_file_fails_at_once_and_is_not_waited_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    dst = tmp_path / "v.mp4"
    dst.write_bytes(OLD)
    os.chmod(dst, stat.S_IREAD)
    monkeypatch.setattr(atomic, "time", clock)
    try:
        with pytest.raises(PermissionError) as ei:
            atomic.write_bytes_atomic(dst, NEW)
    finally:
        os.chmod(dst, stat.S_IWRITE)
    assert not isinstance(ei.value, NsError) and clock.slept == 0
    assert dst.read_bytes() == OLD and not list(tmp_path.glob("*.tmp"))


@windows_only
def test_the_update_page_never_reads_idle_while_a_third_program_holds_the_state_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unexplained flake of `test_the_update_page_never_reads_idle...`: while a program other than the
    page (a virus scan, an indexer) held `update_state.json` for longer than the retries lasted, the old
    fallback truncated the file in place and wrote it again; a poll in between read an empty file, which
    `read_status` takes for 'no update running'. The copy is stretched to 20 ms here so that poll lands in it
    every time. Now the write fails with the coded error and the page keeps showing the last whole state."""
    from ns_dashboard.updater import read_update_state
    from ns_supervisor import update_job

    paths = Paths(tmp_path / "home")
    update_job.write_status(paths, "running", "Updating.", pid=os.getpid())
    status = update_job.status_file(paths)
    in_place = writes_in_place(monkeypatch, status, stretch_s=WINDOW_S)
    monkeypatch.setattr(atomic, "time", FakeClock(real_per_fake=0.01))  # a 15 s wait takes 0.15 s

    def once() -> None:
        state = read_update_state(paths)["state"]
        assert state == "running", state

    with status.open("rb"), Hammer(once) as seen:  # the third program, and the page polling
        for n in range(8):
            with pytest.raises(NsError) as ei:
                update_job.write_status(paths, "running", f"Step {n}.", pid=os.getpid())
            assert ei.value.code == "NS-E119"
    assert in_place == []
    assert seen.reads > 8 and seen.torn == [], (
        f"{len(seen.torn)} torn reads out of {seen.reads}: {seen.torn[:3]}"
    )
    assert json.loads(status.read_text(encoding="utf-8"))["message"] == "Updating."
    assert not list(status.parent.glob("*.tmp"))


def test_a_failed_swap_of_a_read_only_source_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`copy2` copies the READONLY attribute onto the temp file, which `unlink` then refuses to delete."""
    src = tmp_path / "ro.bin"
    src.write_bytes(b"x")
    os.chmod(src, stat.S_IREAD)
    dst = tmp_path / "out" / "ro.bin"

    def die(a: Any, b: Any) -> None:
        raise OSError("power lost")

    monkeypatch.setattr(atomic.os, "replace", die)
    try:
        with pytest.raises(OSError):
            atomic.copy_atomic(src, dst)
    finally:
        monkeypatch.undo()
        os.chmod(src, stat.S_IWRITE)
    assert list(dst.parent.glob("*")) == []


# ------------------------------------------------------------------ leftovers of a crash
def test_a_temporary_name_is_short_and_matches_no_sibling_glob(tmp_path: Path) -> None:
    target = tmp_path / ("a-very-long-outbox-name-" * 6 + ".mp4")
    tmp = atomic._tmp_for(target)  # noqa: SLF001
    assert len(tmp.name) < 30 and tmp.parent == target.parent
    assert tmp != atomic._tmp_for(target)  # noqa: SLF001 - two writers to one file never share a name
    for pattern in (f"{target.stem}.*", "source.*", "*.mp4", "*.json", "*.zip", "*.yaml"):
        assert not tmp.match(pattern), pattern


def test_find_download_never_takes_a_temporary_file_for_the_video(tmp_path: Path) -> None:
    from ns_studio.clipper.source import find_download

    (tmp_path / "source.mkv.tmp").write_bytes(b"x" * 100)
    assert find_download(tmp_path) is None


def test_stale_temporary_files_are_swept_and_young_or_foreign_ones_are_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = atomic._tmp_for(tmp_path / "v.mp4")  # noqa: SLF001
    stale.write_bytes(b"x" * 10)
    mine = tmp_path / "notes.tmp"  # somebody else's file
    mine.write_text("keep", encoding="utf-8")
    assert atomic.sweep_stale_tmp(tmp_path) == 0 and stale.exists()  # a moment old is not stale
    assert atomic.sweep_stale_tmp(tmp_path / "missing") == 0
    monkeypatch.setattr(atomic, "STALE_TMP_S", -5.0)  # "an hour ago" is now in the future of every file
    assert atomic.sweep_stale_tmp(tmp_path) == 1
    assert not stale.exists() and mine.exists()


def test_the_outbox_copy_sweeps_the_temporary_video_a_power_cut_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ns_studio.media.outbox import copy_if_changed

    out = tmp_path / "outbox"
    out.mkdir()
    left_over = atomic._tmp_for(out / "v.mp4")  # noqa: SLF001
    left_over.write_bytes(b"x" * 1000)
    src = tmp_path / "src.mp4"
    src.write_bytes(b"video")
    monkeypatch.setattr(atomic, "STALE_TMP_S", -5.0)
    copy_if_changed(src, out / "v.mp4")
    assert [p.name for p in out.iterdir()] == ["v.mp4"]


def test_the_diagnostics_bundle_sweeps_the_temporary_zips_a_power_cut_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from installer.stage2 import diagzip

    paths = _paths(tmp_path)
    paths.logs.mkdir()
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    left = [atomic._tmp_for(paths.logs / "x.zip"), atomic._tmp_for(desktop / "x.zip")]  # noqa: SLF001
    for f in left:
        f.write_bytes(b"PK half a zip")
    monkeypatch.setattr(atomic, "STALE_TMP_S", -5.0)
    out = diagzip.write_fallback_bundle(paths, desktop=desktop)
    assert not any(f.exists() for f in left)
    assert read_whole_zip(out) is not None and (desktop / out.name).is_file()


def test_an_update_zip_that_stops_half_way_leaves_no_zip_and_no_temp_file(tmp_path: Path) -> None:
    import httpx

    from ns_common.errors import NsError
    from ns_supervisor import releases
    from tests.test_ns_update_github import client_for, release_json

    data = release_json("0.2.0")
    rel = releases.parse_release(data)

    visible: list[bool] = []

    class Cut(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"PK-half"
            visible.append((tmp_path / rel.zip_name).exists())  # is the half zip under its final name now?
            raise httpx.ReadError("connection lost")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.github.com":
            return httpx.Response(200, json=data)
        return httpx.Response(200, stream=Cut())

    cut = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    with pytest.raises(NsError):
        releases.download(rel, tmp_path, cut)
    assert visible == [False] and list(tmp_path.iterdir()) == []
    z = releases.download(rel, tmp_path, client_for(data))  # and the whole download still works
    assert z.read_bytes().startswith(b"PK") and sorted(p.name for p in tmp_path.iterdir()) == [
        z.name,
        z.name + ".sha256",
    ]


def _watch_swaps(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every `os.replace` through `atomic`, as (name it is moved to, SHA-256 of the bytes it moves)."""
    swaps: list[tuple[str, str]] = []
    real = os.replace

    def spy(src: Any, dst: Any) -> None:
        swaps.append((Path(dst).name, hashlib.sha256(Path(src).read_bytes()).hexdigest()))
        real(src, dst)

    monkeypatch.setattr(atomic.os, "replace", spy)
    return swaps


def test_an_update_zip_that_fails_its_check_is_never_put_under_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ns_supervisor import releases
    from tests.test_ns_update_github import client_for, release_json

    data = release_json("0.2.0")
    data["assets"][1]["digest"] = "sha256:" + "0" * 64  # not the digest of what the server sends
    rel = releases.parse_release(data)
    swaps = _watch_swaps(monkeypatch)
    with pytest.raises(NsError) as ei:
        releases.download(rel, tmp_path, client_for(data))
    assert swaps == [], f"the unchecked zip was moved to {swaps}: for a moment it had its final name"
    assert list(tmp_path.iterdir()) == []
    assert ei.value.code == "NS-E701" and "mismatch" in ei.value.detail
    assert "discarded" in ei.value.detail and "nothing was saved" in ei.value.detail, ei.value.detail


def test_an_update_zip_is_moved_under_its_name_only_after_its_digest_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ns_supervisor import releases
    from tests.test_ns_update_github import DIGEST, client_for, release_json

    data = release_json("0.2.0")
    rel = releases.parse_release(data)
    swaps = _watch_swaps(monkeypatch)
    z = releases.download(rel, tmp_path, client_for(data))
    assert swaps[0] == (z.name, DIGEST), (
        f"the zip moved to its name carried other bytes than the pinned ones: {swaps}"
    )
    assert [name for name, _ in swaps] == [z.name, z.name + ".sha256"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [z.name, z.name + ".sha256"]


def test_a_failed_download_leaves_no_zip_of_that_name_not_even_an_older_one(tmp_path: Path) -> None:
    """The documented behaviour kept as it was: after a failure nothing sits under the name, so a later step
    never takes a leftover for the zip it asked for."""
    from ns_supervisor import releases
    from tests.test_ns_update_github import ZIP_BYTES, client_for, release_json

    good = release_json("0.2.0")
    z = releases.download(releases.parse_release(good), tmp_path, client_for(good))
    assert z.read_bytes() == ZIP_BYTES
    bad = release_json("0.2.0")
    bad["assets"][1]["digest"] = "sha256:" + "f" * 64
    with pytest.raises(NsError):
        releases.download(releases.parse_release(bad), tmp_path, client_for(bad))
    assert not z.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_an_update_upload_that_is_too_big_or_empty_leaves_nothing_under_its_name(tmp_path: Path) -> None:
    from ns_dashboard.errors import ApiError
    from ns_dashboard.svc_library import _save_upload

    dest = tmp_path / "Nightshift-Update-0.2.0.zip"
    with pytest.raises(ApiError) as ei:
        _save_upload(io.BytesIO(b"x" * 2_000_000), dest, 1_500_000, "too big")
    assert ei.value.status == 413 and list(tmp_path.iterdir()) == []
    assert _save_upload(io.BytesIO(b""), dest, 10, "too big") == 0 and list(tmp_path.iterdir()) == []
    assert _save_upload(io.BytesIO(b"PK1234"), dest, 10, "too big") == 6
    assert dest.read_bytes() == b"PK1234" and [p.name for p in tmp_path.iterdir()] == [dest.name]


def test_the_uploaded_updates_checksum_file_is_swapped_in_whole(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_round3_misc import ZIP, sha_of, update_dash

    dash, fake = update_dash(tmp_home)
    swaps = _watch_swaps(monkeypatch)
    sidecar = io.BytesIO(sha_of(b"PK\x03\x04zip"))
    dash.library.update_apply_file(ZIP, io.BytesIO(b"PK\x03\x04zip"), sidecar)
    assert [name for name, _ in swaps] == [ZIP, ZIP + ".sha256"], "`ns update` reads the checksum file next"
    (zip_path,) = fake.files
    assert (
        zip_path.with_name(ZIP + ".sha256")
        .read_text(encoding="utf-8")
        .startswith(hashlib.sha256(b"PK\x03\x04zip").hexdigest())
    )
    assert not list(zip_path.parent.glob("*.tmp"))


def test_a_backup_does_not_carry_a_temporary_file_a_crash_left_in_config(tmp_path: Path) -> None:
    from ns_supervisor import backup

    p = _paths(tmp_path)
    (p.config / "ui_prefs.json").write_text("{}", encoding="utf-8")
    left = atomic._tmp_for(p.config / "runtime.json")  # noqa: SLF001
    left.write_bytes(b"half a file")
    with zipfile.ZipFile(backup.make_backup(p, tmp_path / "b.zip")) as zf:
        names = zf.namelist()
    assert "config/ui_prefs.json" in names and not any(n.endswith(".tmp") for n in names), names


# ------------------------------------------------------ callers that catch OSError meet the coded error
def test_a_restore_whose_swap_is_refused_rolls_everything_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ns_supervisor import backup

    p = _paths(tmp_path)
    cfg = p.config / "ui_prefs.json"
    cfg.write_text('{"theme": "old"}', encoding="utf-8")
    z = tmp_path / "b.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr(backup.MANIFEST, "{}")
        zf.writestr("config/ui_prefs.json", '{"theme": "new"}')
    refuse_every_swap(monkeypatch)
    monkeypatch.setattr(atomic, "time", FakeClock())
    with pytest.raises(NsError) as ei:
        backup.restore_backup(p, z)
    monkeypatch.undo()
    assert ei.value.code == "NS-E703" and "NS-E119" in ei.value.detail, ei.value.detail
    assert cfg.read_text(encoding="utf-8") == '{"theme": "old"}'
    assert not list(p.config.glob("*.tmp"))


def test_the_updater_goes_on_when_a_progress_line_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ns_supervisor import update_job

    paths = Paths(tmp_path / "home")
    update_job.write_status(paths, "running", "Step 1.", pid=os.getpid())
    refuse_every_swap(monkeypatch)
    monkeypatch.setattr(atomic, "time", FakeClock())
    update_job._progress(paths, "running", "Step 2.", pid=os.getpid())  # noqa: SLF001 - must not raise
    monkeypatch.undo()
    assert "could not write update_state.json" in capsys.readouterr().err
    assert update_job.read_status(paths)["message"] == "Step 1."


# ---------------------------------------------------------------- F-9 break-it round


def test_an_uploaded_update_zip_that_does_not_match_its_checksum_is_never_put_under_its_name(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The zip used to be published under its final name by `_save_upload` before any digest check; the check
    came later in `ns update`, and a refusal there left the unverified zip in `data/updates`."""
    from ns_dashboard.errors import ApiError
    from tests.test_round3_misc import ZIP, sha_of, update_dash

    dash, fake = update_dash(tmp_home)
    folder = dash.state.paths.data / "updates"
    swaps = _watch_swaps(monkeypatch)
    with pytest.raises(ApiError) as ei:
        dash.library.update_apply_file(ZIP, io.BytesIO(b"PK-other"), io.BytesIO(sha_of(b"PK-pinned")))
    assert ei.value.status == 409 and ei.value.code == "update_refused"
    assert "NS-E701" in str(ei.value) and "mismatch" in str(ei.value) and "not saved" in str(ei.value)
    assert swaps == [], f"the unchecked zip was moved to {swaps}: for a moment it had its final name"
    assert fake.files == [] and list(folder.iterdir()) == []


def test_a_refused_update_upload_leaves_the_earlier_pair_of_that_name_as_it_was(tmp_home: Path) -> None:
    from ns_dashboard.errors import ApiError
    from tests.test_round3_misc import ZIP, sha_of, update_dash

    dash, fake = update_dash(tmp_home)
    dash.library.update_apply_file(ZIP, io.BytesIO(b"PK-first"), io.BytesIO(sha_of(b"PK-first")))
    (zip_path,) = fake.files
    side = zip_path.with_name(ZIP + ".sha256")
    before = (zip_path.read_bytes(), side.read_bytes())
    with pytest.raises(ApiError):
        dash.library.update_apply_file(ZIP, io.BytesIO(b"PK-second"), io.BytesIO(sha_of(b"PK-other")))
    assert (zip_path.read_bytes(), side.read_bytes()) == before
    assert sorted(p.name for p in zip_path.parent.iterdir()) == [ZIP, ZIP + ".sha256"]


@pytest.mark.parametrize(
    ("checksum", "code"),
    [(b"  \n", "empty_file"), (b"not a digest\n", "bad_checksum"), (b"ab" * 31 + b"\n", "bad_checksum")],
)
def test_a_checksum_file_that_is_not_a_digest_saves_neither_file(
    tmp_home: Path, checksum: bytes, code: str
) -> None:
    from ns_dashboard.errors import ApiError
    from tests.test_round3_misc import ZIP, update_dash

    dash, fake = update_dash(tmp_home)
    with pytest.raises(ApiError) as ei:
        dash.library.update_apply_file(ZIP, io.BytesIO(b"PK-zip"), io.BytesIO(checksum))
    assert ei.value.code == code and fake.files == []
    assert not (dash.state.paths.data / "updates").exists(), "refused before anything was saved"


def test_an_empty_update_zip_with_a_matching_checksum_is_not_saved(tmp_home: Path) -> None:
    from ns_dashboard.errors import ApiError
    from tests.test_round3_misc import ZIP, sha_of, update_dash

    dash, fake = update_dash(tmp_home)
    with pytest.raises(ApiError) as ei:
        dash.library.update_apply_file(ZIP, io.BytesIO(b""), io.BytesIO(sha_of(b"")))
    assert ei.value.code == "empty_file" and fake.files == []
    assert list((dash.state.paths.data / "updates").iterdir()) == []


def _good_download(tmp_path: Path) -> tuple[Any, dict[str, Any], Path]:
    from ns_supervisor import releases
    from tests.test_ns_update_github import client_for, release_json

    data = release_json("0.2.0")
    rel = releases.parse_release(data)
    return rel, data, releases.download(rel, tmp_path, client_for(data))


def test_a_download_whose_write_fails_removes_an_older_zip_of_that_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full disk left the older zip under the name although the docstring promised nothing would."""
    from ns_supervisor import releases
    from tests.test_ns_update_github import client_for

    rel, data, older = _good_download(tmp_path)

    def full(src: Any, dst: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(atomic.os, "replace", full)
    with pytest.raises(OSError, match="No space"):
        releases.download(rel, tmp_path, client_for(data))
    monkeypatch.undo()
    assert not older.exists() and not list(tmp_path.glob("*.tmp"))


def test_a_refused_download_takes_the_older_zips_checksum_file_with_it(tmp_path: Path) -> None:
    """The `.sha256` vouches for the zip that was just discarded: it must not stay beside a name with no zip
    (or, later, beside a zip it was never written for)."""
    from ns_supervisor import releases
    from tests.test_ns_update_github import client_for

    rel, data, older = _good_download(tmp_path)
    sidecar = older.with_name(older.name + ".sha256")
    assert sidecar.is_file()
    bad = {**data, "assets": [dict(a) for a in data["assets"]]}
    bad["assets"][1]["digest"] = "sha256:" + "f" * 64
    with pytest.raises(NsError) as ei:
        releases.download(releases.parse_release(bad), tmp_path, client_for(bad))
    assert ei.value.code == "NS-E701"
    assert not older.exists() and not sidecar.exists()
    assert not list(tmp_path.glob("*.tmp"))


@windows_only
def test_a_mismatch_says_so_even_when_the_older_zip_is_open_in_another_program(tmp_path: Path) -> None:
    """`unlink` of the older zip raised a bare PermissionError that hid the NS-E701 message."""
    from ns_supervisor import releases
    from tests.test_ns_update_github import client_for

    rel, data, older = _good_download(tmp_path)
    bad = {**data, "assets": [dict(a) for a in data["assets"]]}
    bad["assets"][1]["digest"] = "sha256:" + "f" * 64
    with older.open("rb"):  # a plain open: Windows will not delete the file under it
        with pytest.raises(NsError) as ei:
            releases.download(releases.parse_release(bad), tmp_path, client_for(bad))
    assert ei.value.code == "NS-E701" and "mismatch" in ei.value.detail
    assert not list(tmp_path.glob("*.tmp"))


@windows_only
def test_a_download_whose_older_zip_is_open_in_another_program_says_why_and_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented exception: Windows will neither replace nor delete the open name, so the older zip stays
    and the error is the coded one."""
    from ns_supervisor import releases
    from tests.test_ns_update_github import ZIP_BYTES, client_for

    rel, data, older = _good_download(tmp_path)
    monkeypatch.setattr(atomic, "time", FakeClock())
    with older.open("rb"), pytest.raises(NsError) as ei:
        releases.download(rel, tmp_path, client_for(data))
    assert ei.value.code == "NS-E119"
    assert older.read_bytes() == ZIP_BYTES and not list(tmp_path.glob("*.tmp"))


def test_the_updaters_lines_do_not_stall_it_for_long_while_a_program_holds_the_state_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each write could wait the full 15 s, and `_final` tries ten times: a holder that kept
    `update_state.json` for the whole run stalled the updater for about 150 s at the end."""
    from ns_supervisor import update_job

    paths = Paths(tmp_path / "home")
    update_job.write_status(paths, "running", "Step 1.", pid=os.getpid())
    refuse_every_swap(monkeypatch)
    clock = FakeClock()
    monkeypatch.setattr(atomic, "time", clock)
    update_job._progress(paths, "running", "Step 2.", pid=os.getpid())  # noqa: SLF001
    assert 0.5 <= clock.slept <= update_job.PROGRESS_WAIT_S + 0.01, (
        f"one progress line waited {clock.slept} s"
    )

    clock.slept = 0.0

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)

    update_job._final(paths, "done", "Done.", sleep=sleep)  # noqa: SLF001
    assert clock.slept <= 13.0, f"the final line stalled the updater for {clock.slept:.0f} s"
    monkeypatch.undo()
    assert update_job.read_status(paths)["message"] == "Step 1."


def test_the_supervisors_pid_file_does_not_block_its_event_loop_for_15_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ns_supervisor.core import STATE_WAIT_S
    from tests.test_supervisor_core import make

    sup, _, paths = make(tmp_path)
    assert sup.write_state() is True
    before = sup.state_file.read_bytes()
    refuse_every_swap(monkeypatch)
    clock = FakeClock()
    monkeypatch.setattr(atomic, "time", clock)
    assert sup.write_state() is False  # given up after STATE_WAIT_S; it never raises (see the start test)
    monkeypatch.undo()
    assert 1.0 <= clock.slept <= STATE_WAIT_S + 0.01, f"write_state waited {clock.slept} s"
    assert sup.state_file.read_bytes() == before and not list(sup.state_file.parent.glob("*.tmp"))
    lines = [x for f in paths.logs.glob("supervisor-*.jsonl") for x in f.read_text("utf-8").splitlines()]
    records = [json.loads(x) for x in lines]
    assert [r for r in records if r["level"] == "WARN" and "NS-E119" in r["msg"]], records
    sup.close_job()


def test_a_held_state_file_does_not_fail_a_healthy_start_or_orphan_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`write_state` raised NS-E119 right after the spawn, outside the try that terminates the child: a
    program holding `run/supervisor.json` for more than STATE_WAIT_S left a running (GPU) child nobody
    supervised and failed the acquire, or turned a healthy start into a failure after 'ready'."""
    import asyncio

    from tests.test_supervisor_core import make, running

    sup, _, _ = make(tmp_path)
    held = sup.state_file
    real_replace = os.replace

    def refuse_state(src: Any, dst: Any) -> None:
        if Path(dst) == held:
            raise PermissionError(13, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(atomic.os, "replace", refuse_state)
    monkeypatch.setattr(atomic, "time", FakeClock())

    async def main() -> None:
        async with running(sup):
            granted = await sup.acquire("llama", "A")  # NS-E119 here before the fix
            assert granted["endpoint"].startswith("http://127.0.0.1:")
            assert sup.status()["services"]["llama"]["state"] == "ready"
            assert await sup.release("llama", "A") is True

    asyncio.run(main())
