import importlib.util
import json
import re
from pathlib import Path

import pytest
import typer
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
from ns_studio.stages import REGISTRY, register
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


def test_run_holds_the_pc_awake_until_the_jobs_are_done(
    tmp_home: Path, stub_stages, monkeypatch: pytest.MonkeyPatch
):
    """Gap L2: a long `studio run` must not be interrupted by Windows' sleep timer."""
    from contextlib import contextmanager

    from ns_studio import cli

    held: list[str] = []

    @contextmanager
    def fake_awake():
        held.append("hold")
        try:
            yield
        finally:
            held.append("release")

    monkeypatch.setattr(cli, "keep_awake", fake_awake)
    res = runner.invoke(app, ["run", "--fake-services", "--format", "shorts_only"])
    assert res.exit_code == 0, res.output
    assert held == ["hold", "release"] and CALLS == ["a", "b"]


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


def test_master_first_plan_includes_short_combos_and_gates(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(FORMAT_STAGES, "master_first", list(FORMAT_STAGES["master_first"]))
    assert jobs.prepare_plan("master_first", True) is True
    names = FORMAT_STAGES["master_first"]
    assert names.index("short_combos") == names.index("shorts_select") + 1
    assert names.index("policy_text") == names.index("seo_shorts") + 1
    assert names.index("policy_final") == names.index("outbox") - 1


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


def test_fake_llm_jobs_pin_the_fixture_budget(tmp_home: Path):
    env = jobs.open_env(frozenset({"llm"}))
    ch = jobs.ensure_demo_channel(env)
    for _ in range(12):
        ctx = jobs.make_job(env, ch, "shorts_only")
        assert ctx.combo.length_bucket == 50 and ctx.combo.pacing == "standard"
    explicit = jobs.make_job(env, ch, "shorts_only", fixed={"length_bucket": "35"})
    assert explicit.combo.length_bucket == 35 and explicit.combo.pacing == "standard"


def test_real_llm_jobs_are_not_pinned(tmp_home: Path):
    env = jobs.open_env(frozenset({"tts"}))
    ch = jobs.ensure_demo_channel(env)
    seen = {jobs.make_job(env, ch, "shorts_only").combo.length_bucket for _ in range(30)}
    assert len(seen) > 1


# ---------------------------------------------------------------- mounted sub-apps (CR-C8-5, C11-3, C9-7)
def _plain(text: str) -> str:
    """Help output without the colour codes rich puts between the dashes of an option name."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_clip_help_is_mounted(tmp_home: Path):
    res = runner.invoke(app, ["clip", "--help"])
    assert res.exit_code == 0, res.output
    assert "--channel" in _plain(res.output)


def test_series_help_is_mounted(tmp_home: Path):
    res = runner.invoke(app, ["series", "--help"])
    assert res.exit_code == 0, res.output
    for word in ("start", "produce", "status"):
        assert word in res.output


@pytest.mark.skipif(importlib.util.find_spec("fastapi") is None, reason="fastapi not installed")
def test_ask_help_is_mounted(tmp_home: Path):
    res = runner.invoke(app, ["ask", "--help"])
    assert res.exit_code == 0, res.output
    assert "--yes" in _plain(res.output)


def test_ask_without_dashboard_prints_a_plain_message(tmp_home: Path):
    from ns_studio import cli

    parent = typer.Typer()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(importlib.util, "find_spec", lambda name, *a, **k: None)
        assert cli.mount_ask(parent) is False
    res = runner.invoke(parent, ["what is waiting?"])
    assert res.exit_code == 1
    assert "fastapi" in res.output


def test_fake_flag_is_rewritten_for_value_commands_only():
    from ns_studio.cli import normalize_fake_flag

    assert normalize_fake_flag(["run", "--fake-services"]) == ["run", "--fake-services=all"]
    clip_args = ["clip", "v.mp4", "--fake-services"]
    assert normalize_fake_flag(clip_args) == ["clip", "v.mp4", "--fake-services=all"]
    assert normalize_fake_flag(["series", "list", "--fake-services"]) == ["series", "list", "--fake-services"]
    assert normalize_fake_flag(["ask", "hi", "--fake-services"]) == ["ask", "hi", "--fake-services"]


def test_series_stage_module_registers_script_import():
    assert "ns_studio.series.stages" in jobs.STAGE_MODULES
    assert "ns_studio.series.stages" in jobs.load_stage_modules()
    assert "script_import" in REGISTRY


def test_policy_gates_also_cover_the_script_plan(monkeypatch: pytest.MonkeyPatch):
    for fmt in ("master_first", "master_from_script"):
        monkeypatch.setitem(FORMAT_STAGES, fmt, list(FORMAT_STAGES[fmt]))
    assert jobs.prepare_plan("master_first", True) is True
    names = FORMAT_STAGES["master_from_script"]
    assert names.index("policy_text") == names.index("seo_shorts") + 1
    assert names.index("policy_final") == names.index("outbox") - 1
    assert names.index("short_combos") == names.index("script_import") + 1  # series Shorts get combos too
