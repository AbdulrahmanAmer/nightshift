"""A new job, upload, linked video or night never gets an id that already names another one (card D-ids).

An id is `<name>-<yyyymmdd>-<4 hex>`: 16 random bits per name and day. Before this change nothing looked
whether the id was taken, and the makers created the job folder with `exist_ok=True` and wrote `job.json`
over whatever was there, so a repeated draw merged two jobs in one folder. Every test here makes the draw
repeat on purpose and enters where the product enters.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import pytest

from ns_common import db as dbmod
from ns_common.db import JOB_ID_TRIES, claim_dir, claim_job_id
from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_dashboard import fakes
from ns_dashboard.uploads import UploadManager
from ns_studio import daily, jobs
from ns_studio import orchestrator as orch
from ns_studio.clipper import stages_clip as sc
from ns_studio.clipper.models import ClipParams
from ns_studio.context import FAKEABLE
from ns_studio.stages import REGISTRY
from tests.helpers import make_channel, yaml_only_channel

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def repeat(monkeypatch: pytest.MonkeyPatch, *draws: str) -> list[str]:
    """Make the id draw give `draws` in order, then count upwards; returns the list of draws handed out."""
    given: list[str] = []
    lock = threading.Lock()
    rest: Iterator[str] = iter(draws)

    def token_hex(nbytes: int = 2) -> str:
        assert nbytes == 2
        with lock:
            value = next(rest, None) or f"{0xF000 + len(given):04x}"
            given.append(value)
            return value

    monkeypatch.setattr(dbmod.secrets, "token_hex", token_hex)
    return given


# ---------- the one helper ----------
def test_a_repeated_draw_gets_another_id_and_the_first_folder_is_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repeat(monkeypatch, "aaaa", "aaaa", "bbbb")
    first = claim_job_id("after-dark", claim_dir(lambda j: tmp_path / j), NOW)
    (tmp_path / first / "job.json").write_text("first", encoding="utf-8")
    second = claim_job_id("after-dark", claim_dir(lambda j: tmp_path / j), NOW)
    assert (first, second) == ("after-dark-20261120-aaaa", "after-dark-20261120-bbbb")
    assert (tmp_path / first / "job.json").read_text(encoding="utf-8") == "first"
    assert sorted(p.name for p in tmp_path.iterdir()) == [first, second]


def test_claim_dir_makes_missing_parents_and_refuses_an_existing_folder(tmp_path: Path):
    claim = claim_dir(lambda j: tmp_path / "jobs" / "deep" / j)
    claim("x-20261120-0001")
    assert (tmp_path / "jobs" / "deep" / "x-20261120-0001").is_dir()
    with pytest.raises(FileExistsError):
        claim("x-20261120-0001")


def test_a_taken_row_is_a_refusal_too(monkeypatch: pytest.MonkeyPatch):
    repeat(monkeypatch, "aaaa", "bbbb")
    seen: list[str] = []

    def claim(job_id: str) -> None:
        seen.append(job_id)
        if job_id.endswith("aaaa"):
            raise sqlite3.IntegrityError("UNIQUE constraint failed: runs.run_id")

    assert claim_job_id("daily", claim, NOW) == "daily-20261120-bbbb"
    assert seen == ["daily-20261120-aaaa", "daily-20261120-bbbb"]


def test_after_the_bound_it_stops_with_a_coded_error(tmp_path: Path):
    calls: list[str] = []

    def never(job_id: str) -> None:
        calls.append(job_id)
        raise FileExistsError(job_id)

    with pytest.raises(NsError) as ei:
        claim_job_id("after-dark", never, NOW)
    assert ei.value.code == "NS-E205" and len(calls) == JOB_ID_TRIES
    assert "after-dark" in ei.value.detail and calls[-1] in ei.value.detail


def test_another_error_of_the_claim_is_not_swallowed():
    def denied(job_id: str) -> None:
        raise PermissionError(13, "Access is denied", job_id)

    with pytest.raises(PermissionError):
        claim_job_id("after-dark", denied, NOW)


def test_two_makers_with_the_same_forced_draw_get_two_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The dashboard and the nightly run at the same moment: both are handed `aaaa` first."""
    repeat(monkeypatch, "aaaa", "aaaa")
    start = threading.Barrier(2)
    got: list[str] = []
    errors: list[BaseException] = []

    def maker(text: str) -> None:
        try:
            start.wait(timeout=10)
            job_id = claim_job_id("after-dark", claim_dir(lambda j: tmp_path / j), NOW)
            (tmp_path / job_id / "job.json").write_text(json.dumps({"job_id": job_id, "by": text}), "utf-8")
            got.append(job_id)
        except BaseException as e:  # noqa: BLE001 - reported by the assert below
            errors.append(e)

    threads = [threading.Thread(target=maker, args=(name,)) for name in ("dashboard", "nightly")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not errors and len(set(got)) == 2 and "after-dark-20261120-aaaa" in got
    for job_id in got:  # each folder holds its own job, none was written over
        assert json.loads((tmp_path / job_id / "job.json").read_text("utf-8"))["job_id"] == job_id


# ---------- the makers, entered where the product enters ----------
def test_make_job_never_writes_into_another_jobs_folder(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    env = jobs.open_env(frozenset(FAKEABLE))
    ch = yaml_only_channel(Paths.from_env())
    repeat(monkeypatch, "aaaa", "aaaa", "bbbb")
    first = jobs.make_job(env, ch, "shorts_only", now=NOW, rng=random.Random(1))
    before = (first.job_dir / "job.json").read_bytes()
    (first.job_dir / "story.md").write_text("the first job's story", encoding="utf-8")
    second = jobs.make_job(env, ch, "shorts_only", now=NOW, rng=random.Random(2))
    assert first.job_id.endswith("-aaaa") and second.job_id.endswith("-bbbb")
    assert first.job_dir != second.job_dir and second.job_dir.is_dir()
    assert (first.job_dir / "job.json").read_bytes() == before
    assert json.loads((second.job_dir / "job.json").read_text("utf-8"))["job_id"] == second.job_id
    assert not (second.job_dir / "story.md").exists()
    # the ledger and the owner files know two jobs
    assert {r["job_id"] for r in env.db.query("select job_id from combos")} == {first.job_id, second.job_id}


def test_a_new_clip_job_gets_its_own_folder_and_a_given_id_is_kept(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    params = ClipParams(source="https://videos.example.com/v", channel="after-dark", rights_confirmed=True)
    repeat(monkeypatch, "aaaa", "aaaa", "bbbb")
    first = sc.create_clip_job(Paths.from_env(), make_channel(), params, fake=FAKEABLE)
    (first.job_dir / "transcript.json").write_text("{}", encoding="utf-8")
    second = sc.create_clip_job(Paths.from_env(), make_channel(), params, fake=FAKEABLE)
    assert first.job_id != second.job_id and not (second.job_dir / "transcript.json").exists()
    # a caller that names the job (a rerun) gets that job, folder and all
    again = sc.create_clip_job(Paths.from_env(), make_channel(), params, fake=FAKEABLE, job_id=first.job_id)
    assert again.job_dir == first.job_dir and (again.job_dir / "transcript.json").exists()


def test_a_series_part_takes_a_free_id(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    from tests import test_series_workshop as tsw

    rec = tsw.Recorder()
    ws, slug = tsw.ready_to_produce(tmp_home, rec)
    taken = ws.paths.job_dir("after-dark-" + datetime.now(UTC).strftime("%Y%m%d") + "-aaaa")
    taken.mkdir(parents=True)
    (taken / "job.json").write_text("another job", encoding="utf-8")
    repeat(monkeypatch, "aaaa", "bbbb", "aaaa", "cccc", "dddd")
    results = ws.produce(slug, "all")
    ids = [r["job_id"] for r in results]
    assert len(set(ids)) == 3 and taken.name not in ids
    assert (taken / "job.json").read_text(encoding="utf-8") == "another job"
    assert not (taken / "script_source.json").exists()
    for job_id in ids:
        assert (ws.paths.job_dir(job_id) / "script_source.json").is_file()


def test_a_second_upload_never_truncates_the_first(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    mgr = UploadManager(Paths.from_env())
    payload = bytes(range(256)) * 8
    repeat(monkeypatch, "aaaa", "aaaa", "bbbb")
    first = mgr.start("one.mp4", len(payload))
    mgr.append(first.upload_id, 0, payload[:1000])
    second = mgr.start("two.mp4", 10)
    assert first.upload_id != second.upload_id
    assert mgr.status(first.upload_id).offset == 1000 and mgr.status(first.upload_id).filename == "one.mp4"
    mgr.append(first.upload_id, 1000, payload[1000:])
    assert mgr.complete(first.upload_id).sha256 == hashlib.sha256(payload).hexdigest()


def test_two_linked_videos_never_share_a_download_folder(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    from ns_dashboard import studio_bridge
    from tests.test_dashboard_url_clips import URL, body, make

    dash, state = make(tmp_home)
    repeat(monkeypatch, "aaaa", "aaaa", "bbbb")
    # the queue row's own id comes from the same draw; only the source ids matter here
    first = dash.jobs.create_clip(body(url=URL, template="auto"))["params"]["source_id"]
    second = dash.jobs.create_clip(body(url=URL + "&n=2", template="auto"))["params"]["source_id"]
    assert first != second
    assert studio_bridge.download_dir(state.paths, first) != studio_bridge.download_dir(state.paths, second)


class OkStage:
    name = "daily_ok"
    tenant = None
    inputs = ()
    outputs = ("ok.json",)

    def run(self, ctx):  # noqa: ANN001, ANN201 - a test stage
        ctx.write_json("ok.json", {"ok": True})


def test_a_second_night_on_the_same_day_gets_its_own_run_id(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    from tests.test_cadence import _channel, _env

    paths, db = _env(tmp_home)
    _channel(paths, db, 9, format="shorts_only", shorts_per_day_max=1)
    monkeypatch.setitem(REGISTRY, "daily_ok", OkStage())
    monkeypatch.setitem(orch.FORMAT_STAGES, "shorts_only", ["daily_ok"])
    env = jobs.open_env(light=True)
    given = repeat(monkeypatch, "aaaa")
    first = daily.run_daily(env, now=NOW, rng=random.Random(1))
    assert first["run_id"] == "daily-20261120-aaaa" and first["failures"] == []
    given.clear()
    repeat(monkeypatch, "aaaa", "bbbb")
    again = daily.run_daily(env, now=NOW, rng=random.Random(2))  # before: IntegrityError on the runs row
    assert again["run_id"] == "daily-20261120-bbbb"
    assert {r["run_id"] for r in db.query("select run_id from runs")} >= {first["run_id"], again["run_id"]}


# ---------- the fakes ----------
def test_fake_ids_have_the_products_form_and_never_repeat():
    ids = [fakes.fake_job_id("after-dark", NOW) for _ in range(500)]
    assert len(set(ids)) == 500
    assert all(re.fullmatch(r"after-dark-20261120-[0-9a-f]{4}", i) for i in ids)


def test_five_hundred_seeded_outputs_have_five_hundred_ids(tmp_home: Path):
    from ns_dashboard.handlers import build_state
    from ns_dashboard.supervisor_link import SupervisorLink

    state = build_state(
        Paths.from_env(), fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
    )
    fakes.seed_channel(state.db, state.paths, "after-dark")
    seed = partial(fakes.seed_output, state.db, state.paths, channel="after-dark", with_files=False)
    ids = [seed() for _ in range(500)]
    assert len(set(ids)) == 500

