from pathlib import Path

import pytest

from ns_common.errors import NsError
from ns_studio import orchestrator as orch
from ns_studio.stages import register
from tests.helpers import make_ctx


def _stage(name, tenant):
    class S:
        inputs = ()
        outputs = (f"{name}.json",)

        def run(self, ctx):
            ctx.write_json(f"{name}.json", {"ok": True})

    S.name, S.tenant = name, tenant
    return S()


def test_run_job_groups_tenants(tmp_home: Path, monkeypatch):
    monkeypatch.setitem(orch.FORMAT_STAGES, "test_fmt", ["t1", "t2", "t3", "t4"])
    for n, t in (("t1", "llama"), ("t2", "llama"), ("t3", "tts"), ("t4", None)):
        register(_stage(n, t))
    ctx = make_ctx(tmp_home, fmt="test_fmt")
    ran = orch.run_job(ctx)
    assert ran == ["t1", "t2", "t3", "t4"]
    ev = ctx.services.acquirer.events  # type: ignore[attr-defined]
    assert ev == [
        ("acquire", "llama", f"studio:{ctx.job_id}"),
        ("release", "llama", f"studio:{ctx.job_id}"),
        ("acquire", "tts", f"studio:{ctx.job_id}"),
        ("release", "tts", f"studio:{ctx.job_id}"),
    ]


def test_unknown_format_is_e306(tmp_home: Path):
    with pytest.raises(NsError) as ei:
        orch.plan_job("series_compilation")
    assert ei.value.code == "NS-E306"


def test_unregistered_stage_is_e306(tmp_home: Path, monkeypatch):
    monkeypatch.setitem(orch.FORMAT_STAGES, "f3", ["does_not_exist"])
    with pytest.raises(NsError) as ei:
        orch.plan_job("f3")
    assert ei.value.code == "NS-E306"


def test_dry_run_lists_without_running(tmp_home: Path, monkeypatch):
    monkeypatch.setitem(orch.FORMAT_STAGES, "f2", ["x1"])
    register(_stage("x1", None))
    ctx = make_ctx(tmp_home, fmt="f2")
    assert orch.run_job(ctx, dry_run=True) == ["x1"]
    assert not ctx.file("x1.json").exists()


def test_release_on_failure(tmp_home: Path, monkeypatch):
    class Boom:
        name = "boom"
        tenant = "tts"
        inputs = ()
        outputs = ()

        def run(self, ctx):
            raise NsError("NS-E306", "boom")

    register(Boom())
    monkeypatch.setitem(orch.FORMAT_STAGES, "f4", ["boom"])
    ctx = make_ctx(tmp_home, fmt="f4")
    with pytest.raises(NsError):
        orch.run_job(ctx)
    assert ctx.services.acquirer.events[-1][0] == "release"  # type: ignore[attr-defined]
