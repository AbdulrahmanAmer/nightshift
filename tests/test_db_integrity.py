# ruff: noqa: E501
"""Card db-integrity: a damaged studio.db is found at start, set aside, replaced by the newest good nightly copy,
and the owner is told plainly what was lost.

Every damaged file here is a REAL SQLite file broken the deterministic way: 4 KiB of 0xFF written at page 3 of a
closed database (its quick check then fails), a file cut to half, a file of text, zero bytes, a torn `-wal`, a
rollback journal left behind. No test answers "damaged" for the product. The kill and race tests run the opener
in real child processes. On the base (before this item) every opener was `Db(paths.db_path)`: `open_db` below
falls back to it, so each test fails there at an assertion.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import random
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ns_common import db as dbmod
from ns_common.db import Db
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.paths import Paths

REPO = Path(__file__).resolve().parents[1]
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows refuses to rename an open file; POSIX allows it")
#: The day the nightly copies were made (their mtimes): day d is BASE + d days.
BASE = datetime(2026, 11, 1, 3, 30, tzinfo=UTC).timestamp()
DAY = 86400.0

try:  # the base has no restore module: the tests that need its seams then fail at their assertion
    from ns_common import dbrestore
except ImportError:  # pragma: no cover - the base only
    dbrestore = None  # type: ignore[assignment]


# ------------------------------------------------------------------------------------------- helpers
@pytest.fixture(autouse=True)
def _few_megabytes(tmp_path: Path) -> Any:
    """No test of this file writes more than a few MB (the brief's bound; a backup is ~150 KB)."""
    yield
    sizes = [(p.stat().st_size, p) for p in tmp_path.rglob("*") if p.is_file()]
    total = sum(s for s, _ in sizes)
    assert total < 6 * 1024 * 1024, f"{total} bytes written; largest {max(sizes, key=lambda x: x[0])}"


def home() -> Paths:
    paths = Paths.from_env()
    paths.ensure()
    return paths


def open_db(paths: Paths) -> Db:
    """The product's one opener of studio.db; on the base, the opener every reader used (`Db(path)`)."""
    opener = getattr(dbmod, "open_studio_db", None)
    if opener is None:
        return Db(paths.db_path)
    from ns_studio.notifications import db_note

    return opener(paths, NsLogger("studio", paths.logs), note=db_note(paths))


def migrated(paths: Paths) -> Db | None:
    """`open_db` and `migrate`, as every reader does; None when the opener handed over a file SQLite refuses
    (the base's opener does, for a damaged one)."""
    db = open_db(paths)
    try:
        db.migrate()
    except sqlite3.DatabaseError:
        db.close()
        return None
    return db


def build_db(path: Path, marker: str) -> None:
    """A migrated studio.db with one `runs` row named `marker`, closed (its WAL folded in)."""
    db = Db(path)
    db.migrate()
    db.execute("insert into runs(run_id, started_at, mode) values (?,?,?)", (marker, "2026-11-01T00:00:00", "daily"))
    db.close()


def quick_ok(path: Path) -> bool:
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?immutable=1", uri=True)
        try:
            return conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def damage_page3(path: Path) -> None:
    """4 KiB of 0xFF over page 3 of a closed database (the card's deterministic damage)."""
    with path.open("r+b") as fh:
        head = fh.read(100)
        page = int.from_bytes(head[16:18], "big")
        page = 65536 if page == 1 else page
        fh.seek(2 * page)
        fh.write(b"\xff" * 4096)
    assert not quick_ok(path), "the damage must be one SQLite itself reports"


def cut_to_half(path: Path) -> None:
    os.truncate(path, path.stat().st_size // 2)


def as_text(path: Path) -> None:
    path.write_text("these are not the bytes of a database\n" * 40, encoding="utf-8")


def backups(paths: Paths, days: range | list[int], damaged: tuple[int, ...] = ()) -> None:
    """Nightly copies `studio-202611DD.db` of day d, marked `day-d`, made at BASE + d days."""
    paths.backup_dir.mkdir(parents=True, exist_ok=True)
    for d in days:
        f = paths.backup_dir / f"studio-202611{d:02d}.db"
        build_db(f, f"day-{d}")
        if d in damaged:
            damage_page3(f)
        os.utime(f, (BASE + d * DAY, BASE + d * DAY))


def runs_of(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("select run_id from runs order by run_id")]
    finally:
        conn.close()


def notes(paths: Paths) -> list[str]:
    from ns_supervisor import notify

    return [n["message"] for n in notify.read_notes(paths)]


def restored_record(paths: Paths) -> dict[str, Any]:
    f = paths.home / "run" / "db-restored.json"
    assert f.is_file(), "run/db-restored.json says what was used and what is missing"
    return json.loads(f.read_text(encoding="utf-8"))


def aside(paths: Paths) -> list[str]:
    folder = paths.data / "damaged"
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def finished_job(paths: Paths, job_id: str, made_at: float, *, items: tuple[str, ...] = ("s1",), channel: str = "after-dark",
                 sheet: str | None = None) -> list[str]:
    """A job folder as the outbox stage leaves it: the video, its sheet `.json` and `.txt` in the Outbox,
    `outbox/manifest.json` (mtime `made_at`) and `render/report.json`. Returns the output ids."""
    from ns_common.models import Metadata

    job = paths.job_dir(job_id)
    (job / "outbox").mkdir(parents=True, exist_ok=True)
    (job / "render").mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {}
    report: dict[str, Any] = {}
    for item in items:
        k = int(item[1:])
        folder = paths.outbox / channel / "stories" / job_id / "shorts"
        folder.mkdir(parents=True, exist_ok=True)
        mp4 = folder / f"short {k}.mp4"
        mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        meta = Metadata(
            channel=channel, job_id=job_id, kind="short", k=k, title=f"The {job_id[-4:]} story part {k}",
            description="Original fiction.", tags=["horror"], hashtags=["#shorts"],
            publish_after="2026-11-20T18:00", ai_disclosure="yes", made_for_kids=False,
            age_restrict_suggested=False, clips=[], combo_id=f"combo-{job_id[-4:]}", prompt_versions={},
        )
        json_path = mp4.with_suffix(".json")
        json_path.write_text(sheet if sheet is not None else meta.model_dump_json(indent=2), encoding="utf-8")
        mp4.with_suffix(".txt").write_text("TITLE: x\n", encoding="utf-8")
        manifest[item] = {"mp4": str(mp4), "json": str(json_path), "txt": str(mp4.with_suffix(".txt"))}
        report[item] = {"path": str(mp4), "duration_s": 40.0 + k}
    (job / "render" / "report.json").write_text(json.dumps(report), encoding="utf-8")
    man = job / "outbox" / "manifest.json"
    man.write_text(json.dumps(manifest), encoding="utf-8")
    os.utime(man, (made_at, made_at))
    return [f"{job_id}/{item}" for item in items]


def child(code: str, *args: str, env: dict[str, str] | None = None, stdin: Any = None) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", code, *args], cwd=REPO, env={**os.environ, **(env or {})}, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=stdin,
    )


#: A child that opens studio.db through the product's opener, with a stop or a pause at one step.
OPENER = r'''
import json, os, sys, time
from pathlib import Path
from ns_common.paths import Paths
from ns_common.logging import NsLogger
from ns_common import dbrestore
from ns_common.db import open_studio_db
from ns_studio.notifications import db_note

point, home = sys.argv[1], Path(sys.argv[2])
paths = Paths(home)
real_copy, real_replace, real_say = dbrestore._copy_backup, os.replace, dbrestore._say
if point == "before_copy":          # the card's kill: record written, damaged files moved, copy not made
    dbrestore._copy_backup = lambda src, dst: os._exit(9)
elif point == "after_rename":       # the copy is studio.db, the record still says set_aside
    def replace(src, dst):
        real_replace(src, dst)
        if Path(dst) == paths.db_path:
            os._exit(9)
    os.replace = replace
elif point == "before_tell":        # migrated and the videos put back; no note yet
    dbrestore._tell = lambda *a: os._exit(9)
elif point == "after_note":         # the note left, the record not yet removed
    def say(note, message, log):
        real_say(note, message, log)
        if message.startswith("NS-W213"):
            os._exit(9)
    dbrestore._say = say
elif point == "slow_copy":          # the race: hold the lock for a while inside the restore
    def slow(src, dst):
        Path(sys.argv[3]).write_text("copying", encoding="utf-8")
        time.sleep(6)
        real_copy(src, dst)
    dbrestore._copy_backup = slow
started = time.monotonic()
db = open_studio_db(paths, NsLogger("studio", paths.logs), note=db_note(paths))
db.migrate()
runs = [r["run_id"] for r in db.query("select run_id from runs")]
db.close()
print(json.dumps({"pid": os.getpid(), "runs": runs, "took": time.monotonic() - started}))
'''


def run_child(point: str, paths: Paths, *extra: str) -> tuple[int, str]:
    proc = child(OPENER, point, str(paths.home), *extra)
    out, _ = proc.communicate(timeout=120)
    return proc.returncode, out


# ------------------------------------------------------------------------------------------- card tests
def test_a_sound_database_is_opened_with_one_quick_check_per_process(tmp_home: Path) -> None:
    paths = home()
    build_db(paths.db_path, "live")
    db = open_db(paths)
    db.migrate()
    db.close()
    record = paths.home / "run" / "db-check.json"
    assert record.is_file(), "the check at start writes run/db-check.json"
    first = json.loads(record.read_text(encoding="utf-8"))
    assert first["result"] == "ok" and first["at"] and first["pid"] == os.getpid()

    db = open_db(paths)  # a second open in the same process: no second check
    db.migrate()
    db.close()
    assert json.loads(record.read_text(encoding="utf-8")) == first

    rc, out = run_child("none", paths)  # another process checks once for itself
    assert rc == 0, out
    again = json.loads(record.read_text(encoding="utf-8"))
    assert again["result"] == "ok" and again["pid"] != os.getpid() and again["at"] > first["at"]
    assert aside(paths) == [] and notes(paths) == [] and runs_of(paths.db_path) == ["live"]


def test_a_damaged_database_is_replaced_by_the_newest_good_backup(tmp_home: Path) -> None:
    paths = home()
    backups(paths, range(1, 6), damaged=(5,))
    not_ours = paths.backup_dir / "studio-20261106.db"  # newer, whole, but not a copy of studio.db
    conn = sqlite3.connect(not_ours)
    conn.execute("create table other(x)")
    conn.close()
    build_db(paths.db_path, "live")
    damage_page3(paths.db_path)
    before = sha(paths.db_path)

    db = open_db(paths)

    assert aside(paths), "the damaged file is set aside in data/damaged"
    db.migrate()
    assert [r["run_id"] for r in db.query("select run_id from runs")] == ["day-4"], "day 5 is damaged: day 4"
    db.close()
    kept = aside(paths)
    main = [n for n in kept if n.endswith(".db")]
    assert len(main) == 1 and main[0].startswith("studio-") and sha(paths.data / "damaged" / main[0]) == before
    assert all(n.startswith(main[0]) for n in kept), f"the -wal and -shm go with it: {kept}"
    assert quick_ok(paths.db_path)
    info = restored_record(paths)
    assert info["backup"] == str(paths.backup_dir / "studio-20261104.db")
    assert info["backup_made"] and info["why"] == "damaged" and info["missing"]
    assert sorted(info["refused"]) == ["studio-20261105.db", "studio-20261106.db"]
    said = notes(paths)
    assert len(said) == 1 and said[0].startswith("NS-W213: Nightshift's database was damaged")
    assert "replaced by the nightly copy made" in said[0] and "data\\damaged" in said[0]
    assert "No video was made since that copy" in said[0]
    assert not (paths.home / "run" / "db-restore.json").exists(), "the record of the restore is gone"
    assert not list(paths.data.glob("studio.db.restoring*"))


def test_videos_made_since_the_backup_are_put_back_in_the_queue(tmp_home: Path) -> None:
    paths = home()
    backups(paths, [1, 2])
    copy_made = BASE + 2 * DAY
    old = finished_job(paths, "after-dark-20261101-aaaa", copy_made - 3600)  # in the copy's time: not touched
    new = finished_job(paths, "after-dark-20261102-bbbb", copy_made + 3600)
    new += finished_job(paths, "after-dark-20261102-cccc", copy_made + 7200)
    finished_job(paths, "after-dark-20261102-dddd", copy_made + 7300, sheet="{ torn")  # its sheet cannot be read
    build_db(paths.db_path, "live")
    cut_to_half(paths.db_path)

    db = migrated(paths)
    assert db is not None, "the opener leaves a database SQLite can use"
    rows = {r["output_id"]: dict(r) for r in db.query("select * from outputs")}
    db.close()

    assert sorted(rows) == sorted(new), "the two videos made since the copy, not the older one"
    for oid in new:
        r = rows[oid]
        assert r["status"] == "pending" and r["kind"] == "short" and r["k"] == 1
        assert r["title"].startswith("The ") and Path(r["path"]).is_file() and r["duration_s"] == 41.0
        assert r["combo_id"] == f"combo-{oid.split('/')[0][-4:]}" and r["channel"] == "after-dark"
    assert old[0] not in rows
    info = restored_record(paths)
    assert sorted(info["put_back"]) == sorted(new) and info["unreadable"] == 1
    (said,) = notes(paths)
    assert "the 2 videos you made are back in the Queue as 'to review'" in said
    assert "if you had approved or posted any of them, do it again in the Queue" in said
    assert "1 more video could not be put back" in said and "posted" in said and "analytics" in said


@pytest.mark.parametrize("copies", ["every_copy_damaged", "no_copy"])
def test_with_no_good_backup_a_fresh_database_is_built_from_the_job_folders(tmp_home: Path, copies: str) -> None:
    paths = home()
    if copies == "every_copy_damaged":
        backups(paths, [1, 2], damaged=(1, 2))
    made = finished_job(paths, "after-dark-20261101-aaaa", BASE) + finished_job(paths, "after-dark-20261102-bbbb", BASE + DAY)
    build_db(paths.db_path, "live")
    as_text(paths.db_path)

    db = open_db(paths)
    assert aside(paths), "the damaged file is set aside, never deleted"
    db.migrate()
    assert [r["run_id"] for r in db.query("select run_id from runs")] == [], "a new, empty database"
    assert sorted(r["output_id"] for r in db.query("select output_id from outputs where status='pending'")) == sorted(made)
    db.close()
    assert quick_ok(paths.db_path)
    (said,) = notes(paths)
    assert said.startswith("NS-W213") and "so a new one was made" in said
    assert "history before now is gone" in said and "The videos are not lost" in said
    assert ("none of its 2 nightly copies passed the check" in said) == (copies == "every_copy_damaged")
    assert ("there was no nightly copy of it yet" in said) == (copies == "no_copy")


HOLDER = "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute('pragma user_version').fetchone(); print('open', flush=True); sys.stdin.read()"


@windows_only
def test_another_process_holding_the_database_stops_the_restore_before_any_change(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = home()
    backups(paths, [1, 2])
    build_db(paths.db_path, "live")
    damage_page3(paths.db_path)
    before = sha(paths.db_path)
    if dbrestore is not None:
        monkeypatch.setattr(dbrestore, "RENAME_WAIT_S", 0.5)
    holder = child(HOLDER, str(paths.db_path), stdin=subprocess.PIPE)
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "open"
        refused: NsError | None = None
        try:
            open_db(paths).migrate()
        except NsError as e:
            refused = e
        except sqlite3.DatabaseError:
            refused = None
        assert refused is not None and refused.code == "NS-E209", "the restore stops with NS-E209"
        assert sha(paths.db_path) == before and aside(paths) == [], "nothing moved or copied"
        assert not (paths.home / "run" / "db-restore.json").exists() and not list(paths.data.glob("studio.db.restoring*"))
        (said,) = notes(paths)
        assert said.startswith("NS-E209: Nightshift's database is damaged") and "another Nightshift window is using it" in said
        assert "Close every Nightshift window (Stop All), then open the Dashboard" in said
        assert "Stop All" in str(refused.data.get("hint"))

        from typer.testing import CliRunner

        from ns_studio.cli import app

        out = CliRunner().invoke(app, ["status"])  # every studio command says the same, once in the bell
        assert out.exit_code == 1 and "NS-E209" in out.output and "Stop All" in out.output
        assert len(notes(paths)) == 1, "the same words are not added again while they wait"
    finally:
        if holder.stdin is not None:
            holder.stdin.close()
        holder.wait(timeout=30)

    db = open_db(paths)  # the window is closed: tried again, the restore is made
    db.migrate()
    assert [r["run_id"] for r in db.query("select run_id from runs")] == ["day-2"]
    db.close()
    assert len([n for n in aside(paths) if n.endswith(".db")]) == 1


@pytest.mark.parametrize("point", ["before_copy", "after_rename", "before_tell", "after_note"])
def test_a_restore_killed_after_the_set_aside_is_finished_by_the_next_start(tmp_home: Path, point: str) -> None:
    paths = home()
    backups(paths, [1, 2, 3])
    made = finished_job(paths, "after-dark-20261103-bbbb", BASE + 3 * DAY + 60)
    build_db(paths.db_path, "live")
    damage_page3(paths.db_path)

    rc, out = run_child(point, paths)
    assert rc == 9, f"the child is stopped at {point}: {out}"
    assert (paths.home / "run" / "db-restore.json").is_file(), "the record says how far the restore came"
    if point == "before_copy":
        assert not paths.db_path.exists(), "set aside, and no copy yet"

    db = open_db(paths)  # the next start finishes it from the recorded copy; it never makes an empty database
    db.migrate()
    assert [r["run_id"] for r in db.query("select run_id from runs")] == ["day-3"]
    assert [r["output_id"] for r in db.query("select output_id from outputs")] == made, "put back once"
    db.close()
    assert len([n for n in aside(paths) if n.endswith(".db")]) == 1, "the damaged file once, never the copy"
    assert not (paths.home / "run" / "db-restore.json").exists()
    w213 = [n for n in notes(paths) if n.startswith("NS-W213")]
    assert len(w213) == 1 and "the 1 video you made is back in the Queue" in w213[0]
    assert restored_record(paths)["backup"].endswith("studio-20261103.db")


def test_two_processes_starting_together_restore_once(tmp_home: Path, tmp_path: Path) -> None:
    paths = home()
    backups(paths, [1, 2, 3, 4])
    build_db(paths.db_path, "live")
    damage_page3(paths.db_path)
    marker = tmp_path / "copying"

    first = child(OPENER, "slow_copy", str(paths.home), str(marker))  # the dashboard's start, say
    deadline = time.monotonic() + 90
    while not marker.exists():
        assert first.poll() is None, first.communicate()[0]
        assert time.monotonic() < deadline, "the first process never reached the copy"
        time.sleep(0.05)
    second = child(OPENER, "none", str(paths.home))  # the night, in the same moment
    out1, _ = first.communicate(timeout=120)
    out2, _ = second.communicate(timeout=120)
    assert first.returncode == 0 and second.returncode == 0, (out1, out2)
    a, b = json.loads(out1.strip().splitlines()[-1]), json.loads(out2.strip().splitlines()[-1])
    assert a["runs"] == b["runs"] == ["day-4"], "both work on the restored file"
    assert restored_record(paths)["by_pid"] == a["pid"], "the first restored; the second waited for it"
    logged = [json.loads(line) for f in paths.logs.glob("studio-*.jsonl") for line in f.read_text(encoding="utf-8").splitlines()]
    assert any(r["event"] == "db_restore_wait" for r in logged), "the second process waited on the restore lock"
    assert len([n for n in aside(paths) if n.endswith(".db")]) == 1, "set aside once"
    assert len([n for n in notes(paths) if n.startswith("NS-W213")]) == 1, "one note"


def test_a_backup_is_written_whole_and_checked_before_old_ones_go(tmp_path: Path) -> None:
    folder = tmp_path / "backup"
    live = tmp_path / "studio.db"
    build_db(live, "live")
    db = Db(live)
    for d in range(1, 16):
        db.backup(folder, today=date(2026, 10, d))
    db.close()
    before = {p.name: sha(p) for p in folder.iterdir()}
    assert len(before) == 14 and "studio-20261015.db" in before and "studio-20261001.db" not in before
    assert all(quick_ok(folder / n) for n in before)

    damage_page3(live)
    bad = Db(live)
    failures: list[str] = []
    for d in (15, 16):  # the same date as a good copy, and a new date
        try:
            bad.backup(folder, today=date(2026, 10, d))
        except Exception as e:  # noqa: BLE001 - the type is asserted below
            failures.append(type(e).__name__)
    bad.close()
    assert failures == ["BackupFailed", "BackupFailed"], "a copy that fails its check is refused"
    assert {p.name: sha(p) for p in folder.iterdir()} == before, "the 14 copies stay, that date's too, no .tmp"


def _two_due_channels(paths: Paths) -> None:
    from ns_studio.channels import crud
    from tests.helpers import make_channel

    db = Db(paths.db_path)
    db.migrate()
    created = (datetime.now(UTC) - timedelta(weeks=11)).isoformat(timespec="seconds")
    crud.add_channel(paths, db, make_channel(slug="after-dark", format="master_first", created_at=created))
    crud.add_channel(paths, db, make_channel(
        slug="night-desk", format="master_first", created_at=created,
        niche="quiet ghost stories told by a night receptionist", voice_notes="Slow, warm, close to the mic.",
    ))
    db.close()


def test_a_night_whose_database_breaks_mid_way_stops_with_one_note(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ns_studio import daily, jobs
    from tests import d31_driver as driver

    paths = home()
    _two_due_channels(paths)
    driver.install(monkeypatch)
    tried: list[str] = []

    def breaks(ctx: Any) -> list[str]:
        tried.append(ctx.job_id)
        raise sqlite3.DatabaseError("database disk image is malformed")

    env = jobs.open_env(light=True)
    stopped: BaseException | None = None
    try:
        daily.run_daily(env, rng=random.Random(1), run_fn=breaks)
    except Exception as e:  # noqa: BLE001 - asserted below
        stopped = e
    env.db.close()

    assert len(tried) == 1, "the other jobs are not tried: each would fail the same way"
    assert isinstance(stopped, NsError) and stopped.code == "NS-E209", "the night ends, with NS-E209"
    (report_file,) = paths.logs.glob("nightly-*.json")
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["failed"] == "NS-E209"
    assert [f["code"] for f in report["failures"]] == ["NS-E209"] and report["failures"][0]["job_id"] == tried[0]
    said = [n for n in notes(paths) if "NS-E209" in n]
    assert len(said) == 1 and "became damaged during tonight's run" in said[0]
    assert "Close every Nightshift window (Stop All), then open the Dashboard" in said[0]


def test_a_damaged_assistant_database_is_set_aside_and_started_fresh(tmp_home: Path) -> None:
    from ns_dashboard.assistant.store import AssistantStore

    paths = home()
    first = AssistantStore(paths)
    cid = first.new_conversation("hello")
    first.close()
    damage_page3(paths.assistant_db)
    getattr(dbmod, "_checked", set()).clear()  # the dashboard's next start: a new process checks again

    store = None
    try:
        store = AssistantStore(paths)
    except sqlite3.DatabaseError:
        store = None
    assert [n for n in aside(paths) if n.startswith("assistant-") and n.endswith(".db")], "set aside in data/damaged"
    assert store is not None and not store.has_conversation(cid), "a new, empty history"
    store.db.close()
    assert notes(paths) == ["The Assistant's conversation history was damaged and was set aside; it starts empty."]


def test_backups_made_by_the_previous_release_are_used(tmp_home: Path) -> None:
    paths = home()

    def rc3_backup(db: Db, folder: Path, today: date) -> Path:  # ns_common/db.py:Db.backup as 0.1.15-rc3 ships it
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"studio-{today:%Y%m%d}.db"
        dest = sqlite3.connect(target)
        try:
            db.connect().backup(dest)
        finally:
            dest.close()
        return target

    build_db(paths.db_path, "night-1")
    live = Db(paths.db_path)
    copy = rc3_backup(live, paths.backup_dir, date(2026, 11, 1))
    live.execute("insert into runs(run_id, started_at, mode) values ('night-2', '2026-11-02T00:00:00', 'daily')")
    live.close()
    made = finished_job(paths, "after-dark-20261102-bbbb", copy.stat().st_mtime + 60)
    damage_page3(paths.db_path)

    db = open_db(paths)
    db.migrate()
    assert [r["run_id"] for r in db.query("select run_id from runs")] == ["night-1"]
    assert [r["output_id"] for r in db.query("select output_id from outputs")] == made
    db.close()
    assert restored_record(paths)["backup"] == str(copy)


# ------------------------------------------------------------------------------------------- the states
@pytest.mark.parametrize("kind", ["page3", "half", "text", "zero_bytes", "missing"])
def test_every_kind_of_damage_is_found_and_replaced(tmp_home: Path, kind: str) -> None:
    """D2: SQLite's own answers (page 3 0xFF, half a file, text) and a file lost from a home with history (zero
    bytes or none at all, with nightly copies there) all end with the newest good copy and the note."""
    paths = home()
    backups(paths, [1, 2])
    build_db(paths.db_path, "live")
    {"page3": damage_page3, "half": cut_to_half, "text": as_text,
     "zero_bytes": lambda p: p.write_bytes(b""), "missing": lambda p: p.unlink()}[kind](paths.db_path)

    db = migrated(paths)
    assert db is not None, f"{kind}: the opener leaves a database SQLite can use"
    runs = [r["run_id"] for r in db.query("select run_id from runs")]
    db.close()
    assert runs == ["day-2"], f"{kind}: the newest good copy is in place"
    (said,) = notes(paths)
    assert said.startswith("NS-W213") and ("was missing or empty" in said) == (kind in ("zero_bytes", "missing"))
    assert bool(aside(paths)) == (kind != "missing")


def _newer_schema(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA writable_schema = ON")
    conn.execute("update sqlite_master set sql = 'CREATE TABLE runs(run_id TEXT) WITHOUT SOMETHING NEW' where name = 'runs'")
    conn.commit()
    conn.close()


def _torn_wal(path: Path) -> None:
    Path(str(path) + "-wal").write_bytes(b"\xff" * 4096)


def _journal_left(path: Path) -> None:
    Path(str(path) + "-journal").write_bytes(b"\xff" * 2048)


@pytest.mark.parametrize(("kind", "result"), [
    ("torn_wal", "ok"), ("journal_left", "ok"), ("newer_schema", "newer"), ("locked", "not_checked"),
    ("too_slow", "too_slow"), ("new_install", "new"),
])
def test_what_is_not_damage_is_opened_as_it_is(tmp_home: Path, monkeypatch: pytest.MonkeyPatch, kind: str, result: str) -> None:
    """D2: a torn -wal (SQLite drops the frames whose checksum fails), a journal left behind (rolled back by a
    writer, as every open does), a schema a newer program wrote, a file another program locks, a check that
    runs out of time and a new install are NOT damage: nothing is set aside, no note, the check says why."""
    paths = home()
    holder: sqlite3.Connection | None = None
    before = ""
    if kind != "new_install":
        backups(paths, [1])
        build_db(paths.db_path, "live")
        before = sha(paths.db_path)
    if kind == "torn_wal":
        _torn_wal(paths.db_path)
    elif kind == "journal_left":
        _journal_left(paths.db_path)
    elif kind == "newer_schema":
        _newer_schema(paths.db_path)
        before = sha(paths.db_path)
    elif kind == "locked":
        if dbrestore is not None:
            monkeypatch.setattr(dbrestore, "CHECK_BUSY_MS", 200)
        holder = sqlite3.connect(paths.db_path, isolation_level=None)
        holder.execute("PRAGMA locking_mode = EXCLUSIVE")
        holder.execute("begin exclusive")
    elif kind == "too_slow" and dbrestore is not None:  # a disk that crawls: the check never ends by itself
        real_run = dbrestore._run

        def crawl(conn: sqlite3.Connection, sql: str) -> list[Any]:
            while True:
                real_run(conn, sql)

        monkeypatch.setattr(dbrestore, "_run", crawl)
        monkeypatch.setattr(dbrestore, "CHECK_LIMIT_S", 0.2)
    try:
        open_db(paths).close()
    finally:
        if holder is not None:
            holder.execute("rollback")
            holder.close()
    record = paths.home / "run" / "db-check.json"
    assert record.is_file() and json.loads(record.read_text(encoding="utf-8"))["result"] == result
    assert aside(paths) == [] and notes(paths) == [] and not (paths.home / "run" / "db-restore.json").exists()
    if kind in ("newer_schema", "locked", "too_slow"):
        assert sha(paths.db_path) == before, "the file is left exactly as it was"
    if kind in ("locked", "newer_schema", "too_slow"):
        logged = [json.loads(line) for f in paths.logs.glob("studio-*.jsonl") for line in f.read_text(encoding="utf-8").splitlines()]
        assert [r["level"] for r in logged if r["event"] == "db_not_checked"] == ["INFO"], "one INFO"


def test_a_full_disk_stops_the_copy_and_the_next_start_finishes_it(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D6: no room for the copy: NS-E209 naming the full disk, the record stays (studio.db is set aside and no
    empty database is made), the next start (room again) finishes."""
    paths = home()
    backups(paths, [1, 2])
    build_db(paths.db_path, "live")
    damage_page3(paths.db_path)
    if dbrestore is not None:
        def full(src: Path, dst: Path) -> None:
            raise OSError(errno.ENOSPC, "No space left on device", str(dst))

        monkeypatch.setattr(dbrestore, "_copy_backup", full)
    refused: NsError | None = None
    try:
        open_db(paths).migrate()
    except NsError as e:
        refused = e
    except sqlite3.DatabaseError:
        refused = None
    assert refused is not None and refused.code == "NS-E209" and "disk is full" in str(refused)
    assert "Free space" in str(refused.data.get("hint"))
    assert (paths.home / "run" / "db-restore.json").is_file() and not paths.db_path.exists()
    assert aside(paths) and not list(paths.data.glob("studio.db.restoring*"))
    (said,) = notes(paths)
    assert said.startswith("NS-E209") and "the disk is full" in said

    monkeypatch.undo()
    db = open_db(paths)
    db.migrate()
    assert [r["run_id"] for r in db.query("select run_id from runs")] == ["day-2"]
    db.close()
    assert [n for n in notes(paths) if n.startswith("NS-W213")]


# ------------------------------------------------------------------------------------------- the dashboard
def _dashboard(paths: Paths) -> Any:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ns_dashboard.handlers import build_state
    from ns_dashboard.server import create_app
    from ns_dashboard.supervisor_link import SupervisorLink

    try:
        state = build_state(paths, fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2))
    except sqlite3.DatabaseError as e:  # the base: the dashboard's start meets the damaged file as it is
        raise AssertionError(f"the dashboard did not start: {e}") from e
    app = create_app(state=state, start_worker=False, static_dir=paths.home / "static", heartbeat_s=0.01)
    return TestClient(app, base_url="http://127.0.0.1:8100", raise_server_exceptions=False), state


def test_the_dashboard_start_replaces_a_damaged_database(tmp_home: Path) -> None:
    paths = home()
    backups(paths, [1, 2])
    made = finished_job(paths, "after-dark-20261102-bbbb", BASE + 2 * DAY + 60)
    build_db(paths.db_path, "live")
    damage_page3(paths.db_path)

    client, state = _dashboard(paths)
    try:
        assert aside(paths), "the dashboard's start set the damaged file aside"
        assert client.get("/api/status").status_code == 200
        outputs = client.get("/api/outputs").json()
        ids = [o.get("output_id") or o.get("id") for o in (outputs.get("items", outputs) if isinstance(outputs, dict) else outputs)]
        assert made[0] in ids, "the video made since the copy is in the Queue"
        assert [n for n in notes(paths) if n.startswith("NS-W213")]
    finally:
        state.db.close()


@windows_only
def test_the_dashboard_answers_503_while_the_database_cannot_be_used(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = home()
    backups(paths, [1])
    build_db(paths.db_path, "live")
    damage_page3(paths.db_path)
    if dbrestore is not None:
        monkeypatch.setattr(dbrestore, "RENAME_WAIT_S", 0.3)
    holder = child(HOLDER, str(paths.db_path), stdin=subprocess.PIPE)
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "open"
        client, state = _dashboard(paths)  # it starts, and says why it cannot work
        answer = client.get("/api/outputs")
        assert answer.status_code == 503
        body = answer.json()["error"]
        assert body["code"] == "NS-E209" and "Stop All" in body["hint"]
        assert client.get("/api/health").status_code == 200
        assert [n for n in notes(paths) if n.startswith("NS-E209")]
    finally:
        if holder.stdin is not None:
            holder.stdin.close()
        holder.wait(timeout=30)


def test_the_dashboard_answers_503_when_the_database_breaks_while_it_runs(tmp_home: Path) -> None:
    """4b.2 in the dashboard: SQLite's "malformed" under a request is 503 with the way out, not an uncoded 500."""
    paths = home()
    build_db(paths.db_path, "live")
    client, state = _dashboard(paths)
    try:
        def broken(*_a: Any, **_k: Any) -> Any:
            raise sqlite3.DatabaseError("database disk image is malformed")

        state.db.db.query = broken  # type: ignore[method-assign]
        answer = client.get("/api/outputs")
        assert answer.status_code == 503
        body = answer.json()["error"]
        assert body["code"] == "NS-E209" and "Close every Nightshift window (Stop All)" in body["message"]
    finally:
        state.db.close()


# ------------------------------------------------------------------------------------------- the guard
def test_every_product_opener_goes_through_the_check() -> None:
    """No `Db(<...>.db_path)` or `Db(<...>.assistant_db)` in product code outside ns_common/db.py and
    ns_common/dbrestore.py: every route into the database is checked first (card section 5)."""
    import re

    pattern = re.compile(r"\bDb\(\s*[\w.]*(?:db_path|assistant_db)\s*\)")
    found = []
    for top in ("ns_common", "ns_studio", "ns_dashboard", "ns_supervisor", "ns_tts", "installer"):
        for f in (REPO / top).rglob("*.py"):
            rel = f.relative_to(REPO).as_posix()
            if rel in ("ns_common/db.py", "ns_common/dbrestore.py"):
                continue
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    found.append(f"{rel}:{n}: {line.strip()}")
    assert found == []
    assert hasattr(dbmod, "open_studio_db") and hasattr(dbmod, "open_assistant_db")
