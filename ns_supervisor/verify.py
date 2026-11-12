"""C2 `ns verify [--quick]`: health, completion, TTS, whisper, embed, NVENC, idle VRAM, benchmarks.

Writes `logs\\verify-report.json` shaped like `GateReport` (spec 15.6) with the spec 14.6 table as
`bench.*` checks. Every check is tolerant: a missing component becomes a failed check carrying the
NS code and fix hint, never an exception.

What the voice and memory numbers measure (card verify-limits, spec 14.6):
- `tts.preset`, `tts.design`, `tts.clone`: the voice speaks. One sentence each, quick or full; the value is
  the whole call in seconds (the voice program's start and model load included), and the row carries NO
  limit: it passes when sound came back (L1).
- `tts.rate` (full only): the speaking rate on the product's real route, ONE batch request of six sentences
  through `/v1/audio/speech/batch`: (arrival of the last sentence - arrival of the first) / (audio seconds
  of the sentences after the first), from the stamps the service took while it read the voice program's
  output. Limit: the RTF target; `bench.tts_rtf` records it (L2). An engine without stamps (fake, Windows
  speech, reference) gives the whole batch call over its audio instead, and the row's log says so.
- `tts.start_s` (full only): seconds from the service beginning that batch to its first sentence: the voice
  program's start, its model load and one sentence. `bench.tts_start_s` has a SOFT target of 15 s (L3).
- `save_rtf` still gets the single calls' whole ratio (the worst), as before this card (L4).
- `vram.idle` / `bench.idle_vram_mb`: what Nightshift left on the card, (in use after unload) minus (in use
  before the test loaded anything of ours), read only when the supervisor's status showed none of our
  services loaded; without such a moment the row is "n/a" with the reason and passes (L5).
- A failed row that carries a `limit` is a missed target; a failed row without one is a part that did not
  work (`split_failures`, L6). The exit code is unchanged: non-zero when any row failed.

The sample job (`job.fake_services`, full only; card verify-job-isolated J1 to J7): the install's own
`studio` runs `studio run --fake-services` with a home of its own, `<home>\\run\\verify-job`, made new for
each run (real folders and files only, the install's `runtime.json` and formats copied in, never linked) and
handed over with NIGHTSHIFT_HOME and the marker that vouches for it (`ns_common.paths.home_env`). Its data,
Outbox, ledger, owner files and logs are its own; the install's are never written. The folder is removed when
the check ends, pass or fail, and at the next run's start when a kill left it, and only when it is proven to
be the one this check made (`not_ours`). A failed job's own error lines go into the row and the verify log.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from ns_common import __version__, ffmpeg
from ns_common.acquire import Endpoint, Service
from ns_common.atomic import write_text_atomic
from ns_common.embed_client import EmbedClient
from ns_common.errors import NsError
from ns_common.llm_client import LlmClient
from ns_common.logging import NsLogger
from ns_common.models import GateCheck, GateReport, Runtime
from ns_common.paths import Paths, home_env
from ns_common.tts_client import TtsClient, TtsItem, TtsStamp
from ns_common.tts_timeout import save_rtf
from ns_supervisor import check_names as names
from ns_supervisor import procs
from ns_supervisor.benchmark import measure_tok_s
from ns_supervisor.childenv import child_env, window_flags
from ns_supervisor.gpu import SIMULATED_EVIDENCE, labelled, simulated_mark, smi_source

OWNER = "verify"
TTS_SENTENCE = "Nightshift verification sentence. The quick brown fox jumps over the lazy dog."
NOT_MEASURED = "short_job_s,master_job_s,nightly_run_s"
#: The batch `tts.rate` is measured on: six plain sentences of about the length of a narration's, one per item
#: (one chunk each), all with the first talker's voice, so the service runs them in ONE voice process.
RATE_SENTENCES: tuple[str, ...] = (
    "The rain had stopped by the time the last train left the station.",
    "Nobody on the platform noticed the man with the grey umbrella.",
    "He walked to the end of the line and waited without a word.",
    "A light came on in the signal box, then went out again.",
    "Somewhere in the town a church bell rang eleven times.",
    "When the silence came back, the man and his umbrella were gone.",
)
#: The soft target of `bench.tts_start_s` (L3): logged when missed, never a failure.
TTS_START_SOFT_S = 15.0
#: Supervisor states of a service that holds nothing on the card (`ns_supervisor.core`: stopped, starting,
#: ready, stopping, error; "error" is a service that crashed out and has no process).
UNLOADED_STATES = ("stopped", "error")
#: The services that load a model onto the graphics card (`ns_supervisor.services`: the memory index and the
#: Dashboard run on the processor, and the Dashboard is up during every `ns verify`).
CARD_SERVICES = ("llama", "tts", "whisper")
#: How long the first talker (the preset: the engine check, which carries the cold load of the voice engine)
#: may take to answer, as it always had, and how long each further talker may take. Up to three talkers are
#: spoken one after the other, and 0.1.14's updater runs `ns verify --quick` with no timeout of its own while
#: setup's s08 gives it 3600 s. A further talker (a designed or a cloned voice) only needs its own model
#: loaded: a 1.7 B model in Q8 is about 2 GB, 20 to 45 s from a hard disk at 40 to 100 MB/s, so 180 s is four
#: times the slowest load and a hung engine costs 2 * 180 s = 6 minutes on top of the first talker, not tens.
#: 1800 + 2 * 180 stays well under s08's 3600 s.
FIRST_TALKER_TIMEOUT_S = 1800.0
LATER_TALKER_TIMEOUT_S = 180.0
#: What the owner is told, in every sentence about a voice that did not speak: a voice cannot be mended in
#: place (a new voice may not take an old name: `ns_tts.voices.VoiceLibrary._check_new`), so the way out is a
#: new voice, or another narrator for the channels that use it (`Channels > Edit channel`, the Narrator
#: choice, `Save channel`; `Library > Voices` makes the new voice).
TALKER_WAY_OUT = (
    "Make the voice again under another name (Library > Voices), then choose it as the narrator under "
    "Channels > Edit channel and press Save channel, or choose another narrator there."
)


def talker_warning(mode: str, voice: str, reason: str) -> GateCheck:
    """The row of a designed or cloned voice that did not speak: visible, never a failure of the run.

    It is `passed` (the report, the exit code of `ns verify` and so an update and setup go on) and carries
    `value == names.TALKER_WARNING_VALUE` and the sentence the owner reads, in `log`.
    """
    return GateCheck(
        name=names.tts_mode(mode), passed=True, value=names.TALKER_WARNING_VALUE,
        log=(
            f"The {mode} voice {voice!r} did not speak a test sentence ({reason}). Nightshift went on "
            f"without it; a channel that narrates with it cannot make videos until it has another voice. "
            f"{TALKER_WAY_OUT}"
        ),
    )


def talker_warnings(checks: list[GateCheck]) -> list[str]:
    """The sentences of the voices that did not speak, from a report's checks (see `talker_warning`)."""
    return [
        str(c.log) for c in checks
        if c.passed and c.value == names.TALKER_WARNING_VALUE
        and c.name.startswith(names.TTS_MODE_PREFIX) and c.log
    ]


def talker_key(sentence: str) -> str:
    """Which voice a sentence is about: its start, up to the reason ("The clone voice 'Cleo' did not speak").

    The reason carries an error detail that can change from run to run; a note is the same note when the
    mode and the voice are the same.
    """
    return sentence.partition(" did not speak")[0]


def leave_voice_notes(paths: Paths, checks: list[GateCheck]) -> list[str]:
    """Leave each voice sentence of `checks` as a morning note unless one about that mode and voice waits.

    Returns the sentences. Best effort: a note is a courtesy; the report file and the result carry them too.
    """
    sentences = talker_warnings(checks)
    try:
        from ns_supervisor import notify

        def waits(key: str) -> Callable[[list[notify.Note]], bool]:
            return lambda notes: any(talker_key(n["message"]) == key for n in notes)

        for sentence in sentences:  # the owner of the notes decides "already said" under its lock
            notify.leave_note(paths, "Nightshift", sentence, once=waits(talker_key(sentence)))
    except Exception:
        pass
    return sentences


def read_talker_warnings(paths: Paths) -> list[str]:
    """The same sentences from `logs/verify-report.json` (what an update reads after its verify step)."""
    try:
        data = json.loads(report_path(paths).read_text(encoding="utf-8-sig"))
        checks = [GateCheck.model_validate(c) for c in data["checks"]]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    return talker_warnings(checks)


def speaking_rate(stamps: tuple[TtsStamp, ...] | None) -> tuple[float, float, float] | None:
    """(rate, start seconds, audio seconds after the first) from a batch's stamps (L2, L3); None when they
    cannot give one: no stamps, one utterance, no audio after the first, or two utterances that did not
    arrive one after the other (a voice program that held its output and wrote it at once would otherwise
    give a rate near 0, a pass measured on nothing)."""
    if not stamps or len(stamps) < 2:
        return None
    after = sum(s.audio_s for s in stamps[1:])
    if after <= 0 or any(b.at_s <= a.at_s for a, b in zip(stamps, stamps[1:], strict=False)):
        return None
    return (stamps[-1].at_s - stamps[0].at_s) / after, stamps[0].at_s, after


def nothing_of_ours_loaded(status: dict[str, Any]) -> str | None:
    """None when the supervisor's status shows none of our services loaded and nobody holding the card; else
    the reason, in words (L5: only then is the card's memory in use a baseline of the other programs)."""
    services = status.get("services")
    if not isinstance(services, dict):
        return "the supervisor's status named no services"
    loaded = sorted(
        str(n) for n, s in services.items()
        if n in CARD_SERVICES
        and (not isinstance(s, dict) or s.get("state") not in UNLOADED_STATES or s.get("pid") is not None)
    )
    if loaded:
        return f"{', '.join(loaded)} {'was' if len(loaded) == 1 else 'were'} loaded"
    if status.get("gpu_owner"):
        return f"{status.get('gpu_owner')} held the graphics card"
    return None


#: `bench.*` rows that record the same figure as a check row: when both miss, it is ONE target missed, said
#: by the check row (the studio PC's report counted the voice speed and the idle memory twice each).
MIRRORS: dict[str, str] = {"bench.tts_rtf": names.TTS_RATE, "bench.idle_vram_mb": names.VRAM_IDLE}


def split_failures(checks: list[GateCheck]) -> tuple[list[GateCheck], list[GateCheck]]:
    """(targets missed, parts that did not work) of a report's failed rows (L6).

    The rule, with no field of its own: a failed row that carries a `limit` measured something and missed
    its target; a failed row without one is a part that did not work. A bench row whose check row (`MIRRORS`)
    also failed is that same target and is not counted again.
    """
    failed = [c for c in checks if not c.passed]
    gone = {c.name for c in failed}
    missed = [c for c in failed if c.limit and MIRRORS.get(c.name) not in gone]
    return missed, [c for c in failed if not c.limit]


def _missed(c: GateCheck) -> str:
    return f"{c.name} {c.value} (limit {c.limit})"


def verdict(checks: list[GateCheck]) -> str:
    """The report in one plain sentence (the printed table's last line, L6)."""
    missed, broken = split_failures(checks)
    if not missed and not broken:
        return "ALL CHECKS PASSED"
    said: list[str] = []
    if broken:
        n = len(broken)
        said.append(f"{n} {'part' if n == 1 else 'parts'} did not work: {', '.join(c.name for c in broken)}.")
    else:
        said.append("Everything works.")
    if missed:
        n = len(missed)
        rows = "; ".join(_missed(c) for c in missed)
        said.append(f"{n} {'target' if n == 1 else 'targets'} missed: {rows}.")
    return " ".join(said)


class Controller(Protocol):
    """What verify needs from the supervisor (`SupervisorClient` and the test `SyncSupervisor`)."""

    def acquire(
        self, service: Service, owner: str, *, model_alias: str | None = None, timeout_s: int = 7200
    ) -> Endpoint: ...

    def release(self, service: Service, owner: str) -> None: ...

    def status(self) -> dict[str, Any]: ...

    def stop_all(self) -> None: ...


@dataclass(frozen=True)
class Targets:
    """Spec 14.6 targets for the detected profile (32 GB column, or the 16 GB bracket)."""

    tok_s: float
    load_s: float
    rtf: float
    short_job_s: float
    idle_vram_mb: float
    profile: str


def targets_for(ram_gb: float | None) -> Targets:
    """32 GB column when RAM >= 28 GB, the bracketed 16 GB column otherwise (unknown RAM: 16 GB)."""
    if ram_gb is not None and ram_gb >= 28:
        return Targets(40, 30, 0.5, 180, 300, "32gb")
    return Targets(30, 30, 0.8, 300, 300, "16gb")


def ram_gb() -> float | None:
    """Installed RAM in GB (Windows `GlobalMemoryStatusEx`); None when unavailable."""
    if sys.platform != "win32":
        return None
    import ctypes

    class _Mem(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    m = _Mem()
    m.dwLength = ctypes.sizeof(_Mem)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        return None
    return round(m.ullTotalPhys / 1024**3, 1)


# --------------------------------------------------------------------------------------------
# Probes that touch the machine (replaced in tests)
# --------------------------------------------------------------------------------------------
WHISPER_SNIPPET = """
import json, sys, time
try:
    from ns_common import gpu_dlls
    gpu_dlls.ensure_on_path()
except ImportError:
    pass
from faster_whisper import WhisperModel
t = time.perf_counter()
WhisperModel(sys.argv[1], device=sys.argv[2], compute_type=sys.argv[3], local_files_only=True)
print(json.dumps({"ok": True, "load_s": round(time.perf_counter() - t, 2)}))
"""


def whisper_probe(paths: Paths, rt: Runtime) -> float:
    """Load the whisper model in the studio venv (the process that will use it); returns load seconds."""
    py = paths.venvs / "studio" / "Scripts" / "python.exe"
    if not py.exists():
        raise NsError("NS-E202", f"studio venv missing: {py}")
    local = paths.models_whisper / rt.whisper.model
    model = str(local) if local.is_dir() else rt.whisper.model
    cmd = [str(py), "-c", WHISPER_SNIPPET, model, rt.whisper.device, rt.whisper.compute_type]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600, check=False, env=child_env(),
            creationflags=window_flags(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise NsError("NS-E202", f"whisper probe could not run: {e}") from e
    if r.returncode != 0:
        tail = " | ".join((r.stderr or r.stdout).strip().splitlines()[-3:])
        low = tail.lower()
        code = "NS-E202"
        if "cublas" in low or "cudnn" in low or "dll" in low:
            code = "NS-E112"
        raise NsError(code, f"whisper failed to load on {rt.whisper.device}: {tail}")
    try:
        return float(json.loads(r.stdout.strip().splitlines()[-1])["load_s"])
    except (ValueError, KeyError, IndexError) as e:
        raise NsError("NS-E202", f"whisper probe gave no result: {r.stdout[-200:]}") from e


def nvenc_probe() -> None:
    """Encode a 2 second colour source with `h264_nvenc`. Raises `NS-W401` when NVENC is unusable."""
    if not ffmpeg.available():
        raise NsError("NS-E401", "ffmpeg/ffprobe not found on PATH")
    try:
        ffmpeg.run(
            [
                "-f", "lavfi", "-i", "color=c=0x2060a0:s=1280x720:r=30:d=2",
                "-c:v", "h264_nvenc", "-f", "null", "-",
            ],  # fmt: skip
            timeout_s=120,
        )
    except NsError as e:
        raise NsError("NS-W401", e.detail) from e


def x264_probe() -> None:
    """Encode a 2 second colour source with `libx264` (a dev install: no NVENC, the CPU encoder renders)."""
    if not ffmpeg.available():
        raise NsError("NS-E401", "ffmpeg/ffprobe not found on PATH")
    ffmpeg.run(
        [
            "-f", "lavfi", "-i", "color=c=0x2060a0:s=1280x720:r=30:d=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-f", "null", "-",
        ],  # fmt: skip
        timeout_s=120,
    )


# -- the sample job's own home (card verify-job-isolated) ----------------------------------------------------
#: `<home>\run\verify-job`: the sample job's whole home, made new for each full run (J1).
SCRATCH_NAME = "verify-job"
#: The file this check writes FIRST into the folder it made; nothing without it is ever removed (J6).
SCRATCH_MARKER = "nightshift-verify-job.json"
SCRATCH_KIND = "ns verify sample job"
JOB_ARGS: tuple[str, ...] = ("run", "--fake-services", "--format", "shorts_only", "--count", "1")
JOB_COMMAND = "studio " + " ".join(JOB_ARGS)
JOB_TIMEOUT_S = 1800.0
#: The studio's own warning and error lines a failed sample job leaves in the verify log (J2).
JOB_LOG_LINES = 20


@dataclass(frozen=True)
class JobRun:
    """What the sample job gave: its seconds, and what the clean-up has to say (a file that stayed)."""

    seconds: float
    notes: tuple[str, ...] = ()


def scratch_home(paths: Paths) -> Path:
    r"""`<home>\run\verify-job`: where the sample job lives while it runs (and nowhere else)."""
    return paths.home / "run" / SCRATCH_NAME


#: The name-surrogate bit of a reparse tag (`IsReparseTagNameSurrogate`): set for a junction (mount point,
#: 0xA0000003), a symbolic link (0xA000000C) and every other tag that names another place; not set for cloud
#: files (OneDrive, 0x9000001A and its kin), which are ordinary files and folders to us.
NAME_SURROGATE = 0x20000000


def link_by_stat(mode: int, attributes: int, tag: int) -> bool:
    """Is a name with this `lstat` (mode, Windows file attributes, reparse tag) a link: a symbolic link, a
    junction, or any reparse point whose tag names another place? A reparse point of another kind (a cloud
    file or folder) is not (review of block E2, finding 7)."""
    if stat.S_ISLNK(mode):
        return True
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT) and bool(tag & NAME_SURROGATE)


def _is_link(p: Path) -> bool:
    """True for a symbolic link, a junction or any other name-surrogate reparse point (never followed, never
    entered); `link_by_stat`."""
    try:
        st = os.lstat(p)
    except OSError:
        return False
    return link_by_stat(
        st.st_mode, getattr(st, "st_file_attributes", 0), getattr(st, "st_reparse_tag", 0)
    )


def _real(p: Path) -> str:
    return os.path.normcase(os.path.realpath(p))


def _mark(folder: Path, pids: Sequence[int]) -> None:
    """Write (or rewrite) the folder's marker: what it is, whose home, and the processes that use it."""
    body = {"kind": SCRATCH_KIND, "home": str(folder.parent.parent), "pids": [int(p) for p in pids],
            "made": datetime.now().isoformat(timespec="seconds")}
    write_text_atomic(folder / SCRATCH_MARKER, json.dumps(body) + "\n")


def _marker_of(folder: Path) -> dict[str, Any] | None:
    f = folder / SCRATCH_MARKER
    if _is_link(f) or not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("kind") == SCRATCH_KIND else None


def pid_alive(pid: int) -> bool | None:
    """True while a process with this pid exists, False when it is gone, None when that cannot be told (a
    `tasklist` that did not run or answered nothing; review of block E2, finding 6: the caller then leaves the
    folder). A reused pid only keeps a folder one run longer."""
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return None
        return True
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True,
            timeout=15, check=False, env=child_env(), creationflags=window_flags(),
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        return None
    return out.startswith('"')  # a CSV row names the process; "INFO: No tasks ..." (any language) does not


def not_ours(paths: Paths, alive: Callable[[int], bool | None] = pid_alive) -> str | None:
    r"""Why what is at `<home>\run\verify-job` must be left alone (J6), in words; None only when it is
    provably the folder this check made and no live process of another run uses it:
    `<home>\run` is no link; the name is no link and a real folder; its resolved path is directly under the
    resolved `<home>\run`; it carries this check's marker file; no process the marker names (another
    `ns verify`, or the studio it started, which may outlive a killed `ns verify`) is still running."""
    run = paths.home / "run"
    folder = run / SCRATCH_NAME
    if _is_link(run):
        return f"{run} is a link, so {folder} may lead outside this install"
    if _is_link(folder):
        return f"{folder} is a link, not the folder this check made"
    if not folder.is_dir():
        return f"{folder} is not a folder"
    real = Path(os.path.realpath(folder))
    if os.path.normcase(real.name) != os.path.normcase(SCRATCH_NAME) or _real(real.parent) != _real(run):
        return f"{folder} leads to {real}, outside {run}"
    mark = _marker_of(folder)
    if mark is None:
        return (f"{folder} has no {SCRATCH_MARKER} of this check, so it is not this test's folder. Move that "
                f"folder out of {run}, then run the test again")
    pids = mark.get("pids")
    others = [p for p in (pids if isinstance(pids, list) else []) if isinstance(p, int) and p != os.getpid()]
    said = {p: alive(p) for p in others}
    busy = [p for p, up in said.items() if up]
    if busy:
        return f"{folder} is in use by another model test (process {busy[0]} still runs)"
    unknown = [p for p, up in said.items() if up is None]
    if unknown:  # not known is alive (finding 6): the folder waits for a run that can tell
        return (f"{folder} may be in use by another model test (whether process {unknown[0]} still runs "
                "could not be told)")
    return None


def _remove_entry(p: Path, stayed: list[Path]) -> None:
    """Remove ONE name: a file, or a link itself (`rmdir` takes a junction or a folder link away without
    entering it, `unlink` a file link). A read-only file gets its write bit first."""
    if _is_link(p):
        try:
            os.rmdir(p)
            return
        except OSError:
            pass
        try:
            os.unlink(p)
        except OSError:
            stayed.append(p)
        return
    try:
        os.unlink(p)
        return
    except PermissionError:
        pass
    except FileNotFoundError:
        return
    except OSError:
        stayed.append(p)
        return
    try:
        os.chmod(p, stat.S_IWRITE)
        os.unlink(p)
    except FileNotFoundError:
        return
    except OSError:
        stayed.append(p)


def _remove_tree(folder: Path, keep: str | None = None) -> list[Path]:
    """Remove `folder` and everything in it WITHOUT EVER FOLLOWING A LINK: a link inside is removed as a
    name, its target is never entered. A hard link is a name too: its other names keep the file. Returns
    what stayed (a file another program holds open, a folder that is then not empty).

    `keep` (the marker's name) is left in `folder`, and `folder` itself then stays: the caller removes the
    marker LAST, only once everything else went, so a folder that kept a held file still carries the proof
    that it is this check's and the next run removes it."""
    stayed: list[Path] = []
    try:
        with os.scandir(folder) as it:
            entries = list(it)
    except FileNotFoundError:
        return stayed
    except OSError:
        return [folder]
    for e in entries:
        p = Path(e.path)
        if keep is not None and e.name == keep:
            continue
        if not _is_link(p) and e.is_dir(follow_symlinks=False):
            stayed += _remove_tree(p)
        else:
            _remove_entry(p, stayed)
    if keep is not None:
        return stayed
    if not _rmdir(folder) and not stayed:
        stayed.append(folder)
    return stayed


def _rmdir(folder: Path) -> bool:
    """`os.rmdir` (it removes nothing but an empty folder); a read-only folder of the scratch home gets its
    read-only attribute cleared first. True when the folder is gone. Never for a link: callers checked."""
    for attempt in (1, 2):
        try:
            os.rmdir(folder)
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            if attempt == 2:
                return False
            try:
                os.chmod(folder, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            except OSError:
                return False
        except OSError:
            return False
    return False


def _empty_unmarked(paths: Paths) -> bool:
    r"""True when `<home>\run\verify-job` is a real, EMPTY folder directly under a `run` that is no link and
    has no marker: what a kill between its `mkdir` and its marker, or between the marker's removal and its
    `rmdir`, leaves (review of block E2, finding 3). Removing it with `os.rmdir` can remove nothing else."""
    run = paths.home / "run"
    folder = run / SCRATCH_NAME
    if _is_link(run) or _is_link(folder) or not folder.is_dir():
        return False
    if _real(Path(os.path.realpath(folder)).parent) != _real(run) or _marker_of(folder) is not None:
        return False
    try:
        with os.scandir(folder) as it:
            return next(it, None) is None
    except OSError:
        return False


def remove_scratch(paths: Paths, alive: Callable[[int], bool | None] = pid_alive) -> str | None:
    """Remove the sample job's folder when `not_ours` proves it is this check's (J6). Never raises.

    Returns the sentence for the row, or None when nothing was there or it all went: what was left alone and
    why, or what stayed (a held file) with the word that the next full `ns verify` removes it."""
    folder = scratch_home(paths)
    if not os.path.lexists(folder):
        return None
    try:
        why = not_ours(paths, alive)
    except Exception as e:  # tolerant: a folder that cannot be looked at is left alone
        why = f"{folder} could not be looked at ({type(e).__name__}: {e})"
    if why is not None:
        if _empty_unmarked(paths) and _rmdir(folder):  # a kill's empty leftover: rmdir takes nothing else
            return None
        return f"Left alone: {why}."
    if _is_link(folder):  # once more, the moment before it is entered
        return f"Left alone: {folder} became a link."
    stayed = _remove_tree(folder, keep=SCRATCH_MARKER)
    if not stayed:  # everything else went: now the marker, then the empty folder
        _remove_entry(folder / SCRATCH_MARKER, stayed)
        if not stayed and not _rmdir(folder):
            stayed.append(folder)
    if not stayed:
        return None
    first = stayed[0]
    return (f"{len(stayed)} {'item' if len(stayed) == 1 else 'items'} of the sample job's folder stayed "
            f"(another program holds it open: {first}); the next full ns verify removes {folder}.")


def _copy_settings(paths: Paths, folder: Path) -> None:
    """The install's settings the job reads, copied as files (never linked): `runtime.json` (encoder,
    profile, dev mode) and the owner's formats. Notifications are off in the copy: the job shows no toast."""
    config = folder / "config"
    config.mkdir()
    src = paths.runtime_file
    if src.is_file() and not _is_link(src):
        shutil.copyfile(src, config / src.name)
    formats = paths.formats_dir
    if formats.is_dir() and not _is_link(formats):
        (config / formats.name).mkdir()
        for f in formats.iterdir():
            if f.is_file() and not _is_link(f):
                shutil.copyfile(f, config / formats.name / f.name)
    (config / "dashboard.json").write_text('{"notifications": false}\n', encoding="utf-8")


def make_scratch(paths: Paths) -> Path:
    r"""Make `<home>\run\verify-job` new, its marker FIRST, then the settings it reads and its own `tmp`.

    Raises NsError when it cannot (`<home>\run` is a link; the name is taken this moment by another run).
    A folder made without its marker is taken away again at once (it is empty), so no folder of this name
    is ever left that this check could not prove to be its own."""
    run = paths.home / "run"
    run.mkdir(parents=True, exist_ok=True)
    if _is_link(run):
        raise NsError("NS-E202", f"{run} is a link; the sample job is not run through it")
    folder = run / SCRATCH_NAME
    try:
        folder.mkdir()
    except FileExistsError as e:
        raise NsError("NS-E202", f"{folder} was made by another model test this moment") from e
    try:
        _mark(folder, [os.getpid()])
    except OSError:
        with contextlib.suppress(OSError):
            os.rmdir(folder)
        raise
    try:
        _copy_settings(paths, folder)
        (folder / "tmp").mkdir()
    except BaseException:  # half made (a full disk): it goes again, by the same proof as ever
        remove_scratch(paths, lambda _pid: False)  # (only this process is in the marker yet)
        raise
    return folder


def _tool_env(paths: Paths) -> dict[str, str]:
    r"""NS_FFMPEG and NS_FFPROBE as this install finds them (an override, PATH, or its own `bin\ffmpeg`): the
    job's home has no `bin`, and the studio asks those variables first."""
    out: dict[str, str] = {}
    tools = (("NS_FFMPEG", "ffmpeg", ffmpeg.ffmpeg_exe), ("NS_FFPROBE", "ffprobe", ffmpeg.ffprobe_exe))
    for var, name, find in tools:
        try:
            out[var] = find()
        except Exception:  # noqa: BLE001 - not found here: the job says so itself
            own = paths.home / "bin" / "ffmpeg" / f"{name}.exe"
            if own.is_file():
                out[var] = str(own)
    return out


def _job_env(paths: Paths, folder: Path) -> dict[str, str]:
    """The job's environment: this one, with its home handed over on purpose and its temp files inside it."""
    tmp = str(folder / "tmp")
    return child_env({**_tool_env(paths), **home_env(folder), "TEMP": tmp, "TMP": tmp})


def _studio_lines(folder: Path) -> list[str]:
    """The studio's own warning and error lines from the job's logs (the last `JOB_LOG_LINES`)."""
    lines: list[str] = []
    for f in sorted((folder / "logs").glob("studio-*.jsonl")):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("level") in ("WARN", "ERROR"):
                data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
                code = f" {data.get('code')}" if data and data.get("code") else ""
                msg = rec.get("msg") or ""
                lines.append(f"{rec.get('level')} {rec.get('event')}{code}" + (f": {msg}" if msg else ""))
    return lines[-JOB_LOG_LINES:]


def _run_studio(
    command: Sequence[str], env: dict[str, str], folder: Path, timeout_s: float
) -> tuple[int, str]:
    """Start the job, put its pid in the marker (a later run then sees it while it lives), wait for it.

    At the time limit the job and everything it started are ended (`procs.kill_tree`)."""
    proc = subprocess.Popen(
        [*command, *JOB_ARGS], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=env, cwd=str(folder), creationflags=window_flags(),
    )
    try:
        with contextlib.suppress(OSError):
            _mark(folder, [os.getpid(), proc.pid])
        try:
            out, _ = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired as e:
            procs.kill_tree(proc.pid)
            proc.kill()
            try:
                out, _ = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                out = ""
            tail = " | ".join((out or "").strip().splitlines()[-3:])
            raise NsError("NS-E202", f"the sample job did not end within {timeout_s:.0f} s: {tail}") from e
    finally:
        if proc.poll() is None:  # an exception of our own in the wait: the job does not run on unseen
            procs.kill_tree(proc.pid)
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.communicate(timeout=30)
    return int(proc.returncode), out or ""


def fake_job(
    paths: Paths, *, command: Sequence[str] | None = None, timeout_s: float = JOB_TIMEOUT_S,
    alive: Callable[[int], bool | None] = pid_alive,
) -> JobRun | None:
    """`studio run --fake-services --format shorts_only --count 1` in its own home (card verify-job-isolated).

    None when studio is not installed. Raises NsError when the job failed or could not get its own folder;
    the folder is removed in every case (`remove_scratch`), and what stayed or was left alone is said in the
    row. `command` is the studio program (default: this install's `venvs\\studio\\Scripts\\studio.exe`)."""
    if command is None:
        exe = paths.venvs / "studio" / "Scripts" / "studio.exe"
        studio = str(exe) if exe.exists() else shutil.which("studio")
        if studio is None:
            return None
        command = [studio]
    # D-48 for the install: the job's own gate looks at the job's home, while it runs the install's programs
    from installer.stage2.web import venvrepair

    why = venvrepair.home_refusal(paths.home)
    if why is not None:
        raise NsError(venvrepair.CODE, why)
    folder = scratch_home(paths)
    before = remove_scratch(paths, alive)  # what a killed earlier run left (J2), only when it is ours (J6)
    if os.path.lexists(folder):
        raise NsError("NS-E202", f"The sample job was not run: it needs {folder} new. "
                                 f"{before or 'It is still there.'} Nothing of this install was changed.")
    log = NsLogger("verify", paths.logs)
    made = False
    try:
        make_scratch(paths)
        made = True
        t0 = time.perf_counter()
        try:
            rc, out = _run_studio(command, _job_env(paths, folder), folder, timeout_s)
        except OSError as e:
            raise NsError("NS-E202", f"fake-services job could not run: {e}") from e
        secs = round(time.perf_counter() - t0, 1)
        if rc != 0:
            tail = " | ".join(out.strip().splitlines()[-3:])
            said = _studio_lines(folder)
            log.warn("sample_job_failed", exit=rc, output=out.strip().splitlines()[-JOB_LOG_LINES:],
                     studio=said)
            last = f" The studio's last error: {said[-1]}." if said else ""
            raise NsError("NS-E202", f"fake-services job exited {rc}: {tail}.{last}")
    except BaseException as e:
        note = remove_scratch(paths, alive) if made else None
        if note:
            log.warn("sample_job_folder", note)
            if isinstance(e, NsError):
                raise NsError(e.code, f"{e.detail} {note}") from e
        raise
    note = remove_scratch(paths, alive)
    if note:
        log.warn("sample_job_folder", note)
    return JobRun(secs, (note,) if note else ())


def tts_engine_kind(base_url: str) -> str | None:
    """`engine_kind` from the voice service's /health (qwentts, reference, fake); None when it is absent."""
    import httpx

    try:
        body = httpx.get(f"{base_url}/health", timeout=10).json()
    except (httpx.HTTPError, ValueError):
        return None
    kind = body.get("engine_kind") if isinstance(body, dict) else None
    return str(kind) if kind else None


@dataclass
class Deps:
    """Machine probes, replaceable in tests."""

    whisper: Callable[[Paths, Runtime], float] = whisper_probe
    nvenc: Callable[[], None] = nvenc_probe
    x264: Callable[[], None] = x264_probe       # a dev install probes the CPU encoder instead of NVENC
    # the sample job (a test's stand-in may give its seconds alone)
    job: Callable[[Paths], JobRun | float | None] = fake_job
    # None: this install's nvidia-smi (`gpu.smi_source`: the simulated card under `fake_gpu`, H-10)
    vram_used: Callable[[], int | None] | None = None
    gpu_name: Callable[[], str | None] | None = None
    ram: Callable[[], float | None] = ram_gb
    tts_engine: Callable[[str], str | None] = tts_engine_kind
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.perf_counter  # times the voice's calls (tests give a fake one)


@dataclass
class Out:
    """Result of one check body."""

    passed: bool
    value: float | str | None = None
    limit: str | None = None
    log: str | None = None
    data: Any = None


def explain(e: NsError) -> str:
    """`NS-E203: message (detail) | Hint: ...`"""
    return f"{e} | Hint: {e.definition.hint}"


@dataclass
class _Recorder:
    log: NsLogger
    checks: list[GateCheck] = field(default_factory=list)
    benchmarks: dict[str, float] = field(default_factory=dict)

    def run(self, name: str, fn: Callable[[], Out], code: str = "NS-E202") -> Out | None:
        """Run a check body; a raised error becomes a failed check. Returns the Out when it ran."""
        try:
            out = fn()
        except NsError as e:
            self.checks.append(GateCheck(name=name, passed=False, log=explain(e)))
            self.log.warn("check_failed", name=name, code=e.code, detail=e.detail)
            return None
        except Exception as e:  # tolerant by design: any probe failure is reported, not raised
            wrapped = NsError(code, f"{type(e).__name__}: {e}")
            self.checks.append(GateCheck(name=name, passed=False, log=explain(wrapped)))
            self.log.error("check_error", name=name, error=str(e))
            return None
        self.checks.append(
            GateCheck(name=name, passed=out.passed, value=out.value, limit=out.limit, log=out.log)
        )
        self.log.info("check", name=name, passed=out.passed, value=out.value)
        return out

    def bench(self, key: str, value: float, limit: str, ok: bool, soft: bool = False) -> None:
        """Add a spec 14.6 row. `soft` rows are logged but never fail the report."""
        self.benchmarks[key] = value
        log = None if ok else ("over the soft limit; logged only" if soft else "target missed")
        self.checks.append(
            GateCheck(name=f"bench.{key}", passed=ok or soft, value=value, limit=limit, log=log)
        )


def _need(ep: Endpoint | None, service: str) -> Endpoint:
    if ep is None:
        raise NsError("NS-E202", f"{service} is not available (see the {service}.start check)")
    return ep


def run_verify(
    paths: Paths, rt: Runtime, ctl: Controller, *, quick: bool = False, deps: Deps | None = None,
    progress: Callable[[str], None] | None = None,
) -> GateReport:
    """Run all checks against the supervisor `ctl` and return the report (not yet written).

    `progress` gets one line as each talker starts: the voice test can take minutes and nothing else shows it.
    """
    d = deps or Deps()
    smi = smi_source(paths, rt)
    mark = simulated_mark(smi)  # H-10: a simulated card's numbers are labelled and never gate evidence
    vram_used = d.vram_used or (lambda: procs.gpu_memory_used_mb(smi))
    gpu_name = d.gpu_name or (lambda: procs.gpu_name(smi))
    rec = _Recorder(NsLogger("verify", paths.logs))
    ram = d.ram()
    tg = targets_for(ram)

    def acquire(service: Service, alias: str | None = None) -> tuple[Endpoint, float]:
        t0 = time.perf_counter()
        ep = ctl.acquire(service, OWNER, model_alias=alias, timeout_s=600)
        return ep, time.perf_counter() - t0

    def release(service: Service) -> None:
        try:
            ctl.release(service, OWNER)
        except NsError as e:
            rec.log.warn("release_failed", service=service, detail=e.detail)

    # -- supervisor ------------------------------------------------------------------------
    def supervisor() -> Out:
        st = ctl.status()
        states = {n: s.get("state") for n, s in st.get("services", {}).items()}
        return Out(True, "up", log=json.dumps(states))

    def cold_start() -> Out:
        ctl.stop_all()
        return Out(True, "all services stopped", log="so the model load below is measured cold")

    rec.run(names.SUPERVISOR_STATUS, supervisor)
    if not quick:
        rec.run(names.SUPERVISOR_COLD_START, cold_start)

    # A real install on a PC without an NVIDIA card (profile cpu-dev, answer `allow_cpu`) renders with libx264
    # and has no nvidia-smi: the encoder check runs on the processor and nothing measures video memory.
    no_card = rt.profile == "cpu-dev"

    # -- the card's memory before the test loads anything of ours (L5) -----------------------------------
    # Read only when the supervisor's own status shows none of our services loaded (quick: at the start;
    # full: after the cold start stopped them). Without such a moment `vram.idle` compares nothing, says why.
    baseline: dict[str, Any] = {}

    def read_baseline() -> None:
        if rt.dev_mode or no_card or mark is not None:
            return
        try:
            why = nothing_of_ours_loaded(ctl.status())
        except NsError as e:
            why = f"the supervisor's status could not be read ({e.code})"
        except Exception as e:  # tolerant by design: a baseline that cannot be had is said, never guessed
            why = f"the supervisor's status could not be read ({type(e).__name__})"
        if why is None:
            used = settled()
            if used is None:
                why = "nvidia-smi gave no figure"
            else:
                baseline["mb"] = float(used)
        if why is not None:
            baseline["why"] = why
        rec.log.info("vram_baseline", mb=baseline.get("mb"), waited_s=baseline.get("waited_s"),
                     why=baseline.get("why"))  # fmt: skip

    def reading() -> int | None:
        try:
            return vram_used()
        except Exception:
            return None

    def settled() -> int | None:
        """The card's memory in use once it stopped falling (review of block E2, finding 5): a model stopped
        a moment ago may still be counted for up to a few seconds (on the studio PC about one), and a
        baseline that held it would hide a leak. The idle row's own allowance: at most 10 x 0.5 s."""
        used = reading()
        waited = 0.0
        for _ in range(10):
            if used is None:
                break
            d.sleep(0.5)
            waited += 0.5
            later = reading()
            if later is None or later >= used:
                break
            used = later
        baseline["waited_s"] = waited
        return used

    read_baseline()

    # -- llama -----------------------------------------------------------------------------
    llama: list[Endpoint] = []

    def llama_start() -> Out:
        ep, secs = acquire("llama", "code")
        llama.append(ep)
        return Out(True, round(secs, 1), log=f"{ep.base_url} alias={ep.model_alias}")

    started = rec.run(names.LLAMA_START, llama_start, "NS-E203")
    ep_llama = llama[0] if llama else None
    if started is not None and not quick:
        secs = float(started.value or 0)
        rec.bench("model_load_s", secs, f"<= {tg.load_s:.0f} s (soft)", secs <= tg.load_s, soft=True)
    rec.run(names.LLAMA_HEALTH, lambda: Out(LlmClient(_need(ep_llama, "llama").base_url, "code").health()))

    def llama_json() -> Out:
        out = LlmClient(_need(ep_llama, "llama").base_url, "code").complete_json(
            prompt_name="verify",
            system="You answer with JSON only.",
            user='Reply with {"ok": true}.',
            schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
            temperature=0.0,
            max_tokens=64,
        )
        return Out(out.get("ok") is True, "json ok" if out.get("ok") is True else json.dumps(out))

    rec.run(names.LLAMA_JSON, llama_json, "NS-E301")

    if not quick:

        def llama_speed() -> Out:
            return Out(True, measure_tok_s(_need(ep_llama, "llama").base_url))

        sp = rec.run(names.LLAMA_TOK_S, llama_speed, "NS-E301")
        if sp is not None:
            v = float(sp.value or 0)
            rec.bench("code_tok_s", v, f">= {tg.tok_s:.0f} tok/s", v >= tg.tok_s)
    if ep_llama is not None:
        release("llama")

    # -- embed -----------------------------------------------------------------------------
    emb: list[Endpoint] = []

    def embed_start() -> Out:
        ep, secs = acquire("embed")
        emb.append(ep)
        return Out(True, round(secs, 1), log=ep.base_url)

    rec.run(names.EMBED_START, embed_start, "NS-E203")

    def embed_vec() -> Out:
        vecs = EmbedClient(_need(emb[0] if emb else None, "embed").base_url).embed(["nightshift verify"])
        ok = len(vecs) == 1 and len(vecs[0]) > 0
        return Out(ok, float(len(vecs[0])) if ok else 0.0, log="embedding dimension")

    rec.run(names.EMBED_VECTOR, embed_vec, "NS-E301")
    if emb:
        release("embed")

    # -- tts -------------------------------------------------------------------------------
    tts: list[Endpoint] = []

    def tts_start() -> Out:
        ep, secs = acquire("tts")
        tts.append(ep)
        if d.tts_engine(ep.base_url) == "reference":
            rec.checks.append(GateCheck(
                name=names.TTS_ENGINE, passed=True, value="reference",
                log="NS-W301: the voice service runs the slower PyTorch reference engine, not qwentts.cpp",
            ))
        return Out(True, round(secs, 1), log=ep.base_url)

    rec.run(names.TTS_START, tts_start, "NS-E203")

    def speaking(client: TtsClient, voice: str, out_dir: Path) -> None:
        """`tts.rate` and `tts.start_s` (L2, L3): six sentences in ONE batch request, the product's own route.

        A batch that fails is a part that did not work (a row without a limit); a rate over the target is a
        missed target (the row carries the limit); a slow start is logged only (a soft bench).
        """
        say = progress or (lambda line: None)
        say(f"timing the voice {voice} on six sentences in one request ...")
        items = [TtsItem(f"rate-{k + 1}", text, voice) for k, text in enumerate(RATE_SENTENCES)]
        rec.log.info("speaking_rate", voice=voice, sentences=len(items))
        t0 = d.clock()
        try:
            batch = client.speak_batch_stamped(items, out_dir)
        except Exception as e:  # tolerant by design: the row says what failed
            err = e if isinstance(e, NsError) else NsError("NS-E310", f"{type(e).__name__}: {e}")
            rec.log.warn("speaking_rate_failed", code=err.code, detail=err.detail)
            rec.checks.append(GateCheck(
                name=names.TTS_RATE, passed=False,
                log=f"six sentences in one request with the voice {voice!r} failed: {explain(err)}",
            ))
            return
        whole_s = d.clock() - t0
        audio_s = sum(f.seconds for f in batch.files)
        got = speaking_rate(batch.stamps)
        if got is not None:
            rate, start_s, after_s = got
            how = (f"speaking only: first to last of {len(batch.stamps or ())} sentences in one request, "
                   f"over the {after_s:.2f} s of audio after the first (the whole call took {whole_s:.1f} s)")
        elif audio_s > 0:
            rate, start_s = whole_s / audio_s, None
            why = ("its per-sentence times did not arrive one after the other" if batch.stamps else
                   "this voice engine gives no per-sentence times (fake, Windows speech, reference engine)")
            how = (f"the whole call over its audio: {why}; {len(batch.files)} sentences, {audio_s:.2f} s "
                   "of audio")
        else:
            rec.checks.append(GateCheck(
                name=names.TTS_RATE, passed=False,
                log=f"six sentences in one request with the voice {voice!r} came back without sound",
            ))
            return
        ok = rate <= tg.rtf
        rec.checks.append(GateCheck(
            name=names.TTS_RATE, passed=ok, value=round(rate, 3), limit=f"<= {tg.rtf} RTF", log=how
        ))
        rec.bench("tts_rtf", round(rate, 3), f"<= {tg.rtf}", ok)
        if start_s is None:
            rec.checks.append(GateCheck(
                name=names.TTS_START_S, passed=True, value="n/a",
                log="no usable per-sentence times came back, so the start is not told apart from speaking",
            ))
            return
        rec.checks.append(GateCheck(
            name=names.TTS_START_S, passed=True, value=round(start_s, 1),
            log="seconds to the first sentence of that request: the voice program's start, its model load "
                "and one sentence",
        ))  # fmt: skip
        soft = f"<= {TTS_START_SOFT_S:.0f} s (soft)"
        rec.bench("tts_start_s", round(start_s, 1), soft, start_s <= TTS_START_SOFT_S, soft=True)

    def tts_modes() -> Out:
        base_url = _need(tts[0] if tts else None, "tts").base_url
        client = TtsClient(base_url, timeout_s=FIRST_TALKER_TIMEOUT_S)
        later = TtsClient(base_url, timeout_s=LATER_TALKER_TIMEOUT_S)
        voices = client.voices()
        by_mode: dict[str, str] = {}
        for v in voices:
            by_mode.setdefault(str(v.get("mode")), str(v.get("name")))
        if not by_mode:
            raise NsError("NS-E202", "tts reports no voices")
        order = ["preset", "design", "clone"]
        picked = [(m, by_mode[m]) for m in order if m in by_mode]
        picked = picked or list(by_mode.items())[:1]
        # One sentence with every installed talker, quick or not (the Base talker of clone voices and
        # VoiceDesign are tested at install, not at the first video): it proves the voice SPEAKS, so the row
        # carries no limit and its value is the whole call in seconds (L1; one sentence is one start of the
        # voice program and its model, so its time says nothing about speaking speed). The first talker (the
        # preset) is the engine check: when it does not speak its row fails, as it always did. A designed or
        # cloned voice that does not speak is a WARNING row naming the mode and the voice (`talker_warning`):
        # the run, an update and setup go on, and the next talker is still spoken. The speed is measured
        # after them, full run only, on the batch route (`speaking`).
        say = progress or (lambda line: None)
        rtfs: list[float] = []
        failed: list[str] = []
        first_spoke = False
        try:
            with tempfile.TemporaryDirectory(prefix="ns-verify-") as tmp:
                for i, (mode, voice) in enumerate(picked):
                    line = f"speaking a test sentence with the {mode} voice {voice} ..."
                    say(line)
                    rec.log.info("speaking", mode=mode, voice=voice)
                    t0 = d.clock()
                    try:
                        audio_s = (client if i == 0 else later).speak(
                            TTS_SENTENCE, voice, 1.0, Path(tmp) / f"{mode}.wav"
                        )
                    except Exception as e:  # tolerant by design: one talker failing must not hide the others
                        err = e if isinstance(e, NsError) else NsError("NS-E310", f"{type(e).__name__}: {e}")
                        failed.append(mode)
                        rec.log.warn(
                            "talker_failed", mode=mode, voice=voice, code=err.code, detail=err.detail
                        )
                        if i == 0:
                            rec.checks.append(GateCheck(
                                name=names.tts_mode(mode), passed=False,
                                log=f"{mode} voice {voice!r} did not speak: {explain(err)}",
                            ))
                        else:
                            rec.checks.append(talker_warning(mode, voice, f"{err.code}: {err.detail}"))
                        continue
                    if i > 0 and audio_s <= 0:
                        failed.append(mode)
                        rec.checks.append(talker_warning(mode, voice, "the answer had no sound"))
                        continue
                    whole_s = d.clock() - t0
                    if audio_s > 0:
                        rtfs.append(whole_s / audio_s)
                        first_spoke = first_spoke or i == 0
                    rec.checks.append(
                        GateCheck(
                            name=names.tts_mode(mode),
                            passed=audio_s > 0,
                            value=round(whole_s, 3),
                            limit=None,
                            log=f"voice={voice} audio_s={audio_s}",
                        )
                    )
                if not quick and first_spoke:
                    speaking(client, picked[0][1], Path(tmp) / "rate")
        finally:
            client.client.close()
            later.client.close()
        if rtfs:
            # L4: the voice stage sizes its batch time limit from this figure, so it keeps getting exactly
            # what it got before card verify-limits: the single calls' whole ratio (start and load included).
            # The rate of `tts.rate` is smaller, and nothing yet shows a real job's limit may shrink to it.
            save_rtf(paths, max(rtfs), quick=quick)
        note = "modes exercised" if not failed else f"modes exercised; did not speak: {', '.join(failed)}"
        return Out(True, ",".join(m for m, _ in picked), log=note)

    rec.run(names.TTS_VOICES, tts_modes, "NS-E202")
    if tts:
        release("tts")

    # -- whisper ---------------------------------------------------------------------------
    def whisper() -> Out:
        ctl.acquire("whisper", OWNER, timeout_s=600)
        try:
            if rt.dev_mode:     # a dev install has no faster-whisper and no model (the studio fakes it)
                return Out(True, 0.0, log="dev mode: fake speech recognition, no model to load")
            secs = d.whisper(paths, rt)
        finally:
            release("whisper")
        return Out(True, round(secs, 1), log=f"{rt.whisper.model} on {rt.whisper.device}")

    rec.run(names.WHISPER_LOAD, whisper, "NS-E411")

    # -- NVENC -----------------------------------------------------------------------------
    def nvenc() -> Out:
        if rt.dev_mode or no_card:
            d.x264()
            why = "dev mode" if rt.dev_mode else "no graphics card (cpu-dev profile)"
            return Out(True, "libx264 ok", log=f"{why}: 2 s colour source encoded on the processor")
        d.nvenc()
        return Out(True, "h264_nvenc ok", log="2 s colour source encoded")

    rec.run(names.NVENC_PROBE, nvenc, "NS-W401")

    # -- fake-services job -----------------------------------------------------------------
    if not quick:

        def job() -> Out:
            got = d.job(paths)
            if got is None:
                return Out(True, "skipped", log="studio is not installed yet")
            run = got if isinstance(got, JobRun) else JobRun(float(got))
            said = f"{JOB_COMMAND}, in a home of its own (run\\{SCRATCH_NAME}), removed when it ended"
            return Out(True, run.seconds, log=" ".join([said + ".", *run.notes]))

        rec.run(names.JOB_FAKE_SERVICES, job, "NS-E202")

    # -- idle VRAM -------------------------------------------------------------------------
    def idle_vram() -> Out:
        if rt.dev_mode or no_card:
            why = "dev mode" if rt.dev_mode else "cpu-dev profile"
            return Out(True, "n/a", log=f"{why}: no graphics card to measure")
        if mark is not None:
            return Out(True, f"n/a: {SIMULATED_EVIDENCE}", log=f"{mark}: its numbers are no evidence")
        try:
            ctl.stop_all()
        except NsError as e:  # no supervisor means nothing of ours is loaded; still measure the GPU
            rec.log.warn("stop_all_failed", e.detail)
        base = baseline.get("mb")
        used: int | None = None
        for _ in range(10):  # the driver frees a stopped process's memory within a few seconds
            used = vram_used()
            if used is None or base is None or used - base <= tg.idle_vram_mb:
                break
            d.sleep(0.5)
        if used is None:
            raise NsError("NS-E102", "nvidia-smi is not available")
        if base is None:
            return Out(True, "n/a", log=(
                f"{used:.0f} MB in use after unload, not compared: what other programs held could not be "
                f"read at a moment with none of Nightshift's models loaded ({baseline.get('why')})"
            ))  # fmt: skip
        ours = max(0.0, float(used) - base)  # (other programs may have freed memory meanwhile: never below 0)
        limit = f"<= {tg.idle_vram_mb:.0f} MB"
        ok = ours <= tg.idle_vram_mb
        rec.bench("idle_vram_mb", ours, limit, ok)
        waited = float(baseline.get("waited_s") or 0.0)
        said = (f"{used:.0f} MB in use, {base:.0f} MB of it held by other programs before the test (read "
                f"once it stopped falling, after {waited:.1f} s)")
        return Out(ok, ours, limit, said)

    rec.run(names.VRAM_IDLE, idle_vram, "NS-E102")

    machine: dict[str, str | float | None] = {
        "gpu": None if rt.dev_mode or no_card else labelled(mark, gpu_name()),
        "ram_gb": ram,
        "disk_type": None,
        "os": platform.platform(),
        "nightshift": __version__,
        "profile": rt.profile,
        "targets": tg.profile,
        "not_measured": NOT_MEASURED,
    }
    return GateReport(
        slice="verify-quick" if quick else "verify",
        date=datetime.now().isoformat(timespec="seconds"),
        machine=machine,
        checks=rec.checks,
        benchmarks=rec.benchmarks,
        dev_mode=rt.dev_mode,
    )


def report_path(paths: Paths) -> Path:
    """`logs\\verify-report.json`."""
    return paths.logs / "verify-report.json"


def write_report(paths: Paths, report: GateReport) -> Path:
    """Write the report as JSON; returns the file."""
    return write_text_atomic(report_path(paths), json.dumps(report.model_dump(mode="json"), indent=2) + "\n")


def format_lines(report: GateReport) -> list[str]:
    """Console table: one line per check, failures and warnings with their reason (and the way out)."""
    lines = []
    warned = 0
    for c in report.checks:
        warn = c.passed and c.value == names.TALKER_WARNING_VALUE and c.name.startswith(names.TTS_MODE_PREFIX)
        warned += warn
        mark = "WARN" if warn else ("PASS" if c.passed else "FAIL")
        val = "" if c.value is None else f" = {c.value}"
        lim = f"  [{c.limit}]" if c.limit else ""
        lines.append(f"[{mark}] {c.name}{val}{lim}")
        if c.log and (not c.passed or warn or c.log.startswith("NS-W")):
            lines.append(f"       {c.log}")
    # L6: a failed row is said as a missed target (it carries its limit) or a part that did not work
    result = verdict(report.checks) if not report.passed else "ALL CHECKS PASSED"
    if report.passed and warned:
        result += " (a voice did not speak: see the WARN lines above)"
    lines.append("Result: " + result)
    return lines


