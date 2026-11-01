import json
import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from ns_common import ffmpeg
from ns_common.config import save_runtime
from ns_common.db import Db
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_studio import jobs
from ns_studio.cli import app
from ns_studio.orchestrator import FORMAT_STAGES
from ns_studio.stages import register
from tests.helpers import make_channel

runner = CliRunner()
CALLS: list[str] = []


class StubA:
    name = "stub_a"
    tenant = "llama"
    inputs = ()
    outputs = ("a.json",)

    def run(self, ctx):
        CALLS.append("a")
        ctx.write_json("a.json", {"n": 1})


class StubB:
    name = "stub_b"
    tenant = None
    inputs = ("a.json",)
    outputs = ("b.json",)

    def run(self, ctx):
        CALLS.append("b")
        ctx.write_json("b.json", {"n": ctx.read_json("a.json")["n"] + 1})


@pytest.fixture
def stub_stages(monkeypatch: pytest.MonkeyPatch):
    CALLS.clear()
    register(StubA())
    register(StubB())
    monkeypatch.setitem(FORMAT_STAGES, "shorts_only", ["stub_a", "stub_b"])
    monkeypatch.setattr(jobs, "STAGE_MODULES", ())
    monkeypatch.setattr("ns_studio.policy.gate.available", lambda: False)


def _job_ids(output: str) -> list[str]:
    return re.findall(r"job (\S+-\d{8}-[0-9a-f]{4}) ", output)


def test_help_lists_commands(tmp_home: Path):
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0
    for word in ("run", "channel", "voice"):
        assert word in res.output
    assert "studio" in runner.invoke(app, []).output


def test_run_fake_services_creates_jobs(tmp_home: Path, stub_stages):
    res = runner.invoke(app, ["run", "--fake-services", "--format", "shorts_only", "--count", "2"])
    assert res.exit_code == 0, res.output
    ids = _job_ids(res.output)
    assert len(ids) == 2 and ids[0] != ids[1]
    paths = Paths.from_env()
    for jid in ids:
        assert (paths.job_dir(jid) / "stage_state.json").exists()
        assert (paths.job_dir(jid) / "job.json").exists()
        assert (paths.job_dir(jid) / "b.json").exists()
    db = Db(paths.db_path)
    assert len(db.query("select * from combos")) == 2
    assert paths.channel_file("demo").exists()
    assert CALLS == ["a", "b", "a", "b"]


def test_dry_run_creates_nothing(tmp_home: Path, stub_stages):
    res = runner.invoke(app, ["run", "--dry-run", "--format", "shorts_only"])
    assert res.exit_code == 0, res.output
    assert "stub_a -> stub_b" in res.output
    assert not Paths.from_env().jobs_dir.exists() or not list(Paths.from_env().jobs_dir.iterdir())
    assert CALLS == []


def test_resume_reruns_only_missing_output(tmp_home: Path, stub_stages):
    res = runner.invoke(app, ["run", "--fake-services", "--format", "shorts_only"])
    jid = _job_ids(res.output)[0]
    job_dir = Paths.from_env().job_dir(jid)
    (job_dir / "b.json").unlink()
    CALLS.clear()
    res2 = runner.invoke(app, ["run", "--resume", jid, "--fake-services"])
    assert res2.exit_code == 0, res2.output
    assert CALLS == ["b"]
    assert (job_dir / "b.json").exists()


def test_resume_unknown_job_fails(tmp_home: Path, stub_stages):
    res = runner.invoke(app, ["run", "--resume", "nope-20260101-abcd", "--fake-services"])
    assert res.exit_code == 1
    assert "NS-E326" in res.output


def test_fake_subset_is_accepted_and_bad_name_rejected(tmp_home: Path, stub_stages):
    ok = runner.invoke(app, ["run", "--fake-services=llm,tts", "--format", "shorts_only"])
    assert ok.exit_code == 0, ok.output
    bad = runner.invoke(app, ["run", "--fake-services=banana", "--format", "shorts_only"])
    assert bad.exit_code == 2


def test_ns_error_exits_one_with_code(tmp_home: Path, stub_stages):
    res = runner.invoke(app, ["run", "--fake-services", "--format", "series_compilation"])
    assert res.exit_code == 1
    assert "NS-E306" in res.output


def test_bad_format_value_is_usage_error(tmp_home: Path, stub_stages):
    assert runner.invoke(app, ["run", "--format", "banana"]).exit_code == 2


def test_no_channel_without_fakes_fails_clearly(tmp_home: Path, stub_stages):
    paths = Paths.from_env()
    paths.ensure()

    save_runtime(paths, Runtime.default("cpu-dev"))
    res = runner.invoke(app, ["run", "--format", "shorts_only"])
    assert res.exit_code == 1
    assert "NS-E322" in res.output


# ---------------------------------------------------------------- channel and voice
def _add(slug: str = "after-dark", niche: str = "original short horror fiction") -> list[str]:
    return ["channel", "add", "--slug", slug, "--name", slug.title(), "--niche", niche, "--keywords", "a,b"]


def test_channel_add_list_and_duplicate(tmp_home: Path):
    res = runner.invoke(app, _add())
    assert res.exit_code == 0, res.output
    lst = runner.invoke(app, ["channel", "list"])
    assert "after-dark" in lst.output and "pairs=16" in lst.output
    dup = runner.invoke(app, _add())
    assert dup.exit_code == 1


def test_channel_add_requires_distinct_voice(tmp_home: Path):
    assert runner.invoke(app, _add()).exit_code == 0
    clash = runner.invoke(app, _add("clone"))
    assert clash.exit_code == 1 and "NS-E321" in clash.output
    ok = runner.invoke(app, [*_add("clone"), "--speaker", "Aiden"])
    assert ok.exit_code == 0, ok.output


def test_channel_add_from_yaml_enforces_minimums(tmp_home: Path, tmp_path: Path):
    data = make_channel().model_dump(mode="json")
    data["pools"]["hooks"] = ["a", "b"]
    f = tmp_path / "bad.yaml"
    f.write_text(yaml.safe_dump(data), encoding="utf-8")
    res = runner.invoke(app, ["channel", "add", "--from-yaml", str(f)])
    assert res.exit_code == 1
    assert "NS-E320" in res.output and "hooks" in res.output


def test_channel_add_missing_fields_is_usage_error(tmp_home: Path):
    assert runner.invoke(app, ["channel", "add", "--slug", "x"]).exit_code == 2


def test_channel_edit_and_validate(tmp_home: Path):
    runner.invoke(app, _add())
    ok = runner.invoke(app, ["channel", "edit", "after-dark", "--set", "critic_bar=8.5"])
    assert ok.exit_code == 0, ok.output
    paths = Paths.from_env()
    assert yaml.safe_load(paths.channel_file("after-dark").read_text(encoding="utf-8"))["critic_bar"] == 8.5
    bad = runner.invoke(app, ["channel", "edit", "after-dark", "--set", 'pools.hooks=["a"]'])
    assert bad.exit_code == 1 and "NS-E320" in bad.output
    small = runner.invoke(
        app,
        ["channel", "edit", "after-dark", "--set", 'pools.hooks=["a","b","c"]', "--set",
         'pools.structures=["x","y","z"]'],
    )
    assert small.exit_code == 0, small.output
    val = runner.invoke(app, ["channel", "validate"])
    assert val.exit_code == 0 and "warning" in val.output and "after-dark: ok" in val.output
    assert runner.invoke(app, ["channel", "edit", "after-dark"]).exit_code == 2
    assert runner.invoke(app, ["channel", "edit", "ghost", "--set", "critic_bar=7"]).exit_code == 1


def test_channel_validate_unknown_slug(tmp_home: Path):
    res = runner.invoke(app, ["channel", "validate", "ghost"])
    assert res.exit_code == 1 and "NS-E322" in res.output


def test_voice_list_and_preview_with_fakes(tmp_home: Path):
    lst = runner.invoke(app, ["voice", "list", "--fake-services"])
    assert lst.exit_code == 0 and "Ryan" in lst.output
    out = tmp_home / "p.wav"
    res = runner.invoke(app, ["voice", "preview", "Ryan", "--fake-services", "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert out.exists() and out.stat().st_size > 1000


def test_master_first_plan_includes_short_combos_and_gates(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    jobs.load_stage_modules()
    monkeypatch.setitem(FORMAT_STAGES, "master_first", list(FORMAT_STAGES["master_first"]))
    monkeypatch.setattr(jobs, "STAGE_MODULES", ())
    res = runner.invoke(app, ["run", "--dry-run", "--format", "master_first", "--policy"])
    assert res.exit_code == 0, res.output
    plan = res.output.split("plan:")[1].split("->")
    names = [p.strip() for p in plan]
    assert names.index("short_combos") == names.index("shorts_select") + 1
    assert names.index("policy_text") > names.index("seo_shorts")
    assert "policy_final" in names


@pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")
def test_full_fake_shorts_pipeline_with_policy_gates(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    jobs.load_stage_modules()
    monkeypatch.setitem(FORMAT_STAGES, "shorts_only", list(FORMAT_STAGES["shorts_only"]))
    monkeypatch.setattr(jobs, "STAGE_MODULES", ())
    res = runner.invoke(app, ["run", "--fake-services", "--format", "shorts_only", "--policy"])
    assert res.exit_code == 0, res.output
    paths = Paths.from_env()
    job_dir = next(paths.jobs_dir.iterdir())
    for name in ("policy_text.json", "policy_final.json"):
        data = json.loads((job_dir / name).read_text(encoding="utf-8"))
        assert data["passed"] is True, data
    assert list(paths.outbox.rglob("*.mp4"))
