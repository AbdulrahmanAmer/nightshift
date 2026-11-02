import csv
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from ns_common.db import Db
from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_studio import jobs
from ns_studio.calibrate import run as cal
from ns_studio.channels import crud
from ns_studio.cli import app
from ns_studio.errors import StudioError
from tests.helpers import make_channel, make_ctx

STORY = " ".join(["The porch light flickered twice before the knocking started again."] * 3)


def _env(tmp_home: Path) -> tuple[Paths, Db]:
    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    crud.add_channel(paths, db, make_channel())
    return paths, db


def _critique(avg: float) -> dict:
    return {"scores": {k: avg for k in cal.RUBRIC}, "fixes": []}


def test_spearman_known_values():
    assert cal.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert cal.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert cal.spearman([1, 1, 2], [1, 2, 3]) == pytest.approx(0.8660254, abs=1e-6)
    assert cal.spearman([5, 5, 5], [1, 2, 3]) == 0.0
    assert cal.spearman([1], [1]) == 0.0
    with pytest.raises(ValueError):
        cal.spearman([1, 2], [1])


def test_ranks_average_ties():
    assert cal._ranks([10, 20, 20, 30]) == [1.0, 2.5, 2.5, 4.0]


def test_refusal_like():
    assert cal.refusal_like("")
    assert cal.refusal_like("I'm sorry, I can't help with that request. " * 5)
    assert cal.refusal_like("Short.")
    assert not cal.refusal_like(STORY)


def test_recommend_bar_meets_80_20_target():
    critic = [9.0, 8.8, 8.5, 8.2, 7.9, 6.0, 6.5, 7.0, 7.2, 8.4]
    human_avg = [8.5] * 5 + [6.0] * 5
    human_min = [7.0] * 5 + [4.0] * 5
    rec = cal.recommend_bar(critic, human_avg, human_min)
    assert rec.satisfied
    assert rec.bar == pytest.approx(8.2)
    assert rec.good_pass_rate >= 0.8 and rec.bad_pass_rate <= 0.2


def test_recommend_bar_unsatisfiable_returns_best_gap():
    critic = [8.0, 8.0, 8.0, 8.0]
    human_avg = [9.0, 9.0, 5.0, 5.0]
    human_min = [7.0, 7.0, 3.0, 3.0]
    rec = cal.recommend_bar(critic, human_avg, human_min)
    assert not rec.satisfied
    assert rec.bar == pytest.approx(8.0)
    assert cal.recommend_bar([], [], []).satisfied is False


def test_generate_flags_refusals_and_critic_failures(tmp_home: Path):
    texts = iter([STORY, STORY, "I'm sorry, I can't write that. " * 6, STORY])
    critics = iter([_critique(8.0), _critique(7.0), "boom"])

    def gen(ctx):
        return next(texts)

    def crit(ctx, text):
        c = next(critics)
        if c == "boom":
            raise NsError("NS-E302", "bad json")
        return c

    stories = cal.generate(lambda i: make_ctx(tmp_home), 4, generator=gen, critic=crit)
    assert [s.story_id for s in stories] == ["cal-01", "cal-02", "cal-03", "cal-04"]
    assert [s.refusal for s in stories] == [False, False, True, True]
    assert stories[0].critic_avg == pytest.approx(8.0)
    assert "NS-E302" in stories[3].note


def test_default_generator_without_c5_stages_is_e306(tmp_home: Path):
    from ns_studio.stages import REGISTRY

    if "ideas" in REGISTRY:
        pytest.skip("C5 stages are registered in this process")
    with pytest.raises(NsError) as ei:
        cal.default_generator(make_ctx(tmp_home))
    assert ei.value.code == "NS-E306"


def _make_stories(avgs: list[float]) -> list[cal.CalibrationStory]:
    out = []
    for i, a in enumerate(avgs, 1):
        out.append(cal.CalibrationStory(f"cal-{i:02d}", STORY, {k: a for k in cal.RUBRIC}, a))
    return out


def _write_human(path: Path, template: Path, human: dict[str, float]) -> None:
    with template.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["story_id", *cal.RUBRIC])
        for r in rows:
            if r["story_id"] in human:
                w.writerow([r["story_id"], *([human[r["story_id"]]] * len(cal.RUBRIC))])
            else:
                w.writerow([r["story_id"], *([""] * len(cal.RUBRIC))])


def test_write_generation_files(tmp_home: Path):
    paths, db = _env(tmp_home)
    stories = _make_stories([8.0, 7.0, 9.0])
    stories.append(cal.CalibrationStory("cal-04", "I cannot help", refusal=True, note="refusal"))
    d = cal.write_generation(paths, db, "after-dark", stories)
    assert (d / "stories.csv").exists() and (d / "cal-01.txt").exists() and (d / "cal-04.txt").exists()
    header = (d / "scores.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header == "story_id," + ",".join(cal.RUBRIC)
    assert len(db.query("select * from calibration where channel=?", ("after-dark",))) == 4


def test_score_round_trip_updates_channel_bar(tmp_home: Path):
    paths, db = _env(tmp_home)
    critic = [9.0, 8.8, 8.5, 8.2, 7.9, 6.0, 6.5, 7.0, 7.2, 8.4]
    stories = _make_stories(critic)
    stories.append(cal.CalibrationStory("cal-11", "I cannot help", refusal=True))
    d = cal.write_generation(paths, db, "after-dark", stories)
    human = {f"cal-{i:02d}": (8.5 if i <= 5 else 6.0) for i in range(1, 11)}
    scores = tmp_home / "human.csv"
    _write_human(scores, d / "scores.csv", human)
    rep = cal.score(paths, db, "after-dark", scores)
    assert rep.rows == 10 and rep.satisfied
    assert rep.overall_spearman > 0.7
    assert rep.refusal_rate == pytest.approx(1 / 11, abs=1e-3)
    assert rep.two_model_recommended
    saved = yaml.safe_load(paths.channel_file("after-dark").read_text(encoding="utf-8"))
    assert saved["critic_bar"] == pytest.approx(rep.recommended_bar)
    assert (d / "calibration_report.json").exists()
    sql = "select human_json from calibration where channel=? and story_id=?"
    row = db.one(sql, ("after-dark", "cal-01"))
    assert row is not None and row["human_json"]


def test_score_error_paths(tmp_home: Path):
    paths, db = _env(tmp_home)
    with pytest.raises(StudioError) as ei:
        cal.score(paths, db, "after-dark", tmp_home / "none.csv")
    assert ei.value.code == "NS-E324"
    d = cal.write_generation(paths, db, "after-dark", _make_stories([8.0, 7.0, 9.0]))
    few = tmp_home / "few.csv"
    _write_human(few, d / "scores.csv", {"cal-01": 8.0, "cal-02": 7.0})
    with pytest.raises(StudioError) as ei2:
        cal.score(paths, db, "after-dark", few)
    assert "at least" in ei2.value.detail
    bad = tmp_home / "bad.csv"
    _write_human(bad, d / "scores.csv", {"cal-01": 11.0})
    with pytest.raises(StudioError):
        cal.score(paths, db, "after-dark", bad)
    with pytest.raises(StudioError):
        cal._read_rows(tmp_home / "missing.csv")


def test_cli_generate_then_score(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    runner = CliRunner()
    monkeypatch.setattr(jobs, "STAGE_MODULES", ())
    counter = {"i": 0}

    def gen(ctx):
        counter["i"] += 1
        return STORY

    def crit(ctx, text):
        counter["i"] += 1
        return _critique(9.0 if counter["i"] % 4 < 2 else 6.0)

    monkeypatch.setattr(cal, "default_generator", gen)
    monkeypatch.setattr(cal, "default_critic", crit)
    add = runner.invoke(app, ["channel", "add", "--slug", "after-dark", "--name", "AD", "--niche", "horror"])
    assert add.exit_code == 0, add.output
    res = runner.invoke(app, ["calibrate", "--channel", "after-dark", "--n", "8", "--fake-services"])
    assert res.exit_code == 0, res.output
    assert "wrote 8 stories" in res.output and "refusal rate so far: 0.0%" in res.output
    d = Paths.from_env().calibration_dir / "after-dark"
    human = {f"cal-{i:02d}": (9.0 if i % 2 == 0 else 5.0) for i in range(1, 9)}
    scores = tmp_home / "human.csv"
    _write_human(scores, d / "scores.csv", human)
    res2 = runner.invoke(app, ["calibrate", "--channel", "after-dark", "--score", str(scores)])
    assert res2.exit_code == 0, res2.output
    assert "recommended critic_bar" in res2.output and "spearman overall" in res2.output


def test_cli_generate_takes_the_embed_lease(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    """Review row 7: calibrate held only llama; with embed stopped every story failed NS-E301 as a refusal."""
    from ns_common.acquire import DevAcquirer

    events: list[tuple[str, str]] = []
    real = DevAcquirer.acquire

    def spy(self, service, owner, **kw):
        events.append((service, "acquire"))
        return real(self, service, owner, **kw)

    monkeypatch.setattr(DevAcquirer, "acquire", spy)
    monkeypatch.setattr(jobs, "STAGE_MODULES", ())
    monkeypatch.setattr(cal, "default_generator", lambda ctx: STORY)
    monkeypatch.setattr(cal, "default_critic", lambda ctx, text: _critique(9.0))
    runner = CliRunner()
    runner.invoke(app, ["channel", "add", "--slug", "after-dark", "--name", "AD", "--niche", "horror"])
    res = runner.invoke(app, ["calibrate", "--channel", "after-dark", "--n", "2", "--fake-services"])
    assert res.exit_code == 0, res.output
    assert events == [("llama", "acquire"), ("embed", "acquire")]


def test_two_model_advice_needs_a_story_model(tmp_home: Path):
    """Review gap 14: the CLI advised a mode that cannot be switched on without story_model_path."""
    runner = CliRunner()
    runner.invoke(app, ["channel", "add", "--slug", "after-dark", "--name", "AD", "--niche", "horror"])
    stories = _make_stories([9.0, 6.0] * 5) + [cal.CalibrationStory("cal-11", "", refusal=True)]
    paths = Paths.from_env()
    db = Db(paths.db_path)
    db.migrate()
    d = cal.write_generation(paths, db, "after-dark", stories)
    scores = tmp_home / "human.csv"
    _write_human(scores, d / "scores.csv", {f"cal-{i:02d}": (9.0 if i % 2 else 5.0) for i in range(1, 11)})
    res = runner.invoke(app, ["calibrate", "--channel", "after-dark", "--score", str(scores)])
    assert res.exit_code == 0, res.output
    assert "no story model is configured" in res.output and "switch to two-model mode" not in res.output


def test_cli_score_without_generation_fails(tmp_home: Path):
    runner = CliRunner()
    add = runner.invoke(app, ["channel", "add", "--slug", "x-ch", "--name", "X", "--niche", "n"])
    assert add.exit_code == 0
    res = runner.invoke(app, ["calibrate", "--channel", "x-ch", "--score", str(tmp_home / "s.csv")])
    assert res.exit_code == 1 and "NS-E324" in res.output
