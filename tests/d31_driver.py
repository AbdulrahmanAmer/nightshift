"""A tiny fake-services nightly for tests/test_interrupted_nightly.py (defect D-31).

It runs the real `daily.run_daily` (planner, job, stage runner, run row) on a home made by the test, with
three stub stages in place of the story pipeline, so a killed night can be shown without a model or FFmpeg:

    NIGHTSHIFT_HOME=<home> D31_HANG_AT=d31_b D31_READY=<file> python -m tests.d31_driver

At the stage named in `D31_HANG_AT` it writes `D31_READY` and then waits to be killed (a power cut).
Without it the night runs to the end and the last stage records the finished video in `outputs`.
"""

from __future__ import annotations

import os
import random
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ns_studio import daily, jobs
from ns_studio import orchestrator as orch
from ns_studio.stages import REGISTRY

STAGES = ("d31_a", "d31_b", "d31_c")
CALLS_FILE = "d31-calls.log"


def calls(home: Path) -> list[str]:
    """Every `<stage>:start` and `<stage>:done` the stub stages wrote, in order, across processes."""
    f = home / CALLS_FILE
    return f.read_text(encoding="utf-8").split() if f.exists() else []


class _Stub:
    tenant = None

    def __init__(self, name: str, inputs: tuple[str, ...], output: str, *, last: bool = False) -> None:
        self.name, self.inputs, self.outputs, self.last = name, inputs, (output,), last

    def run(self, ctx: Any) -> None:
        with (ctx.paths.home / CALLS_FILE).open("a", encoding="utf-8") as fh:
            fh.write(f"{self.name}:start\n")
        if os.environ.get("D31_HANG_AT") == self.name:
            if os.environ.get("D31_TREE"):  # tests/test_night_kill_tree.py: the stage's children, then ready
                from tests import night_tree

                night_tree.start_children(Path(os.environ["D31_TREE"]), ctx.job_dir / "render.mp4")
            Path(os.environ["D31_READY"]).write_text(self.name, encoding="utf-8")
            time.sleep(600)  # the test kills this process long before
        ctx.write_json(self.outputs[0], {"stage": self.name})
        if self.last:
            ctx.db.execute(
                "insert or ignore into outputs(output_id, job_id, channel, kind, title, path, created_at)"
                " values (?,?,?,?,?,?,?)",
                (
                    f"{ctx.job_id}/long",
                    ctx.job_id,
                    ctx.channel.slug,
                    "long",
                    "A finished video",
                    str(ctx.file(self.outputs[0])),
                    datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )
        with (ctx.paths.home / CALLS_FILE).open("a", encoding="utf-8") as fh:
            fh.write(f"{self.name}:done\n")


def install(monkeypatch: Any = None) -> None:
    """Register the stub stages as the plan of `master_first` (through `monkeypatch` when given)."""
    stubs = {
        "d31_a": _Stub("d31_a", (), "d31_a.json"),
        "d31_b": _Stub("d31_b", ("d31_a.json",), "d31_b.json"),
        "d31_c": _Stub("d31_c", ("d31_b.json",), "d31_c.json", last=True),
    }
    for name, stub in stubs.items():
        if monkeypatch is not None:
            monkeypatch.setitem(REGISTRY, name, stub)
        else:
            REGISTRY[name] = stub
    if monkeypatch is not None:
        monkeypatch.setitem(orch.FORMAT_STAGES, "master_first", list(STAGES))
    else:
        orch.FORMAT_STAGES["master_first"] = list(STAGES)


def main() -> None:
    """One nightly run with the stub stages (fake services, the planner and the run row are the real ones)."""
    install()
    env = jobs.open_env(light=True)
    daily.run_daily(env, rng=random.Random(1))


if __name__ == "__main__":
    main()
