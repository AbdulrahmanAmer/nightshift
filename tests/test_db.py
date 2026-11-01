import re
from datetime import date
from pathlib import Path

from ns_common.db import Db, new_job_id


def test_migrate_creates_tables(tmp_path: Path):
    with Db(tmp_path / "studio.db") as db:
        db.migrate()
        names = {r["name"] for r in db.query("select name from sqlite_master where type='table'")}
    assert {"channels", "ideas", "stories", "parts", "clips", "clip_usage", "combos", "outputs", "runs",
            "uploads", "analytics", "pool_weights", "calibration", "incidents", "sources",
            "dashboard_jobs"} <= names


def test_migrate_is_idempotent(tmp_path: Path):
    db = Db(tmp_path / "s.db")
    db.migrate()
    db.migrate()
    db.close()


def test_insert_and_query(tmp_path: Path):
    with Db(tmp_path / "s.db") as db:
        db.migrate()
        db.execute(
            "insert into channels(slug, display_name, created_at) values (?,?,?)", ("a", "A", "2026-11-01")
        )
        row = db.one("select display_name from channels where slug=?", ("a",))
        assert row is not None
        assert row["display_name"] == "A"


def test_backup_rotates(tmp_path: Path):
    db = Db(tmp_path / "s.db")
    db.migrate()
    bdir = tmp_path / "backup"
    for d in range(1, 17):
        db.backup(bdir, keep=14, today=date(2026, 11, d))
    files = sorted(p.name for p in bdir.glob("studio-*.db"))
    assert len(files) == 14 and files[0] == "studio-20261103.db"
    db.close()


def test_new_job_id_format():
    jid = new_job_id("after-dark")
    assert re.fullmatch(r"after-dark-\d{8}-[0-9a-f]{4}", jid)
