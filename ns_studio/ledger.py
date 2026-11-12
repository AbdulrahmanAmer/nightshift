"""The night ledger: where the time of every night, job, stage and wait for the graphics card goes.

Card night-fits. One JSON line per event, one file a month: `data/ledger/ledger-<yyyymm>.jsonl`. Every route
that runs a stage writes its `stage` line (the nightly, `studio run`, a dashboard job, a Retry, a series
part), tagged with its `origin`, so the estimates the nightly plans with (`nightclock`) come from what THIS
install measured. The kinds:

- `night_start` (run id, task start and how it was found, limit, deadline), `night_end` (seconds used, left);
- `stage` (job, stage, seconds, `outcome` `done` or the error code), `acquire` (the GPU tenant, `seconds` of
  the whole call, `wait_s`/`load_s` when the acquirer can tell them apart, `timeout_s` the night allowed);
- `job_end` (job, plan, seconds of this run of it, `outcome`); `cut_expected` (the night still ran two minutes
  before Windows' limit: the stage it was in);
- `first_stage` (the seconds from the task's start to the night's first stage: what a fresh night spends
  before work here), `missed` (a night that began with a job had too little time for its next stage: evidence
  for that plan and stage, `nightfit.miss_sample`), `decision` (a time decision of the night that was not a
  plain BEGIN, `nightfit`) and `too_long_told` (a stage that cannot fit a night was told in the morning note
  with this evidence).

Writing never stops anything. A file another program holds open (a scanner, an editor) takes the line in the
month's spare file `-2` (then `-3` ... `-9`) with one WARN per file and process; a line that no file takes is
only logged. A half line left by a kill is skipped by the reader, and the next line starts on its own line.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ns_common.logging import NsLogger
from ns_common.paths import Paths

LEDGER_DIR = "ledger"
#: Spare files a month may use when its own file cannot be opened (`-2` to `-9`).
SPARES = 8
#: The months a reader looks back for estimates (the newest first): enough for every plan's last five jobs.
HISTORY_MONTHS = 3

_warned: set[str] = set()
_write_lock = threading.Lock()


def ledger_dir(paths: Paths) -> Path:
    """`data/ledger`: the night ledger's monthly files (in the diagnostics zip with `data`; never pruned)."""
    return paths.data / LEDGER_DIR


def month_file(paths: Paths, when: datetime, n: int = 1) -> Path:
    """The ledger file of the month of `when` (UTC); `n` > 1 names its spare `-<n>`."""
    stem = f"ledger-{when.astimezone(UTC):%Y%m}"
    return ledger_dir(paths) / (f"{stem}.jsonl" if n == 1 else f"{stem}-{n}.jsonl")


def _append(path: Path, data: bytes) -> None:
    """Append one line; a file whose last line was cut by a kill gets a line break first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as fh:
        fh.seek(0, 2)
        if fh.tell():
            fh.seek(-1, 2)
            if fh.read(1) != b"\n":
                data = b"\n" + data
        fh.seek(0, 2)
        fh.write(data)


def write(paths: Paths, line: dict[str, Any], *, when: datetime, log: NsLogger | None = None) -> Path | None:
    """Append `line` to the month's file (or a spare); the file written, None when none took it."""
    data = (json.dumps(line, ensure_ascii=False, default=str) + "\n").encode("utf-8")
    with _write_lock:
        for n in range(1, SPARES + 2):
            target = month_file(paths, when, n)
            try:
                _append(target, data)
            except OSError as e:
                key = str(target)
                if key not in _warned and log is not None:
                    _warned.add(key)
                    log.warn("ledger_file_busy", f"{target} could not be written ({e}); the next file takes "
                             "the line", file=key)
                continue
            return target
    if log is not None:
        log.warn("ledger_line_lost", "no ledger file could be written", line=line)
    return None


def _month_files(paths: Paths, months: int | None) -> list[Path]:
    try:
        files = sorted(ledger_dir(paths).glob("ledger-*.jsonl"))
    except OSError:
        return []
    if months is None:
        return files
    keep = sorted({f.name[7:13] for f in files})[-months:]
    return [f for f in files if f.name[7:13] in keep]


def read(paths: Paths, *, months: int | None = HISTORY_MONTHS) -> list[dict[str, Any]]:
    """Every readable line of the newest `months` months (None: all), oldest first by `at`.

    A line that is not a JSON object (a half line a kill left, a file damaged by hand) is skipped; a file
    that cannot be read just now is skipped too: the estimates then have fewer samples, nothing else."""
    out: list[tuple[str, int, dict[str, Any]]] = []
    seq = 0
    for f in _month_files(paths, months):
        try:
            raw = f.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue
        for text in raw.splitlines():
            try:
                item = json.loads(text)
            except ValueError:
                continue
            if isinstance(item, dict):
                seq += 1
                out.append((str(item.get("at") or ""), seq, item))
    return [item for _at, _n, item in sorted(out, key=lambda t: (t[0], t[1]))]


def take_job(paths: Paths, job_id: str) -> list[str]:
    """Take every line of `job_id` out of the ledger files and return them (raw text, oldest file first).

    For a story the owner removes (`ns_studio.removal`): its stage times were measured on a job that no longer
    exists, so they must not keep steering the estimates, and the removal record keeps them. A file is
    rewritten whole through a temporary file and only when it holds such a line; a line that is not JSON, and
    every other job's line, stays exactly as it is. A file that cannot be read or replaced is left as it was
    (its lines are then not returned) and the next call tries again. Never raises for a file error."""
    from ns_common.atomic import write_text_atomic

    taken: list[str] = []
    with _write_lock:
        for f in _month_files(paths, None):
            try:
                raw = f.read_bytes().decode("utf-8", errors="replace")
            except OSError:
                continue
            keep: list[str] = []
            mine: list[str] = []
            for text in raw.splitlines():
                try:
                    item = json.loads(text)
                except ValueError:
                    item = None
                if isinstance(item, dict) and item.get("job_id") == job_id:
                    mine.append(text)
                else:
                    keep.append(text)
            if not mine:
                continue
            try:
                write_text_atomic(f, "".join(line + "\n" for line in keep))
            except OSError:
                continue
            taken.extend(mine)
    return taken


def stage_passes(lines: Iterable[Mapping[str, Any]], plan: str, stage: str) -> list[tuple[float, bool]]:
    """THE one rule for what the estimates count, as (seconds, is a miss), oldest first: the WORK of one pass
    of `stage` in a job of `plan`.

    Counted, one sample per job (its latest), oldest job first:
    - a `stage` line of that plan and stage with `outcome: done` (its `seconds` is the stage alone: never a
      wait for the graphics card, which has its own `acquire` line, and never another stage);
    - a `cut_expected` line of that plan and stage: the stage was still running at the hard mark, so its
      `seconds` so far is a lower bound (a stage longer than a night never writes a `done` line);
    - a `missed` line (`nightfit.miss_sample`): a night that began with this job had too little time left
      for the stage; each miss is a sample of its own (`sample_id`), over the room by the shortfall, so the
      two misses that give a job up mark the stage for its plan (no running time: `True`).
    Not counted: failed stages, `acquire` waits and loads, `job_end` totals, other plans (a Short's `voice` is
    not a master's) and the earlier runs of a stage a job ran again."""
    latest: dict[str, tuple[float, bool]] = {}
    for line in lines:
        kind = line.get("kind")
        if line.get("plan") != plan or line.get("stage") != stage:
            continue
        if not ((kind == "stage" and line.get("outcome") == "done") or kind in ("cut_expected", "missed")):
            continue
        try:
            seconds = float(line["seconds"])
        except (KeyError, TypeError, ValueError):
            continue
        key = str(line.get("job_id") or "")
        if kind == "missed":
            key = str(line.get("sample_id") or key)
        latest.pop(key, None)  # re-inserted: the order is that of the latest sample
        latest[key] = (seconds, kind == "missed")
    return list(latest.values())


def stage_samples(lines: Iterable[Mapping[str, Any]], plan: str, stage: str) -> list[float]:
    """The seconds of `stage_passes` (the one rule), oldest first."""
    return [seconds for seconds, _miss in stage_passes(lines, plan, stage)]


def last_done_at(lines: Iterable[Mapping[str, Any]], job_id: str) -> str | None:
    """When a stage of `job_id` last finished (`stage` line, `outcome: done`, any route): the job's progress
    as the ledger saw it, also on a night that was killed after it (no decision wrote it then)."""
    times = [str(x.get("at")) for x in lines if x.get("kind") == "stage" and x.get("outcome") == "done"
             and x.get("job_id") == job_id and x.get("at")]
    return max(times) if times else None


def _numbers(lines: Iterable[Mapping[str, Any]], kind: str, key: str, **match: Any) -> list[float]:
    out: list[float] = []
    for line in lines:
        if line.get("kind") != kind or any(line.get(k) != v for k, v in match.items()):
            continue
        value = line.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
            out.append(float(value))
    return out


def overhead_samples(lines: Iterable[Mapping[str, Any]]) -> list[float]:
    """What each timed night spent before its first stage (`first_stage` lines, oldest first): start-up,
    recovery, planning, the wait for the graphics card and the model load."""
    return _numbers(lines, "first_stage", "overhead_s")


def load_samples(lines: Iterable[Mapping[str, Any]], service: str) -> list[float]:
    """The model loads of `service` the acquirer told apart from the wait (`acquire` lines with `load_s`)."""
    return _numbers(lines, "acquire", "load_s", service=service)
