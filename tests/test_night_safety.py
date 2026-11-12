"""Block G, WP4 (night safety): the day's database copy, the doctor's database row, the Queue's own line for a
set-aside idea, and the model pre-check on a home made by an older release (rc5). NOT RUN by the builder (the
laptop runs no pytest in this block): the reviewer's run is the first."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from ns_common import dbrestore
from ns_common.db import Db
from ns_common.paths import Paths


def _home(tmp_path: Path) -> Paths:
    paths = Paths(tmp_path / "ns")
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    db.close()
    return paths


def _copies(paths: Paths) -> list[str]:
    return sorted(p.name for p in paths.backup_dir.glob("studio-*.db"))


def test_the_days_copy_is_made_once_and_checked(tmp_path: Path) -> None:
    paths = _home(tmp_path)
    made = dbrestore.daily_backup(paths)
    assert made is not None and made.name == _copies(paths)[0]
    assert dbrestore.check(made, full=True, fixed=True).verdict == dbrestore.OK
    assert dbrestore.daily_backup(paths) is None, "a second start the same day makes no second copy"
    assert len(_copies(paths)) == 1


def test_no_database_yet_makes_no_copy_and_no_error(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "ns")
    paths.ensure()
    assert dbrestore.daily_backup(paths) is None and _copies(paths) == []


def test_a_copy_that_cannot_be_made_never_raises_and_the_doctor_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _home(tmp_path)

    def boom(self: Db, backup_dir: Path, keep: int = 14, today: object = None) -> Path:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Db, "backup", boom)
    assert dbrestore.daily_backup(paths) is None  # no exception: the work goes on
    state = json.loads(dbrestore.daily_state_path(paths).read_text(encoding="utf-8"))
    assert "No space left" in state["error"]
    ok, words = dbrestore.backup_facts(paths)
    assert "today's copy failed" in words
    assert not (paths.home / "run" / dbrestore.DAILY_LOCK_FILE).exists(), "the lock is released on failure"
    monkeypatch.undo()
    assert dbrestore.daily_backup(paths) is not None, "the next start tries again"
    assert not dbrestore.daily_state_path(paths).exists(), "a copy made ends the failure record"


def test_a_stale_lock_of_a_killed_copy_is_taken_over_a_fresh_one_is_respected(tmp_path: Path) -> None:
    paths = _home(tmp_path)
    lock = paths.home / "run" / dbrestore.DAILY_LOCK_FILE
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("", encoding="utf-8")
    assert dbrestore.daily_backup(paths) is None and _copies(paths) == [], "another process is copying"
    old = time.time() - dbrestore.DAILY_LOCK_STALE_S - 5
    os.utime(lock, (old, old))
    assert dbrestore.daily_backup(paths) is not None


def test_the_doctor_row_fails_with_no_copy_after_the_first_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ns_supervisor import doctor

    paths = _home(tmp_path)
    born = datetime.now(UTC).timestamp()
    monkeypatch.setattr(dbrestore, "db_born", lambda p: born)
    ok, said, = doctor.database_row(paths)[1:]
    assert ok and "no backup yet" in said and "KiB" in said, said  # the first day: not a failure
    later = datetime.now(UTC) + timedelta(hours=30)
    name, ok, said = doctor.database_row(paths, later)
    assert name == "database" and not ok and "NO BACKUP" in said and "ns backup" in said, said
    dbrestore.daily_backup(paths)
    ok, said = doctor.database_row(paths, later)[1:]
    assert ok and f"newest backup {datetime.now().strftime('%Y-%m-%d')}" in said, said


def test_ns_backup_command_names_a_route_that_clears_the_fail() -> None:
    import inspect

    from ns_supervisor import cli

    assert "daily_backup" in inspect.getsource(cli.cmd_backup)


def test_the_idea_note_reaches_the_report_entry(tmp_path: Path) -> None:
    from ns_studio import daily

    class Ctx:
        def read_json(self, name: str) -> object:
            if name != "idea_note.json":
                raise FileNotFoundError(name)
            return {"message": "Your idea \"X\" was set aside by the safety check.", "set_aside": []}

    assert daily._idea_note(Ctx()).startswith("Your idea \"X\" was set aside")  # type: ignore[arg-type]
    assert daily._idea_note(SimpleNamespace(read_json=lambda n: (_ for _ in ()).throw(OSError()))) == ""  # type: ignore[arg-type]


def test_an_rc5_home_with_a_verified_file_of_another_listed_size_is_not_stopped(tmp_path: Path) -> None:
    """rc5's setup hashed the file (its marker matches); this version's manifest lists another size. The night
    is not stopped for a 'damaged' file; the note names the file and Repair. A truncated file still stops."""
    from ns_studio import notifications
    from ns_supervisor import modelcheck
    from tests.test_model_files import SIZE, make_install

    inst = make_install(tmp_path)
    manifest = inst.app / "manifest.json"
    man = json.loads(manifest.read_text(encoding="utf-8"))
    llm = man["profiles"]["gpu"]["llm"]
    llm["bytes"] = SIZE + 1
    manifest.write_text(json.dumps(man), encoding="utf-8")
    target = next(Path(i.dest) for i in inst.items if Path(i.dest).name == Path(llm["file"]).name)
    intact: list[str] = []
    assert modelcheck.precheck(inst.paths, inst.app, intact=intact) == [], "an intact file stopped the night"
    assert len(intact) == 1 and str(target) in intact[0] and "choose Repair" in intact[0], intact
    note = notifications.model_intact_note({"said": " ".join(intact)}) or ""
    assert target.name in note and "Repair" in note
    assert notifications.model_intact_note(None) is None
    # a truncated file: its marker no longer matches, so it is damage and the night stops
    target.write_bytes(target.read_bytes()[:-1])
    stops = modelcheck.precheck(inst.paths, inst.app, intact=[])
    assert len(stops) == 1 and target.name in stops[0], stops


@pytest.mark.parametrize(("markers", "stopped"), [("old", False), ("none", True)])
def test_an_rc5_marker_without_hashed_at_counts_as_setup_verified(
    tmp_path: Path, markers: str, stopped: bool
) -> None:
    """rc5's setup wrote markers of {size, mtime_ns, sha256} and no `hashed_at` (v0.1.15-rc5's
    `downloader.write_marker`): the owner's real home has only such markers. A file of another listed size
    with one is intact; the same file with no marker at all is still a stop (the control)."""
    from ns_supervisor import modelcheck
    from tests.test_model_files import SIZE, make_install

    inst = make_install(tmp_path, markers=markers)
    manifest = inst.app / "manifest.json"
    man = json.loads(manifest.read_text(encoding="utf-8"))
    llm = man["profiles"]["gpu"]["llm"]
    llm["bytes"] = SIZE + 1
    manifest.write_text(json.dumps(man), encoding="utf-8")
    target = next(Path(i.dest) for i in inst.items if Path(i.dest).name == Path(llm["file"]).name)
    if markers == "old":
        marker = json.loads(target.with_name(target.name + ".ok").read_text(encoding="utf-8"))
        assert "hashed_at" not in marker and marker["sha256"], marker  # the shape rc5 wrote
    intact: list[str] = []
    stops = modelcheck.precheck(inst.paths, inst.app, intact=intact)
    if stopped:
        assert any(target.name in s for s in stops) and not intact, (stops, intact)
    else:
        assert stops == [], "a file rc5's setup verified stopped the night"
        assert len(intact) == 1 and str(target) in intact[0], intact
