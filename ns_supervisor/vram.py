"""vram-busy: the graphics card's free memory is compared with what a model needs BEFORE the model loads.

The ONE owner of "may a model process start on the card now" (card vram-busy, 4b.2). `_start_locked`
asks `check` right before every spawn of a GPU tenant (after the other tenants were stopped, after a variant
change stopped the old process), so no route reaches a load without it: the nightly, `studio run`, a dashboard
job, a Retry, the Assistant, Nightshift Code, a voice preview or make, `ns verify` and the warmup. What the
night then does with a refusal is not decided here: the engine waits (a nightly acquire, inside the time the
night's own decision gave the acquire: `timeout_s`) or refuses at once (anyone else), and the nightly turns
the refusal (NS-E208) into the busy-card path it already has for NS-E201 (`ns_studio.daily`).

What the card is asked (H-10's `SmiSource`, the one way to nvidia-smi): `memory.used,memory.total`; when the
memory is short also the compute apps (`pid,process_name,used_memory`) and the plain table's Processes section
(it also lists graphics programs, `G` and `C+G`, which the compute list leaves out). Under WDDM, the driver of
the GPU PC, per-process memory is `[N/A]`: who uses how much can then NOT be known, so the words name the
programs Windows lists without numbers and say so, and never guess. Our own processes (every service the
engine runs and the GPU holder's own process, such as the studio holding whisper) are never named, and their
memory is taken off "used by others" only where the driver gives it. System processes count but are never
named (closing them is no advice). Programs are named by process name only, never a path or a window title.

What a model needs: the largest of the last `KEEP` measured loads of the same service, variant, model file,
context and flags (`run/vram-need.json`: total used after `ready` minus total used just before the spawn,
which works under WDDM too), or the lower bound a failed load left (`min_mb`), else the estimate
`model file MiB + ctx / 1024 x KV_MB_PER_1K_CTX + LOAD_OVERHEAD_MB`, which ignores what `-ngl` and
`--n-cpu-moe` keep off the card and so NEVER refuses a load: a never-measured model is tried, and measured.

The rule since card vram-spill (plan 2026-11-12, `judge`, `choose`, `spilled`): others are everything in use
that is not ours (free + others = total). A load that is not fitted is refused (or a nightly waits) when a
MEASURED need does not fit a SETTLED reading: NS-E203 when not even an idle card would hold it, else
NS-E208. A llama load that llama.cpp's own fit places (`services.fitted`) steps down the ladder (as tuned, MTP
off, experts in RAM) and is refused only when card + RAM cannot hold it; after `ready` a card with less than
`SPILL_FREE_MB` free (or fit's "failed to fit") is a spill. The older wording, kept for its other parts:
a load is refused ONLY when a MEASURED need does not fit a SETTLED
reading because of memory OTHER programs hold, and would fit an idle card. Settled (`settled`, `check`): after
one of our tenants stops, the card is read again (every 0.5 s, at most 10 s) until its memory came back;
without one, until the reading stops falling; a reading still waiting for our own memory never refuses.
Others: used minus our known MiB minus what an idle card of this PC keeps (`record_idle`: the smallest reading
seen with nothing of ours on it, else `IDLE_ALLOWANCE_MB`) and, for a studio holder, its own context
(`HOLDER_CONTEXT_MB`). A need that does not fit even an idle card is a tuning matter (NS-E203), never "another
program".
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ns_common.atomic import read_text_retry, write_text_atomic
from ns_common.paths import Paths
from ns_supervisor.fitlog import FitResult
from ns_supervisor.gpu import SmiSource, labelled, simulated_mark
from ns_supervisor.ramroom import Room
from ns_supervisor.services import FIT_TARGET_MB, ServiceDef, fitted, mtp_on

#: Memory a load must leave free beyond its need (a window opening, the driver's own growth).
MARGIN_MB = 512
#: A load that leaves less than this free on the card after `ready` spilled into system memory through the
#: driver (plan rule 4, revision 2; G-53 stopped at 460 with -ngl 99 and llama.cpp's fit off). "Shared GPU
#: memory" is NEVER read: with `--load-mode none` it is llama.cpp's pinned host buffer (evidence 11, 14).
SPILL_FREE_MB = 256
#: What a load should leave free after `ready` (rule 4); between this and `SPILL_FREE_MB` it is said, not
#: stopped.
READY_FREE_MB = 512
#: What the PC's memory must keep beyond what a fitted load puts in it (rule 3).
RAM_SPARE_MB = 2048
#: What llama-server's private bytes grow by after `ready` (GPU PC evidence 15: 15.8 GB at ready, 18.35 GB
#: after twelve requests: the prompt cache and its checkpoints), added to the measured commit at ready.
COMMIT_GROWTH_MB = 2560
#: The MTP draft's card memory, measured on the story model (16k q8_0: +14136 with, +12956 without). Used only
#: to estimate the MTP-off step from a MEASURED tuned need before the MTP-off step itself was measured.
MTP_DRAFT_MB = 1180
#: The ladder's step names (`choose`), as `/status` and the log say them.
STEP_WORDS = {
    "tuned": "as tuned, the whole model on the graphics card",
    "mtp_off": "the MTP draft off, the whole model on the graphics card",
    "experts_in_ram": "as tuned, llama.cpp keeping some expert layers in the PC's memory",
    "experts_in_ram_mtp_off": "the MTP draft off, llama.cpp keeping some expert layers in the PC's memory",
}
#: How often a nightly acquire looks at the card again while it waits for memory.
VRAM_POLL_S = 120.0
#: The longest a nightly acquire waits for memory (inside the acquire's own `timeout_s`).
VRAM_WAIT_S = 1800
#: A proof's shorter wait (`devmode.test_install` installs only), in whole seconds, both ends taken.
WAIT_ENV = "NS_TEST_VRAM_WAIT_S"
WAIT_MIN_S, WAIT_MAX_S = 60, 1800
#: The KV cache per 1024 tokens of context before a measurement exists. llama.cpp's own number depends on
#: the model's layers and heads, which the engine does not read from the GGUF; 64 MB is the card's default
#: (16 GB at 256K, 1 GB at the story model's 16K). The GPU kit's first night replaces it by a measurement.
KV_MB_PER_1K_CTX = 64
#: Compute buffers and the CUDA context of a load, beyond the weights and the KV cache.
LOAD_OVERHEAD_MB = 700
#: What an idle card of this PC keeps in use (Windows' desktop, the driver) before Nightshift has read its own
#: idle card (`record_idle`): the GPU PC showed 812 MiB idle (tests/fixtures/supervisor).
IDLE_ALLOWANCE_MB = 1024
#: Readings that must agree (within `SETTLE_STEP_MB`) before a candidate becomes this PC's idle baseline.
IDLE_AGREE = 3
#: The studio's own CUDA context (whisper ran in its process), which a WDDM driver never reports per process:
#: allowed for when the GPU holder is a studio process that named its pid.
HOLDER_CONTEXT_MB = 512
#: After one of our tenants stops, how long the card is read again for its memory to come back (as `ns verify`
#: polls 10 x 0.5 s), how often, and what counts as "stopped falling" and as "given back".
SETTLE_S, SETTLE_POLL_S = 10.0, 0.5
SETTLE_READS = round(SETTLE_S / SETTLE_POLL_S)  # 20: the settle never reads more often than this
SETTLE_STEP_MB, SETTLE_SLACK_MB = 64, 256
#: A stop of ours this long before a check is past: its memory had its time (a later reading is the card's).
RELEASE_STALE_S = 30.0
#: Measured loads kept per model (the need is the largest of them).
KEEP = 3
READ_TIMEOUT_S = 5.0
NEED_FILE = "vram-need.json"
MEMORY_ARGS = ("--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits")
APPS_ARGS = ("--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader,nounits")
#: Windows' own processes: their memory counts, their names are no advice to anyone.
SYSTEM = frozenset({
    "dwm.exe", "csrss.exe", "winlogon.exe", "explorer.exe", "searchhost.exe", "searchapp.exe",
    "shellexperiencehost.exe", "startmenuexperiencehost.exe", "textinputhost.exe", "lockapp.exe",
    "applicationframehost.exe", "systemsettings.exe", "widgets.exe", "system",
})  # fmt: skip
#: How many programs a sentence names before "and N more".
NAMES_MAX = 5
_ROW = re.compile(r"^\|\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(C\+G|C|G)\s+(.+?)\s+(\d+MiB|N/A)\s*\|\s*$")
_STAMP = "%Y-%m-%dT%H:%M:%S"


# ------------------------------------------------------------------------------------------ the reading
@dataclass(frozen=True)
class Program:
    """One program the driver lists on the card: its pid, process name (no folder), MiB or None (`[N/A]`)."""

    pid: int
    name: str
    mb: int | None = None


@dataclass(frozen=True)
class Reading:
    total_mb: int
    used_mb: int
    programs: tuple[Program, ...] = ()
    listed: bool = False  # the program lists were asked (only when the memory is short)


def process_name(raw: str) -> str:
    """The process name as the owner knows it: the last part of what the driver prints (a full path on
    Windows, or the table's name cut at the front with `...`)."""
    text = raw.strip()
    return re.split(r"[\\/]", text)[-1] or text


def parse_memory(text: str | None) -> tuple[int, int] | None:
    """`(used, total)` MiB from the memory query's first line; None when it does not read as two numbers."""
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            used, total = int(parts[0]), int(parts[1])
            return (used, total) if total > 0 else None
        if line.strip():
            return None
    return None


def parse_apps(text: str | None) -> list[Program]:
    """The compute apps list: `pid, process_name, used_memory` (`[N/A]` under WDDM). A row that does not
    read is skipped; the totals still decide."""
    out: list[Program] = []
    for line in (text or "").splitlines():
        head, _, mb = line.rpartition(",")
        pid, _, name = head.partition(",")
        if not pid.strip().isdigit() or not name.strip():
            continue
        mb = mb.strip().removesuffix("MiB").strip()
        out.append(Program(int(pid), process_name(name), int(mb) if mb.isdigit() else None))
    return out


def parse_table(text: str | None) -> list[Program]:
    """The Processes section of the plain `nvidia-smi` table (graphics programs too)."""
    out: list[Program] = []
    in_procs = False
    for line in (text or "").splitlines():
        if "Processes:" in line:
            in_procs = True
            continue
        m = _ROW.match(line) if in_procs else None
        if m is not None:
            mb = m.group(4)
            out.append(
                Program(int(m.group(1)), process_name(m.group(3)), int(mb[:-3]) if mb != "N/A" else None)
            )
    return out


def read_memory(smi: SmiSource) -> tuple[int, int] | None:
    """`(used, total)` MiB now, or None when the tool gives no answer that reads."""
    return parse_memory(smi.query(MEMORY_ARGS, READ_TIMEOUT_S))


def read_programs(smi: SmiSource) -> tuple[Program, ...]:
    """Who is on the card: the compute list first (its full names), then the table's graphics programs. A
    list the tool does not give is empty (the words then say Windows did not say which program)."""
    found: dict[int, Program] = {}
    for p in parse_apps(smi.query(APPS_ARGS, READ_TIMEOUT_S)):
        found.setdefault(p.pid, p)
    for p in parse_table(smi.query([], READ_TIMEOUT_S)):
        found.setdefault(p.pid, p)
    return tuple(found.values())


# ------------------------------------------------------------------------------------------ the need
def _arg(cmd: Sequence[str], flag: str) -> str | None:
    try:
        return cmd[list(cmd).index(flag) + 1]
    except (ValueError, IndexError):
        return None


def need_key(d: ServiceDef) -> dict[str, Any]:
    """What makes two loads the same: service, variant, model file, context and the other flags (the
    address and the alias are not memory)."""
    cmd = list(d.cmd)
    ctx = _arg(cmd, "-c")
    flags: list[str] = []
    skip = False
    for a in cmd[1:]:
        if skip:
            skip = False
            continue
        if a in ("-m", "-c", "--port", "--host", "--alias"):
            skip = True
            continue
        flags.append(a)
    return {"service": d.name, "variant": d.variant, "model": _arg(cmd, "-m") or "",
            "ctx": int(ctx) if ctx and ctx.isdigit() else None, "flags": " ".join(flags)}  # fmt: skip


def _mib(path: Path) -> int:
    try:
        if path.is_dir():
            total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        else:
            total = path.stat().st_size
    except OSError:
        return 0
    return math.ceil(total / (1024 * 1024))


def estimate_mb(d: ServiceDef, model_dir: str | None = None) -> int:
    """The need before any measurement: model file MiB + ctx / 1024 x `KV_MB_PER_1K_CTX` + 700 (the voice
    service: its model folder + 700)."""
    key = need_key(d)
    weights = _mib(Path(key["model"])) if key["model"] else (_mib(Path(model_dir)) if model_dir else 0)
    kv = math.ceil((key["ctx"] or 0) * KV_MB_PER_1K_CTX / 1024)
    return weights + kv + LOAD_OVERHEAD_MB


@dataclass(frozen=True)
class Need:
    mb: int
    measured: bool  # a measured load (or a failed load's lower bound); False: the estimate (never refuses)
    lower: bool = False  # "more than `mb`": the model did not start with `mb` + margin free
    # a fitted load (llama.cpp's fit): `mb` is the WHOLE model on the card; these come from its fit lines
    floor_mb: int | None = None  # the card memory with every expert in RAM (rule 3's floor)
    commit_mb: int | None = None  # the process's private bytes after `ready` (Windows commit it takes)
    offset_mb: int | None = None  # measured card delta minus fit's projection, on a load that moved nothing
    tok_s: float | None = None  # the last speed measured for this exact setting (`record_speed`), if any


def _need_file(paths: Paths) -> Path:
    return paths.home / "run" / NEED_FILE


def _load(paths: Paths) -> dict[str, Any]:
    """`run/vram-need.json`; a missing, torn or foreign file is empty (estimates and the idle allowance)."""
    try:
        data = json.loads(read_text_retry(_need_file(paths), "utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    entries = data.get("entries")
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def _save(paths: Paths, data: dict[str, Any]) -> None:
    path = _need_file(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps({"version": 1, **data}, indent=2) + "\n")


def _same(e: dict[str, Any], key: dict[str, Any], mark: str | None) -> bool:
    """The same model, measured on the same kind of card: an entry of the simulated card (`card` set) is
    never a real card's number, nor the other way round (H-10)."""
    return all(e.get(k) == v for k, v in key.items()) and e.get("card") == mark


def need(paths: Paths, d: ServiceDef, model_dir: str | None = None, mark: str | None = None) -> Need:
    """The largest of the last `KEEP` measured loads of this exact model on this kind of card (`mark`: the
    simulated card's, None for a real one), and of a failed load's lower bound (`min_mb`), else the
    estimate (`measured` False: it never refuses a load)."""
    key = need_key(d)
    for e in _entries(_load(paths)):
        if _same(e, key, mark):
            mbs = [int(m) for m in e.get("mb") or [] if isinstance(m, int) and m > 0][-KEEP:]
            low = e.get("min_mb") if isinstance(e.get("min_mb"), int) and e["min_mb"] > 0 else None
            extra: dict[str, Any] = {k: _whole(e.get(k)) for k in ("floor_mb", "commit_mb", "offset_mb")}
            speed = e.get("tok_s")
            extra["tok_s"] = float(speed) if isinstance(speed, int | float) and speed > 0 else None
            if mbs or low:
                top = max([*mbs, low or 0])
                return Need(top, True, lower=low is not None and low >= max(mbs, default=0), **extra)
            return Need(estimate_mb(d, model_dir), False, **extra)
    return Need(estimate_mb(d, model_dir), False)


def _whole(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _mine(data: dict[str, Any], d: ServiceDef, mark: str | None) -> dict[str, Any]:
    key = need_key(d)
    entries = _entries(data)
    data["entries"] = entries
    mine = next((e for e in entries if _same(e, key, mark)), None)
    if mine is None:
        mine = dict(key)
        entries.append(mine)
    return mine


def _stamp(entry: dict[str, Any], mark: str | None, now: datetime) -> None:
    entry["at"] = now.strftime(_STAMP)
    if mark is not None:
        entry["card"] = mark  # measured on the simulated card: never evidence of a real one
    else:
        entry.pop("card", None)


def record(paths: Paths, d: ServiceDef, mb: int, mark: str | None, now: datetime) -> None:
    """Keep one measured load (`mb` > 0) of `d`'s model; a load that worked ends a failed load's lower bound.
    Raises OSError when the file cannot be written (the caller logs it; the next load measures again)."""
    if mb <= 0:
        return
    data = _load(paths)
    mine = _mine(data, d, mark)
    mine["mb"] = [*[m for m in mine.get("mb") or [] if isinstance(m, int)], int(mb)][-KEEP:]
    mine.pop("min_mb", None)
    _stamp(mine, mark, now)
    _save(paths, data)


def fit_need(
    delta_mb: int, fit: FitResult, offset_mb: int | None, free_after_mb: int | None = None
) -> tuple[int, bool, int | None]:
    """What a fitted load's measurement says the WHOLE model needs on the card: `(mb, lower, offset)`.
    No fit lines printed (build 12032 below `-lv 4`): the delta; a lower bound when the card was left with
    less than fit's target + `SETTLE_SLACK_MB` free (fit may have moved experts: then the delta is only what
    fitted). Nothing moved: the delta itself (and the offset delta - projected, kept for the loads that
    move). Experts moved: fit's projection of the whole model + the offset learned on a load that moved
    nothing (0 before one). Moved with no projection printed: the delta, as a lower bound."""
    if not fit.seen:
        maybe_moved = free_after_mb is not None and free_after_mb < FIT_TARGET_MB + SETTLE_SLACK_MB
        return delta_mb, maybe_moved, None
    if not fit.moved:
        off = delta_mb - fit.projected_mb if fit.projected_mb is not None else None
        return delta_mb, False, off
    if fit.projected_mb is not None:
        return fit.projected_mb + max(0, offset_mb or 0), False, None
    return delta_mb, True, None


def record_fit(paths: Paths, d: ServiceDef, delta_mb: int, fit: FitResult, commit_mb: int | None,
               mark: str | None, now: datetime, free_after_mb: int | None = None) -> int:  # fmt: skip
    """Keep a fitted load: the whole model's need (`fit_need`), fit's floor (rule 3), the private bytes it
    took, and what fit chose (`fit`). Returns the need kept. Raises OSError (the caller logs it)."""
    data = _load(paths)
    mine = _mine(data, d, mark)
    mb, lower, off = fit_need(delta_mb, fit, _whole(mine.get("offset_mb")), free_after_mb)
    if mb <= 0:
        return 0
    if lower:
        old = _whole(mine.get("min_mb")) or 0
        mine["min_mb"] = max(old, mb)
    else:
        mine["mb"] = [*[m for m in mine.get("mb") or [] if isinstance(m, int)], int(mb)][-KEEP:]
        mine.pop("min_mb", None)
    if off is not None:
        mine["offset_mb"] = max(0, off)
    floor = fit.floor_mb()
    if floor is not None:
        mine["floor_mb"] = floor
    if commit_mb is not None and commit_mb > 0:
        mine["commit_mb"] = int(commit_mb)
    mine["fit"] = fit.as_dict()
    mine["card_mb"] = int(delta_mb)
    _stamp(mine, mark, now)
    _save(paths, data)
    return mb


def record_speed(paths: Paths, d: ServiceDef, tok_s: float, mark: str | None, now: datetime) -> None:
    """Keep the speed measured for this exact setting (`ns tune`, `ns verify`): `/status` says it beside the
    setting. Raises OSError."""
    if tok_s <= 0:
        return
    data = _load(paths)
    mine = _mine(data, d, mark)
    mine["tok_s"] = round(float(tok_s), 1)
    _stamp(mine, mark, now)
    _save(paths, data)


def record_failure(paths: Paths, d: ServiceDef, free_mb: int, mark: str | None, now: datetime) -> None:
    """A load whose need was MEASURED failed to start with `free_mb` free: its need is more than that minus
    the margin, so the next check compares that lower bound (`min_mb`). The caller never calls it for a model
    only estimated (its failure may be a damaged file: NS-E202's ground, not memory). Raises OSError."""
    low = free_mb - MARGIN_MB + 1
    if low <= 0:
        return
    data = _load(paths)
    mine = _mine(data, d, mark)
    old = mine.get("min_mb")
    mine["min_mb"] = max(old if isinstance(old, int) else 0, low)
    _stamp(mine, mark, now)
    _save(paths, data)


def _idle_slot(mark: str | None) -> str:
    """Where the idle card of this kind is kept: a simulated card's idle never stands for the real card's."""
    return "idle" if mark is None else "idle_simulated"


def idle_mb(paths: Paths, mark: str | None = None) -> int | None:
    """What this PC's card (of this kind: `mark`) keeps in use with nothing of Nightshift's on it, once
    CONFIRMED (`record_idle`); None before (the caller then uses `IDLE_ALLOWANCE_MB`)."""
    idle = _load(paths).get(_idle_slot(mark))
    mb = idle.get("mb") if isinstance(idle, dict) else None
    return mb if isinstance(mb, int) and mb >= 0 else None


def record_idle(paths: Paths, used_mb: int, mark: str | None, now: datetime) -> bool:
    """One reading taken with nothing of ours on the card. The baseline is only ever TAKEN or LOWERED, never
    raised, and never from one reading (a game may be open at the first): a reading is a CANDIDATE, kept
    when lower than the candidate by more than `SETTLE_STEP_MB`, counted when within that of it; the
    candidate becomes the baseline (`mb`) after `IDLE_AGREE` agreeing readings, and only when lower than the
    baseline it would replace. True when the file was written."""
    data = _load(paths)
    slot = _idle_slot(mark)
    raw = data.get(slot)
    old: dict[str, Any] = raw if isinstance(raw, dict) else {}

    def whole(key: str) -> int | None:
        v = old.get(key)
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    base, cand, agree = whole("mb"), whole("candidate"), whole("agree") or 0
    if cand is None or used_mb < cand - SETTLE_STEP_MB:
        cand, agree = int(used_mb), 1  # a new, lower candidate (or the first): provisional
    elif used_mb <= cand + SETTLE_STEP_MB:
        agree += 1
    else:
        return False  # higher than the candidate: something else is on the card; not counted
    new_base = cand if agree >= IDLE_AGREE and (base is None or cand < base) else base
    if agree > IDLE_AGREE and new_base == base:
        return False  # confirmed already: one more agreeing reading changes nothing (no write per check)
    idle: dict[str, Any] = {"candidate": cand, "agree": agree}
    if new_base is not None:
        idle["mb"] = new_base
    _stamp(idle, mark, now)
    data[slot] = idle
    data["entries"] = _entries(data)
    _save(paths, data)
    return True


# ------------------------------------------------------------------------------------------ the check
def model_words(d: ServiceDef) -> str:
    if d.name == "tts":
        return "the voice model"
    if d.name == "llama":
        return {"story": "the story model", "code": "the code model"}.get(d.variant, "the language model")
    return f"the {d.name} model"


def gb(mb: float) -> str:
    return f"{max(0.0, mb) / 1024:.1f} GB"


def _minutes(seconds: float) -> str:
    m = round(seconds / 60)
    return f"{m} minute{'s' if m != 1 else ''}" if m else f"{round(seconds)} s"


@dataclass(frozen=True)
class Verdict:
    """The check's answer and the numbers and names its words are made of. `refuse` only when a MEASURED need
    does not fit a SETTLED reading because of memory OTHER programs hold, and it would fit without them."""

    need: Need
    reading: Reading
    free_mb: int  # total minus used (our own known MiB taken off)
    others_mb: int  # used minus our own known MiB minus what an idle card and our holder's context keep
    allowance_mb: int  # what an idle card of this PC keeps (+ the studio holder's own context)
    fits: bool
    settled: bool
    oversize: bool  # does not fit even an idle card: a tuning matter, never "another program"
    refuse: bool
    named: tuple[Program, ...] = ()  # other programs, not system ones, not ours
    mark: str | None = None
    what: str = "the model"
    # a fitted load (llama.cpp's own fit, rule 3): the card AND the PC's memory hold it
    fit: bool = False
    room: Room | None = None  # the PC's memory when the check was made
    ram_part_mb: int = 0  # what fit must keep in the PC's memory at this reading (0: all on the card)
    short: str = ""  # why it was refused: "card" (below the floor), "ram", "commit"; "" when not

    @property
    def ok(self) -> bool:
        return not self.refuse

    def programs(self) -> str:
        """`(game.exe, chrome.exe; Windows does not say how much each uses)`, with numbers where the driver
        gives every one, or `(Windows did not say which program)`."""
        if not self.named:
            return "(Windows did not say which program)"
        shown = list(self.named[:NAMES_MAX])
        more = len(self.named) - len(shown)
        tail = f" and {more} more" if more else ""
        if all(p.mb is not None for p in self.named):
            return "(" + ", ".join(f"{p.name} {gb(p.mb or 0)}" for p in shown) + tail + ")"
        return "(" + ", ".join(p.name for p in shown) + tail + "; Windows does not say how much each uses)"

    def others(self, *, now: bool) -> str:
        verb = "are" if now else "were"
        return (f"other programs {verb} using {gb(self.others_mb)} of the graphics card's "
                f"{gb(self.reading.total_mb)} {self.programs()}")  # fmt: skip

    def needs(self) -> str:
        if not self.need.measured:
            return f"{self.what} needs about {gb(self.need.mb)} (not measured yet)"
        if self.need.lower:
            return f"{self.what} needs more than {gb(self.need.mb)} (it did not start with that much free)"
        return f"{self.what} needs {gb(self.need.mb)} (measured on its last load)"

    def pc_words(self) -> str:
        """The PC's side of a fitted load that was refused for it (rule 3), "" otherwise."""
        r = self.room
        if self.short == "ram" and r is not None and r.avail_mb is not None:
            return (f"llama.cpp would keep {gb(self.ram_part_mb)} of it in the PC's memory, which had "
                    f"{gb(r.avail_mb)} available ({gb(RAM_SPARE_MB)} must stay for Windows and your "
                    "programs)")  # fmt: skip
        if self.short == "commit" and r is not None and r.commit_room_mb is not None:
            need = (self.need.commit_mb or 0) + COMMIT_GROWTH_MB
            return (f"it takes {gb(need)} of Windows' memory commitment and only {gb(r.commit_room_mb)} "
                    f"were left even after the page file grows ({gb(RAM_SPARE_MB)} must stay)")  # fmt: skip
        return ""

    def detail(self, waited_s: float | None = None) -> str:
        """NS-E208's detail (the caller's error, the morning note): what held the memory, what was needed.
        Free and the others' memory add up to the card's total (G-53's "0 GB" with 1.35 GB in use)."""
        pc = self.pc_words()
        if pc:
            text = f"{self.needs()}: {pc}; close programs you do not need and start again"
        else:
            spare = FIT_TARGET_MB if self.fit else MARGIN_MB
            text = (f"{self.others(now=False)}, so {gb(self.free_mb)} were free and {self.needs()} with "
                    f"{gb(spare)} to spare")  # fmt: skip
            if self.fit and self.need.floor_mb is not None:
                text += f" (at least {gb(self.need.floor_mb)} on the card with its experts in the PC's RAM)"
            if self.named:
                text += f"; close {', '.join(p.name for p in self.named[:NAMES_MAX])} and start again"
        if waited_s:
            text += f"; Nightshift waited {_minutes(waited_s)} for it"
        return labelled(self.mark, text) or text

    def too_big(self) -> str:
        """NS-E203's detail when even an idle card cannot hold the need: tuning, no program blamed."""
        if self.fit and self.need.floor_mb is not None:
            text = (f"{self.what} needs at least {gb(self.need.floor_mb)} on the graphics card even with "
                    f"every expert layer in the PC's memory, with {gb(FIT_TARGET_MB)} to spare: more than "
                    f"the card's {gb(self.reading.total_mb)} holds beside what Windows keeps "
                    f"({gb(self.allowance_mb)})")  # fmt: skip
        else:
            text = (f"{self.needs()} with {gb(MARGIN_MB)} to spare, more than the graphics card's "
                    f"{gb(self.reading.total_mb)} holds beside what Windows keeps "
                    f"({gb(self.allowance_mb)})")  # fmt: skip
        return labelled(self.mark, text) or text

    def waiting(self) -> str:
        """The Queue's sentence while a nightly acquire waits (`/status` `gpu_wait.said`)."""
        pc = self.pc_words()
        if pc:
            text = f"Waiting for the PC's memory: {self.needs()}: {pc}. Close programs and the run goes on."
        else:
            text = (f"Waiting for the graphics card's memory: {self.others(now=True)}, and {self.needs()}. "
                    "Close them and the run goes on.")  # fmt: skip
        return labelled(self.mark, text) or text

    def why_tried(self) -> str | None:
        """Why a load that does not fit the reading goes ahead anyway (None when it fits, is refused, or is
        fitted: llama.cpp keeps what the card cannot hold in the PC's memory, said as the ladder step)."""
        if self.fits or self.refuse or self.fit:
            return None
        if not self.need.measured:
            return ("the need is only estimated (a model is tried and measured before a check can refuse it; "
                    "a load that spills is stopped)")  # fmt: skip
        return "the memory Nightshift gave back had not come free within the settle time"

    def numbers(self) -> dict[str, Any]:
        out = {"need_mb": self.need.mb, "need_measured": self.need.measured, "free_mb": self.free_mb,
               "others_mb": self.others_mb, "total_mb": self.reading.total_mb,
               "names": [p.name for p in self.named]}  # fmt: skip
        if self.fit:
            out.update({"ram_part_mb": self.ram_part_mb, "floor_mb": self.need.floor_mb,
                        "short": self.short or None})  # fmt: skip
            out.update(self.room.as_dict() if self.room else {})
        return out


def judge(
    reading: Reading,
    want: Need,
    ours: Collection[int],
    *,
    allowance_mb: int,
    settled: bool,
    mark: str | None,
    what: str,
    fit: bool = False,
    room: Room | None = None,
) -> Verdict:
    """THE rule. Free = total - used (+ our own MiB where the driver gives it). Others = used - our own known
    MiB (card vram-spill cause 1: no idle allowance taken off, so free + others = total in every sentence).

    A load that is not fitted (the voice service; llama on an older build or with a layer count set on
    purpose) fits at free >= need + margin and is refused when it does not fit AND the need is measured AND
    the reading is settled (cause 2: never "load and hope"); `oversize` (it would not fit even beside only
    `allowance_mb`, an idle card and the studio holder's context) makes that NS-E203 instead of NS-E208.

    A fitted load (`fit`, llama.cpp's own fit, plan rule 3): it "fits" (the whole model on the card) at free
    >= need + `FIT_TARGET_MB`; otherwise llama.cpp keeps `need + target - free` in the PC's memory. It is
    refused ONLY when card + RAM cannot hold it, by MEASURED numbers on a SETTLED reading: free below the
    model's floor (every expert in RAM) + target; available RAM below that part + `RAM_SPARE_MB`; or commit
    headroom (against the limit the page file can grow to) below the process's private bytes + spare. A
    number not read never refuses."""
    own = sum(p.mb or 0 for p in reading.programs if p.pid in ours)
    free = reading.total_mb - reading.used_mb + own
    others = max(0, reading.used_mb - own)
    short = ""
    ram_part = 0
    if not fit:
        fits = free >= want.mb + MARGIN_MB
        oversize = want.mb + MARGIN_MB > reading.total_mb - allowance_mb
        refuse = not fits and want.measured and settled
    else:
        fits = free >= want.mb + FIT_TARGET_MB
        ram_part = max(0, want.mb + FIT_TARGET_MB - free)
        floor = want.floor_mb
        commit_room = room.commit_room_mb if room is not None else None
        if floor is not None and free < floor + FIT_TARGET_MB:
            short = "card"
        elif ram_part and room is not None and room.avail_mb is not None \
                and room.avail_mb < ram_part + RAM_SPARE_MB:  # fmt: skip
            short = "ram"
        elif want.commit_mb is not None and commit_room is not None \
                and commit_room < want.commit_mb + COMMIT_GROWTH_MB + RAM_SPARE_MB:  # fmt: skip
            short = "commit"
        oversize = floor is not None and floor + FIT_TARGET_MB > reading.total_mb - allowance_mb
        refuse = bool(short) and want.measured and settled
        if not refuse:
            short = ""
    named = [p for p in reading.programs if p.pid not in ours and p.name.lower() not in SYSTEM]
    named.sort(key=lambda p: -(p.mb or 0))
    unique: dict[str, Program] = {}
    for p in named:
        if p.name.lower() in unique:  # two processes of one program: one name, their MiB together
            q = unique[p.name.lower()]
            both = None if q.mb is None or p.mb is None else q.mb + p.mb
            unique[p.name.lower()] = Program(q.pid, q.name, both)
        else:
            unique[p.name.lower()] = p
    return Verdict(want, reading, free, others, allowance_mb, fits, settled, oversize, refuse,
                   tuple(unique.values()), mark, what, fit, room, ram_part, short)  # fmt: skip


# ------------------------------------------------------------------------------------------ the ladder
def ctx_of(d: ServiceDef) -> int | None:
    """The `-c` a command starts with (None: no context flag, as a stub or the voice service)."""
    ctx = _arg(d.cmd, "-c")
    return int(ctx) if ctx and ctx.isdigit() else None


def choose(
    paths: Paths, options: Sequence[ServiceDef], v: Verdict, ours: Collection[int] = (), *,
    model_dir: str | None = None, room: Room | None = None,
) -> tuple[Verdict, ServiceDef, str]:  # fmt: skip
    """Rule 2's ladder on the settled reading `v` (taken for `options[0]`): the verdict, the setting to load
    and its step (`STEP_WORDS`). `options` is `services.llama_ladder`: [as tuned, MTP off] (one entry for
    anything not fitted, which has no ladder: `v` decides, re-judged here).

    (a) as tuned, when its need is not measured yet (tried, measured; fit keeps it safe) or it fits the card
        with the fit target free;
    (b) the MTP draft off, when that fits (its own measured need, else the tuned need less `MTP_DRAFT_MB`);
    (c) as tuned with llama.cpp keeping experts in RAM, unless card + RAM cannot hold it (rule 3); then (c)
        with the MTP draft off when that is held; else the refusal (the tuned setting's verdict)."""

    def verdict(d: ServiceDef, want: Need, what: str) -> Verdict:
        return judge(v.reading, want, ours, allowance_mb=v.allowance_mb, settled=v.settled, mark=v.mark,
                     what=what, fit=fitted(d), room=room)  # fmt: skip

    first = options[0]
    va = verdict(first, need(paths, first, model_dir, v.mark), v.what)
    if not fitted(first):
        return va, first, ""
    if not va.need.measured or va.fits:
        return va, first, step_of(first)
    if len(options) < 2:  # (after a spill the retry's one option may be the MTP-off step: said as such)
        return va, first, "experts_in_ram_mtp_off" if step_of(first) == "mtp_off" else "experts_in_ram"
    second = options[1]
    nb = need(paths, second, model_dir, v.mark)
    if not nb.measured and mtp_on(first) and not mtp_on(second):
        nb = dataclasses.replace(nb, mb=max(1, va.need.mb - MTP_DRAFT_MB), measured=False,
                                 floor_mb=nb.floor_mb or va.need.floor_mb,
                                 commit_mb=nb.commit_mb or va.need.commit_mb)  # fmt: skip
    vb = verdict(second, nb, f"{v.what} (with the MTP draft off)")
    if vb.fits:
        return vb, second, "mtp_off"
    if not va.refuse:
        return va, first, "experts_in_ram"
    if not vb.refuse:
        return vb, second, "experts_in_ram_mtp_off"
    return va, first, "experts_in_ram"


def step_of(d: ServiceDef) -> str:
    """The ladder step a fitted llama definition IS (`ServiceDef.step`: "tuned" or "mtp_off"), "" for anything
    not fitted. Never a literal "tuned": the retry after a spill loads the MTP-off definition alone."""
    return (d.step or "tuned") if fitted(d) else ""


def spilled(total_mb: int, used_mb: int, fit: FitResult | None = None) -> bool:
    """Rule 4 after `ready`: the driver put part of the load in system memory. Less than `SPILL_FREE_MB` free
    on the card, or llama.cpp said its fit failed (it then loads as if fit were off). Shared memory is never
    looked at."""
    return total_mb - used_mb < SPILL_FREE_MB or (fit is not None and fit.failed)


#: What the memory of our own process that was just stopped is: (total used just before the stop, the MiB
#: its model was measured to take, or None when it never was).
Release = tuple[int, int | None]


def settled(used: int, prev: int | None, release: Release | None) -> bool:
    """Is a reading what others hold now, with nothing of ours still on its way out? With a release of ours
    pending: used has fallen by that model's measured MiB (within `SETTLE_SLACK_MB`), or, its MiB unknown,
    has fallen at all and stopped falling. Without one: it stopped falling (two readings within
    `SETTLE_STEP_MB`)."""
    falling = prev is None or prev - used > SETTLE_STEP_MB
    if release is None:
        return not falling
    before, gone = release
    if gone is not None:
        return used <= before - gone + SETTLE_SLACK_MB
    return used <= before - SETTLE_SLACK_MB and not falling


def check(
    smi: SmiSource,
    paths: Paths,
    d: ServiceDef,
    ours: Collection[int],
    *,
    model_dir: str | None = None,
    holder_mb: int = 0,
    release: Release | None = None,
    programs: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = SETTLE_POLL_S,
    room: Room | None = None,
) -> Verdict | None:
    """Is there room for `d`'s model now? `room`: the PC's memory, for a fitted load (`judge`).
    None: no reading (the caller loads as before and says once that
    the memory was not checked). Blocking (nvidia-smi, the settle reads): call it off the event loop.

    Settling: while a release of ours is pending, or the load does not fit, the card is read again every
    `poll_s` for at most `SETTLE_S` (`settled`). `programs`: list who is on the card whatever the answer
    (the words of a failed load); otherwise only when the load is refused."""
    mark = simulated_mark(smi)
    want = need(paths, d, model_dir, mark)
    mem = read_memory(smi)
    if mem is None:
        return None
    stored = idle_mb(paths, mark)
    allowance = (stored if stored is not None else IDLE_ALLOWANCE_MB) + holder_mb
    # bounded twice: at most SETTLE_READS readings AND SETTLE_S of real time (`time.monotonic`, which no
    # injected clock stops); it logs nothing, whatever the readings do
    deadline = time.monotonic() + SETTLE_S
    prev: int | None = None
    n = 1
    while True:
        used, total = mem
        calm = settled(used, prev, release)
        verdict = judge(Reading(total, used), want, ours, allowance_mb=allowance, settled=calm, mark=mark,
                        what=model_words(d), fit=fitted(d), room=room)  # fmt: skip
        if calm or (verdict.fits and release is None) or n >= SETTLE_READS or time.monotonic() >= deadline:
            break
        sleep(poll_s)
        nxt = read_memory(smi)
        if nxt is None:
            break
        prev, mem, n = used, nxt, n + 1
    if not (verdict.refuse or (programs and not verdict.fits)):
        return verdict
    full = Reading(verdict.reading.total_mb, verdict.reading.used_mb, read_programs(smi), True)
    return judge(full, want, ours, allowance_mb=allowance, settled=verdict.settled, mark=mark,
                 what=model_words(d), fit=fitted(d), room=room)  # fmt: skip


def test_wait_s(environ: Mapping[str, str], test_install: Callable[[], bool]) -> tuple[float, str | None]:
    """The wait bound: `VRAM_WAIT_S`, or a proof's `NS_TEST_VRAM_WAIT_S` (60 to 1800 s) on a test install
    (`devmode.test_install`, asked only when the variable is set); the second value is the sentence to log
    when the variable was set and not taken."""
    raw = environ.get(WAIT_ENV)
    if raw is None or not raw.strip():
        return float(VRAM_WAIT_S), None
    if not test_install():
        return float(VRAM_WAIT_S), f"{WAIT_ENV} is read only on a dev or engine-test install; ignored"
    value = raw.strip()
    if value.isdigit() and WAIT_MIN_S <= int(value) <= WAIT_MAX_S:
        return float(int(value)), None
    return float(VRAM_WAIT_S), f"{WAIT_ENV}={value!r} is not whole seconds from {WAIT_MIN_S} to {WAIT_MAX_S}"
