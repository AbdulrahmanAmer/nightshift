import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from ns_common.db import Db
from ns_common.paths import Paths
from ns_studio import review
from ns_studio.channels import crud
from ns_studio.cli import app
from ns_studio.errors import StudioError
from tests.helpers import add_output, make_channel

runner = CliRunner()
JOB = "after-dark-20261101-abcd"


def outbox_job(paths: Paths) -> Path:
    return paths.outbox / "after-dark" / "2026-11-01" / JOB


def _env(tmp_home: Path):
    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    crud.add_channel(paths, db, make_channel())
    return paths, db


def _output(paths: Paths, db: Db, job: str = "after-dark-20261101-abcd", item: str = "s1", **kw) -> str:
    base = paths.outbox / "after-dark" / "2026-11-01" / job
    mp4 = base / ("long/master.mp4" if item == "long" else f"shorts/{item}.mp4")
    mp4.parent.mkdir(parents=True, exist_ok=True)
    mp4.write_bytes(b"video")
    mp4.with_suffix(".json").write_text("{}", encoding="utf-8")
    mp4.with_suffix(".txt").write_text("sheet", encoding="utf-8")
    if item == "long":
        (mp4.parent / "thumb_1.jpg").write_bytes(b"jpg")
    return add_output(db, job, item, path=str(mp4), **kw)


def test_pending_lists_by_channel(tmp_home: Path):
    paths, db = _env(tmp_home)
    a = _output(paths, db)
    add_output(db, "other-20261101-aaaa", "s1", channel="other", path="x")
    assert [r["output_id"] for r in review.pending(db)] == sorted([a, "other-20261101-aaaa/s1"])
    assert [r["output_id"] for r in review.pending(db, "after-dark")] == [a]


def test_describe_shows_story_policy_and_advice(tmp_home: Path):
    paths, db = _env(tmp_home)
    oid = _output(paths, db, title="The Porch Light")
    job_dir = paths.job_dir("after-dark-20261101-abcd")
    job_dir.mkdir(parents=True)
    (job_dir / "story.json").write_text(json.dumps({"text": "Knock knock."}), encoding="utf-8")
    (job_dir / "policy_final.json").write_text(
        json.dumps(
            {
                "stage": "final",
                "passed": True,
                "checks": {"clip_reuse": {"passed": True, "detail": "3 uses"}},
                "advice": {"ai_disclosure": "yes", "made_for_kids": False, "age_restrict_suggested": False},
            }
        ),
        encoding="utf-8",
    )
    text = "\n".join(review.describe(paths, db, oid))
    assert "The Porch Light" in text and "Knock knock." in text and "clip_reuse: 3 uses" in text
    assert "altered/synthetic content = yes" in text
    with pytest.raises(StudioError) as ei:
        review.describe(paths, db, "nope/s1")
    assert ei.value.code == "NS-E313"


def test_approve_then_mark_uploaded(tmp_home: Path):
    paths, db = _env(tmp_home)
    oid = _output(paths, db)
    with pytest.raises(StudioError):
        review.mark_uploaded(db, "after-dark-20261101-abcd", "s1", "https://youtu.be/abcdefghijk")
    review.approve(db, oid, "looks fine")
    with pytest.raises(StudioError):
        review.approve(db, oid)  # not pending any more
    with pytest.raises(StudioError):
        review.mark_uploaded(db, "after-dark-20261101-abcd", "s1", "https://example.com/x")
    assert review.mark_uploaded(db, "after-dark-20261101-abcd", "s1", "https://youtu.be/abcdefghijk") == oid
    row = db.one("select status from outputs where output_id=?", (oid,))
    assert row is not None and row["status"] == "uploaded"
    up = db.one("select video_url from uploads where output_id=?", (oid,))
    assert up is not None and up["video_url"].endswith("abcdefghijk")


def test_reject_moves_files_and_records_reason(tmp_home: Path):
    paths, db = _env(tmp_home)
    oid = _output(paths, db, item="long")
    with pytest.raises(StudioError):
        review.reject(paths, db, oid, "  ")
    dest = review.reject(paths, db, oid, "voice sounds robotic")
    rejected_long = paths.rejected_dir("after-dark", "2026-11-01", JOB) / "long"
    assert dest.exists() and dest.parent == rejected_long
    assert (dest.parent / "master.json").exists() and (dest.parent / "thumb_1.jpg").exists()
    assert not (outbox_job(paths) / "long" / "master.mp4").exists()
    row = db.one("select status, review_note, path from outputs where output_id=?", (oid,))
    assert row is not None and row["status"] == "rejected" and row["review_note"] == "voice sounds robotic"
    assert row["path"] == str(dest)
    with pytest.raises(StudioError):
        review.reject(paths, db, oid, "again")


def test_reject_when_file_is_outside_outbox(tmp_home: Path):
    paths, db = _env(tmp_home)
    mp4 = tmp_home / "elsewhere.mp4"
    mp4.write_bytes(b"v")
    oid = add_output(db, "j-20261101-abcd", "s1", path=str(mp4))
    dest = review.reject(paths, db, oid, "off topic")
    assert dest.name == "elsewhere.mp4" and dest.exists()


def test_pause_and_resume_record_incidents(tmp_home: Path):
    paths, db = _env(tmp_home)
    with pytest.raises(StudioError):
        review.pause(paths, db, "after-dark", " ")
    review.pause(paths, db, "after-dark", "Reused content notice")
    assert crud.get_channel(paths, "after-dark").status == "paused"
    row = db.one("select status from channels where slug='after-dark'")
    assert row is not None and row["status"] == "paused"
    rows = db.query("select kind, note from incidents order by incident_id")
    kinds = [(r["kind"], r["note"]) for r in rows]
    assert kinds == [("pause", "Reused content notice")]
    with pytest.raises(StudioError):
        review.pause(paths, db, "ghost", "x")
    review.resume(paths, db, "after-dark")
    assert crud.get_channel(paths, "after-dark").status == "active"
    with pytest.raises(StudioError):
        review.resume(paths, db, "after-dark")
    rows = db.query("select kind from incidents order by incident_id")
    assert [r["kind"] for r in rows] == ["pause", "resume"]


def test_open_in_player_handles_missing_file(tmp_path: Path):
    assert review.open_in_player(tmp_path / "missing.mp4") is False


# ---------------------------------------------------------------- CLI
def test_cli_review_flow(tmp_home: Path):
    paths, db = _env(tmp_home)
    a = _output(paths, db, item="s1", title="First")
    b = _output(paths, db, item="s2", title="Second")
    listing = runner.invoke(app, ["review"])
    assert listing.exit_code == 0 and "First" in listing.output and "Second" in listing.output
    assert "nothing pending" in runner.invoke(app, ["review", "--channel", "nobody"]).output
    assert "[pending]" in runner.invoke(app, ["review", "show", a]).output
    assert runner.invoke(app, ["review", "approve", a, "--note", "ok"]).exit_code == 0
    bad = runner.invoke(app, ["review", "reject", b])
    assert bad.exit_code == 1 and "NS-E313" in bad.output
    assert runner.invoke(app, ["review", "reject", b, "--reason", "boring"]).exit_code == 0
    uploaded = ["mark-uploaded", "after-dark-20261101-abcd", "s1", "https://youtu.be/abcdefghijk"]
    up = runner.invoke(app, uploaded)
    assert up.exit_code == 0, up.output
    assert "nothing pending" in runner.invoke(app, ["review"]).output
    st = runner.invoke(app, ["status"])
    assert st.exit_code == 0 and "uploaded=1" in st.output and "boring" in st.output
    miss = runner.invoke(app, ["review", "show", "ghost/s1"])
    assert miss.exit_code == 1 and "NS-E313" in miss.output


def test_cli_pause_resume_and_status(tmp_home: Path):
    paths, db = _env(tmp_home)
    assert runner.invoke(app, ["pause", "after-dark"]).exit_code == 1  # reason required
    res = runner.invoke(app, ["pause", "after-dark", "--reason", "strike"])
    assert res.exit_code == 0, res.output
    status = runner.invoke(app, ["status"])
    assert "[paused]" in status.output and "strike" in status.output
    out = runner.invoke(app, ["resume", "after-dark"], input="n\n")
    assert out.exit_code == 1
    assert crud.get_channel(paths, "after-dark").status == "paused"
    ok = runner.invoke(app, ["resume", "after-dark", "--yes"])
    assert ok.exit_code == 0, ok.output
    assert crud.get_channel(paths, "after-dark").status == "active"


def test_cli_learn_import_and_milestone(tmp_home: Path, tmp_path: Path):
    paths, db = _env(tmp_home)
    oid = add_output(db, "after-dark-20261001-abcd", "s1", title="The Porch Light")
    db.execute(
        "insert into uploads(output_id, video_url, published_at) values (?,?,?)",
        (oid, "https://youtu.be/abcdefghijk", "2026-10-02T00:00:00+00:00"),
    )
    f = tmp_path / "export.csv"
    f.write_text(
        "Content,Video title,Views,Viewed vs. swiped away (%),Average percentage viewed (%)\n"
        "abcdefghijk,Renamed,500,80,90\n"
        "zzzzzzzzzzz,Never uploaded,10,50,50\n",
        encoding="utf-8",
    )
    res = runner.invoke(app, ["learn", "--import", str(f), "--date", "2026-12-19"])
    assert res.exit_code == 0, res.output
    assert "matched 1 of 2" in res.output and "unmatched: Never uploaded" in res.output
    assert "after-dark:" in res.output and "pool weights changed" in res.output
    assert db.one("select views from analytics where output_id=?", (oid,)) is not None
    ms = runner.invoke(app, ["status", "--milestone"])
    assert ms.exit_code == 0 and "milestone after-dark" in ms.output
    st = runner.invoke(app, ["status", "--channel", "after-dark"])
    assert "last import" in st.output and "top:" in st.output
    bad = runner.invoke(app, ["learn", "--import", str(tmp_path / "nope.csv")])
    assert bad.exit_code == 1 and "NS-E310" in bad.output
    badmap = runner.invoke(app, ["learn", "--import", str(f), "--map", "oops"])
    assert badmap.exit_code == 2


def test_cli_status_without_channels(tmp_home: Path):
    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0 and "no channels configured" in res.output


def test_cli_daily_dry_run_and_real(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    from ns_studio import jobs
    from ns_studio import orchestrator as orch
    from ns_studio.stages import REGISTRY

    class Stub:
        name = "d_stub"
        tenant = None
        inputs = ()
        outputs = ("o.json",)

        def run(self, ctx):
            ctx.write_json("o.json", {})

    monkeypatch.setitem(REGISTRY, "d_stub", Stub())
    monkeypatch.setitem(orch.FORMAT_STAGES, "shorts_only", ["d_stub"])
    monkeypatch.setattr(jobs, "STAGE_MODULES", ())
    monkeypatch.setattr("ns_studio.policy.gate.available", lambda: False)
    paths, db = _env(tmp_home)
    crud.edit_channel(paths, db, "after-dark", {"format": "shorts_only"})
    dry = runner.invoke(app, ["run", "--daily", "--dry-run", "--fake-services"])
    assert dry.exit_code == 0, dry.output
    assert "after-dark: 1 x shorts_only" in dry.output
    real = runner.invoke(app, ["run", "--daily", "--fake-services"])
    assert real.exit_code == 0, real.output
    assert "done: 1 stages" in real.output and "backup:" in real.output
    assert list(paths.backup_dir.glob("studio-*.db"))
    again = runner.invoke(app, ["run", "--daily", "--dry-run", "--fake-services"])
    assert "nothing due today" in again.output
    cfg = yaml.safe_load(paths.channel_file("after-dark").read_text(encoding="utf-8"))
    assert cfg["format"] == "shorts_only"
