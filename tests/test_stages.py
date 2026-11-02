from pathlib import Path

import pytest

from ns_common import ffmpeg as ff
from ns_studio.stages import StageState, inputs_hash, run_stage
from tests.helpers import make_ctx


class Double:
    name = "double"
    tenant = None
    inputs = ("a.json",)
    outputs = ("b.json",)

    def __init__(self):
        self.runs = 0

    def run(self, ctx):
        self.runs += 1
        ctx.write_json("b.json", {"v": ctx.read_json("a.json")["v"] * 2})


def test_run_stage_skips_when_inputs_unchanged(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.write_json("a.json", {"v": 2})
    st = Double()
    assert run_stage(ctx, st) is True
    assert ctx.read_json("b.json") == {"v": 4}
    assert run_stage(ctx, st) is False  # skipped
    ctx.write_json("a.json", {"v": 5})
    assert run_stage(ctx, st) is True  # inputs changed
    assert st.runs == 2
    state = StageState.load(ctx.job_dir)
    assert state.is_done("double", inputs_hash(ctx, st))


def test_force_reruns(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.write_json("a.json", {"v": 1})
    st = Double()
    run_stage(ctx, st)
    assert run_stage(ctx, st, force=True) is True
    assert st.runs == 2


def test_missing_output_reruns(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.write_json("a.json", {"v": 1})
    st = Double()
    run_stage(ctx, st)
    ctx.file("b.json").unlink()
    assert run_stage(ctx, st) is True


def test_state_survives_reload(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    state = StageState.load(ctx.job_dir)
    assert state.entries == {}
    state.mark_done("s", "h1")
    state.save(ctx.job_dir)
    assert StageState.load(ctx.job_dir).is_done("s", "h1")
    assert not StageState.load(ctx.job_dir).is_done("s", "h2")


def test_a_later_stage_that_rewrites_an_input_does_not_invalidate_earlier_stages(tmp_home):
    """Review row 11: `--resume` redid the LLM stages after the duration check expanded a chapter."""
    from ns_studio.stages import REGISTRY, run_stage

    runs: list[str] = []

    class Early:
        name, tenant = "rw_early", None
        inputs, outputs = ("story_rw.json",), ("early.json",)

        def run(self, ctx):
            runs.append("early")
            ctx.write_json("early.json", {"n": 1})

    class Late:
        name, tenant = "rw_late", None
        inputs, outputs = ("early.json", "story_rw.json"), ("late.json",)
        rewrites = ("story_rw.json",)

        def run(self, ctx):
            runs.append("late")
            ctx.write_json("story_rw.json", {"text": "expanded"})
            ctx.write_json("late.json", {})

    early, late = Early(), Late()
    REGISTRY[early.name], REGISTRY[late.name] = early, late
    ctx = make_ctx(tmp_home)
    ctx.write_json("story_rw.json", {"text": "short"})
    assert run_stage(ctx, early) and run_stage(ctx, late)
    assert run_stage(ctx, early) is False  # before the fix: True, its input bytes had changed
    assert run_stage(ctx, late) is False
    assert runs == ["early", "late"]
    ctx.write_json("story_rw.json", {"text": "edited by the owner"})  # a real change still re-runs
    assert run_stage(ctx, early) is True


@pytest.mark.skipif(not ff.available(), reason="ffmpeg required")
def test_voice_leaves_parts_json_alone(tmp_home):
    """The voice stage used to write measured seconds into parts.json, an input of the LLM stages."""
    from ns_studio.media.voice import VoiceStage

    ctx = make_ctx(tmp_home)
    ctx.write_json("parts.json", [{"n": 1, "text": "One two three four five.", "hook": "", "est_seconds": 1.0}])
    before = ctx.file("parts.json").read_bytes()
    VoiceStage().run(ctx)
    assert ctx.file("parts.json").read_bytes() == before
