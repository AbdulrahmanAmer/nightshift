"""Stage protocol, idempotent stage state file and registry (component C7)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from ns_common.atomic import read_text_retry, write_text_atomic
from ns_studio.context import JobContext

Tenant = Literal["llama", "tts", "whisper"] | None
STATE_FILE = "stage_state.json"


class Stage(Protocol):
    """One idempotent unit of a job: reads `inputs`, writes `outputs` in the job directory."""

    name: str
    tenant: Tenant
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]

    def run(self, ctx: JobContext) -> None:
        """Do the work; raise NsError on failure."""
        ...


@dataclass
class StageState:
    """Contents of `stage_state.json`: per stage status, finish time and inputs hash."""

    entries: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, job_dir: Path) -> StageState:
        """Load the state file from `job_dir` (empty state when absent)."""
        f = job_dir / STATE_FILE
        return cls(json.loads(read_text_retry(f))) if f.exists() else cls()

    def save(self, job_dir: Path) -> None:
        """Write the state file into `job_dir`."""
        write_text_atomic(job_dir / STATE_FILE, json.dumps(self.entries, indent=2) + "\n")  # resume state

    def mark_done(self, name: str, h: str) -> None:
        """Record that stage `name` finished with inputs hash `h`."""
        self.entries[name] = {
            "status": "done",
            "finished_at": datetime.now(UTC).isoformat(),
            "inputs_hash": h,
        }

    def is_done(self, name: str, h: str) -> bool:
        """True when stage `name` finished earlier with the same inputs hash."""
        e = self.entries.get(name)
        return bool(e and e.get("status") == "done" and e.get("inputs_hash") == h)


def inputs_hash(ctx: JobContext, stage: Stage) -> str:
    """SHA-256 over the names and bytes of every input file of `stage` that exists."""
    hsh = hashlib.sha256()
    for name in stage.inputs:
        p = ctx.file(name)
        hsh.update(name.encode())
        if p.exists():
            hsh.update(p.read_bytes() if p.is_file() else b"dir")
    return hsh.hexdigest()


def run_stage(ctx: JobContext, stage: Stage, *, force: bool = False) -> bool:
    """Run `stage` unless its outputs exist and its inputs are unchanged; True when it ran."""
    h = inputs_hash(ctx, stage)
    outputs_exist = all(ctx.file(o).exists() for o in stage.outputs)
    if not force and outputs_exist and StageState.load(ctx.job_dir).is_done(stage.name, h):
        ctx.log.info("stage_skip", stage=stage.name, job_id=ctx.job_id)
        recheck = getattr(stage, "recheck_on_skip", None)  # a gate that reads state outside its inputs
        if recheck is not None:
            recheck(ctx)
        return False
    with ctx.log.timed("stage", stage=stage.name, job_id=ctx.job_id, tenant=stage.tenant):
        stage.run(ctx)
    rewrites = rewritten_inputs(stage)
    if rewrites:  # the stage edited its own inputs on purpose (expansion): record the state it left behind
        h = inputs_hash(ctx, stage)
    state = StageState.load(ctx.job_dir)
    state.mark_done(stage.name, h)
    if rewrites:
        refresh_upstream_hashes(ctx, state, rewrites, skip=stage.name)
    state.save(ctx.job_dir)
    return True


def rewritten_inputs(stage: Stage) -> tuple[str, ...]:
    """Files `stage` legitimately edits although earlier stages read them (class attribute `rewrites`)."""
    return tuple(getattr(stage, "rewrites", ()))


def refresh_upstream_hashes(ctx: JobContext, state: StageState, files: tuple[str, ...], *, skip: str) -> None:
    """Re-record the inputs hash of every finished stage that reads one of `files`.

    A later stage (the duration check expanding a chapter) rewrites `story.json` and the plans in a way that
    keeps them consistent with each other. Without this, `--resume` sees the new bytes as a changed input and
    redoes the LLM stages that produced the titles and the Shorts selection.
    """
    for name, entry in state.entries.items():
        other = REGISTRY.get(name)
        if name == skip or other is None or entry.get("status") != "done":
            continue
        if set(other.inputs) & set(files):
            entry["inputs_hash"] = inputs_hash(ctx, other)


def needs_embed(stage: Stage) -> bool:
    """True for a stage whose code calls the embed service (idea dedupe, validators, self-similarity).

    Declared with a class attribute `uses_embed = True`; it is not part of the `Stage` protocol so stages
    that never embed stay unchanged.
    """
    return bool(getattr(stage, "uses_embed", False))


REGISTRY: dict[str, Stage] = {}


def register(stage: Stage) -> Stage:
    """Add `stage` to the global registry (keyed by name) and return it."""
    REGISTRY[stage.name] = stage
    return stage
