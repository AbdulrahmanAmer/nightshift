"""The GPU kit (card gpu-kit, block E): one command the owner or partner runs on the GPU PC.

    GPU-KIT.cmd -> dev\\kit\\gpukit.ps1 (finds the install) -> this file, with the install's own Python.

It records the PC, runs the checks that fit a real install, makes one real story video on its own paused
channel, keeps Automatic runs on for two nights and, on a later run, collects everything into ONE zip under
`<home>\\kit\\` for the person to send back. It talks to the product only through its routes (the dashboard
API on 127.0.0.1, `ns` and `studio` of the install), reads the product's files and sends nothing anywhere.

Steps, state in `<home>\\kit\\<version>\\kit-state.json` (written atomically after each step's result): K0
start, K1 this PC, K2 each model once and the proofs, K3 the story video, K4 arm two nights, K5 night 1, K6
night 2, K7 collect and put back; K8 the person's questions (offered from K3 on, every run until answered).
Every step is safe to run twice; whatever stops the kit, the next run continues from the step it reached. A
failed step still ends in the zip. The kit records facts; whether the nights passed is the Head's reading.

`main(argv, env=...)` takes stand-ins for the clock, the product and the Windows readers (tests); the real
Windows readers refuse to run under pytest.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

KIT_DIR = Path(__file__).resolve().parent
ROOT = KIT_DIR.parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))  # after the venv: the install's own product modules win

from dev.prove_ctx import SetupError, read_install  # noqa: E402

CHANNEL = "gpu-kit"
GUARD_S = 30 * 60  # the time guard's spare: the video must end this long before the nightly run
NIGHT_LIMIT_S = 6 * 3600  # the task's limit (schedule_cfg.TASK_TIME_LIMIT_S): a night is read after it
NIGHT_WINDOW_S = 30 * 60  # a run row belongs to a night from this long before its run time
DRY_NIGHT_LIMIT_S = 1200  # -NightNow: the test night limit (NS_TEST_NIGHT_LIMIT_S)
POLL_S = 60.0
OUT_LIMIT = 20_000
ASK_TIMEOUT_S = 15 * 60
EVENT_IDS = (1, 42, 41, 1074, 6006, 6008)  # power and restart events only
STEPS = ("K0", "K1", "K2", "K3", "K4", "K5", "K6", "K7")
TITLES = {"K0": "start", "K1": "record this PC", "K2": "test each model and run the proofs",
          "K3": "make one story video", "K4": "switch Automatic runs on for two nights", "K5": "night 1",
          "K6": "night 2", "K7": "collect everything and put Automatic runs back"}  # fmt: skip
PLAN = (
    "What the kit does, in order. You can close this window at any time: run GPU-KIT.cmd again and it goes",
    "on from where it stopped. Nothing is sent anywhere; the only changes are the kit's own paused channel",
    "and Automatic runs for two nights (switched off again at the end if they were off).",
    "  1. Start: checks this is the GPU install (seconds)",
    "  2. Record this PC: graphics card, driver, disk, sleep settings, model files (up to 15 minutes)",
    "  3. Test each model once and run the proof scripts (10 to 40 minutes)",
    "  4. Make one story video with the real models on the kit's own paused channel (about 3 hours)",
    "  5. Switch Automatic runs on for the next two nights if they are off (seconds); the kit then stops",
    "  6. and 7. The two nights run by themselves; run GPU-KIT.cmd again after the second morning",
    "  8. Collect everything into one zip and put Automatic runs back (about 5 minutes)",
    "  Then a few questions only you can answer (about 15 minutes; 'later' keeps them for the next run).",
)


class Refused(Exception):
    """K0 refuses to start (exit 2): the words say what is wrong and the way out. Nothing was changed."""


# ---------------------------------------------------------------------------------------------- stand-ins
class Clock(Protocol):
    def now(self) -> datetime: ...
    def sleep(self, seconds: float) -> None: ...


class RealClock:
    def __init__(self, offset: timedelta = timedelta(0)) -> None:
        self.offset = offset

    def now(self) -> datetime:
        return datetime.now().astimezone() + self.offset

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class Proc(Protocol):
    pid: int

    def poll(self) -> int | None: ...
    def kill(self) -> None: ...


Answer = tuple[int | None, Any]


class Product(Protocol):
    """The install's own routes: the dashboard API, `ns`, `studio`, its Python, FFmpeg and nightly stub."""

    def api(self, method: str, path: str, body: Any = None, timeout: float = 60) -> Answer: ...
    def cli(self, program: str, args: list[str], timeout: float) -> tuple[int | None, str]: ...
    def start_studio(self, args: list[str], log: Path) -> Proc: ...
    def alive(self, pid: int, created: float | None) -> bool: ...
    def created(self, pid: int) -> float | None: ...
    def prove(self, args: list[str], timeout: float) -> tuple[int | None, str]: ...
    def start_engine(self, timeout: float) -> tuple[bool, str]: ...
    def night_now(self, limit_s: int, timeout: float) -> tuple[int | None, str]: ...
    def frame_grid(self, video: Path, out: Path, timeout: float) -> str | None: ...


class Windows(Protocol):
    """What only Windows says (read only): nvidia-smi, the disk, the power plan, the System log."""

    def nvidia_smi(self, args: list[str]) -> str | None: ...
    def physical_disk(self, drive: str) -> str | None: ...
    def powercfg(self, args: list[str]) -> str | None: ...
    def events(self, since: datetime, until: datetime) -> str | None: ...


@dataclass
class KitEnv:
    clock: Clock | None = None
    product: Product | None = None
    windows: Windows | None = None
    ask: Callable[[str], str] | None = None
    say: Callable[[str], None] = print


def _run(argv: list[str], timeout: float, env: dict[str, str] | None = None) -> tuple[int | None, str]:
    """A program's exit code and output (its last OUT_LIMIT characters); rc None when it timed out."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=env, check=False)  # fmt: skip
    except subprocess.TimeoutExpired:
        return None, f"did not finish in {timeout:.0f} s"
    except OSError as e:
        return 1, f"could not be started: {e}"
    return done.returncode, ((done.stdout or "") + (done.stderr or ""))[-OUT_LIMIT:]


POWERSHELL = ["powershell.exe", "-NoProfile", "-NonInteractive"]


class InstallProduct:
    """The real routes of the install at `home`. The dashboard is only ever asked on 127.0.0.1."""

    def __init__(self, home: Path, port: int) -> None:
        self.home, self.port = home, port
        self.env = {**os.environ, "NIGHTSHIFT_HOME": str(home), "NS_HOME_EXPLICIT": str(home)}

    def _exe(self, program: str) -> str:
        venv = "supervisor" if program == "ns" else "studio"
        return str(self.home / "venvs" / venv / "Scripts" / f"{program}.exe")

    def api(self, method: str, path: str, body: Any = None, timeout: float = 60) -> Answer:
        import urllib.error
        import urllib.request

        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"})  # fmt: skip
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as resp:
                status, raw = resp.status, resp.read(4_000_000)
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read(4_000_000)
        except OSError:
            return None, None
        try:
            return status, json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return status, raw.decode("utf-8", "replace")

    def cli(self, program: str, args: list[str], timeout: float) -> tuple[int | None, str]:
        return _run([self._exe(program), *args], timeout, self.env)

    def start_studio(self, args: list[str], log: Path) -> Proc:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as out:
            return subprocess.Popen([self._exe("studio"), *args], stdout=out, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=self.env, cwd=str(self.home))  # fmt: skip

    def created(self, pid: int) -> float | None:
        import psutil

        try:
            return float(psutil.Process(pid).create_time())
        except (psutil.Error, OSError):
            return None

    def alive(self, pid: int, created: float | None) -> bool:
        now = self.created(pid)
        return now is not None and (created is None or abs(now - created) < 1.0)

    def prove(self, args: list[str], timeout: float) -> tuple[int | None, str]:
        return _run([sys.executable, str(ROOT / "dev" / "prove.py"), *args], timeout, self.env)

    def start_engine(self, timeout: float) -> tuple[bool, str]:
        nsup = str(KIT_DIR / "tools" / "nsup.ps1")
        rc, text = _run([*POWERSHELL, "-ExecutionPolicy", "Bypass", "-File", nsup, "-NsHome", str(self.home)],
                        timeout, self.env)  # fmt: skip
        return rc == 0, text

    def night_now(self, limit_s: int, timeout: float) -> tuple[int | None, str]:
        stub = str(self.home / "nightshift-launch.ps1")  # the task's action, without the task
        return _run([*POWERSHELL, "-ExecutionPolicy", "Bypass", "-File", stub, "-Nightly", "-Studio",
                     self._exe("studio")], timeout, {**self.env, "NS_TEST_NIGHT_LIMIT_S": str(limit_s)})

    def frame_grid(self, video: Path, out: Path, timeout: float) -> str | None:
        from ns_common import ffmpeg

        rc, text = _run([ffmpeg.ffmpeg_exe(), "-y", "-v", "error", "-i", str(video), "-vf",
                         "fps=1/60,scale=320:-1,tile=4x4", "-frames:v", "1", str(out)], timeout)  # fmt: skip
        return None if rc == 0 and out.is_file() else (text or f"FFmpeg exit {rc}")


class RealWindows:
    """The PC's own readers, read only, no elevation. Refused under pytest (tests pass canned answers)."""

    def __init__(self, home: Path) -> None:
        self.home = home

    @staticmethod
    def _guard(what: str) -> None:
        from ns_common import testguard

        testguard.refuse_real(f"this PC's {what}", "Pass a Windows stand-in: gpukit.main(env=KitEnv(...)).")

    def nvidia_smi(self, args: list[str]) -> str | None:
        self._guard("nvidia-smi")
        from ns_common.paths import Paths
        from ns_supervisor import gpu

        # the install's own source: the simulated card on a fake_gpu test install (simulated_mark says so)
        return gpu.smi_source(Paths(self.home)).query(args, 30)

    def physical_disk(self, drive: str) -> str | None:
        self._guard("disks")
        # the drive letter goes in as data (an environment variable), never into the script's text
        script = ("$n = (Get-Partition -DriveLetter $env:NS_KIT_DRIVE).DiskNumber; Get-PhysicalDisk | "
                  "Where-Object { $_.DeviceId -eq [string]$n } | "
                  "Select-Object FriendlyName, MediaType, BusType, Size | "
                  "ConvertTo-Json -Compress")  # fmt: skip
        rc, text = _run([*POWERSHELL, "-Command", script], 60, {**os.environ, "NS_KIT_DRIVE": drive[:1]})
        return text if rc == 0 else None

    def powercfg(self, args: list[str]) -> str | None:
        self._guard("power settings")
        rc, text = _run(["powercfg.exe", *args], 30)
        return text if rc == 0 else None

    def events(self, since: datetime, until: datetime) -> str | None:
        self._guard("Windows event log")
        fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        a, b = since.astimezone(UTC), until.astimezone(UTC)
        span = f"@SystemTime>='{a:{fmt}}' and @SystemTime<='{b:{fmt}}'"
        ids = " or ".join(f"EventID={i}" for i in EVENT_IDS)
        rc, text = _run(["wevtutil", "qe", "System", f"/q:*[System[({ids}) and TimeCreated[{span}]]]",
                         "/rd:false", "/c:200", "/f:text"], 60)  # fmt: skip
        return text if rc == 0 else None


def timed_input(prompt: str, timeout_s: float = ASK_TIMEOUT_S) -> str:
    """One answer typed in this window, or 'later' when nobody answers within `timeout_s` (or no console)."""
    print(prompt, end=" ", flush=True)
    if sys.platform != "win32" or not sys.stdin or not sys.stdin.isatty():
        line = sys.stdin.readline() if sys.stdin else ""
        return line.strip() if line else "later"
    import msvcrt

    end, typed = time.monotonic() + timeout_s, ""
    while time.monotonic() < end:
        if not msvcrt.kbhit():
            time.sleep(0.05)
            continue
        ch = msvcrt.getwche()
        if ch in "\r\n":
            print()
            return typed.strip()
        typed = typed[:-1] if ch == "\b" else typed + ch
    print("(no answer: later)")
    return "later"


# ---------------------------------------------------------------------------------------------- the scrubber
#: What `scrub_text` masks in every text file of the bundle (the bundle's README lists it too).
MASKED = (
    "the stock-footage keys (Pexels: 56 letters and digits; Pixabay: digits-32 hex) and any value named "
    "key, token, secret, password or authorization",
    "GitHub tokens and Bearer tokens",
    "the Windows user name (and the profile folder C:\\Users\\<name>)",
    "the install's home path",
    "the PC's name",
)
_SECRET_NAMED = re.compile(
    r"""(?i)(["']?[\w-]*(?:api[_-]?key|key|token|secret|password|passwd|authorization)["']?\s*[:=]\s*)"""
    r"""("[^"\r\n]*"|'[^'\r\n]*'|[^\s,;&"'{}\[\]]+)"""
)
_SECRET_SHAPES = (
    re.compile(r"\b\d{6,10}-[0-9a-f]{32}\b"),  # Pixabay
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{56}(?![A-Za-z0-9])"),  # Pexels
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
)
TEXT_SUFFIXES = {".json", ".jsonl", ".txt", ".log", ".md", ".csv", ".xml", ".html", ".yaml", ".yml", ".ps1"}


@dataclass(frozen=True)
class Masks:
    """The PC-specific words to mask: the home path, the user's name and profile folder, the PC's name."""

    home: str
    user: str
    profile: str
    computer: str

    @classmethod
    def here(cls, home: Path) -> Masks:
        e = os.environ
        return cls(str(home), e.get("USERNAME", ""), e.get("USERPROFILE", ""), e.get("COMPUTERNAME", ""))


def scrub_text(text: str, masks: Masks) -> str:
    """`text` with every item of MASKED replaced: secrets by `***`, the home by `<home>`, the profile folder
    by `<profile>`, the user name by `<user>`, the PC's name by `<pc>` (any letter case; backslashes as
    written, doubled as in JSON, or forward slashes)."""
    for rx in _SECRET_SHAPES:
        text = rx.sub("***", text)
    def hide(m: re.Match[str]) -> str:  # a quoted value keeps its quotes: the JSON stays readable
        value = m.group(2)
        return m.group(1) + (value[0] + "***" + value[0] if value[:1] in ("'", '"') else "***")

    text = _SECRET_NAMED.sub(hide, text)
    for value, word in ((masks.home, "<home>"), (masks.profile, "<profile>")):
        if len(value) >= 3:
            for form in {value, value.replace("\\", "\\\\"), value.replace("\\", "/")}:
                text = re.sub(re.escape(form), word, text, flags=re.IGNORECASE)
    # the PC's name first: it may hold the user's name ("AHMED-PC")
    for value, word in ((masks.computer, "<pc>"), (masks.user, "<user>")):
        if len(value) >= 2:
            text = re.sub(rf"(?i)(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])", word, text)
    return text


def scrub_zip(src: Path, out: zipfile.ZipFile, prefix: str, masks: Masks) -> None:
    """Every entry of the zip `src` into `out` under `prefix`; text entries and every name scrubbed."""
    with zipfile.ZipFile(src) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            data = z.read(info)
            if Path(info.filename).suffix.lower() in TEXT_SUFFIXES:
                data = scrub_text(data.decode("utf-8", "replace"), masks).encode("utf-8")
            out.writestr(scrub_text(prefix + info.filename, masks), data)


# ---------------------------------------------------------------------------------------------- the kit
@dataclass
class Result:
    result: str  # PASS | FAIL | WAIT | NOT RUN
    line: str
    done: bool = True  # the next run does not redo this step
    stop: bool = False  # the steps after it do not run now


QUESTIONS: dict[str, tuple[str, ...]] = {
    "watch": (
        "Open the kit's video (its path is above). Does the picture move with the story all the way "
        "through, with no black or frozen part?",
        "Is the narration clear and at one steady level from start to end?",
        "Do the captions show the words that are spoken, at the time they are spoken?",
    ),
    "paste": (  # dev/kit/K8_PASTE_CHECK.md: its steps, these five questions and the extra checks below
        "Paste check (follow dev\\kit\\K8_PASTE_CHECK.md first). Was the title accepted as pasted?",
        "Was the description accepted as pasted (nothing cut, chapters shown after saving)?",
        "Were the tags accepted as pasted (all there, counter at or under 500)?",
        "Could the altered-content answer be set as the sheet says?",
        "Could the made-for-kids answer be set as the sheet says?",
    ),
    "morning": (
        "Open Nightshift's Dashboard, Queue page. Is there a note about the last night at the top?",
        "Does the note say what the night did (videos made, or why not) as the Queue shows it?",
    ),
}
PASTE_EXTRA = (
    ("field_name", "Step 7: what is the altered-content field called in Studio?"),
    ("E1", "E1: the title counter (100/100 or 101/100) and whether Save was allowed"),
    ("E2", "E2: the tag counter and whether all ten tags stayed"),
    ("E3", "E3: the description counters (Queue copy, Notepad copy)"),
    ("E4", "E4: which part of #true-crime became a link"),
)


@dataclass
class Kit:
    home: Path
    version: str
    kind: str
    dry_run: bool
    night_now: bool
    env: KitEnv
    clock: Clock
    product: Product
    windows: Windows
    ask: Callable[[str], str]
    ev: Path = field(init=False)
    state: dict[str, Any] = field(default_factory=dict)
    mark: str | None = None
    lock: Path | None = None

    def __post_init__(self) -> None:
        self.ev = self.home / "kit" / self.version

    def close(self) -> None:
        """Give the kit lock back (only ours: `lock` is set once `take_lock` succeeded)."""
        if self.lock is not None:
            with contextlib.suppress(OSError):
                self.lock.unlink()
            self.lock = None

    # -------------------------------------------------------------------------------------- small helpers
    def say(self, text: str) -> None:
        self.env.say(text)

    def now(self) -> datetime:
        return self.clock.now()

    def iso(self) -> str:
        return self.now().isoformat(timespec="seconds")

    def save(self) -> None:
        from ns_common.atomic import write_text_atomic

        text = json.dumps(self.state, indent=2, default=str) + "\n"
        write_text_atomic(self.ev / "kit-state.json", text, keep_backup=True)

    def write_json(self, name: str, data: Any) -> Path:
        from ns_common.atomic import write_text_atomic

        return write_text_atomic(self.ev / name, json.dumps(data, indent=2, default=str) + "\n")

    def label(self, step: str) -> str:
        return f"Step {STEPS.index(step) + 1} of {len(STEPS)} ({TITLES[step]})"

    @staticmethod
    def at(iso: str) -> datetime:
        return datetime.fromisoformat(iso).astimezone()

    @staticmethod
    def when(t: datetime) -> str:
        return t.strftime("%A %d %B %H:%M")

    def come_back(self, when: datetime) -> datetime:
        self.state["come_back"] = when.isoformat(timespec="seconds")
        return when

    def db(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Rows of a read-only query on the install's studio.db (none when it cannot be read)."""
        path = self.home / "data" / "studio.db"
        if not path.is_file():
            return []
        con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, params).fetchall()]
        except sqlite3.Error:
            return []
        finally:
            con.close()

    def ledger(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for f in sorted((self.home / "data" / "ledger").glob("ledger-*.jsonl")):
            with contextlib.suppress(OSError):
                for text in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    with contextlib.suppress(ValueError):
                        item = json.loads(text)
                        if isinstance(item, dict):
                            out.append(item)
        return out

    def settings(self) -> dict[str, Any] | None:
        status, body = self.product.api("GET", "/api/settings")
        return body if status == 200 and isinstance(body, dict) else None

    def engine(self) -> str | None:
        """The dashboard answers (started with the kit's nsup when it did not: then stopped by `finish`)."""
        if self.product.api("GET", "/api/health", timeout=10)[0] == 200:
            return None
        self.state["engine_started_by_kit"] = True
        self.save()  # before the start: a kill from here on still stops it at the end of the next run
        ok, text = self.product.start_engine(300)
        if ok and self.product.api("GET", "/api/health", timeout=10)[0] == 200:
            return None
        return f"Nightshift's engine did not start ({text[-300:].strip()})"

    def run_times(self, after: datetime, count: int) -> list[datetime]:
        """The next `count` nightly run times after `after`, at the install's run time (Settings)."""
        _status, body = self.product.api("GET", "/api/status")
        hhmm = str(body.get("schedule_time") or "03:00") if isinstance(body, dict) else "03:00"
        hh, mm = (int(x) for x in hhmm.split(":"))
        first = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        first = first if first > after else first + timedelta(days=1)
        return [first + timedelta(days=i) for i in range(count)]

    # -------------------------------------------------------------------------------------- K1 this PC
    def k1(self) -> Result:
        w, rec = self.windows, {"simulated": self.mark, "at": self.iso()}
        smi = rec["nvidia_smi"] = {
            "query_gpu": w.nvidia_smi(["--query-gpu=name,driver_version,memory.total,memory.used,memory.free",
                                       "--format=csv,noheader"]),
            "table": w.nvidia_smi([]),
            "compute_apps": w.nvidia_smi(["--query-compute-apps=pid,used_memory", "--format=csv,noheader"]),
        }  # fmt: skip
        found = {k: re.search(rf"{k} Version:\s*([\d.]+)", smi["table"] or "") for k in ("Driver", "CUDA")}
        line = ((smi["query_gpu"] or "").strip().splitlines() or [""])[0]
        cells = [c.strip() for c in line.split(",")] if line else []
        cell = (lambda i: cells[i] if len(cells) > i else None)  # noqa: E731
        rec["gpu"] = {"name": cell(0), "driver": found["Driver"] and found["Driver"].group(1),
                      "cuda": found["CUDA"] and found["CUDA"].group(1), "vram_total": cell(2),
                      "vram_used": cell(3)}  # fmt: skip
        drive = os.path.splitdrive(str(self.home))[0] or str(self.home)[:2]
        usage = shutil.disk_usage(self.home)
        rec["disk"] = {"drive": drive, "free_gb": round(usage.free / 1e9, 1),
                       "total_gb": round(usage.total / 1e9, 1), "physical": w.physical_disk(drive)}
        rec["power"] = {"wake_timers": w.powercfg(["/q", "SCHEME_CURRENT", "SUB_SLEEP", "RTCWAKE"]),
                        "sleep_states": w.powercfg(["/a"])}  # fmt: skip
        import platform

        rec.update({"windows": platform.platform(), "cpu": [platform.processor(), os.cpu_count()]})
        with contextlib.suppress(Exception):
            import psutil

            rec["ram_gb"] = round(psutil.virtual_memory().total / 1e9, 1)
        with contextlib.suppress(OSError, ValueError, AttributeError):
            runtime = (self.home / "config" / "runtime.json").read_text("utf-8-sig")
            rec["llama"] = json.loads(runtime).get("llama")
        rec["schedule"] = self.product.cli("ns", ["schedule", "show"], 60)
        rec["doctor"] = self.product.cli("ns", ["doctor"], 300)
        check = self.home / "logs" / "model-check.json"
        fresh = check.is_file() and time.time() - check.stat().st_mtime < 86400
        rec["models"] = (None, "skipped: logs\\model-check.json is under a day old") if fresh else \
            self.product.cli("ns", ["verify", "--downloads"], 45 * 60)  # fmt: skip
        self.write_json("machine.json", rec)
        if not rec["gpu"]["name"]:
            return Result("FAIL", "the graphics card's driver did not answer (nvidia-smi). What to do: "
                          "install the newest NVIDIA driver, restart the PC, then run GPU-KIT.cmd again.",
                          done=False, stop=True)  # fmt: skip
        # (a dry run's test install has stub models: their check is recorded, not a stop)
        if not fresh and rec["models"][0] != 0 and not self.dry_run:
            return Result("FAIL", "a model file is missing or damaged (ns verify --downloads, in "
                          "machine.json). What to do: run INSTALL.cmd again (it repairs the downloads), then "
                          "run GPU-KIT.cmd again.", done=False, stop=True)  # fmt: skip
        rows = (rec["doctor"][1] or "").splitlines()
        fails = [ln.strip() for ln in rows if ln.strip().startswith("[FAIL]")]
        mark = f"{self.mark}: " if self.mark else ""
        return Result("PASS", f"recorded this PC: {mark}{rec['gpu']['name']}, driver {rec['gpu']['driver']}, "
                      f"{rec['gpu']['vram_total']} of video memory; {drive} has {rec['disk']['free_gb']} GB "
                      f"free; ns doctor: {'; '.join(fails) or 'no FAIL row'}")  # fmt: skip

    # -------------------------------------------------------------------------------------- K2 checks
    def k2(self) -> Result:
        rc, text = self.product.cli("ns", ["verify"], 40 * 60)
        self.write_json("verify.json", {"exit": rc, "output": text})
        if rc != 0:
            return Result("FAIL", "a model did not answer its short test (ns verify; its report "
                          "logs\\verify-report.json is in the zip). What to do: send the zip.",
                          done=False, stop=True)  # fmt: skip
        args = ["--home", str(self.home), "--dept", "kit", "--lock-file", str(self.ev / "kit.lock"),
                "--tools", str(KIT_DIR / "tools"), "--runs", str(self.ev / "proofs"), "--all", "--strict",
                "--no-window"]  # fmt: skip
        prc, out = self.product.prove(args, 60 * 60)
        self.write_json("proofs.json", {"exit": prc, "output": out})
        tail = (out or "").strip().splitlines()
        summary = next((ln for ln in reversed(tail) if " passed, " in ln), tail[-1] if tail else "no output")
        if prc == 2 and "no proofs to run" in (out or ""):
            return Result("PASS", "every model answered its short test; no proof fits this install")
        # a failed proof is a fact for the Head: the kit goes on
        word = "PASS" if prc == 0 else "FAIL"
        return Result(word, f"every model answered its short test; proofs: {summary}")

    # -------------------------------------------------------------------------------------- K3 story video
    def kit_jobs(self) -> list[dict[str, Any]]:
        """The jobs of the kit's channel (`data/jobs/*/job.json`): the kit's own, oldest first."""
        out = []
        for f in (self.home / "data" / "jobs").glob("*/job.json"):
            with contextlib.suppress(OSError, ValueError):
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("channel") == CHANNEL:
                    out.append(data)
        return sorted(out, key=lambda d: str(d.get("created_at")))

    def long_rows(self, job_id: str | None) -> list[dict[str, Any]]:
        return self.db("select output_id, path, duration_s from outputs where job_id = ? and kind = 'long'",
                       (job_id or "",))  # fmt: skip

    def estimate_s(self) -> float:
        """The worst of this PC's last five full videos (the ledger), else night-fits' default (3 h)."""
        done = [float(x.get("seconds") or 0) for x in self.ledger() if x.get("kind") == "job_end"
                and x.get("plan") == "master_first" and x.get("outcome") == "done"]  # fmt: skip
        if done:
            return max(done[-5:])
        try:
            from ns_studio.nightfit import DEFAULT_JOB_S

            return float(DEFAULT_JOB_S["master_first"])
        except (ImportError, KeyError):
            return 3 * 3600.0

    def guard(self, est: float) -> Result | None:
        """H-14's time guard: the video (estimate + 30 min) must end before the next nightly run."""
        now = self.now()
        nxt = self.run_times(now, 1)[0]
        if now + timedelta(seconds=est + GUARD_S) <= nxt:
            return None
        h = f"{est / 3600:.1f}"
        back = self.come_back(nxt + timedelta(seconds=NIGHT_LIMIT_S))
        return Result("WAIT", f"not started now: the video takes about {h} h here plus 30 minutes to spare, "
                      f"and the nightly run starts at {self.when(nxt)} (in {_span(now, nxt)}). Run "
                      f"GPU-KIT.cmd again after {self.when(back)}, or earlier on a day with {h} h and 30 "
                      "minutes before the run time.", done=False, stop=True)  # fmt: skip

    def channel(self) -> str | None:
        """The kit's channel exists and is active (made from the first active channel); None, or why not."""
        status, body = self.product.api("GET", f"/api/channels/{CHANNEL}")
        if status == 200 and isinstance(body, dict):
            if (body.get("profile") or {}).get("status") == "paused":
                st, _ = self.product.api("POST", f"/api/channels/{CHANNEL}/resume", {"note": "GPU kit video"})
                return None if st == 200 else f"the kit's channel could not be resumed (HTTP {st})"
            return None
        _st, cards = self.product.api("GET", "/api/channels")
        first = next((c for c in cards if isinstance(c, dict) and c.get("status") == "active"
                      and c.get("slug") != CHANNEL), None) if isinstance(cards, list) else None  # fmt: skip
        profile: dict[str, Any] = {}
        if first:
            _st, got = self.product.api("GET", f"/api/channels/{first['slug']}")
            profile = dict(got.get("profile") or {}) if isinstance(got, dict) else {}
        keep = ("language", "voice", "niche", "seed_keywords", "audience", "made_for_kids", "fiction_label",
                "long_minutes", "pools")  # fmt: skip
        body = {"niche": "short scary stories", **{k: profile[k] for k in keep if k in profile},
                "slug": CHANNEL, "display_name": "GPU kit (test video)", "status": "active",
                "format": "master_first"}  # fmt: skip
        self.state["channel_made"] = True
        self.save()
        st, answer = self.product.api("POST", "/api/channels", body)
        why = f"the kit's channel could not be made (HTTP {st}: {answer})"[:300]
        return None if st in (201, 409) else why

    def pause_channel(self) -> bool:
        """The kit's channel is paused (or does not exist); False when the Dashboard could not make it so."""
        if self.engine() is not None:
            return False
        status, body = self.product.api("GET", f"/api/channels/{CHANNEL}")
        if status == 404:
            return True
        if status == 200 and isinstance(body, dict) and (body.get("profile") or {}).get("status") == "paused":
            return True
        note = {"note": "GPU kit: one video only"}
        st, _ = self.product.api("POST", f"/api/channels/{CHANNEL}/pause", note)
        return st == 200

    def k3(self) -> Result:
        job = self.state.setdefault("job", {})
        if job.get("outcome") in ("done", "failed"):
            return self.k3_after(job)
        if job.get("pid") and self.product.alive(job["pid"], job.get("pid_created")):
            self.say("The kit's video is still being made by the studio started last time; waiting for it.")
            res = self.k3_wait(job, None)
            if res is not None:
                return res
        if job.get("id") and self.long_rows(job["id"]):  # it finished while nobody watched: not touched
            job.update({"outcome": "done", "ended": job.get("ended") or self.iso()})
            self.save()
            return self.k3_after(job)
        if self.db("select 1 from runs where mode = 'daily' and finished_at is null"):
            return Result("WAIT", "a nightly run is working now (or one was cut off and is closed by the "
                          "next start of Nightshift). Run GPU-KIT.cmd again in the afternoon.",
                          done=False, stop=True)  # fmt: skip
        est = self.estimate_s()
        why = self.engine()
        held = None if why else self.guard(est)
        if held is not None:
            return held
        why = why or self.channel()
        if why:
            return Result("FAIL", f"{why}. What to do: send the zip.", done=False, stop=True)
        found = self.kit_jobs()
        if not job.get("id") and found:  # made by a run killed before it wrote the id: that job is ours
            job["id"] = found[-1]["job_id"]
        args = ["run", "--resume", job["id"]] if job.get("id") else \
            ["run", "--channel", CHANNEL, "--count", "1", "--format", "master_first"]  # fmt: skip
        job.setdefault("started", self.iso())
        self.save()  # the intent first: a kill from here on finds the job through its channel
        what = f"continuing job {job['id']}" if job.get("id") else "a new job"
        self.say(f"Making the story video ({what}), about {est / 3600:.1f} h. You can use the PC; leave it "
                 "on.")  # fmt: skip
        proc = self.product.start_studio(args, self.ev / "story" / "studio-run.log")
        job.update({"pid": proc.pid, "pid_created": self.product.created(proc.pid),
                    "runs": job.get("runs", 0) + 1})  # fmt: skip
        self.save()
        return self.k3_wait(job, proc, est) or Result("FAIL", "the studio ended without a result", done=False)

    def k3_wait(self, job: dict[str, Any], proc: Proc | None, est: float | None = None) -> Result | None:
        """Wait (bounded: three estimates and an hour) for the studio. The result, or None when the studio
        of an EARLIER run ended without finishing the job (it is resumed now)."""
        bound, begun, rc = 3 * (est or self.estimate_s()) + 3600, self.now(), None
        while True:
            if not job.get("id") and self.kit_jobs():
                job["id"] = self.kit_jobs()[-1]["job_id"]
                self.save()
            if proc is not None:
                rc = proc.poll()
            elif not self.product.alive(job["pid"], job.get("pid_created")):
                rc = -1
            if rc is not None:
                break
            if (self.now() - begun).total_seconds() > bound:
                if proc is not None:
                    proc.kill()  # the kit's own child, by its handle
                job.update({"outcome": "failed", "code": "not finished in three times its estimate"})
                break
            self.clock.sleep(POLL_S)
            stages = [str(x.get("stage")) for x in self.ledger() if x.get("kind") == "stage"
                      and x.get("job") == job.get("id") and x.get("outcome") == "done"]  # fmt: skip
            self.say(f"  still making the video: {_span(begun, self.now())}; stages done: "
                     f"{', '.join(stages) or 'none yet'}")  # fmt: skip
        if job.get("outcome") != "failed":
            if self.long_rows(job.get("id")) and (rc == 0 or proc is None):
                job.update({"outcome": "done", "exit": rc if proc is not None else None})
            elif proc is None:
                job["pid"] = None  # the studio of last time ended unfinished: it is resumed now
                self.save()
                return None
            else:
                log = self.ev / "story" / "studio-run.log"
                text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
                codes = re.findall(r"NS-[EW]\d{3}", text)
                job.update({"outcome": "failed", "exit": rc, "code": codes[-1] if codes else f"exit {rc}"})
        job["ended"] = self.iso()
        self.save()
        return self.k3_after(job)

    def k3_after(self, job: dict[str, Any]) -> Result:
        if not self.pause_channel():  # first, also after a kill between the job's end and the pause
            return Result("FAIL", f"the kit's channel '{CHANNEL}' could not be paused. What to do: pause it "
                          "in Channels, then run GPU-KIT.cmd again.", done=False, stop=True)  # fmt: skip
        story = self.ev / "story"
        story.mkdir(parents=True, exist_ok=True)
        rows = self.long_rows(job.get("id"))
        stages = [{k: x.get(k) for k in ("stage", "seconds", "outcome", "at")} for x in self.ledger()
                  if x.get("kind") == "stage" and x.get("job") == job.get("id")]  # fmt: skip
        facts: dict[str, Any] = {**job, "simulated": self.mark, "stages": stages}
        took = _span(job.get("started"), job.get("ended"))
        if job.get("outcome") != "done" or not rows:
            self.write_json("story/story.json", facts)
            return Result("FAIL", f"the video stopped ({job.get('code')}) after {took}; its log goes into "
                          "the zip. Nothing to do now: the kit goes on with the nights.")  # fmt: skip
        video = Path(rows[0]["path"])
        video = video if video.is_absolute() else self.home / video
        facts.update({"video": str(video), "length_s": rows[0]["duration_s"],
                      "output_id": rows[0]["output_id"],
                      "lufs": _number(self.home / "data" / "jobs" / str(job["id"]), "lufs")})  # fmt: skip
        for suffix in (".txt", ".json"):
            if video.with_suffix(suffix).is_file():
                shutil.copy2(video.with_suffix(suffix), story / ("sheet" + suffix))
        if not (story / "frames.png").is_file() and video.is_file():
            facts["grid_problem"] = self.product.frame_grid(video, story / "frames.png", 600)
        self.write_json("story/story.json", facts)
        length = float(facts["length_s"] or 0)
        return Result("PASS", f"video made: {video} ({int(length // 60)} min {int(length % 60)} s long, made "
                      f"in {took}); the kit's channel is paused")  # fmt: skip

    # -------------------------------------------------------------------------------------- K4 arm
    def k4(self) -> Result:
        settings = self.settings()
        if settings is None:
            why = self.engine()
            settings = self.settings()
            if settings is None:
                return Result("FAIL", f"the Dashboard did not answer ({why}). What to do: send the zip.",
                              done=False, stop=True)  # fmt: skip
        if self.state.get("automatic_runs_before") is None:
            self.state["automatic_runs_before"] = bool(settings.get("schedule_registered"))
            self.save()  # FIRST: whatever happens next, the next run knows what to put back
        if self.night_now:
            self.state["automatic_note"] = "not changed: -NightNow runs each night itself, through the stub"
        elif not self.state["automatic_runs_before"]:
            if not settings.get("schedule_registered"):  # (on already: a run killed after its PUT)
                self.say("Switching Automatic runs on for two nights (they are off now; the kit switches "
                         "them off again at the end).")  # fmt: skip
                status, body = self.product.api("PUT", "/api/settings", {"automatic_runs": True})
                if status != 200 or not isinstance(body, dict) or not body.get("schedule_registered"):
                    note = (body.get("schedule_result") or {}).get("note") if isinstance(body, dict) else body
                    return Result("FAIL", f"Automatic runs could not be switched on ({note}). What to do: "
                                  "switch them on in Settings, then run GPU-KIT.cmd again.",
                                  done=False, stop=True)  # fmt: skip
            self.state["turned_on"] = True
        armed = self.at(self.state["armed_at"]) if self.state.get("armed_at") else self.now()
        nights = [armed, armed] if self.night_now else self.run_times(armed, 2)
        self.state.update({"armed_at": armed.isoformat(timespec="seconds"),
                           "nights": [{"run_at": t.isoformat(timespec="seconds")} for t in nights]})
        self.save()
        if self.night_now:
            return Result("PASS", "dry run: the two nights run now, one after the other, through the stub")
        back = self.come_back(nights[1] + timedelta(seconds=NIGHT_LIMIT_S))
        self.save()
        return Result("PASS", f"Automatic runs are on for the next two nights ({self.when(nights[0])} and "
                      f"{self.when(nights[1])}). Leave the PC on or asleep (not shut down) and plugged in. "
                      f"Run GPU-KIT.cmd again after {self.when(back)}.", stop=True)  # fmt: skip

    # -------------------------------------------------------------------------------------- K5, K6 nights
    def night_rows(self, i: int, run_at: datetime) -> list[dict[str, Any]]:
        """The `runs` rows of mode daily that belong to night `i`: started after `armed_at`, from half an
        hour before its run time to half an hour before the next day's (a late start counts)."""
        lo = run_at - timedelta(seconds=NIGHT_WINDOW_S)
        hi = lo + timedelta(days=1)
        armed = self.at(self.state["armed_at"]) - timedelta(seconds=1)
        rows = [r for r in self.db("select run_id, started_at, finished_at, summary_json from runs where "
                                   "mode = 'daily' order by started_at")  # fmt: skip
                if armed <= self.at(r["started_at"])
                and (self.night_now or lo <= self.at(r["started_at"]) < hi)]
        return rows[i:i + 1] if self.night_now else rows[:1]

    def night(self, i: int) -> Result:
        night = self.state["nights"][i]
        run_at = self.at(night["run_at"])
        rows = self.night_rows(i, run_at)
        if self.night_now and not rows:  # a dry-run night with no row never began its work: run it now
            self.say(f"Dry run: night {i + 1} starts now (limit {DRY_NIGHT_LIMIT_S // 60} min).")
            night["ran_now"] = self.iso()
            self.save()
            rc, text = self.product.night_now(DRY_NIGHT_LIMIT_S, DRY_NIGHT_LIMIT_S + 900)
            night["stub"] = {"exit": rc, "output": text[-2000:]}
            rows = self.night_rows(i, run_at)
        late = self.night_now or self.now() >= run_at + timedelta(seconds=NIGHT_LIMIT_S)
        if rows and (rows[0]["finished_at"] or late):
            row = rows[0]
            summary = _loads(row.get("summary_json"))
            night.update({"status": "ran", "run_id": row["run_id"], "start": row["started_at"],
                          "end": row["finished_at"], "summary": summary})  # fmt: skip
            jobs = len(summary.get("jobs") or []) if isinstance(summary, dict) else 0
            end = (row["finished_at"] or "?")[11:16]
            words = f"ran {row['started_at'][11:16]} to {end}, {jobs} job(s) done"
            if not row["finished_at"]:
                words = f"started {row['started_at'][11:16]}; its run was never closed (ended from outside)"
        elif late:
            task = self.product.cli("ns", ["schedule", "show"], 60)
            night.update({"status": "did not run", "schedule": task})
            words = "did not run (Windows' answer for the task is in the zip)"
        else:
            back = self.come_back(run_at + timedelta(seconds=NIGHT_LIMIT_S))
            self.save()
            return Result("WAIT", f"Night {i + 1}: not yet (come back after {self.when(back)})",
                          done=False, stop=True)  # fmt: skip
        self.save()
        return Result("PASS", f"Night {i + 1}: {words}")

    # -------------------------------------------------------------------------------------- K7 collect
    def k7(self) -> Result:
        col = self.ev / "collected"
        col.mkdir(parents=True, exist_ok=True)
        for i, night in enumerate(self.state.get("nights") or []):
            start = self.at(night.get("start") or night["run_at"]) - timedelta(minutes=30)
            end = self.at(night.get("end") or night["run_at"]) + timedelta(seconds=NIGHT_LIMIT_S)
            text = self.windows.events(start, end) or "the System log could not be read\n"
            (col / f"windows-events-night{i + 1}.txt").write_text(text, encoding="utf-8")
        rc, text = self.product.cli("ns", ["doctor", "--bundle", "--out-dir", str(col)], 600)
        (col / "doctor-bundle.txt").write_text(f"exit {rc}\n{text}", encoding="utf-8")
        if self.state.get("turned_on") and not self.state.get("restored"):
            settings = self.settings() if self.engine() is None else None
            if settings is not None and settings.get("schedule_registered"):
                status, _ = self.product.api("PUT", "/api/settings", {"automatic_runs": False})
                settings = None if status != 200 else {}
            if settings is None:
                return Result("FAIL", "Automatic runs could not be switched off again. What to do: switch "
                              "them off in Settings (they were off before the kit), then run GPU-KIT.cmd "
                              "again.", done=False, stop=True)  # fmt: skip
            self.state["restored"] = True
            self.save()
        said = "Automatic runs are off again, as they were before the kit" if self.state.get("restored") \
            else "Automatic runs were left as you had them"  # fmt: skip
        return Result("PASS", f"collected the nights' records; {said}")

    # -------------------------------------------------------------------------------------- K8 questions
    def wants(self, group: str) -> bool:
        steps = self.state.get("steps", {})
        ready = (steps.get("K6") or {}).get("done") if group == "morning" else \
            (steps.get("K3") or {}).get("result") == "PASS"  # fmt: skip
        answers = self.state.get("answers", {}).get(group, {})
        open_extra = group == "paste" and any(answers.get("extra", {}).get(k) in (None, "", "later")
                                              for k, _ in PASTE_EXTRA)  # fmt: skip
        return bool(ready) and (open_extra or any(answers.get(f"q{n}", {}).get("answer") not in ("y", "n")
                                                  for n in range(1, len(QUESTIONS[group]) + 1)))

    def k8(self) -> None:
        """The person's checklists: every answer is saved as it is given; `later` keeps a question open."""
        groups = [g for g in QUESTIONS if self.wants(g)]
        if groups:
            self.say("Questions only you can answer. Type y, n (then what you saw) or later; then Enter.")
        for group in groups:
            answers = self.state.setdefault("answers", {}).setdefault(group, {})
            for n, question in enumerate(QUESTIONS[group], 1):
                if answers.get(f"q{n}", {}).get("answer") in ("y", "n"):
                    continue
                reply = (self.ask(f"{question} [y/n/later]") or "later").strip()
                word = reply[:1].lower() if reply[:1].lower() in ("y", "n") else "later"
                note = reply[1:].strip(" :-,") if word == "n" else ""
                if word == "n" and not note:
                    note = (self.ask("What did you see? (one line)") or "").strip()
                answers[f"q{n}"] = {"question": question, "answer": word, "note": note, "at": self.iso()}
                self.save()
            if group == "paste":
                extra = answers.setdefault("extra", {})
                for key, prompt in PASTE_EXTRA:
                    if extra.get(key) in (None, "", "later"):
                        extra[key] = (self.ask(f"{prompt} (or later)") or "later").strip()
                        self.save()
            self.write_json(f"{group}.json", answers)

    # -------------------------------------------------------------------------------------- the zip
    def zip_path(self) -> Path:
        day = self.at(self.state["started"]).strftime("%Y%m%d")
        return self.home / "kit" / f"nightshift-gpu-kit-{self.version}-{day}.zip"

    def parts(self) -> list[tuple[str, Path, str]]:
        """(name in the zip, file, why it may be missing): the Head's checklist (card 4b.3)."""
        ev, home = self.ev, self.home
        out = [(n, ev / n, w) for n, w in (
            ("machine.json", "step 2 has not run yet"), ("verify.json", "step 3 has not run yet"),
            ("proofs.json", "step 3 has not run yet"), ("story/story.json", "step 4 has not finished"),
            ("story/frames.png", "no finished video (or FFmpeg failed: story.json says why)"),
            ("story/sheet.txt", "no finished video"), ("story/sheet.json", "no finished video"),
            ("story/studio-run.log", "the kit started no video"), ("watch.json", "not answered yet"),
            ("paste.json", "not answered yet"), ("morning.json", "not answered yet"))]  # fmt: skip
        out += [("logs/verify-report.json", home / "logs" / "verify-report.json", "ns verify wrote none"),
                ("run/vram-need.json", home / "run" / "vram-need.json", "no model load was measured yet"),
                ("run/morning_note.json", home / "run" / "morning_note.json", "no morning note waits")]
        nights = self.state.get("nights") or []
        if nights:
            ledger = sorted((home / "data" / "ledger").glob("ledger-*.jsonl"))
            out += [(f"ledger/{f.name}", f, "") for f in ledger]
            days = sorted({(self.at(n["run_at"]) + timedelta(days=d)).strftime("%Y%m%d") for n in nights
                           for d in (0, 1)})  # fmt: skip
            for day in days:
                out.append((f"logs/nightly-{day}.json", home / "logs" / f"nightly-{day}.json",
                            "no nightly report for that date"))  # fmt: skip
                out += [(f"logs/{f.name}", f, "") for f in sorted((home / "logs").glob(f"*-{day}.jsonl"))]
        for folder in (ev / "collected", ev / "proofs"):
            if folder.is_dir():
                files = [p for p in sorted(folder.rglob("*")) if p.is_file()]
                out += [(p.relative_to(ev).as_posix(), p, "") for p in files]
        return out

    def bundle(self) -> Path:
        """ONE zip of everything there is now (rewritten whole, atomically); a missing part named with why."""
        from ns_common.atomic import write_zip_atomic

        masks, missing = Masks.here(self.home), []

        def fill(z: zipfile.ZipFile) -> None:
            z.writestr("README.txt", self.readme())
            for name, path, why in [*self.parts(), ("kit-state.json", self.ev / "kit-state.json", "")]:
                _put(z, name, path, masks, missing, why)
            z.writestr("MISSING.txt", "\n".join(missing) + "\n" if missing else "nothing is missing\n")

        target = write_zip_atomic(self.zip_path(), fill)
        self.state["zip"] = str(target)
        self.save()
        return target

    def readme(self) -> str:
        top = (f"SIMULATED GRAPHICS CARD ({self.mark}): this zip comes from a test install. Nothing in it is "
               "evidence of the GPU PC." if self.mark else "Real graphics card: the GPU PC's own readings.")
        lines = [top, f"Nightshift GPU kit {self.version}, install kind {self.kind}"
                 + (" (dry run)" if self.dry_run else ""), ""]  # fmt: skip
        for s in STEPS:
            rec = self.state.get("steps", {}).get(s) or {}
            lines.append(f"{s} {TITLES[s]}: {rec.get('result', 'NOT RUN')}  {rec.get('line', 'not reached')}")
        lines += ["", "Masked in every text file:", *(f"- {m}" for m in MASKED), "",
                  "The kit records facts; whether the nights passed is read by the Head from them."]
        return scrub_text("\n".join(lines) + "\n", Masks.here(self.home))

    # -------------------------------------------------------------------------------------- the run
    def run(self) -> int:
        steps = self.state.setdefault("steps", {})
        fns: dict[str, Callable[[], Result]] = {"K1": self.k1, "K2": self.k2, "K3": self.k3, "K4": self.k4,
                                                "K5": lambda: self.night(0), "K6": lambda: self.night(1),
                                                "K7": self.k7}  # fmt: skip
        stopped: str | None = None
        failed = False
        self.state.pop("come_back", None)
        try:
            for step in STEPS[1:]:
                rec = steps.get(step) or {}
                if rec.get("done"):
                    self.say(f"{rec['result']:7} {self.label(step)}: {rec['line']} (done on an earlier run)")
                    continue
                if stopped:
                    steps[step] = {"result": "NOT RUN" if failed else "WAIT", "line": stopped, "done": False}
                    self.save()
                    self.say(f"{steps[step]['result']:7} {self.label(step)}: {stopped}")
                    continue
                self.say(f"{self.label(step)}: starting.")
                begun = self.now()
                res = fns[step]()
                steps[step] = {"result": res.result, "line": res.line, "done": res.done, "at": self.iso(),
                               "seconds": round((self.now() - begun).total_seconds())}  # fmt: skip
                self.save()
                self.say(f"{res.result:7} {self.label(step)}: {res.line}")
                failed = failed or res.result == "FAIL"
                if res.stop:
                    n = STEPS.index(step) + 1
                    stopped = {"FAIL": f"not run: step {n} failed first",
                               "PASS": "waits for the nights: run GPU-KIT.cmd again after the time above"
                               }.get(res.result, f"waits for step {n}")  # fmt: skip
            self.k8()
        except KeyboardInterrupt:
            where = next((s for s in STEPS if not (steps.get(s) or {}).get("done")), "K7")
            self.say(f"Stopped by you during {self.label(where)}. Nothing is lost: run GPU-KIT.cmd again and "
                     "it continues from there.")  # fmt: skip
            return 130
        self.finish(blocked=failed and stopped is not None and stopped.startswith("not run"))
        return 1 if failed else 0

    def finish(self, blocked: bool) -> None:
        """Stop the engine only when the kit started it, then the zip (also after a failed step), then the
        last lines: what to send, or when to come back."""
        if self.state.get("engine_started_by_kit"):
            self.product.api("POST", "/api/engine/stop_all", {}, timeout=120)  # the product's own Stop All
            self.state["engine_started_by_kit"] = False
            self.save()
        try:
            target = self.bundle()
        except OSError as e:
            self.say(f"FAIL    the zip could not be written ({e}). What to do: free some space on the "
                     "install's drive, then run GPU-KIT.cmd again.")  # fmt: skip
            return
        if self.mark:
            self.say(f"SIMULATED graphics card ({self.mark}): this zip is a test, not the GPU PC's proof.")
        size = f"{target.stat().st_size / 1e6:.1f} MB"
        if (self.state["steps"].get("K7") or {}).get("done"):
            self.say(f"Send this one file: {target} ({size}). The kit sent nothing anywhere.")
        elif blocked:  # a FAIL stopped the steps after it
            self.say(f"The zip so far: {target} ({size}). Do what the FAIL line above says; when it says to "
                     "send the zip, send this one file. The kit sent nothing anywhere.")  # fmt: skip
        else:
            self.say(f"The zip so far: {target} ({size}); it is not finished, do not send it yet.")
            if self.state.get("come_back"):
                self.say(f"Run GPU-KIT.cmd again after {self.when(self.at(self.state['come_back']))}.")


def _put(z: zipfile.ZipFile, name: str, path: Path, masks: Masks, missing: list[str], why: str) -> None:
    """One part into the zip, text scrubbed (a nested zip entry by entry); a part that cannot be read is
    named in MISSING.txt with why, and the rest still goes in."""
    try:
        if not path.is_file():
            raise FileNotFoundError(why or "not there")
        if path.suffix.lower() == ".zip":
            scrub_zip(path, z, name[:-4] + "/", masks)
        elif path.suffix.lower() in TEXT_SUFFIXES:
            z.writestr(name, scrub_text(path.read_text(encoding="utf-8", errors="replace"), masks))
        else:
            z.writestr(name, path.read_bytes())
    except (OSError, zipfile.BadZipFile) as e:
        missing.append(f"{name}: {why or e}" if isinstance(e, FileNotFoundError) else f"{name}: {e}")


def _number(folder: Path, word: str) -> float | None:
    """The first number under a key containing `word` in a qc JSON file of the job (its loudness)."""
    for f in sorted(folder.rglob("qc*.json")) if folder.is_dir() else []:
        with contextlib.suppress(OSError, ValueError):
            todo: list[Any] = [json.loads(f.read_text(encoding="utf-8"))]
            while todo:
                item = todo.pop(0)
                if isinstance(item, dict):
                    for k, v in item.items():
                        if word in str(k).lower() and isinstance(v, int | float) and not isinstance(v, bool):
                            return float(v)
                        todo.append(v)
                elif isinstance(item, list):
                    todo.extend(item)
    return None


def _loads(raw: Any) -> Any:
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return raw


def _span(a: datetime | str | None, b: datetime | str | None) -> str:
    try:
        s = int(((b if isinstance(b, datetime) else datetime.fromisoformat(str(b)))
                 - (a if isinstance(a, datetime) else datetime.fromisoformat(str(a)))).total_seconds())
    except (TypeError, ValueError):
        return "an unknown time"
    return f"{s // 3600} h {s % 3600 // 60} min" if s >= 3600 else f"{s // 60} min {s % 60} s"


# ---------------------------------------------------------------------------------------------- K0
def _kit_alive(pid: int, since: datetime | None) -> bool:
    """A Python process with this pid runs and started before the lock was written (a pid that Windows gave
    to a later process is not the kit)."""
    with contextlib.suppress(Exception):
        import psutil

        p = psutil.Process(pid)
        later = since is not None and p.create_time() > since.timestamp() + 5
        return "python" in p.name().lower() and not later
    return False


def take_lock(path: Path) -> None:
    """The kit lock, also prove.py's lock file (its first field is the department `kit`). Refused when a live
    kit holds it; the lock of a kit that is gone is taken over."""
    line = f"kit | GPU kit pid {os.getpid()} | {datetime.now().astimezone().isoformat(timespec='seconds')}\n"
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
            m = re.search(r"pid (\d+) \| (\S+)", text)
            since = None
            with contextlib.suppress(ValueError):
                since = datetime.fromisoformat(m.group(2)) if m else None
            if m and _kit_alive(int(m.group(1)), since):
                raise Refused(f"Another GPU kit is running (pid {m.group(1)}). Let it finish in its own "
                              "window, or close that window, then run GPU-KIT.cmd again."
                              ) from None  # fmt: skip
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(line)
        return
    raise Refused(f"The kit lock {path} could not be taken. Close other GPU kit windows and run it again.")


def load_state(ev: Path) -> dict[str, Any]:
    """The state, or its last copy (`.bak`) when it is unreadable; refused when neither reads."""
    for f in (ev / "kit-state.json", ev / "kit-state.json.bak"):
        with contextlib.suppress(OSError, ValueError):
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    if (ev / "kit-state.json").is_file():
        raise Refused(f"{ev / 'kit-state.json'} is damaged and has no readable copy. What to do: send that "
                      "folder as a zip, and do not run the kit again until the Head answers.")  # fmt: skip
    return {}


def start(args: argparse.Namespace, env: KitEnv) -> Kit:
    """K0: every refusal comes before anything is written; then the folder, the lock and the state."""
    home = Path(args.home).resolve()
    try:
        info = read_install(home)
    except SetupError as e:
        raise Refused(f"{e}. Pass -Home <the install's folder> (the one that holds app and config).") from e
    from ns_common import devmode
    from ns_common.paths import Paths

    if (args.night_now or args.clock) and not args.dry_run:
        raise Refused("-NightNow and -Clock are for a dry run only (-DryRun, on a laptop test install).")
    if args.dry_run and not devmode.test_install(Paths(home)):
        raise Refused("-DryRun is for a laptop test install only; this is a real install. Run the kit "
                      "without it.")  # fmt: skip
    if not args.dry_run and info.kind != "gpu":
        raise Refused(f"This install ({home}) is not a GPU install (kind: {info.kind}). The kit proves the "
                      "GPU PC: install the candidate there with INSTALL.cmd, then run its GPU-KIT.cmd.")
    offset = timedelta(0)
    if args.clock:
        try:
            offset = datetime.fromisoformat(args.clock).astimezone() - datetime.now().astimezone()
        except ValueError as e:
            raise Refused(f"-Clock needs a time like 2026-11-12T22:00, not {args.clock!r}.") from e
    clock = env.clock or RealClock(offset)
    product = env.product or InstallProduct(home, info.ports["dashboard"])
    status, body = product.api("GET", "/api/status", timeout=30)
    served = (body.get("disk") or {}).get("home") if status == 200 and isinstance(body, dict) else None
    if served and os.path.normcase(str(Path(served).resolve())) != os.path.normcase(str(home)):
        raise Refused(f"The Nightshift of another install ({served}) is running on this PC. Close its "
                      "Dashboard window and press Stop All there, then run GPU-KIT.cmd again.")  # fmt: skip
    kit = Kit(home, info.version, info.kind, args.dry_run, args.night_now, env, clock, product,
              env.windows or RealWindows(home), env.ask or timed_input)  # fmt: skip
    kit.ev.mkdir(parents=True, exist_ok=True)  # (a kit folder of another version is left alone)
    take_lock(kit.ev / "kit.lock")
    kit.lock = kit.ev / "kit.lock"
    try:
        kit.state = load_state(kit.ev)
        from ns_supervisor import gpu

        kit.mark = gpu.simulated_mark(gpu.smi_source(Paths(home)))
        kit.state.setdefault("started", kit.iso())
        kit.state.update({"version": info.version, "kind": info.kind, "simulated": kit.mark,
                          "dry_run": args.dry_run})  # fmt: skip
        kit.state.setdefault("steps", {})["K0"] = {"result": "PASS", "done": True, "at": kit.iso(),
                                                   "line": f"the install is {info.version} at {home}"}
        kit.save()
    except BaseException:
        kit.close()
        raise
    return kit


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="gpukit.py", description="The Nightshift GPU kit (GPU-KIT.cmd).")
    ap.add_argument("--home", required=True, help="the install (NIGHTSHIFT_HOME)")
    ap.add_argument("--dry-run", action="store_true", help="a laptop test install (fake_gpu) only")
    ap.add_argument("--night-now", action="store_true", help="dry run: each night at once, through the stub")
    ap.add_argument("--clock", help="dry run: the kit's clock starts at this ISO time")
    return ap


def main(argv: list[str] | None = None, *, env: KitEnv | None = None) -> int:
    args = build_parser().parse_args(argv)
    env = env or KitEnv()
    stopped = "Stopped by you during step 1 (start). Run GPU-KIT.cmd again: it starts over from there."
    try:
        kit = start(args, env)
    except Refused as e:
        env.say(f"REFUSED {e}")
        return 2
    except KeyboardInterrupt:
        env.say(stopped)
        return 130
    try:
        env.say(f"Nightshift GPU kit for {kit.version} at {kit.home} (NIGHTSHIFT_HOME).")
        if kit.mark:
            env.say(f"SIMULATED graphics card ({kit.mark}): a test install; nothing measured here is "
                    "evidence of the GPU PC.")  # fmt: skip
        for line in PLAN:
            env.say(line)
        if not kit.state.get("confirmed"):
            if (kit.ask("Press Enter to start, or type q and Enter to stop.") or "").strip().lower() == "q":
                env.say("Nothing was changed (only the kit's folder was made). Run GPU-KIT.cmd when ready.")
                return 0
            kit.state["confirmed"] = True
            kit.save()
        env.say(f"PASS    {kit.label('K0')}: the install is {kit.version} at {kit.home}")
        return kit.run()
    except KeyboardInterrupt:
        env.say(stopped)
        return 130
    finally:
        kit.close()


if __name__ == "__main__":
    sys.exit(main())
