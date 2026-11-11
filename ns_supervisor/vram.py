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
which works under WDDM too), else the estimate
`model file MiB + ctx / 1024 x KV_MB_PER_1K_CTX + LOAD_OVERHEAD_MB`.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ns_common.atomic import read_text_retry, write_text_atomic
from ns_common.paths import Paths
from ns_supervisor.gpu import SmiSource, labelled
from ns_supervisor.services import ServiceDef

#: Memory a load must leave free beyond its need (a window opening, the driver's own growth).
MARGIN_MB = 512
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
    measured: bool  # False: the estimate


def _need_file(paths: Paths) -> Path:
    return paths.home / "run" / NEED_FILE


def _entries(paths: Paths) -> list[dict[str, Any]]:
    """The measured loads; a missing, torn or foreign file is none (the estimate is used, as before any)."""
    try:
        data = json.loads(read_text_retry(_need_file(paths), "utf-8-sig"))
    except (OSError, ValueError):
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def need(paths: Paths, d: ServiceDef, model_dir: str | None = None) -> Need:
    """The largest of the last `KEEP` measured loads of this exact model, else the estimate."""
    key = need_key(d)
    for e in _entries(paths):
        if all(e.get(k) == v for k, v in key.items()):
            mbs = [int(m) for m in e.get("mb") or [] if isinstance(m, int) and m > 0][-KEEP:]
            if mbs:
                return Need(max(mbs), True)
    return Need(estimate_mb(d, model_dir), False)


def record(paths: Paths, d: ServiceDef, mb: int, mark: str | None, now: datetime) -> None:
    """Keep one measured load (`mb` > 0) of `d`'s model; a simulated card's number says so (`card`). Raises
    OSError when the file cannot be written (the caller logs it; the next load measures again)."""
    if mb <= 0:
        return
    key = need_key(d)
    entries = _entries(paths)
    mine = next((e for e in entries if all(e.get(k) == v for k, v in key.items())), None)
    if mine is None:
        mine = dict(key)
        entries.append(mine)
    mine["mb"] = [*[m for m in mine.get("mb") or [] if isinstance(m, int)], int(mb)][-KEEP:]
    mine["at"] = now.strftime(_STAMP)
    if mark is not None:
        mine["card"] = mark
    else:
        mine.pop("card", None)
    path = _need_file(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps({"version": 1, "entries": entries}, indent=2) + "\n")


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
    """The check's answer: may the load go ahead, and the numbers and names its words are made of."""

    ok: bool
    need: Need
    reading: Reading
    free_mb: int  # total minus what others hold (our own known MiB taken off)
    others_mb: int  # used minus our own known MiB: other programs and Windows
    named: tuple[Program, ...] = ()  # other programs, not system ones, not ours
    mark: str | None = None
    what: str = "the model"

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
        about = "" if self.need.measured else "about "
        return f"{self.what} needs {about}{gb(self.need.mb)}"

    def detail(self, waited_s: float | None = None) -> str:
        """NS-E208's detail (the caller's error, the morning note): what held the memory, what was needed."""
        text = (f"{self.others(now=False)}, so {gb(self.free_mb)} were free and {self.needs()} with "
                f"{gb(MARGIN_MB)} to spare")  # fmt: skip
        if waited_s:
            text += f"; Nightshift waited {_minutes(waited_s)} for it"
        return labelled(self.mark, text) or text

    def waiting(self) -> str:
        """The Queue's sentence while a nightly acquire waits (`/status` `gpu_wait.said`)."""
        text = (f"Waiting for the graphics card's memory: {self.others(now=True)}, and {self.needs()}. "
                "Close them and the run goes on.")  # fmt: skip
        return labelled(self.mark, text) or text

    def numbers(self) -> dict[str, Any]:
        return {"need_mb": self.need.mb, "need_measured": self.need.measured, "free_mb": self.free_mb,
                "others_mb": self.others_mb, "total_mb": self.reading.total_mb,
                "names": [p.name for p in self.named]}  # fmt: skip


def judge(reading: Reading, want: Need, ours: Collection[int], *, mark: str | None, what: str) -> Verdict:
    """Free = total minus (used minus our own known MiB); the load goes ahead when free >= need + margin."""
    own = sum(p.mb or 0 for p in reading.programs if p.pid in ours)
    others = max(0, reading.used_mb - own)
    free = reading.total_mb - others
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
    return Verdict(
        free >= want.mb + MARGIN_MB, want, reading, free, others, tuple(unique.values()), mark, what
    )


def check(
    smi: SmiSource,
    paths: Paths,
    d: ServiceDef,
    ours: Collection[int],
    *,
    mark: str | None = None,
    model_dir: str | None = None,
) -> Verdict | None:
    """Is there room for `d`'s model now? None: no reading (the caller loads as before and says once that
    the memory was not checked). Blocking (nvidia-smi): call it off the event loop."""
    want = need(paths, d, model_dir)
    mem = read_memory(smi)
    if mem is None:
        return None
    used, total = mem
    verdict = judge(Reading(total, used), want, ours, mark=mark, what=model_words(d))
    if verdict.ok:  # (our own MiB taken off could only add room)
        return verdict
    full = Reading(total, used, read_programs(smi), True)  # short: who is on it; our own known MiB taken off
    return judge(full, want, ours, mark=mark, what=model_words(d))


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
