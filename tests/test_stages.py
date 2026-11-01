from pathlib import Path

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
