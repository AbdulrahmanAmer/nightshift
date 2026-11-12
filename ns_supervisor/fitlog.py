"""What llama.cpp's own fit (`--fit on`, build 12032) chose for a load, read from the lines it prints.

Card vram-spill, plan of 2026-11-12 rule 4: after every llama load the engine reads these lines from the
process's own log (`procs.Proc.log_path`, JSONL whose `msg` is the line) and records them with the setting.
The words are llama.cpp's, as seen on the GPU PC (evidence section 11, F2 at `-lv 4`):

    llama_params_fit_impl: projected to use 14591 MiB of device memory vs. 15172 MiB of free device memory
    llama_params_fit_impl: cannot meet free memory target of 1024 MiB, need to reduce device memory by 443 MiB
    llama_params_fit_impl: with only dense weights in device memory there is a total surplus of 11668 MiB
    llama_params_fit_impl:   - CUDA0 (NVIDIA GeForce RTX 5060 Ti): 42 layers ( 2 overflowing), 14044 MiB ...
    common_fit_params: successfully fit params to free device memory
    load_tensors:    CUDA_Host model buffer size =   755.53 MiB

and a fit that gave up: `common_fit_params: failed to fit params to free device memory: ...` (the driver
then puts the rest in system memory: a spill, rule 4).

Parsing is defensive: a line that is not there leaves its field None, and nothing here raises. Build 12032
prints these lines only at `-lv 4` (GPU PC evidence 15: none at the default or at 3), which the engine does
not pass (its volume per request is not measured): so the lines are OPTIONAL, and the engine's own reading
of the card after `ready` decides (`core._vram_after_ready`); the lines, when printed, add what fit chose.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_PROJECTED = re.compile(r"projected to use (\d+) MiB of device memory vs\. (\d+) MiB of free device memory")
_TARGET = re.compile(r"free memory target of (\d+) MiB, need to reduce device memory by (\d+) MiB")
_LEAVE = re.compile(r"will leave (-?\d+) >= (\d+) MiB")
_DENSE = re.compile(r"with only dense weights in device memory there is a total surplus of (-?\d+) MiB")
_DEVICE = re.compile(r"-\s*(CUDA\d+)\b.*?:\s*(\d+) layers\s*\(\s*(\d+) overflowing\)")
_HOST = re.compile(r"CUDA_Host model buffer size\s*=\s*([\d.]+) MiB")
_CARD = re.compile(r"CUDA\d+ model buffer size\s*=\s*([\d.]+) MiB")
#: Read at most this much of a log from where the load began (a load prints a few hundred lines).
READ_MAX = 4 * 1024 * 1024


@dataclass(frozen=True)
class FitResult:
    """llama.cpp's fit for one load. None: the line was not printed (or not read)."""

    projected_mb: int | None = None  # what the whole model would take on the card (before any reduction)
    free_mb: int | None = None  # the card's free memory as fit saw it
    target_mb: int | None = None  # the free memory fit keeps (`--fit-target`) when it had to reduce
    reduce_mb: int | None = None  # what fit took off the card (experts to RAM)
    dense_surplus_mb: int | None = None  # free beyond the target with only the dense weights on the card
    layers: int | None = None  # layers on the card (all devices)
    overflow_layers: int | None = None  # of those, layers whose experts went to RAM
    host_mb: int | None = None  # pinned RAM llama.cpp holds the overflowed experts in (`CUDA_Host`)
    card_model_mb: int | None = None  # the model buffer on the card
    fitted: bool = False  # "successfully fit params"
    failed: bool = False  # "failed to fit params": the rest went to the driver (a spill)

    @property
    def seen(self) -> bool:
        """Any fit line was read."""
        return self.projected_mb is not None or self.fitted or self.failed

    @property
    def moved(self) -> bool:
        """Fit kept part of the model in RAM (experts of some layers)."""
        return bool(self.overflow_layers) or bool(self.reduce_mb)

    def floor_mb(self) -> int | None:
        """The card memory of the model with every expert in RAM (rule 3's floor), from fit's own numbers:
        free - target - the surplus with only the dense weights on the card. None when not printed."""
        if self.free_mb is None or self.dense_surplus_mb is None:
            return None
        return max(0, self.free_mb - (self.target_mb or 0) - self.dense_surplus_mb)

    def words(self) -> str:
        """`the whole model on the card` / `the experts of 2 layers in RAM (756 MiB)` / `fit failed`."""
        if self.failed:
            return "llama.cpp could not fit it to the card"
        if not self.seen:
            return "llama.cpp printed no fit lines"
        if self.moved:
            n = self.overflow_layers
            ram = f" ({self.host_mb} MiB)" if self.host_mb else ""
            return f"the experts of {n if n is not None else 'some'} layers in the PC's memory{ram}"
        return "the whole model on the graphics card"

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, False)}


def _lines(text: str) -> Iterable[str]:
    """Each line of `text`; a JSONL record of the engine's service log stands for its `msg`."""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("{"):
            try:
                rec = json.loads(line)
            except ValueError:
                yield line
                continue
            if isinstance(rec, dict):
                msg = rec.get("msg")
                if isinstance(msg, str):
                    yield msg
                continue
        yield line


def parse(text: str) -> FitResult:
    """The fit of the FIRST model in `text` (the main model; a draft's lines come later and are ignored for
    the same field). Never raises."""
    found: dict[str, Any] = {}

    def first(key: str, value: Any) -> None:
        found.setdefault(key, value)

    devices: dict[str, tuple[int, int]] = {}  # the LAST line per device before fit said it was done
    for line in _lines(text or ""):
        if (m := _PROJECTED.search(line)) is not None:
            first("projected_mb", int(m.group(1)))
            first("free_mb", int(m.group(2)))
        if (m := _TARGET.search(line)) is not None:
            first("target_mb", int(m.group(1)))
            first("reduce_mb", int(m.group(2)))
        if (m := _LEAVE.search(line)) is not None:
            first("target_mb", int(m.group(2)))
        if (m := _DENSE.search(line)) is not None:
            first("dense_surplus_mb", int(m.group(1)))
        if (m := _DEVICE.search(line)) is not None and "fitted" not in found:
            devices[m.group(1)] = (int(m.group(2)), int(m.group(3)))
        if (m := _HOST.search(line)) is not None:
            first("host_mb", round(float(m.group(1))))
        if (m := _CARD.search(line)) is not None:
            first("card_model_mb", round(float(m.group(1))))
        if "successfully fit params" in line:
            found["fitted"] = True
        if "failed to fit params" in line:
            found["failed"] = True
    if devices:
        found["layers"] = sum(n for n, _ in devices.values())
        found["overflow_layers"] = sum(o for _, o in devices.values())
    return FitResult(**found)


def size(path: Path | None) -> int:
    """The log's size now: where a load's own lines will start (0 when it cannot be read)."""
    try:
        return path.stat().st_size if path is not None else 0
    except OSError:
        return 0


def read_from(path: Path | None, start: int) -> str:
    """The log's text from byte `start` (at most `READ_MAX`); "" when it cannot be read. A log that rolled
    over to a new day's file since `start` is read from its beginning."""
    if path is None:
        return ""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            end = f.tell()
            f.seek(start if 0 <= start <= end else 0)
            return f.read(READ_MAX).decode("utf-8", errors="replace")
    except OSError:
        return ""
