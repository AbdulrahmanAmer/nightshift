"""D-33: a job is refused for lack of disk space BEFORE it starts, and nothing is written.

Every number is faked (``diskneed.free_bytes`` answers; the sizes are the module's own estimates), so
no test writes a large file or fills a drive. The routes: ``studio run`` (also ``--resume`` and
``--daily``), the dashboard's job start (run, clip, series part, resume), and the nightly run's morning note.

``--fake-services`` and the dashboard on fakes use the fake clip source, which writes placeholder files, so
their Short is estimated at ``FAKE_SHORT`` (57 MB); a real install's is ``REAL_SHORT`` (100 MB).
"""

from __future__ import annotations

import contextlib
import json
import random
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import pytest
from typer.testing import CliRunner

from ns_common import diskneed
from ns_common.diskneed import GIB, MIB
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_dashboard.errors import ApiError
from ns_dashboard.fakes import seed_channel
from ns_studio import daily, diskroom, jobs, notifications
from ns_studio.cli import app
from ns_studio.context import FAKEABLE
from ns_studio.stages import StageState
from tests.helpers import make_channel
from tests.test_cadence import _channel, _env
from tests.test_round3_resume import failed_run, make

runner = CliRunner()
NOW = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)
REAL_SHORT = diskneed.short_need().total
FAKE_SHORT = diskneed.short_need(real_footage=False).total


def free_space(monkeypatch: pytest.MonkeyPatch, free: int | None) -> list[Path]:
    """Make the volume of the home report ``free`` bytes (None: unreadable). Returns the paths asked."""
    asked: list[Path] = []

    def fake(path: Path | str, log: Any = None) -> int | None:
        asked.append(Path(path))
        if free is None and log is not None:
            log.warn("disk_free_unknown", "cannot read the free space (faked)")
        return free

    monkeypatch.setattr(diskneed, "free_bytes", fake)
    return asked


def nothing_written(paths: Paths, db: Any = None) -> bool:
    """No job folder and no drawn combination: the refusal came before anything was made."""
    folders = list(paths.jobs_dir.iterdir()) if paths.jobs_dir.exists() else []
    combos = db.query("select * from combos") if db is not None else []
    return not folders and not combos


def log_text(paths: Paths) -> str:
    return "\n".join(f.read_text(encoding="utf-8") for f in sorted(paths.logs.glob("*.jsonl")))


def mark_done(folder: Path, *names: str) -> None:
    state = StageState()
    for name in names:
        state.mark_done(name, "h")
    state.save(folder)


@pytest.fixture
def shorts_channel(tmp_home: Path):
    paths, db = _env(tmp_home)
    ch = _channel(paths, db, 9, format="shorts_only", shorts_per_day_max=2)
    return paths, db, ch


# ---------------------------------------- the estimate
def test_the_estimate_follows_the_channels_own_ranges(tmp_home: Path):
    paths = Paths.from_env()
    short = make_channel(format="shorts_only")
    master = make_channel(format="master_first", long_minutes=(10, 14), shorts_per_master=(4, 6))
    shorter = make_channel(format="master_first", long_minutes=(9, 10), shorts_per_master=(2, 3))
    need_short = diskroom.story_need(paths, short, "shorts_only")
    assert need_short is not None and need_short.total == REAL_SHORT == 100 * MIB
    big = diskroom.story_need(paths, master, "master_first")
    small = diskroom.story_need(paths, shorter, "master_first")
    assert big is not None and small is not None and big.total > small.total > 10 * need_short.total
    assert diskroom.story_need(paths, master, "no_such_format") is None  # no estimate: never a block


def test_fake_footage_and_the_fake_clip_source_make_a_job_lighter(tmp_home: Path):
    paths = Paths.from_env()
    paths.ensure()
    ch = make_channel(format="shorts_only")
    real = diskroom.story_need(paths, ch, "shorts_only")
    faked = diskroom.story_need(paths, ch, "shorts_only", fake={"clips"})
    paths.runtime_file.write_text(json.dumps({"fake_footage": True}), encoding="utf-8")
    placeholder = diskroom.story_need(paths, ch, "shorts_only")
    assert real is not None and faked is not None and placeholder is not None
    assert 0 < faked.total < real.total and placeholder.total == faked.total == FAKE_SHORT


def test_a_resume_needs_only_the_stages_it_has_left(tmp_home: Path):
    paths = Paths.from_env()
    paths.ensure()
    ch = make_channel(format="shorts_only")
    folder = paths.job_dir("after-dark-20261104-aaaa")
    folder.mkdir(parents=True)
    mark_done(folder, "ideas", "voice", "clips")
    assert diskroom.done_stages(folder) == {"ideas", "voice", "clips"}
    fresh = diskroom.story_need(paths, ch, "shorts_only")
    rest = diskroom.story_need(paths, ch, "shorts_only", resume_dir=folder)
    assert fresh is not None and rest is not None
    assert rest.total == REAL_SHORT - (6 * 8 + 5) * MIB  # the clips and the voice are done
    assert diskroom.done_stages(folder / "missing") == set()  # no state yet: all of it is left
    again = diskroom.story_need(paths, ch, "shorts_only", resume_dir=folder / "missing")
    assert again is not None and again.total == fresh.total


def test_a_file_source_is_counted_unless_it_is_already_in_the_home(tmp_home: Path, tmp_path: Path):
    paths = Paths.from_env()
    paths.ensure()
    outside = tmp_path / "talk.mp4"
    outside.write_bytes(b"x" * 4096)
    inside = paths.home / "imports" / "clipper" / "src-abc" / "source.mp4"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"x" * 4096)
    assert diskroom.source_copy_bytes(paths, outside) == 4096
    assert diskroom.source_copy_bytes(paths, inside) == 0  # nothing is copied twice
    assert diskroom.source_copy_bytes(paths, "https://example.com/v") == 0
    assert diskroom.source_copy_bytes(paths, tmp_path / "gone.mp4") == 0  # unreadable: no guess
    assert diskroom.source_copy_bytes(paths, None) == 0
    with_file = diskroom.clip_need(paths, source=outside)
    assert with_file.total - diskroom.clip_need(paths).total == 4096
    assert diskroom.clip_need(paths, url_source=True).total > with_file.total
    assert diskroom.clip_need(paths, rerun=True, source=outside).total < diskroom.clip_need(paths).total


def test_a_video_downloaded_for_the_job_is_copied_by_the_import_so_it_counts(tmp_home: Path):
    """`ImportStage` is given the downloaded file's path (`link=False`): `import_source` copy2s it into the
    clip's own folder before the download is deleted, so it is on the drive twice for a moment."""
    paths = Paths.from_env()
    paths.ensure()
    download = paths.home / "imports" / "clipper" / "_downloads" / "dl-abc" / "video.mp4"
    download.parent.mkdir(parents=True)
    download.write_bytes(b"x" * 4096)
    assert diskroom.source_copy_bytes(paths, download) == 4096
    adopted = paths.home / "imports" / "clipper" / "src-abc" / "source.mp4"
    adopted.parent.mkdir(parents=True)
    adopted.write_bytes(b"x" * 4096)
    assert diskroom.source_copy_bytes(paths, adopted) == 0  # an adopted upload is already in place


def test_a_dashboard_clip_from_a_link_counts_the_copy_of_the_downloaded_video(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_dashboard import studio_bridge

    make(tmp_home)  # the channel
    paths = Paths.from_env()
    download = paths.home / "imports" / "clipper" / studio_bridge.DOWNLOADS / "dl-abc" / "video.mp4"
    download.parent.mkdir(parents=True)
    download.write_bytes(b"x" * 2048)
    params = {"channel": "after-dark", "rights_confirmed": True, "max_clips": 6}
    without_copy = diskroom.clip_need(paths, max_clips=6).total
    # room for the clip job but not for the second copy of the video: refused, nothing created
    free_space(monkeypatch, without_copy + 1024)
    with pytest.raises(NsError) as ei:
        studio_bridge.run_clip(download, params, lambda *a: None, paths=paths, fake=("clips",))
    assert ei.value.code == "NS-E104"
    assert ei.value.data["need_bytes"] == without_copy + 2048
    assert not paths.jobs_dir.exists() or not list(paths.jobs_dir.iterdir())
    assert download.exists()  # the refusal came before the download was touched


def test_the_short_sentence_is_the_one_a_note_can_carry():
    need = diskneed.short_need()
    assert need.short_sentence(20 * MIB, "D:") == "needs about 100 MB; D: has 20 MB free"
    assert need.short_sentence(None, "D:") == "needs about 100 MB; the free space on D: could not be read"
    assert diskneed.check(need, "D:/", free=20 * MIB).short == "needs about 100 MB; D: has 20 MB free"


# ------------------------------------------------------------------------------------------------ studio run
def test_studio_run_refuses_a_job_that_does_not_fit_and_writes_nothing(
    shorts_channel, monkeypatch: pytest.MonkeyPatch
):
    paths, db, _ch = shorts_channel
    asked = free_space(monkeypatch, 20 * MIB)
    res = runner.invoke(app, ["run", "--fake-services", "--no-policy", "--channel", "after-dark"])
    assert res.exit_code == 1, res.output
    assert "NS-E104" in res.output
    assert "This needs about 57 MB" in res.output and "has 20 MB free" in res.output
    assert "60 GB" not in res.output  # the install's rule is not quoted for a job
    assert nothing_written(paths, db)
    assert asked and asked[0] == paths.home  # the volume of the home was the one asked
    assert "disk_room_refused" in log_text(paths)


def test_studio_run_goes_ahead_when_the_job_fits(shorts_channel, monkeypatch: pytest.MonkeyPatch):
    paths, db, _ch = shorts_channel
    free_space(monkeypatch, FAKE_SHORT)  # exactly enough is enough
    monkeypatch.setattr("ns_studio.cli.run_job_guarded", lambda ctx, **kw: ["stage"])
    res = runner.invoke(app, ["run", "--fake-services", "--no-policy", "--channel", "after-dark"])
    assert res.exit_code == 0, res.output
    assert not nothing_written(paths, db)  # the job exists now


def test_studio_run_does_not_block_when_the_free_space_cannot_be_read(
    shorts_channel, monkeypatch: pytest.MonkeyPatch
):
    paths, db, _ch = shorts_channel
    free_space(monkeypatch, None)
    monkeypatch.setattr("ns_studio.cli.run_job_guarded", lambda ctx, **kw: ["stage"])
    res = runner.invoke(app, ["run", "--fake-services", "--no-policy", "--channel", "after-dark"])
    assert res.exit_code == 0, res.output
    assert "disk_free_unknown" in log_text(paths)
    assert not nothing_written(paths, db)


def test_a_real_unreadable_volume_is_logged_and_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def boom(_p: Any) -> Any:
        raise OSError("the volume does not answer")

    monkeypatch.setattr(shutil, "disk_usage", boom)
    log = NsLogger("studio", tmp_path / "logs", echo=False)
    verdict = diskneed.require_room(diskneed.short_need(), tmp_path, log=log)
    assert verdict.ok and verdict.free is None
    [logged] = list((tmp_path / "logs").glob("*.jsonl"))
    assert "disk_free_unknown" in logged.read_text(encoding="utf-8")


def test_studio_resume_needs_only_what_is_left(shorts_channel, monkeypatch: pytest.MonkeyPatch):
    paths, db, ch = shorts_channel
    env = jobs.open_env(light=True)
    ctx = jobs.make_job(env, ch, "shorts_only", rng=random.Random(1))
    mark_done(ctx.job_dir, "voice", "clips")
    monkeypatch.setattr("ns_studio.cli.run_job_guarded", lambda c, **kw: ["stage"])
    free_space(monkeypatch, 52 * MIB)  # a fresh Short needs 57 MB; the rest of this one 47
    res = runner.invoke(app, ["run", "--fake-services", "--no-policy", "--resume", ctx.job_id])
    assert res.exit_code == 0, res.output
    free_space(monkeypatch, 30 * MIB)  # not even the rest fits
    res = runner.invoke(app, ["run", "--fake-services", "--no-policy", "--resume", ctx.job_id])
    assert res.exit_code == 1 and "NS-E104" in res.output and "has 30 MB free" in res.output


def test_studio_clip_is_refused_before_its_job_folder_is_made(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_studio.clipper import cli as clip_cli

    paths, db = _env(tmp_home)
    _channel(paths, db, 9, format="shorts_only")
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"x" * 2048)
    free_space(monkeypatch, 5 * MIB)
    res = runner.invoke(
        clip_cli.app, [str(video), "--channel", "after-dark", "--confirm-rights", "--fake-services=all"]
    )
    assert res.exit_code == 1, res.output
    assert "NS-E104" in res.output and "has 5 MB free" in res.output
    assert nothing_written(paths, db)


# ------------------------------------------------------------------------------------------------ the nightly
def test_the_nightly_run_starts_no_job_that_does_not_fit_and_says_so(
    shorts_channel, monkeypatch: pytest.MonkeyPatch
):
    paths, db, _ch = shorts_channel
    free_space(monkeypatch, 20 * MIB)
    env = jobs.open_env(light=True)
    ran: list[str] = []
    shown: list[tuple[str, str]] = []

    def toast(title: str, message: str) -> bool:
        shown.append((title, message))
        return True

    report = daily.run_daily(
        env,
        now=NOW,
        rng=random.Random(1),
        run_fn=lambda ctx: ran.append(ctx.job_id) or [],
        notifier=toast,
        awake=contextlib.nullcontext,
    )
    assert ran == [] and report["jobs"] == []
    assert report["failures"] == []  # not a failure one stage in
    [entry] = report["no_room"]
    assert entry["channel"] == "after-dark" and entry["format"] == "shorts_only" and entry["jobs"] == 2
    assert entry["need_bytes"] == FAKE_SHORT and entry["free_bytes"] == 20 * MIB
    assert entry["short"].endswith("has 20 MB free") and entry["short"].startswith("needs about 57 MB")
    assert nothing_written(paths, db)
    assert shown == [("Nightshift", f"2 jobs not started, not enough disk space ({entry['short']})")]
    saved = json.loads(next(paths.logs.glob("nightly-*.json")).read_text(encoding="utf-8"))
    assert saved["no_room"][0]["short"] == entry["short"]  # the report on disk carries it too


def test_the_nightly_run_starts_what_fits(shorts_channel, monkeypatch: pytest.MonkeyPatch):
    free_space(monkeypatch, 10 * GIB)
    env = jobs.open_env(light=True)
    ran: list[str] = []
    report = daily.run_daily(
        env,
        now=NOW,
        rng=random.Random(1),
        run_fn=lambda ctx: ran.append(ctx.job_id) or [],
        awake=contextlib.nullcontext,
    )
    assert len(ran) == 2 and "no_room" not in report and report["failures"] == []


def test_the_nightly_does_not_continue_an_interrupted_job_into_a_full_drive(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """D-31 + D-33: a job a dead night left half made stays interrupted, and the next night continues it."""
    from tests import d31_driver as driver
    from tests.test_interrupted_nightly import _dead_daily_job, _setup

    paths, db = _setup(tmp_home)
    driver.install(monkeypatch)
    dead = _dead_daily_job(paths, db)
    attempts: list[str] = []

    def run(ctx: Any) -> list[str]:
        attempts.append(ctx.job_id)
        return ["d31_a"]

    free_space(monkeypatch, 5 * MIB)
    report = daily.run_daily(jobs.open_env(light=True), rng=random.Random(1), run_fn=run)
    assert attempts == [] and report["failures"] == [] and report["jobs"] == []
    assert any(e["format"].endswith("(resumed)") for e in report["no_room"])  # the job is listed
    assert "interrupted_jobs" in report and all(i["job_id"] != dead for i in report["interrupted_jobs"])
    free_space(monkeypatch, 10 * GIB)  # the next night has room
    again = daily.run_daily(jobs.open_env(light=True), rng=random.Random(1), run_fn=run)
    assert attempts[0] == dead and again["failures"] == [] and "no_room" not in again


def test_the_morning_note_line_for_jobs_that_were_not_started():
    report: dict[str, Any] = {
        "jobs": [],
        "failures": [],
        "no_room": [
            {"channel": "a", "jobs": 2, "short": "needs about 100 MB; D: has 20 MB free"},
            {"channel": "b", "jobs": 1, "short": "needs about 3 GB; D: has 20 MB free"},
        ],
    }
    msg = notifications.nightly_message(report, 0, 0)
    assert msg == "3 jobs not started, not enough disk space (needs about 100 MB; D: has 20 MB free)"
    with_fail = {**report, "failures": [{"code": "NS-E203"}]}
    assert "run failed (NS-E203)" in str(notifications.nightly_message(with_fail, 0, 0))
    assert "not started" in str(notifications.nightly_message(with_fail, 0, 0))
    assert notifications.nightly_message({"jobs": [], "failures": []}, 0, 0) is None


def test_studio_run_daily_prints_the_jobs_it_did_not_start(shorts_channel, monkeypatch: pytest.MonkeyPatch):
    paths, db, _ch = shorts_channel
    free_space(monkeypatch, 20 * MIB)
    monkeypatch.setattr(notifications, "nightly_notifier", lambda p, log: None)
    res = runner.invoke(app, ["run", "--daily", "--fake-services", "--no-policy"])
    # a scheduler reads the exit code: a night that made nothing for lack of room is not a success
    assert res.exit_code == 1, res.output
    assert "NOT STARTED" in res.output and "has 20 MB free" in res.output
    assert nothing_written(paths, db)


# ---------------------------------------- the dashboard
def test_the_dashboards_run_now_is_refused_with_507_and_nothing_is_queued(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    dash = make(tmp_home)
    free_space(monkeypatch, 20 * MIB)
    with pytest.raises(ApiError) as ei:
        dash.jobs.create_run({"channel": "after-dark", "format": "shorts_only"})
    e = ei.value
    assert e.status == 507 and e.code == "NS-E104"
    assert "This needs about 57 MB" in e.message and "has 20 MB free" in e.message
    assert e.hint and "60 GB" not in e.hint
    assert e.details == {"need_bytes": FAKE_SHORT, "free_bytes": 20 * MIB}
    assert dash.state.store.next_queued() is None
    with pytest.raises(ApiError) as again:  # the Assistant's card gate says the same
        dash.jobs.check_run({"channel": "after-dark", "format": "shorts_only"})
    assert again.value.code == "NS-E104"
    free_space(monkeypatch, 100 * MIB)
    assert dash.jobs.create_run({"channel": "after-dark", "format": "shorts_only"})["status"] == "queued"


def test_the_dashboard_job_start_over_http_answers_507(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    from tests.test_dashboard_api import make_env

    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths)
    free_space(monkeypatch, 20 * MIB)
    r = client.post("/api/jobs/run", json={"channel": "after-dark", "format": "shorts_only"})
    assert r.status_code == 507
    err = r.json()["error"]
    assert err["code"] == "NS-E104" and "has 20 MB free" in err["message"]
    assert client.get("/api/jobs").json() == []


def test_the_dashboards_master_needs_far_more_than_a_short(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    dash = make(tmp_home)
    free_space(monkeypatch, 1 * GIB)  # a Short fits, a 14-minute master does not
    assert dash.jobs.create_run({"channel": "after-dark", "format": "shorts_only"})["status"] == "queued"
    with pytest.raises(ApiError) as ei:
        dash.jobs.create_run({"channel": "after-dark", "format": "master_first"})
    assert ei.value.code == "NS-E104" and "has 1 GB free" in ei.value.message


def test_a_dashboard_resume_needs_only_what_is_left(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    dash = make(tmp_home)
    failed = failed_run(dash, ["after-dark-20261104-bbbb"])
    mark_done(dash.state.paths.job_dir("after-dark-20261104-bbbb"), "voice", "clips")
    free_space(monkeypatch, 30 * MIB)
    with pytest.raises(ApiError) as ei:
        dash.jobs.create_resume(failed)
    assert ei.value.status == 507 and dash.state.store.next_queued() is None
    free_space(monkeypatch, 52 * MIB)
    assert dash.jobs.create_resume(failed)["status"] == "queued"  # a fresh Short would need 57


def test_the_dashboards_clip_job_is_checked_too(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dash = make(tmp_home)
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"x" * 2048)
    body = {"path": str(video), "channel": "after-dark", "rights_confirmed": True}
    free_space(monkeypatch, 40 * MIB)
    with pytest.raises(ApiError) as ei:
        dash.jobs.create_clip(body)
    assert ei.value.status == 507 and "has 40 MB free" in ei.value.message
    assert dash.state.store.next_queued() is None
    free_space(monkeypatch, 1 * GIB)
    assert dash.jobs.create_clip(body)["status"] == "queued"
    link = {"url": "https://example.com/watch?v=1", "channel": "after-dark", "rights_confirmed": True}
    with pytest.raises(ApiError) as long:  # a link adds the downloaded video (1.5 GB)
        dash.jobs.create_clip(link)
    assert long.value.code == "NS-E104"


def test_a_scheduled_or_batched_job_is_checked_again_when_it_starts(tmp_home: Path, monkeypatch):
    """The request was accepted with room; the drive filled up before the job began: NS-E104, no job made."""
    from ns_dashboard import studio_bridge

    paths, db = _env(tmp_home)
    _channel(paths, db, 9, format="shorts_only")
    free_space(monkeypatch, 20 * MIB)
    with pytest.raises(NsError) as ei:
        studio_bridge.run_story_jobs(
            "after-dark", "shorts_only", 1, lambda *a: None, fake=tuple(FAKEABLE), paths=paths
        )
    assert ei.value.code == "NS-E104" and nothing_written(paths, db)


def test_room_checks_only_ever_raise_ns_e104(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    paths = Paths.from_env()
    ch = make_channel(format="shorts_only")
    free_space(monkeypatch, 1 * MIB)
    with pytest.raises(NsError) as ei:
        diskroom.require_story_room(paths, None, ch, "shorts_only")
    assert ei.value.code == "NS-E104"
    assert diskroom.require_story_room(paths, None, ch, "no_such_format") is None


# ------------------------------------- a resume of every format is checked (break-it finding 4)
ALL_FORMATS = tuple(get_args(jobs.Fmt))


def test_every_job_format_has_a_story_estimate(tmp_home: Path):
    """`master_from_script` and `clip` used to come back as "no estimate", i.e. no check on a resume."""
    paths = Paths.from_env()
    ch = make_channel(format="master_first", long_minutes=(10, 14), shorts_per_master=(4, 6))
    assert {"master_from_script", "clip"} <= set(ALL_FORMATS)
    for fmt in ALL_FORMATS:
        need = diskroom.story_need(paths, ch, fmt)
        assert need is not None and need.total > 0, fmt
    part = diskroom.story_need(paths, ch, "master_from_script")
    assert part is not None and part.total == diskneed.format_need("series_part").total
    assert diskroom.story_need(paths, ch, "no_such_format") is None  # only a name nobody has is unchecked


@pytest.mark.parametrize("fmt", ["master_from_script", "clip"])
def test_a_resume_of_a_master_from_script_or_clip_job_is_refused_for_room(
    shorts_channel, monkeypatch: pytest.MonkeyPatch, fmt: str
):
    paths, db, ch = shorts_channel
    env = jobs.open_env(light=True)
    ctx = jobs.make_job(env, ch, "shorts_only", rng=random.Random(1))
    info = json.loads((ctx.job_dir / "job.json").read_text(encoding="utf-8"))
    (ctx.job_dir / "job.json").write_text(json.dumps({**info, "fmt": fmt}), encoding="utf-8")
    ran: list[str] = []
    monkeypatch.setattr("ns_studio.cli.run_job_guarded", lambda c, **kw: ran.append(c.job_id) or ["stage"])
    free_space(monkeypatch, 5 * MIB)
    res = runner.invoke(app, ["run", "--fake-services", "--no-policy", "--resume", ctx.job_id])
    assert res.exit_code == 1 and "NS-E104" in res.output and "has 5 MB free" in res.output, res.output
    assert ran == []
    free_space(monkeypatch, 10 * GIB)
    again = runner.invoke(app, ["run", "--fake-services", "--no-policy", "--resume", ctx.job_id])
    assert again.exit_code == 0 and ran == [ctx.job_id]


# ----------------------------- every series part start route is refused up front (break-it finding 3)
def an_approved_story(tmp_home: Path):
    from tests.test_series_workshop import Recorder, ready_to_produce

    rec = Recorder()
    ws, slug = ready_to_produce(tmp_home, rec)
    return ws, slug, rec


def part_statuses(ws: Any, slug: str) -> list[str]:
    from ns_studio.series.store import StoryDir

    st = StoryDir.open(ws.paths, slug).read_status()
    return [st.parts[n].status for n in sorted(st.parts)]


def test_workshop_produce_refuses_a_part_that_does_not_fit_and_writes_nothing(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """The one place every route shares: `Workshop.produce` (Assistant, `studio series`, dashboard)."""
    ws, slug, rec = an_approved_story(tmp_home)
    free_space(monkeypatch, 40 * MIB)
    with pytest.raises(NsError) as ei:
        ws.produce(slug, "all")
    assert ei.value.code == "NS-E104" and "has 40 MB free" in ei.value.detail
    assert rec.requests == []  # nothing queued
    assert nothing_written(ws.paths)  # no job folder, no script_source.json
    assert part_statuses(ws, slug) == ["approved"] * 3  # no part marked producing
    free_space(monkeypatch, 10 * GIB)
    assert [r["part"] for r in ws.produce(slug, "all")] == [1, 2, 3]
    assert part_statuses(ws, slug) == ["producing"] * 3


def test_the_assistants_workshop_produce_tool_is_refused_up_front(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_studio.series.tools import call_tool

    ws, slug, rec = an_approved_story(tmp_home)
    free_space(monkeypatch, 40 * MIB)
    with pytest.raises(NsError) as ei:
        call_tool(ws, "workshop_produce", {"slug": slug})
    assert ei.value.code == "NS-E104" and rec.requests == [] and nothing_written(ws.paths)
    with pytest.raises(NsError) as card:  # the confirm card is never offered for a step that stops at once
        ws.check("workshop_produce", {"slug": slug})
    assert card.value.code == "NS-E104"
    free_space(monkeypatch, 10 * GIB)
    ws.check("workshop_produce", {"slug": slug})  # with room the check passes


def test_studio_series_produce_is_refused_up_front(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    from ns_studio.series.cli import app as series_app

    ws, slug, _rec = an_approved_story(tmp_home)
    free_space(monkeypatch, 40 * MIB)
    res = runner.invoke(series_app, ["produce", slug, "--fake-services"])
    assert res.exit_code == 1, res.output
    assert "NS-E104" in res.output and "has 40 MB free" in res.output
    assert nothing_written(ws.paths) and part_statuses(ws, slug) == ["approved"] * 3


def test_the_series_part_estimate_follows_the_services_the_part_runs_on(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """A read-only session fakes every service, but the part it queues runs on the dashboard's own."""
    from ns_dashboard import studio_bridge

    ws, slug, _rec = an_approved_story(tmp_home)
    real = diskneed.format_need("series_part").total
    fake = diskneed.format_need("series_part", real_footage=False).total
    assert fake < real
    free_space(monkeypatch, (real + fake) // 2)
    with pytest.raises(NsError):  # real footage does not fit
        studio_bridge.queue_next_part(slug, fake=())
    assert part_statuses(ws, slug) == ["approved"] * 3
    queued = studio_bridge.queue_next_part(slug, fake=studio_bridge.FAKE_ALL)  # fake footage does
    assert [q["part"] for q in queued] == [1]


def test_the_dashboards_produce_next_part_answers_507_through_the_real_adapter(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_dashboard.adapters import RealSeriesAdapter
    from ns_dashboard.supervisor_link import SupervisorLink

    dash = make(tmp_home)
    ws, slug, _rec = an_approved_story(tmp_home)
    dash.state.adapters.series = RealSeriesAdapter(  # type: ignore[assignment]
        dash.state.paths, SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
    )
    free_space(monkeypatch, 40 * MIB)
    with pytest.raises(ApiError) as ei:
        dash.library.produce_next_part(slug)
    assert ei.value.status == 507 and ei.value.code == "NS-E104" and "has 40 MB free" in ei.value.message
    assert nothing_written(ws.paths) and dash.state.store.next_queued() is None
    free_space(monkeypatch, 10 * GIB)
    assert [q["part"] for q in dash.library.produce_next_part(slug)["queued"]] == [1]


def test_the_fake_series_adapter_refuses_a_part_that_does_not_fit_like_the_workshop(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """A dashboard on fakes (--fake-services) answered 507 for Produce next part until the check moved into
    `Workshop.produce`; its fake adapter has to refuse the same way, with nothing queued or marked."""
    dash = make(tmp_home)
    dash.state.adapters.series.stories = [  # type: ignore[attr-defined]
        {
            "slug": "the-lift",
            "channel": "after-dark",
            "title": "The Lift",
            "parts": [{"n": 1, "title": "One", "status": "approved", "job_id": None}],
        }
    ]
    free_space(monkeypatch, 40 * MIB)
    with pytest.raises(ApiError) as ei:
        dash.library.produce_next_part("the-lift")
    assert ei.value.status == 507 and ei.value.code == "NS-E104" and "has 40 MB free" in ei.value.message
    assert dash.state.store.next_queued() is None
    assert dash.state.adapters.series.stories[0]["parts"][0]["status"] == "approved"  # type: ignore[attr-defined]
    free_space(monkeypatch, 10 * GIB)
    assert [q["part"] for q in dash.library.produce_next_part("the-lift")["queued"]] == [1]


# ------------------------------------------------ the nightly's result (break-it nit 3)
def test_the_nightly_exit_code_is_non_zero_only_when_nothing_was_made_for_lack_of_room():
    """Part of the night ran: the videos exist and the morning note names the rest."""
    from ns_studio import cli as studio_cli

    refused = [{"channel": "a", "jobs": 1, "format": "shorts_only", "message": "m"}]
    made = [{"job_id": "j", "stages": 1, "channel": "a"}]
    report: dict[str, Any] = {"channels": [], "jobs": made, "failures": [], "no_room": refused}
    assert studio_cli.nightly_exit_code(report) == 0  # something was made, the rest is in the morning note
    assert studio_cli.nightly_exit_code({**report, "jobs": []}) == 1  # nothing was made and room was why
    assert studio_cli.nightly_exit_code({**report, "jobs": [], "no_room": []}) == 0  # nothing due is fine
    assert studio_cli.nightly_exit_code({**report, "failures": [{"code": "x"}]}) == 1


def test_the_last_night_card_shows_the_jobs_that_were_not_started():
    """The dashboard reads `summary.no_room` of the newest run; the committed bundle must carry the card."""
    root = Path(__file__).resolve().parents[1]
    src = (root / "ns_dashboard" / "ui" / "src" / "pages" / "Queue.tsx").read_text(encoding="utf-8")
    assert "no_room" in src and "not enough disk space" in src
    bundle = (root / "ns_dashboard" / "static" / "app.js").read_text(encoding="utf-8")
    assert "no_room" in bundle and "not enough disk space" in bundle


# --------------------------------------------------------- a stage_state.json that is not an object
def test_done_stages_survives_a_state_file_that_is_not_an_object(tmp_path: Path):
    for text in ("[]", '"done"', "7", "null", '{"entries": []}', '{"entries": "x"}', "{broken"):
        folder = tmp_path / "job"
        folder.mkdir(exist_ok=True)
        (folder / "stage_state.json").write_text(text, encoding="utf-8")
        assert diskroom.done_stages(folder) == set(), text
