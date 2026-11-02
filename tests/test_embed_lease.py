"""Gap 4: stages that embed text hold the `embed` service while they run."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ns_common.errors import NsError
from ns_studio import jobs
from ns_studio import orchestrator as orch
from ns_studio.stages import REGISTRY, needs_embed, register
from tests.helpers import make_ctx
from tests.test_studio_acquirer import FakeClient, make


def _stage(name: str, tenant, *, embeds: bool, boom: bool = False):
    class S:
        inputs = ()
        outputs = (f"{name}.json",)

        def run(self, ctx):
            if boom:
                raise NsError("NS-E303", "boom")
            ctx.write_json(f"{name}.json", {"ok": True})

    S.name, S.tenant = name, tenant
    if embeds:
        S.uses_embed = True  # type: ignore[attr-defined]
    return S()


def test_embed_is_leased_around_the_stage_inside_the_llama_tenant(tmp_home: Path, monkeypatch):
    register(_stage("e1", "llama", embeds=True))
    register(_stage("e2", "llama", embeds=False))
    register(_stage("e3", None, embeds=True))
    monkeypatch.setitem(orch.FORMAT_STAGES, "embed_fmt", ["e1", "e2", "e3"])
    ctx = make_ctx(tmp_home, fmt="embed_fmt")
    orch.run_job(ctx)
    owner = f"studio:{ctx.job_id}"
    assert ctx.services.acquirer.events == [  # type: ignore[attr-defined]
        ("acquire", "llama", owner),
        ("acquire", "embed", owner),
        ("release", "embed", owner),
        ("release", "llama", owner),
        ("acquire", "embed", owner),  # a stage with no GPU tenant still gets the lease
        ("release", "embed", owner),
    ]


def test_embed_is_released_when_the_stage_fails(tmp_home: Path, monkeypatch):
    register(_stage("e4", "llama", embeds=True, boom=True))
    monkeypatch.setitem(orch.FORMAT_STAGES, "embed_fmt2", ["e4"])
    ctx = make_ctx(tmp_home, fmt="embed_fmt2")
    with pytest.raises(NsError):
        orch.run_job(ctx)
    kinds = [(e[0], e[1]) for e in ctx.services.acquirer.events]  # type: ignore[attr-defined]
    assert kinds == [("acquire", "llama"), ("acquire", "embed"), ("release", "embed"), ("release", "llama")]


def test_the_supervisor_is_asked_to_start_embed(tmp_home: Path, monkeypatch):
    register(_stage("e5", "llama", embeds=True))
    monkeypatch.setitem(orch.FORMAT_STAGES, "embed_fmt3", ["e5"])
    ctx = make_ctx(tmp_home, fmt="embed_fmt3")
    acq, client, _ = make(tmp_home, FakeClient())
    ctx.services.acquirer = acq
    orch.run_job(ctx)
    assert [(c[0], c[1]) for c in client.calls] == [
        ("acquire", "llama"), ("acquire", "embed"), ("release", "embed"), ("release", "llama"),
    ]


def test_every_stage_that_embeds_declares_it(repo_root: Path):
    """A stage added later that calls `ctx.services.embed` without `uses_embed` would hit a closed port."""
    missing: list[str] = []
    for sub in ("text", "policy", "series", "media", "clipper", "variation"):
        for path in sorted((repo_root / "ns_studio" / sub).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
                body = ast.unparse(cls)
                is_stage = any(
                    isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "tenant" for n in cls.body
                )
                if not is_stage or "services.embed" not in body or "def run(" not in body:
                    continue
                declared = any(
                    isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "uses_embed"
                    for n in cls.body
                )
                if not declared:
                    missing.append(f"{path.relative_to(repo_root).as_posix()}:{cls.name}")
    assert not missing, missing


def test_real_text_stages_are_flagged():
    jobs.load_stage_modules()
    from ns_studio.policy.gate import PolicyTextStage

    register(PolicyTextStage())
    for name in ("ideas", "critic_revise", "chapters_critic", "continuity", "policy_text", "script_import"):
        assert needs_embed(REGISTRY[name]), name
    for name in ("plan", "draft", "voice", "render"):
        assert not needs_embed(REGISTRY[name]), name
