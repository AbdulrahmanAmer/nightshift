# ruff: noqa: E501
"""Re-review row 5: publish slots start on the channel's local date and count earlier nights' Shorts."""

import json
import random
from collections import Counter
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

from ns_studio import daily, jobs
from ns_studio.channels import cadence
from tests.helpers import add_output
from tests.test_cadence import _channel, _env, _finished_job


def sheet_days(manifest: dict[str, dict[str, str]]) -> dict[str, date]:
    out = {}
    for item, e in manifest.items():
        when = json.loads(Path(e["json"]).read_text(encoding="utf-8"))["publish_after"]
        out[item] = datetime.fromisoformat(when).date()
    return out


def test_slots_start_on_the_local_date_not_the_utc_date(tmp_home: Path):
    """UTC+3 at 02:30 on the 21st is still the 20th in UTC: masters got yesterday 18:xx (already past)."""
    paths, db = _env(tmp_home)
    _channel(paths, db, 9)
    env = jobs.open_env(light=True)
    job = "after-dark-20261121-abcd"
    manifest = _finished_job(paths, job, "The Long Night", 2, master=True)
    now = datetime(2026, 11, 20, 23, 30, tzinfo=UTC)  # 02:30 on the 21st in UTC+3
    daily.apply_publish_times(env, [job], random.Random(1), now, tz=timezone(timedelta(hours=3)))
    days = sheet_days(manifest)
    assert days["long"] == date(2026, 11, 21)  # before: 2026-11-20 18:xx, in the past
    assert days["s1"] == days["s2"] == date(2026, 11, 22)


def test_the_default_zone_is_the_pcs_own(tmp_home: Path, monkeypatch):
    paths, db = _env(tmp_home)
    _channel(paths, db, 9)
    env = jobs.open_env(light=True)
    job = "after-dark-20261121-abcd"
    manifest = _finished_job(paths, job, "The Long Night", 0, master=True)
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    assert daily.apply_publish_times(env, [job], random.Random(1), now) == 1
    assert sheet_days(manifest)["long"] == now.astimezone().date()


def test_later_nights_count_the_shorts_already_scheduled(tmp_home: Path):
    """Night 1 puts 2 Shorts on D+1 (cap 2); night 2 used to start from D+1 again: 4 Shorts on one day."""
    paths, db = _env(tmp_home)
    _channel(paths, db, 9, shorts_per_day_max=2)
    env = jobs.open_env(light=True)
    utc = timezone.utc
    night1, night2 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC), datetime(2026, 11, 21, 12, 0, tzinfo=UTC)

    a = "after-dark-20261120-aaaa"
    man_a = _finished_job(paths, a, "Story A", 3, master=True)
    for item, e in man_a.items():
        add_output(db, a, item, path=e["mp4"], created_at=night1.isoformat())
    daily.apply_publish_times(env, [a], random.Random(1), night1, tz=utc)
    assert Counter(d for i, d in sheet_days(man_a).items() if i != "long") == {
        date(2026, 11, 21): 2, date(2026, 11, 22): 1,
    }

    b = "after-dark-20261121-bbbb"
    man_b = _finished_job(paths, b, "Story B", 3, master=False)
    for item, e in man_b.items():
        add_output(db, b, item, path=e["mp4"], created_at=night2.isoformat())
    daily.apply_publish_times(env, [b], random.Random(2), night2, tz=utc)

    per_day = Counter([*(d for i, d in sheet_days(man_a).items() if i != "long"), *sheet_days(man_b).values()])
    assert max(per_day.values()) <= 2, per_day
    assert per_day[date(2026, 11, 21)] == 2 and per_day[date(2026, 11, 22)] == 2  # B fills D+2, then D+3
    assert sheet_days(man_b) == {"s1": date(2026, 11, 22), "s2": date(2026, 11, 23), "s3": date(2026, 11, 23)}


def test_rejected_and_uploaded_shorts_leave_their_slot_free(tmp_home: Path):
    paths, db = _env(tmp_home)
    _channel(paths, db, 9, shorts_per_day_max=2)
    env = jobs.open_env(light=True)
    a = "after-dark-20261120-aaaa"
    man_a = _finished_job(paths, a, "Story A", 2, master=False)
    for (item, e), status in zip(man_a.items(), ("rejected", "uploaded"), strict=True):
        add_output(db, a, item, path=e["mp4"], status=status, created_at="2026-11-20T12:00:00+00:00")
    today = date(2026, 11, 21)
    assert daily.scheduled_shorts(env, "after-dark", today, set(), datetime(2026, 11, 21, tzinfo=UTC)) == {}


def test_assign_publish_times_skips_full_days_and_spreads_new_shorts_between_old_ones():
    items = [cadence.ReleaseItem(f"j/s{i}", "story", "short") for i in range(1, 4)]
    d = date(2026, 11, 21)
    times = cadence.assign_publish_times(
        items, d, "18:00-21:00", 2, random.Random(3), taken={d: 2, d + timedelta(days=1): 1}
    )
    days = [times[f"j/s{i}"].date() for i in range(1, 4)]
    assert days == [d + timedelta(days=1), d + timedelta(days=2), d + timedelta(days=2)]
    # the one new Short on the half-full day takes the second of two slots (the old one holds the first)
    assert times["j/s1"].hour * 60 + times["j/s1"].minute > 19 * 60
    assert cadence.assign_publish_times(items, d, "18:00-21:00", 2, random.Random(3)) != times
