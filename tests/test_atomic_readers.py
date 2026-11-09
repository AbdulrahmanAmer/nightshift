"""F-1 (break-it round): readers of an atomically swapped file ride out the moment Windows refuses an open.

`os.replace` swaps a file in whole, but for a few milliseconds a reader's `open` gets PermissionError although
the file is there. A reader that took that for "no file" showed the update page as idle, saw a dev install as
not dev, fired a toast the owner had switched off, skipped a publish day and so on. Each reader here is run
against a file whose first two opens are refused; it has to come back with the file's content.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from ns_common.paths import Paths
from tests.helpers import make_channel
from tests.test_atomic_writes import ROUNDS, Hammer


@contextlib.contextmanager
def busy_opens(monkeypatch: pytest.MonkeyPatch, name: str, times: int = 2) -> Iterator[dict[str, int]]:
    """The first `times` reads of a file called `name` fail the way Windows does during a replace."""
    real = Path.read_text
    left = {"n": times}

    def read_text(self: Path, *a: Any, **k: Any) -> str:
        if self.name == name and left["n"] > 0:
            left["n"] -= 1
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    yield left


def _home(tmp_path: Path) -> Paths:
    p = Paths(tmp_path / "home")
    p.config.mkdir(parents=True)
    return p


def _update_status(tmp_path: Path) -> tuple[str, Callable[[], object], object]:
    from ns_supervisor import update_job

    p = _home(tmp_path)
    update_job.write_status(p, "done", "Updated.")
    return update_job.status_file(p).name, lambda: update_job.read_status(p).get("state"), "done"


def _runtime_dev(tmp_path: Path) -> tuple[str, Callable[[], object], object]:
    from ns_common import devmode

    p = _home(tmp_path)
    p.runtime_file.write_text('{"dev_mode": true}', encoding="utf-8")
    return p.runtime_file.name, lambda: devmode.is_dev(p), True


def _notifications_off(tmp_path: Path) -> tuple[str, Callable[[], object], object]:
    from ns_common import config

    p = _home(tmp_path)
    (p.config / "dashboard.json").write_text('{"notifications": false}', encoding="utf-8")
    return "dashboard.json", lambda: config.notifications_enabled(p), False


def _answers_load_raw(tmp_path: Path) -> tuple[str, Callable[[], object], object]:
    from installer.stage2 import answers

    f = _home(tmp_path).config / "answers.json"
    f.write_text('{"home_path": "D:/ns"}', encoding="utf-8")
    return f.name, lambda: answers.load_raw(f)["home_path"], "D:/ns"


def _answers_view(tmp_path: Path) -> tuple[str, Callable[[], object], object]:
    from installer.stage2.web import answers_view

    f = _home(tmp_path).config / "answers.json"
    f.write_text('{"home_path": "D:/ns"}', encoding="utf-8")
    return f.name, lambda: answers_view.read_answers(f)["home_path"], "D:/ns"


def _encoder(tmp_path: Path) -> tuple[str, Callable[[], object], object]:
    from installer.stage2.web.controller import SetupController

    p = _home(tmp_path)
    (p.config / "runtime.json").write_text('{"video": {"encoder": "libx264"}}', encoding="utf-8")
    fake_self = SimpleNamespace(home=p.home)
    return "runtime.json", lambda: SetupController._encoder(fake_self), "libx264"  # type: ignore[arg-type]


def _console_report(tmp_path: Path) -> tuple[str, Callable[[], object], object]:
    from installer.stage2.diagzip import write_console_install_report

    p = _home(tmp_path)
    (p.config / "install_state.json").write_text('{"dev_mode": true, "steps": {}}', encoding="utf-8")

    def report() -> object:
        out = write_console_install_report(p, outcome="done", version="0.1.0")
        assert out is not None
        return json.loads(out.read_text(encoding="utf-8"))["dev_mode"]

    return "install_state.json", report, True


def _channel_yaml(tmp_path: Path) -> tuple[str, Callable[[], object], object]:
    from ns_studio.channels import crud

    f = tmp_path / "c.yaml"
    f.write_text(yaml.safe_dump(make_channel().model_dump(mode="json")), encoding="utf-8")
    return f.name, lambda: crud.load_channel_yaml(f).slug, "after-dark"


READERS = {
    "update status (ns update --follow)": _update_status,
    "runtime.json dev flag": _runtime_dev,
    "dashboard.json notifications": _notifications_off,
    "answers.json (installer)": _answers_load_raw,
    "answers.json (setup window)": _answers_view,
    "runtime.json encoder (setup window)": _encoder,
    "install_state.json (console report)": _console_report,
    "channel yaml": _channel_yaml,
}


@pytest.mark.parametrize("name", sorted(READERS))
def test_a_reader_whose_first_opens_are_refused_still_gets_the_file(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filename, read, expected = READERS[name](tmp_path)
    assert read() == expected  # the file is there and the reader works
    with busy_opens(monkeypatch, filename) as left:
        assert read() == expected, f"{name}: a refused open was taken for 'no file'"
    assert left["n"] == 0, f"{name}: the reader never opened {filename}"


def test_the_nightly_scheduler_counts_a_sheet_that_is_briefly_busy(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A publish day read as 'no sheet' is a day that looks free, so two Shorts could land on it."""
    from datetime import UTC, date, datetime

    from ns_studio import daily, jobs
    from tests.helpers import add_output
    from tests.test_cadence import _channel, _env, _finished_job

    paths, db = _env(tmp_home)
    _channel(paths, db, 9)
    env = jobs.open_env(light=True)
    job = "after-dark-20261120-aaaa"
    man = _finished_job(paths, job, "Story A", 1, master=False)
    for item, e in man.items():
        add_output(db, job, item, path=e["mp4"], created_at="2026-11-20T12:00:00+00:00")
    now = datetime(2026, 11, 21, tzinfo=UTC)
    first = daily.scheduled_shorts(env, "after-dark", date(2020, 1, 1), set(), now)
    assert sum(first.values()) == 1
    with busy_opens(monkeypatch, Path(man["s1"]["json"]).name) as left:
        assert daily.scheduled_shorts(env, "after-dark", date(2020, 1, 1), set(), now) == first
    assert left["n"] == 0


def test_the_follower_never_reports_a_false_failure_while_the_status_is_swapped(tmp_path: Path) -> None:
    """`ns update --follow` polls the status file; a refused open used to read as state '' and, with the
    worker already gone, as 'ended without reporting a result'."""
    from ns_supervisor import update_job

    paths = _home(tmp_path)
    update_job.write_status(paths, "running", "Updating.", pid=os.getpid())

    def once() -> None:
        state = update_job.read_status(paths).get("state")
        assert state == "running", state

    with Hammer(once) as seen:
        for n in range(ROUNDS * 3):
            update_job.write_status(paths, "running", f"Step {n}.", pid=os.getpid())
    assert seen.reads > ROUNDS and seen.torn == [], seen.torn[:3]
