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
