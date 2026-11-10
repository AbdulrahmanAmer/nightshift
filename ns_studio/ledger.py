"""The night ledger: where the time of every night, job, stage and wait for the graphics card goes.

Card night-fits. One JSON line per event, one file a month: `data/ledger/ledger-<yyyymm>.jsonl`. Every route
that runs a stage writes its `stage` line (the nightly, `studio run`, a dashboard job, a Retry, a series
part), tagged with its `origin`, so the estimates the nightly plans with (`nightclock`) come from what THIS
install measured. The kinds:

- `night_start` (run id, task start and how it was found, limit, deadline), `night_end` (seconds used, left);
- `stage` (job, stage, seconds, `outcome` `done` or the error code), `acquire` (the GPU tenant, `seconds` of
  the whole call, `wait_s`/`load_s` when the acquirer can tell them apart, `timeout_s` the night allowed);
- `job_end` (job, plan, seconds of this run of it, `outcome`); `cut_expected` (the night still ran two minutes
  before Windows' limit: the stage it was in).

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


def stage_samples(lines: Iterable[Mapping[str, Any]], plan: str, stage: str) -> list[float]:
    """THE one rule for what the estimates count: the WORK of one pass of `stage` in a job of `plan`.

    Counted, one sample per job (its latest), oldest job first:
    - a `stage` line of that plan and stage with `outcome: done` (its `seconds` is the stage alone: never a
      wait for the graphics card, which has its own `acquire` line, and never another stage);
    - a `cut_expected` line of that plan and stage: the stage was still running at the hard mark, so its
      `seconds` so far is a lower bound (a stage longer than a night never writes a `done` line).
    Not counted: failed stages, `acquire` waits and loads, `job_end` totals, other plans (a Short's `voice` is
    not a master's) and the earlier runs of a stage a job ran again."""
    latest: dict[str, float] = {}
    for line in lines:
        kind = line.get("kind")
        if line.get("plan") != plan or line.get("stage") != stage:
            continue
        if not ((kind == "stage" and line.get("outcome") == "done") or kind == "cut_expected"):
            continue
        try:
            seconds = float(line["seconds"])
        except (KeyError, TypeError, ValueError):
            continue
        job = str(line.get("job_id") or "")
        latest.pop(job, None)  # re-inserted: the job's order is that of its latest sample
        latest[job] = seconds
    return list(latest.values())
