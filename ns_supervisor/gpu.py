"""C2 GPU readout for `Status.gpu` (CR-C3-UI-4), and the one way the engine asks nvidia-smi anything.

`SmiSource` is the seam: the status probe here and every helper in `procs` (`vram_mb`, `gpu_memory_used_mb`,
`nvidia_smi_report`, `gpu_name`) ask nvidia-smi through one, and turn what comes back into numbers with their
own parsers. `REAL_SMI` runs the real `nvidia-smi.exe`. `FakeSmi` is the simulated graphics card (`fake_gpu`,
docs/DEV_MODE.md "A simulated graphics card"): the ONLY fake of the card in this program. It stands where the
process would run, answers each query in the text nvidia-smi prints (including its failures: the tool missing,
the tool hanging, the driver error line), and takes its numbers from `run\\fake_gpu.json`, so the product's
parsers, the lock, the lease and the hand-over run unchanged above it. `smi_source` picks one per install; a
real install never gets the fake, whatever its files say.

The supervisor asks a `GpuProbe`; `NvidiaSmiGpuProbe` asks its source at most every `min_interval_s` and
caches the answer (including "absent"), so polling `/status` never spawns a process per request. A probe
returns None
when there is nothing to report (no NVIDIA driver, the `cpu-dev` profile, nvidia-smi failing); the status
object then carries `gpu: null`. A reading of the simulated card says `simulated: true`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from ns_common import devmode
from ns_common.atomic import read_text_retry
from ns_common.logging import NsLogger
from ns_common.models import GpuStatus, Runtime
from ns_common.paths import Paths
from ns_supervisor.childenv import child_env

QUERY = "name,utilization.gpu,memory.used,memory.total,temperature.gpu"
PROBE_ARGS = (f"--query-gpu={QUERY}", "--format=csv,noheader,nounits")
_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MIN_INTERVAL_S = 2.0
PROBE_TIMEOUT_S = 5.0       # the status probe (it runs under /status)
QUERY_TIMEOUT_S = 20.0      # the helpers in `procs`
SMI = "nvidia-smi"

#: What the real tool runs: `(args, timeout_s)` -> the finished process, or OSError / SubprocessError.
Runner = Callable[[list[str], float], "subprocess.CompletedProcess[str]"]


def run_real(args: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
    """Run the real `nvidia-smi` found on PATH with `args`; not on PATH: FileNotFoundError, no spawn."""
    exe = shutil.which(SMI)
    if exe is None:
        raise FileNotFoundError(2, "nvidia-smi is not on PATH", SMI)
    return subprocess.run(
        [exe, *args],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
        env=child_env(),
        creationflags=_NO_WINDOW,
    )


class SmiSource:
    """nvidia-smi as the engine sees it: `query(args)` is its standard output, or None when the tool is
    absent, fails (any exit code but 0, such as the driver error) or does not answer within `timeout_s`.
    `simulated` says the answers come from the simulated card (`FakeSmi`)."""

    def __init__(self, runner: Runner = run_real, *, simulated: bool = False) -> None:
        self.runner = runner
        self.simulated = simulated

    def query(self, args: Sequence[str], timeout_s: float = QUERY_TIMEOUT_S) -> str | None:
        try:
            r = self.runner(list(args), timeout_s)
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError):  # (a name the code page cannot read)
            return None
        return r.stdout if r.returncode == 0 else None


REAL_SMI = SmiSource()

#: The words every reader of the card's numbers puts beside them when they come from the simulated card.
SIMULATED = "simulated (fake_gpu)"


def simulated_mark(source: SmiSource | None) -> str | None:
    """THE mark of a simulated reading (H-10): `"simulated (fake_gpu)"` when `source` is the simulated card,
    None for the real tool. Every reader of the card's state (the status probe, `ns doctor` and its zip,
    `ns verify`, any report or ledger written from these numbers) asks this one function and either puts the
    mark beside what it shows or refuses to give the number as evidence (tests/test_fake_gpu.py fails on a
    module that asks `smi_source` without it)."""
    return SIMULATED if source is not None and source.simulated else None


#: What a check that would measure the card gives instead on the simulated one (it is no evidence).
SIMULATED_EVIDENCE = "simulated graphics card (fake_gpu)"


def labelled(mark: str | None, value: str | None) -> str | None:
    """`value` with the mark in front when it comes from the simulated card: `simulated (fake_gpu): ...`."""
    return f"{mark}: {value}" if mark is not None and value is not None else value


class GpuProbe(Protocol):
    """Anything that can report the GPU's current numbers."""

    def sample(self) -> GpuStatus | None:
        """Latest reading, or None when no GPU readout is available."""
        ...


def _num(text: str) -> float | None:
    """A float from one CSV cell; None for `[N/A]`, `[Not Supported]` and anything non-numeric."""
    try:
        return float(text.strip())
    except ValueError:
        return None


def parse_nvidia_smi(output: str) -> GpuStatus | None:
    """Parse `nvidia-smi --query-gpu=<QUERY> --format=csv,noheader,nounits`; the first GPU wins."""
    for line in output.splitlines():
        if not line.strip():
            continue
        # The name comes first and may contain a comma in theory; the four numbers are always last.
        parts = line.rsplit(",", 4)
        if len(parts) != 5 or not parts[0].strip():
            continue
        load, used, total, temp = (_num(p) for p in parts[1:])
        return GpuStatus(
            name=parts[0].strip(),
            load_pct=load,
            vram_used_mb=int(used) if used is not None else None,
            vram_total_mb=int(total) if total is not None else None,
            temp_c=temp,
        )
    return None


def run_nvidia_smi() -> str | None:
    """The status query on the real tool; None when nvidia-smi is absent, fails or hangs."""
    return REAL_SMI.query(PROBE_ARGS, PROBE_TIMEOUT_S)


class NvidiaSmiGpuProbe:
    """Cached nvidia-smi sampler: one query at most every `min_interval_s`, thread-safe. `simulated`: the
    runner asks the simulated card, so every reading says so."""

    def __init__(
        self,
        *,
        min_interval_s: float = MIN_INTERVAL_S,
        runner: Callable[[], str | None] = run_nvidia_smi,
        clock: Callable[[], float] = time.monotonic,
        simulated: bool = False,
    ) -> None:
        self.min_interval_s = min_interval_s
        self._runner = runner
        self._clock = clock
        self.simulated = simulated
        self._lock = threading.Lock()
        self._at: float | None = None
        self._value: GpuStatus | None = None

    def sample(self) -> GpuStatus | None:
        """The cached reading while it is younger than `min_interval_s`, else a fresh one."""
        with self._lock:
            now = self._clock()
            if self._at is not None and now - self._at < self.min_interval_s:
                return self._value
            out = self._runner()
            value = parse_nvidia_smi(out) if out is not None else None
            if value is not None and self.simulated:
                value = value.model_copy(update={"simulated": True})
            self._value = value
            self._at = now
            return self._value


def source_probe(source: SmiSource) -> NvidiaSmiGpuProbe:
    """The status probe that asks `source` (the real tool, or the simulated card)."""
    return NvidiaSmiGpuProbe(
        runner=lambda: source.query(PROBE_ARGS, PROBE_TIMEOUT_S),
        simulated=simulated_mark(source) is not None,
    )


class NullGpuProbe:
    """No GPU readout (the `cpu-dev` profile without the simulated card)."""

    def sample(self) -> GpuStatus | None:
        return None


# ------------------------------------------------------------------------------------ the simulated card
SIMULATED_NAME = "Simulated GPU (fake_gpu)"
DEFAULT_TOTAL_MB = 16311            # the RTX 5060 Ti of the GPU PC (tests/fixtures/supervisor)
SERVICE_OVERHEAD_MB = 500           # a tenant without its own number: its model file plus this
DEFAULT_BASE_MB = 800               # the GPU PC with nothing of ours loaded used 812 MiB (the fixture)
PER_PROCESS = ("na", "mb")
PROC_TYPES = ("C", "G", "C+G")
#: The services that are tenants of the simulated card and run a process (whisper runs in the studio).
TENANTS: tuple[str, ...] = ("llama", "tts")
#: What nvidia-smi prints (exit code 9) when it cannot reach the driver.
DRIVER_ERROR = (
    "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver. Make sure that the latest "
    "NVIDIA driver is installed and running.\n\n"
)
FAILS = ("missing", "hang", "driver")
NA = "[N/A]"                        # a CSV cell the driver does not report
_IMAGE = {"llama": "llama-server.exe", "tts": "python.exe"}
_UNIT = {"memory.used": "MiB", "memory.total": "MiB", "memory.free": "MiB", "utilization.gpu": "%",
         "used_memory": "MiB"}  # fmt: skip


@dataclass
class Card:
    """`run\\fake_gpu.json` as read: every field checked, a bad one replaced by its default (`problems`)."""

    name: str = SIMULATED_NAME
    total_mb: int = DEFAULT_TOTAL_MB
    base_mb: int = DEFAULT_BASE_MB      # in use with nothing of ours loaded (the desktop, the driver)
    load_pct: float | None = 0.0        # None: the driver reports `[N/A]`
    temp_c: float | None = 40.0
    driver: str = "580.88"
    cuda: str = "13.0"
    per_process: str = "na"             # "na": `[N/A]` per process, as under WDDM (the GPU PC); "mb": MiB
    others: list[tuple[int, str, str, int]] = field(default_factory=list)  # pid, type, process name, MiB
    services: dict[str, int] = field(default_factory=dict)                 # tenant -> MiB
    fail: str | None = None
    delay_s: float = 0.0
    problems: list[str] = field(default_factory=list)


def _int(v: object) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _real(v: object) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def read_card(data: object) -> Card:
    """The card a parsed `fake_gpu.json` describes. Out of range or wrong type: that field's default (MB
    values are held to 0..total), each named in `problems`."""
    card = Card()
    if not isinstance(data, dict):
        card.problems.append("the file is not a JSON object")
        return card
    bad = card.problems.append
    if "name" in data:
        if isinstance(data["name"], str) and data["name"].strip():
            card.name = data["name"].strip()
        else:
            bad("name")
    if "total_mb" in data:
        total = _int(data["total_mb"])
        if total is not None and total > 0:
            card.total_mb = total
        else:
            bad("total_mb")
    for key, lo, hi in (("load_pct", 0.0, 100.0), ("temp_c", 0.0, 120.0), ("delay_s", 0.0, 600.0)):
        if key in data:
            v = _real(data[key])
            if data[key] is None and key != "delay_s":
                setattr(card, key, None)        # the driver does not report it: `[N/A]`
            elif v is not None and lo <= v <= hi:
                setattr(card, key, v)
            else:
                bad(key)
    if "per_process" in data:
        if data["per_process"] in PER_PROCESS:
            card.per_process = str(data["per_process"])
        else:
            bad("per_process")
    for key in ("driver", "cuda"):
        if key in data:
            if isinstance(data[key], str) and data[key].strip():
                setattr(card, key, data[key].strip())
            else:
                bad(key)
    if "fail" in data and data["fail"] is not None:
        if data["fail"] in FAILS:
            card.fail = str(data["fail"])
        else:
            bad("fail")

    def mb(v: object, what: str) -> int:
        n = _int(v)
        if n is None:
            bad(what)
            return 0
        if not 0 <= n <= card.total_mb:
            bad(what)
        return max(0, min(n, card.total_mb))

    if "base_mb" in data:
        card.base_mb = mb(data["base_mb"], "base_mb")
    others = data.get("others", [])
    if not isinstance(others, list):
        bad("others")
        others = []
    for i, o in enumerate(others):
        pid = _int(o.get("pid")) if isinstance(o, dict) else None
        if not isinstance(o, dict) or pid is None or pid <= 0:
            bad(f"others[{i}]")
            continue
        name = o.get("name") if isinstance(o.get("name"), str) and o.get("name") else "app.exe"
        kind = o.get("type", "C")
        if kind not in PROC_TYPES:
            bad(f"others[{i}].type")
            kind = "C"
        card.others.append((pid, str(kind), str(name), mb(o.get("mb", 0), f"others[{i}].mb")))
    services = data.get("services", {})
    if not isinstance(services, dict):
        bad("services")
        services = {}
    for name, value in services.items():
        if name in TENANTS:
            card.services[name] = mb(value, f"services.{name}")
        else:
            bad(f"services.{name}")
    return card


class FakeSmi:
    """The simulated graphics card's nvidia-smi: a `Runner` for `SmiSource`.

    Every call reads `run\\fake_gpu.json` again (a proof may change it while the engine runs; the status probe
    caches 2 s as it does for the real tool). Missing: the defaults. Unreadable or partly wrong: the defaults
    for what is wrong, and one WARN `fake_gpu_file` per content. The engine's own tenants (`tenants()`: pid ->
    service, else the children in `run\\supervisor.json`) use the MiB the file gives each service, by default
    the size of the model file plus 500 (llama) or 500 (the voice service); the file's `others` are
    programs that are not Nightshift's (`type` C, G or C+G as the table prints it; only C is a compute app).
    memory.used is `base_mb` (800, the GPU PC's idle 812 MiB) plus every program's MiB. Per process the
    default is what the GPU PC prints under WDDM: `[N/A]` (the table: N/A), so `procs.vram_mb` is None there
    as on the target; `per_process: "mb"` gives the MiB instead (a TCC or Linux driver; not the GPU PC).
    For the GPU PC's recorded state the table is byte for byte the recording (tests/test_fake_gpu.py).
    `fail` makes it fail as the real tool does: `missing` (not found),
    `hang` (no answer within the caller's timeout), `driver` (exit code 9 and the driver error line).
    `delay_s` answers that much later (a delay at or past the timeout is a hang).
    """

    def __init__(
        self,
        paths: Paths,
        *,
        runtime: Runtime | None = None,
        tenants: Callable[[], Mapping[int, str]] | None = None,
        log: NsLogger | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.paths = paths
        self.runtime = runtime
        self.tenants = tenants
        self.log = log
        self.sleep = sleep
        self.now = now
        self._warned: str | None = None     # the content the last WARN was about
        self._lock = threading.Lock()

    @property
    def file(self) -> Path:
        return devmode.fake_gpu_file(self.paths)

    # ------------------------------------------------------------------ the card as the file says
    def card(self) -> Card:
        try:
            text = read_text_retry(self.file, "utf-8-sig")
        except FileNotFoundError:
            return Card()
        except OSError as e:
            card, text = Card(problems=[f"unreadable ({e})"]), f"<{e}>"
        else:
            try:
                card = read_card(json.loads(text))
            except ValueError as e:
                card = Card(problems=[f"not JSON ({e})"])
        with self._lock:
            if card.problems and self._warned != text:
                self._warned = text
                if self.log is not None:
                    self.log.warn(
                        "fake_gpu_file",
                        f"{self.file}: the defaults are used for {', '.join(card.problems)}",
                        path=str(self.file),
                    )
            elif not card.problems:
                self._warned = None
        return card

    def _tenants(self) -> dict[int, str]:
        if self.tenants is not None:
            return {int(pid): name for pid, name in self.tenants().items() if name in TENANTS}
        try:
            data = json.loads(read_text_retry(self.paths.home / "run" / "supervisor.json", "utf-8-sig"))
            children = data.get("children") if isinstance(data, dict) else None
        except (OSError, ValueError):
            return {}
        if not isinstance(children, dict):
            return {}
        return {pid: name for name, pid in children.items() if name in TENANTS and isinstance(pid, int)}

    def _model_mb(self, service: str) -> int:
        if service != "llama":
            return SERVICE_OVERHEAD_MB
        rt = self.runtime
        if rt is None:
            try:
                rt = Runtime.model_validate_json(read_text_retry(self.paths.runtime_file, "utf-8-sig"))
            except (OSError, ValueError):
                return SERVICE_OVERHEAD_MB
        try:
            size = Path(rt.llama.model_path).stat().st_size if rt.llama.model_path else 0
        except OSError:
            size = 0
        return size // (1024 * 1024) + SERVICE_OVERHEAD_MB

    def processes(self, card: Card) -> list[tuple[int, str, str, int]]:
        """(pid, type, process name, MiB) of every program on the card: the engine's tenants (compute, `C`),
        then the others."""
        ours = [
            (pid, "C", _IMAGE.get(svc, "python.exe"),
             min(card.services.get(svc, self._model_mb(svc)), card.total_mb))
            for pid, svc in sorted(self._tenants().items())
        ]  # fmt: skip
        return ours + card.others

    @staticmethod
    def used_mb(card: Card, procs: list[tuple[int, str, str, int]]) -> int:
        """memory.used: what is in use with nothing of ours loaded, plus every program's MiB (whether or not
        the driver reports them per process), held to the card's total."""
        return min(card.total_mb, card.base_mb + sum(p[3] for p in procs))

    # ------------------------------------------------------------------ the tool
    def __call__(self, args: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        argv = [SMI, *args]
        card = self.card()
        if card.fail == "missing":
            raise FileNotFoundError(2, "The system cannot find the file specified", SMI)
        if card.fail == "hang" or card.delay_s >= timeout_s:
            self.sleep(timeout_s)
            raise subprocess.TimeoutExpired(argv, timeout_s)
        if card.delay_s:
            self.sleep(card.delay_s)
        if card.fail == "driver":
            return subprocess.CompletedProcess(argv, 9, DRIVER_ERROR, "")
        code, out = self._answer(args, card)
        return subprocess.CompletedProcess(argv, code, out, "")

    def _answer(self, args: list[str], card: Card) -> tuple[int, str]:
        if not args:
            return 0, self._report(card)
        if args == ["-L"]:
            return 0, f"GPU 0: {card.name} (UUID: GPU-00000000-0000-0000-0000-000000000000)\n"
        query = next((a for a in args if a.startswith(("--query-gpu=", "--query-compute-apps="))), None)
        fmt = next((a.split("=", 1)[1].split(",") for a in args if a.startswith("--format=")), None)
        if query is None or fmt is None or "csv" not in fmt or len(args) != 2:
            return 2, "Invalid combination of input arguments. Please run 'nvidia-smi -h' for help.\n\n"
        kind, names = query.split("=", 1)
        fields = [f.strip() for f in names.split(",")]
        units = "nounits" not in fmt
        procs = self.processes(card)
        used = self.used_mb(card, procs)
        if kind == "--query-gpu":
            values = {
                "name": card.name, "memory.used": used,
                "utilization.gpu": NA if card.load_pct is None else round(card.load_pct),
                "memory.total": card.total_mb, "memory.free": card.total_mb - used,
                "temperature.gpu": NA if card.temp_c is None else round(card.temp_c),
                "driver_version": card.driver,
            }  # fmt: skip
            rows: list[list[Any]] = [[values.get(f) for f in fields]]
        else:
            # compute processes only (a C+G or G program such as dwm.exe is not listed); under WDDM the
            # driver gives no per-process memory: `[N/A]`, which is what the GPU PC prints
            compute = [p for p in procs if p[1] == "C"]
            per = {"pid": lambda p: p[0], "process_name": lambda p: p[2], "name": lambda p: p[2],
                   "used_memory": lambda p: p[3] if card.per_process == "mb" else NA}  # fmt: skip
            rows = [[per[f](p) if f in per else None for f in fields] for p in compute]
        bad = next((f for f, v in zip(fields, rows[0] if rows else [], strict=False) if v is None), None)
        if bad is None and kind == "--query-compute-apps":
            bad = next((f for f in fields if f not in ("pid", "process_name", "name", "used_memory")), None)
        if bad is not None:
            return 2, f'Field "{bad}" is not a valid field to query.\n\n'
        lines = [] if "noheader" in fmt else [", ".join(_head(f) for f in fields)]
        for row in rows:
            lines.append(", ".join(_cell(f, v, units) for f, v in zip(fields, row, strict=True)))
        return 0, "".join(line + "\n" for line in lines)

    def _report(self, card: Card) -> str:
        """The plain `nvidia-smi` table, laid out as the real one (tests/fixtures/supervisor)."""
        procs = self.processes(card)
        used = self.used_mb(card, procs)
        bar = "+-----------------------------------------------------------------------------------------+"
        cells = "+-----------------------------------------+------------------------+----------------------+"
        top = f"NVIDIA-SMI {card.driver:<23}Driver Version: {card.driver:<15}CUDA Version: {card.cuda:<9}"
        when = self.now()
        temp = "N/A" if card.temp_c is None else f"{round(card.temp_c)}C"
        load = "N/A" if card.load_pct is None else f"{round(card.load_pct)}%"
        out = [
            f"{when:%a %b} {when.day:>2} {when:%H:%M:%S %Y}",  # nvidia-smi pads the day with a space
            bar,
            f"| {top}|",
            "|-----------------------------------------+------------------------+----------------------+",
            "| GPU  Name                  Driver-Model | Bus-Id          Disp.A | Volatile Uncorr. ECC |",
            "| Fan  Temp   Perf          Pwr:Usage/Cap |           Memory-Usage | GPU-Util  Compute M. |",
            "|                                         |                        |               MIG M. |",
            "|=========================================+========================+======================|",
            f"| {'  0  ' + card.name[:26]:<34}WDDM  |   00000000:01:00.0  On |                  N/A |",
            f"|  30%  {temp:>4}    P8              9W /  180W | "
            f"{f'{used}MiB /  {card.total_mb}MiB':>22} | {load:>7}      Default |",
            "|                                         |                        |                  N/A |",
            cells,
            "",
            bar,
            "| Processes:                                                                              |",
            "|  GPU   GI   CI              PID   Type   Process name                        GPU Memory |",
            "|        ID   ID                                                               Usage      |",
            "|=========================================================================================|",
        ]
        if not procs:
            out.append(f"|  {'No running processes found':<86} |")
        for pid, kind, name, mb in procs:
            mem = f"{mb}MiB" if card.per_process == "mb" else "N/A"
            out.append(f"|    0   N/A  N/A{pid:>16}{kind:>7}   {_proc_name(name):<25}{mem:>19}      |")
        out += [bar, ""]
        return "\n".join(out)


def _head(f: str) -> str:
    unit = _UNIT.get(f)
    return f"{f} [{unit}]" if unit else f


def _cell(f: str, v: Any, units: bool) -> str:
    unit = _UNIT.get(f)
    return f"{v} {unit}" if units and unit and v != NA else str(v)


def _proc_name(name: str) -> str:
    """The process name as the table prints it: 25 characters, a longer one cut at the front with `...`."""
    return name if len(name) <= 25 else "..." + name[-22:]


def smi_source(
    paths: Paths,
    runtime: Runtime | None = None,
    *,
    tenants: Callable[[], Mapping[int, str]] | None = None,
    log: NsLogger | None = None,
) -> SmiSource:
    """The nvidia-smi of this install: the simulated card when `fake_gpu` is in effect (the switch on a dev
    or engine-test install: `devmode.fake_gpu_in` of `runtime`, else of the home's runtime.json), else the
    real tool. A real install never gets the fake, whatever its files say."""
    data = runtime.model_dump(mode="json") if runtime is not None else None
    on = devmode.fake_gpu_in(data) if runtime is not None else devmode.fake_gpu(paths)
    if not on:
        return REAL_SMI
    return SmiSource(FakeSmi(paths, runtime=runtime, tenants=tenants, log=log), simulated=True)
