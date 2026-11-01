import sqlite3
from pathlib import Path

from ns_common.db import Db


def _columns(db: Db, table: str) -> set[str]:
    return {r["name"] for r in db.query(f"pragma table_info({table})")}


def test_new_columns_and_assistant_table(tmp_path: Path):
    with Db(tmp_path / "s.db") as db:
        db.migrate()
        assert {"rights_confirmed", "title"} <= _columns(db, "sources")
        assert {"status", "title", "source"} <= _columns(db, "ideas")
        assert {"id", "ts", "role", "content", "tool_calls_json"} == _columns(db, "assistant_messages")
        db.execute("insert into sources(source_id, path, imported_at) values ('s','p','2026-11-01')")
        row = db.one("select rights_confirmed, title from sources where source_id='s'")
        assert row is not None and row["rights_confirmed"] == 0 and row["title"] is None
        db.execute("insert into channels(slug, display_name, created_at) values ('a','A','2026-11-01')")
        db.execute("insert into ideas(channel, premise, twist, created_at) values ('a','p','t','2026-11-01')")
        idea = db.one("select status, source from ideas")
        assert idea is not None and idea["status"] == "new" and idea["source"] is None


def test_migrate_upgrades_a_database_made_by_the_old_schema(tmp_path: Path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "create table sources(source_id text primary key, channel text, path text not null,"
        " duration_s real, sha256 text, imported_at text not null, template text,"
        " status text not null default 'imported');"
        "create table ideas(idea_id integer primary key autoincrement, channel text not null,"
        " job_id text, premise text not null, twist text not null, embedding blob,"
        " created_at text not null, used integer not null default 0);"
    )
    conn.close()
    with Db(path) as db:
        db.migrate()
        db.migrate()
        assert {"rights_confirmed", "title"} <= _columns(db, "sources")
        assert {"status", "title", "source"} <= _columns(db, "ideas")
