from datetime import UTC, datetime
from pathlib import Path

import pytest

from ns_common.db import Db
from ns_common.paths import Paths
from ns_studio.errors import StudioError
from ns_studio.learn import importer, reweight, status
from ns_studio.variation.draw import FLOOR, weights_for
from tests.helpers import add_analytics, add_output, make_channel, make_combo

NOW = datetime(2026, 12, 20, 12, 0, tzinfo=UTC)
HEADER = (
    "Content,Video title,Video publish time,Views,Viewed vs. swiped away (%),"
    "Average percentage viewed (%),Average view duration,Likes,Comments,Shares,Subscribers\n"
)


def _db(tmp_home: Path) -> tuple[Paths, Db]:
    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    return paths, db


def _csv(tmp_path: Path, body: str, header: str = HEADER) -> Path:
    f = tmp_path / "export.csv"
    f.write_text("﻿" + header + body, encoding="utf-8")
    return f


# ---------------------------------------------------------------- parsing
def test_duration_and_video_id_parsing():
    assert importer.parse_duration("0:32") == 32
    assert importer.parse_duration("1:02:03") == 3723
    assert importer.parse_duration("45") == 45
    assert importer.parse_duration("") is None and importer.parse_duration("a:b") is None
    assert importer.video_id_of("https://youtu.be/abcdefghijk?t=3") == "abcdefghijk"
    assert importer.video_id_of("https://www.youtube.com/shorts/ABCDEFGHIJK") == "ABCDEFGHIJK"
    assert importer.video_id_of("https://www.youtube.com/watch?v=abc_def-ghi") == "abc_def-ghi"
    assert importer.video_id_of("abcdefghijk") == "abcdefghijk"
    assert importer.video_id_of("not an id") == ""


def test_read_csv_maps_columns_and_skips_total(tmp_path: Path):
    f = _csv(
        tmp_path,
        'Total,,,"10,000",,,,,,,\n'
        "abcdefghijk,The Porch Light,2026-10-01,\"1,200\",72.5%,81,0:32,10,2,1,5\n",
    )
    rows = importer.read_csv(f)
    assert len(rows) == 1
    r = rows[0]
    assert (r.video_id, r.title, r.views) == ("abcdefghijk", "The Porch Light", 1200)
    assert (r.viewed_pct, r.avg_pct_viewed, r.avg_view_duration_s) == (72.5, 81.0, 32.0)
    assert (r.likes, r.comments, r.shares, r.subs_gained) == (10, 2, 1, 5)


def test_read_csv_alternate_headers_and_overrides(tmp_path: Path):
    header = "Video title,Views,Stayed to watch (%),Average percentage viewed (%)\n"
    f = _csv(tmp_path, "A,5,60,50\n", header)
    assert importer.read_csv(f)[0].viewed_pct == 60.0
    header2 = "Title of video,Views,Retention,Average percentage viewed (%)\n"
    f2 = _csv(tmp_path, "A,5,61,50\n", header2)
    with pytest.raises(StudioError):
        importer.read_csv(f2)  # title column unknown
    f3 = _csv(tmp_path, "A,5,61,50\n", "Video title,Views,Retention,Average percentage viewed (%)\n")
    assert importer.read_csv(f3, {"viewed_pct": "Retention"})[0].viewed_pct == 61.0
    with pytest.raises(StudioError):
        importer.read_csv(f3, {"viewed_pct": "Nope"})


def test_read_csv_errors(tmp_path: Path):
    with pytest.raises(StudioError) as ei:
        importer.read_csv(tmp_path / "missing.csv")
    assert ei.value.code == "NS-E323"
    with pytest.raises(StudioError):
        importer.read_csv(_csv(tmp_path, "1,2\n", "Foo,Bar\n"))
    with pytest.raises(StudioError):
        importer.read_csv(_csv(tmp_path, "t\n", "Video title\n"))


# ---------------------------------------------------------------- matching
def test_match_by_url_then_exact_title(tmp_home: Path, tmp_path: Path):
    _, db = _db(tmp_home)
    a = add_output(db, "j1", "s1", title="By URL")
    b = add_output(db, "j2", "s1", title="By title")
    add_output(db, "j3", "s1", title="Duplicate")
    add_output(db, "j4", "s1", title="Duplicate")
    db.execute(
        "insert into uploads(output_id, video_url, published_at) values (?,?,?)",
        (a, "https://youtube.com/shorts/abcdefghijk", "2026-10-02T00:00:00+00:00"),
    )
    rows = importer.read_csv(
        _csv(
            tmp_path,
            "abcdefghijk,Renamed on YouTube,x,100,70,80,0:20,0,0,0,1\n"
            ",By title,x,200,60,70,0:20,0,0,0,0\n"
            ",Duplicate,x,300,60,70,0:20,0,0,0,0\n"
            ",Unknown video,x,400,60,70,0:20,0,0,0,0\n",
        )
    )
    res = importer.import_rows(db, rows, "2026-12-20")
    assert set(res.matched) == {a, b}
    assert {r.title for r in res.unmatched} == {"Duplicate", "Unknown video"}
    row = db.one("select views, viewed_pct, avg_view_duration_s from analytics where output_id=?", (a,))
    assert row is not None and (row["views"], row["viewed_pct"], row["avg_view_duration_s"]) == (100, 70, 20)
    importer.import_rows(db, rows, "2026-12-20")  # same date again replaces, never duplicates
    assert len(db.query("select * from analytics where output_id=?", (a,))) == 1


# ---------------------------------------------------------------- reweighting
def test_output_score_formulas():
    assert reweight.output_score("short", 40, 80.0, 50.0, None) == pytest.approx(0.4)
    assert reweight.output_score("long", 600, None, None, 300.0) == pytest.approx(0.5)
    assert reweight.output_score("long", 0, None, None, 300.0) is None
    assert reweight.output_score("short", 40, None, 50.0, None) is None


def _combo(i: int, hook: str, structure: str = "twist"):
    return make_combo(combo_id=f"c{i}", hook=hook, structure=structure)


def test_reweight_moves_towards_winners_and_keeps_floor(tmp_home: Path):
    _, db = _db(tmp_home)
    ch = make_channel()
    for i in range(1, 7):
        good = i <= 3
        oid = add_output(db, f"j{i}", "s1", combo=_combo(i, "cold_open" if good else "question"))
        pct = 90.0 if good else 5.0
        add_analytics(db, oid, "2026-12-19", viewed_pct=pct, avg_pct_viewed=pct)
    first = reweight.reweight(db, ch, now=NOW)
    w1 = weights_for(db, ch.slug, "hooks", ch.pools.hooks)
    assert w1["cold_open"] > w1["question"] and sum(w1.values()) == pytest.approx(1.0)
    assert first["hooks"]["cold_open"][1] > first["hooks"]["cold_open"][0]
    for _ in range(40):
        reweight.reweight(db, ch, now=NOW)
    w = weights_for(db, ch.slug, "hooks", ch.pools.hooks)
    assert min(w.values()) >= FLOOR - 1e-9
    assert w["question"] == pytest.approx(FLOOR, abs=1e-6)
    assert w["cold_open"] > 0.4 and w["cold_open"] > 1.5 * w["confession"]


def test_reweight_uses_runtime_for_masters_and_ignores_missing(tmp_home: Path):
    _, db = _db(tmp_home)
    ch = make_channel()
    oid = add_output(db, "m1", "long", combo=_combo(1, "warning"), duration_s=600)
    add_analytics(db, oid, "2026-12-19", viewed_pct=None, avg_pct_viewed=None, avg_view_duration_s=420.0)
    scored = reweight.scored_outputs(db, ch.slug)
    assert scored[0][1] == pytest.approx(0.7)
    nodata = add_output(db, "j9", "s1", combo=_combo(9, "question"))
    add_analytics(db, nodata, "2026-12-19", viewed_pct=None, avg_pct_viewed=None)
    assert len(reweight.scored_outputs(db, ch.slug)) == 1
    assert reweight.reweight(make_db_without_data(tmp_home), ch, now=NOW) == {}


def make_db_without_data(tmp_home: Path) -> Db:
    other = tmp_home / "other.db"
    db = Db(other)
    db.migrate()
    return db


def test_snapshot_roundtrip(tmp_home: Path):
    paths, _ = _db(tmp_home)
    reweight.save_snapshot(paths, "after-dark", {"hooks": {"cold_open": (0.25, 0.4)}}, NOW)
    snap = reweight.load_snapshot(paths, "after-dark")
    assert snap["changes"]["hooks"]["cold_open"] == [0.25, 0.4]
    assert reweight.load_snapshot(paths, "nobody") == {}


# ---------------------------------------------------------------- status
def test_channel_status_lists_performers_rejections_incidents(tmp_home: Path):
    paths, db = _db(tmp_home)
    ch = make_channel()
    ids = []
    for i in range(1, 8):
        oid = add_output(db, f"j{i}", "s1", title=f"Short {i}", combo=_combo(i, "cold_open"))
        add_analytics(db, oid, "2026-12-19", viewed_pct=10.0 * i, avg_pct_viewed=100.0)
        ids.append(oid)
    add_output(db, "jr", "s1", title="Bad one", status="rejected", note="too gory")
    db.execute("insert into incidents(channel, ts, kind, note) values (?,?,?,?)",
               ("after-dark", "2026-12-01T00:00:00", "pause", "strike notice"))
    st = status.channel_status(db, paths, ch)
    assert st.counts["rejected"] == 1 and st.counts["pending"] == 7
    assert st.top[0].title == "Short 7" and st.bottom[0].title == "Short 1"
    assert any("too gory" in r for r in st.reject_reasons)
    text = "\n".join(status.format_status(st, "steady: 1 master(s)/week"))
    assert "Short 7" in text and "strike notice" in text and "cadence: steady" in text


# ---------------------------------------------------------------- milestone
def _upload(db: Db, oid: str, when: str) -> None:
    db.execute("insert into uploads(output_id, video_url, published_at) values (?,?,?)",
               (oid, f"https://youtu.be/{oid[:11].ljust(11, 'x')}", when))


def test_milestone_no_data_and_too_early(tmp_home: Path):
    _, db = _db(tmp_home)
    assert status.milestone(db, "after-dark", NOW).verdict == "no-data"
    oid = add_output(db, "j1", "s1")
    _upload(db, oid, "2026-12-10T00:00:00+00:00")
    add_analytics(db, oid, "2026-12-19")
    m = status.milestone(db, "after-dark", NOW)
    assert m.verdict == "too-early"
    assert m.weeks_since_first_upload is not None and m.weeks_since_first_upload < 8


def test_milestone_go_on_shorts_path(tmp_home: Path):
    _, db = _db(tmp_home)
    oid = add_output(db, "j1", "s1")
    _upload(db, oid, "2026-10-01T00:00:00+00:00")
    add_analytics(db, oid, "2026-12-12", views=100, viewed_pct=60.0, subs_gained=2)
    add_analytics(db, oid, "2026-12-19", views=100, viewed_pct=60.0, subs_gained=10)
    m = status.milestone(db, "after-dark", NOW)
    assert m.shorts_viewed_rate_7d == pytest.approx(60.0)
    assert (m.subs_last_7d, m.subs_prev_7d) == (10, 2)
    assert m.verdict == "go" and m.shorts_path_ok and not m.masters_path_ok
    assert "GO" in "\n".join(status.format_milestone(m))


def test_milestone_go_on_masters_path(tmp_home: Path):
    _, db = _db(tmp_home)
    oid = add_output(db, "m1", "long", duration_s=720)
    _upload(db, oid, "2026-10-01T00:00:00+00:00")
    add_analytics(db, oid, "2026-12-19", views=150000, viewed_pct=None, avg_pct_viewed=None,
                  avg_view_duration_s=200.0)
    m = status.milestone(db, "after-dark", NOW)
    assert m.masters_avg_view_s == 200.0
    assert m.projected_12mo_hours is not None and m.projected_12mo_hours >= status.WATCH_HOURS_TARGET
    assert m.verdict == "go" and m.masters_path_ok


def test_milestone_no_go(tmp_home: Path):
    _, db = _db(tmp_home)
    oid = add_output(db, "j1", "s1")
    _upload(db, oid, "2026-10-01T00:00:00+00:00")
    add_analytics(db, oid, "2026-12-12", views=100, viewed_pct=40.0, subs_gained=5)
    add_analytics(db, oid, "2026-12-19", views=100, viewed_pct=40.0, subs_gained=1)
    m = status.milestone(db, "after-dark", NOW)
    assert m.verdict == "no-go" and not m.shorts_path_ok
