from pathlib import Path

from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_studio.context import FAKEABLE, build_services
from tests.helpers import make_ctx


def test_build_all_fakes(tmp_home: Path):
    p = Paths.from_env()
    p.ensure()
    s = build_services(Runtime.default("cpu-dev"), fake=set(FAKEABLE), paths=p)
    assert s.llm.health() and s.tts.health() and s.embed.health()
    assert s.clip_sources and s.clip_sources[0].name == "fake"
    assert s.transcriber.name == "fake"


def test_build_real_services_do_not_connect(tmp_home: Path):
    p = Paths.from_env()
    keys = {"pexels": "k1", "pixabay": "k2"}
    s = build_services(Runtime.default("cpu-dev"), fake=set(), paths=p, keys=keys)
    assert [c.name for c in s.clip_sources] == ["pexels", "pixabay"]
    assert s.transcriber.name == "whisper"


def test_job_context_json_helpers(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.write_json("sub/a.json", {"x": [1, 2], "t": "é"})
    assert ctx.read_json("sub/a.json") == {"x": [1, 2], "t": "é"}
    assert ctx.file("sub/a.json").parent == ctx.subdir("sub")
    assert ctx.subdir("new").is_dir()


def test_job_context_format_literal_names_every_registered_format():
    from ns_studio import jobs
    from ns_studio.context import JobContext
    from ns_studio.orchestrator import FORMAT_STAGES

    annotation = str(JobContext.__dataclass_fields__["fmt"].type)
    for fmt in ("master_first", "shorts_only", "series_compilation", "clip", "master_from_script"):
        assert f'"{fmt}"' in annotation or f"'{fmt}'" in annotation, fmt
        assert fmt in FORMAT_STAGES
    assert set(jobs.FORMATS) <= set(FORMAT_STAGES)


def test_clip_and_script_contexts_build(tmp_home: Path):
    for fmt in ("clip", "master_from_script"):
        assert make_ctx(tmp_home, fmt=fmt).fmt == fmt


def test_prompt_versions_survive_a_resume(tmp_home):
    """Review row 12: the lineage lived in memory only, so a resumed job's metadata listed a few prompts."""
    from ns_studio import jobs
    from ns_studio.text.prompts import call_json
    from ns_studio.text.stages_shorts import ideas_budget
    from tests.helpers import yaml_only_channel

    env = jobs.open_env(frozenset(FAKEABLE))
    ctx = jobs.make_job(env, yaml_only_channel(Paths.from_env()), "shorts_only")
    call_json(ctx, "ideas", n=8, budget=ideas_budget(ctx), avoid_premises=[])
    assert ctx.file("prompt_versions.json").exists()
    resumed = jobs.load_job(env, ctx.job_id)  # a new process: nothing in memory
    assert resumed.prompt_versions == {}
    assert resumed.versions() == ctx.versions() and "ideas" in resumed.versions()
    resumed.note_prompt("hook", 3)
    assert set(resumed.versions()) == {"ideas", "hook"} and resumed.versions()["hook"] == 3
    assert jobs.load_job(env, ctx.job_id).versions() == resumed.versions()
