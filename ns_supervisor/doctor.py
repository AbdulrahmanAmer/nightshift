"""C2 `ns doctor [--bundle]`: quick diagnosis, and the diagnostics zip the partner sends back (J7).

The bundle holds logs, install_state, runtime, verify-report, install-report, the setup evidence (`setup/`:
wizard flags, answers without keys, the hardware check, `setup-server.err`, the bootstrap logs), nvidia-smi
output, `uv pip freeze` per venv, the last 3 job directories without media, and VERSION. It never holds API
keys, models or videos.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

import installer.stage2.commands as commands
import installer.stage2.shortcut_owner as so
from installer.stage2.web import venvrepair
from ns_common import (
    __version__,
    commandline,
    config_snapshot,
    devmode,
    ffmpeg,
    first_start,
    rollback_intent,
    testguard,
)
from ns_common import stock_answer as sa
from ns_common.atomic import read_text_retry, retry_busy, write_zip_atomic
from ns_common.config import load_install_state
from ns_common.devmode import is_dev
from ns_common.errors import NsError, describe
from ns_common.models import InstallState, Runtime
from ns_common.paths import Paths, repo_root
from ns_common.runtime_read import read_runtime
from ns_common.uv import find_uv
from ns_supervisor import launch_reach, procs, schedule, schedule_cfg
from ns_supervisor.childenv import child_env, window_flags
from ns_supervisor.client import SupervisorClient
from ns_supervisor.gpu import simulated_mark, smi_source

MEDIA_SUFFIXES = frozenset(
    {".mp4", ".mkv", ".mov", ".webm", ".avi", ".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg"}
    | {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
)
MAX_LOG_BYTES = 5 * 1024 * 1024
VENVS = ("studio", "supervisor", "tts")


def desktop_dir() -> Path:
    """The user's Desktop (follows OneDrive redirection via the registry; falls back to ~/Desktop)."""
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "Desktop")
            if isinstance(value, str) and value:
                return Path(value)
        except OSError:
            pass
    return Path.home() / "Desktop"


def freeze_venv(paths: Paths, venv: str) -> str | None:
    """`uv pip freeze` of a venv; None when uv or the venv is absent or freezing fails."""
    py = paths.venvs / venv / "Scripts" / "python.exe"
    uv = find_uv(paths)
    if uv is None or not py.exists():
        return None
    try:
        r = subprocess.run(
            [uv, "pip", "freeze", "--python", str(py)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=child_env(),
            creationflags=window_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _add_file(z: zipfile.ZipFile, src: Path, arcname: str, skipped: list[str]) -> None:
    """Add a file; very large ones keep only their last MAX_LOG_BYTES."""
    try:
        size = src.stat().st_size
        if size > MAX_LOG_BYTES:
            with src.open("rb") as fh:
                fh.seek(size - MAX_LOG_BYTES)
                z.writestr(arcname, fh.read())
            skipped.append(f"{arcname}: truncated to last {MAX_LOG_BYTES} bytes")
        else:
            retry_busy(partial(z.write, src, arcname))
    except OSError as e:
        skipped.append(f"{arcname}: {e}")


def _last_jobs(paths: Paths, n: int = 3) -> list[Path]:
    if not paths.jobs_dir.exists():
        return []
    dirs = [p for p in paths.jobs_dir.iterdir() if p.is_dir()]
    return sorted(dirs, key=lambda p: p.stat().st_mtime, reverse=True)[:n]


def setup_data_dir() -> Path:
    """Where setup keeps its own files (setup-server.err, the bootstrap logs), outside the install folder."""
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) if local else Path.home() / "AppData" / "Local") / "Nightshift"


def _scrubbed_answers(src: Path) -> str | None:
    """answers.json text without any `*_api_key` field; None when the file is missing or not an object."""
    try:
        data = json.loads(read_text_retry(src, "utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    clean = {k: v for k, v in data.items() if not k.endswith("_api_key")}
    return json.dumps(clean, indent=2, ensure_ascii=False) + "\n"


def _add_setup_evidence(
    z: zipfile.ZipFile, paths: Paths, skipped: list[str], setup_dir: Path, temp_dir: Path
) -> None:
    """What a failed setup is diagnosed from: flags, answers (no keys), the hardware check, setup logs."""
    report = paths.logs / "install-report.json"
    if report.is_file():
        _add_file(z, report, "install-report.json", skipped)
    else:
        skipped.append("install-report.json: not found")
    flags = paths.config / "wizard_flags.json"
    if flags.is_file():
        _add_file(z, flags, "setup/wizard_flags.json", skipped)
    answers = _scrubbed_answers(paths.config / "answers.json")
    if answers is not None:
        z.writestr("setup/answers.json", answers)
    boot = setup_dir / "bootstrap"
    hardware = next(
        (p for p in (boot / "preflight.json", temp_dir / "nightshift-preflight.json") if p.is_file()), None
    )
    if hardware is not None:
        _add_file(z, hardware, "setup/preflight.json", skipped)
    else:
        skipped.append("setup/preflight.json: not found")
    err = setup_dir / "setup-server.err"
    if err.is_file():
        _add_file(z, err, "setup/setup-server.err", skipped)
    if boot.is_dir():
        for f in sorted(boot.iterdir()):
            if f.is_file() and f != hardware:
                _add_file(z, f, f"setup/bootstrap/{f.name}", skipped)


def build_bundle(
    paths: Paths,
    *,
    out_dir: Path | None = None,
    now: datetime | None = None,
    freeze: Callable[[Paths, str], str | None] = freeze_venv,
    smi: Callable[[], str | None] | None = None,
    app: Path | None = None,
    setup_dir: Path | None = None,
    temp_dir: Path | None = None,
) -> Path:
    """Write `nightshift-diagnostics-<date>-<HHMM>.zip` (default: Desktop) and return its path.

    The name carries the minute so two zips on one day do not overwrite each other. `setup_dir` and `temp_dir`
    default to the Nightshift folder in LOCALAPPDATA and the temp folder; tests pass folders of their own.
    """
    when = now or datetime.now()
    target_dir = out_dir or desktop_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    out = target_dir / f"nightshift-diagnostics-{when:%Y-%m-%d-%H%M}.zip"
    skipped: list[str] = []
    version_text = __version__
    if app is not None and (app / "VERSION").exists():
        version_text = (app / "VERSION").read_text(encoding="utf-8").strip()

    def fill(z: zipfile.ZipFile) -> None:
        if paths.logs.exists():
            for f in sorted(paths.logs.rglob("*")):
                if f.is_file():
                    _add_file(z, f, "logs/" + f.relative_to(paths.logs).as_posix(), skipped)
        ledger = paths.data / "ledger"  # the night ledger (ns_studio/ledger.py): every night's times
        if ledger.is_dir():
            for f in sorted(ledger.glob("ledger-*.jsonl")):
                _add_file(z, f, f"ledger/{f.name}", skipped)
        for src, arc in (
            (paths.install_state_file, "install_state.json"),
            (paths.runtime_file, "runtime.json"),
            (paths.logs / "verify-report.json", "verify-report.json"),
            (paths.config / "schedule.json", "schedule.json"),
        ):
            if src.is_file():
                _add_file(z, src, arc, skipped)
            elif arc in ("install_state.json", "runtime.json", "verify-report.json"):
                skipped.append(f"{arc}: not found")
        _add_setup_evidence(
            z, paths, skipped, setup_dir or setup_data_dir(), temp_dir or Path(tempfile.gettempdir())
        )
        if smi is not None:
            report = smi()
        else:  # this install's nvidia-smi: the simulated card says so on its first line (H-10)
            source = smi_source(paths)
            report = procs.nvidia_smi_report(source)
            mark = simulated_mark(source)
            if report is not None and mark is not None:
                report = simulated_header(mark) + report
        z.writestr("nvidia-smi.txt", report if report is not None else "nvidia-smi is not available\n")
        for venv in VENVS:
            text = freeze(paths, venv)
            if text is None:
                skipped.append(f"pip-freeze/{venv}.txt: venv or uv not available")
            else:
                z.writestr(f"pip-freeze/{venv}.txt", text)
        for job in _last_jobs(paths):
            for f in sorted(job.rglob("*")):
                if f.is_file() and f.suffix.lower() not in MEDIA_SUFFIXES:
                    _add_file(z, f, f"jobs/{job.name}/{f.relative_to(job).as_posix()}", skipped)
        z.writestr("VERSION", version_text + "\n")
        info = {"created": when.isoformat(timespec="seconds"), "version": version_text, "notes": skipped}
        z.writestr("bundle-info.json", json.dumps(info, indent=2) + "\n")

    # built beside the final name and swapped in whole: the Desktop copy (OneDrive, antivirus, the partner)
    # never meets a zip that is still being written (the freeze steps take seconds), and a second bundle in
    # the same minute does not tear the first
    write_zip_atomic(out, fill)
    return out


def diagnose(
    paths: Paths, client: SupervisorClient, on_wait: Callable[[], None] | None = None
) -> list[tuple[str, bool, str]]:
    """Quick health table for plain `ns doctor`: (check, ok, detail-or-fix-hint). The last row runs the
    start check (`program_folders`); `on_wait` is called once if it has to wait for another program."""
    rows: list[tuple[str, bool, str]] = []
    rt, runtime_rows = runtime_rows_of(paths)
    rows += runtime_rows
    profile = rt.profile if rt is not None else None    # None: the install's kind is unknown
    st, state_row = install_state_row(paths)
    rows.append(state_row)
    for venv in ("studio", "supervisor"):
        py = paths.venvs / venv / "Scripts" / "python.exe"
        rows.append((f"venv {venv}", py.exists(), str(py)))
    up = client.is_up()
    where = client.base_url if up else "not running (opening the Dashboard starts it)"
    rows.append(("supervisor", up, where))
    source = smi_source(paths)
    mark = simulated_mark(source)
    if mark is not None:    # H-10: the simulated graphics card answers, and the row says it is one
        name = procs.gpu_name(source)
        rows.append(("nvidia-smi", name is not None,
                     f"{mark}: {name}" if name is not None
                     else f"{mark}: run\\fake_gpu.json makes it fail (\"fail\")"))  # fmt: skip
    elif is_dev(paths):     # docs/DEV_MODE.md: a dev install has no graphics card to ask
        rows.append(("nvidia-smi", True, "not needed (dev mode)"))
    elif profile == "cpu-dev":
        # D-44: an install that runs on the processor (the engine test, a PC with no NVIDIA card) has no
        # driver to ask for; a gpu install, or one whose runtime.json cannot be read, still asks
        rows.append(("nvidia-smi", True, "not needed (this install runs on the processor: profile cpu-dev)"))
    else:
        smi = procs.nvidia_smi_report(source)
        rows.append(("nvidia-smi", smi is not None, "ok" if smi is not None else "no NVIDIA driver found"))
    row = fake_gpu_row(paths)
    if row is not None:
        rows.append(row)
    have_ffmpeg = ffmpeg.available()
    rows.append(("ffmpeg", have_ffmpeg, "on PATH" if have_ffmpeg else "ffmpeg/ffprobe not found"))
    rows.append(launch_reach.doctor_row(paths))
    rows.append(task_limit_row(paths))
    rows.append(commands_row(paths))
    rows += stock_key_rows(paths)
    app = Path(st.app_path) if st.app_path else repo_root()
    rows += model_rows(paths, app)
    rows.append(program_folders(paths, app, on_wait))
    rows.append(python_environments(paths, app))
    return rows


def _stored_stock_keys() -> dict[str, str]:
    """The keys the studio reads (`ns_studio.jobs.load_keys`: Credential Manager, else `secrets.dpapi`)."""
    from ns_studio.jobs import load_keys

    return load_keys()


def stock_key_rows(
    paths: Paths,
    *,
    keys: Callable[[], dict[str, str]] | None = None,
    ask: Callable[[str, str], sa.Answer] | None = None,
) -> list[tuple[str, bool, str]]:
    """One row per stock footage site (card stock-truth): ok, wrong, could not be checked now, or no key
    stored, each by the one classifier through setup's own key check (`installer.stage2.web.keys.ask`). Never
    the key's text, never a crash: a key store that cannot be read is a row that says so."""
    if devmode.fake_footage(paths):  # a dev install or the engine test: placeholder footage, no key needed
        said = "not needed (this install uses placeholder footage)"
        return [(f"stock key {p}", True, said) for p in sa.PROVIDERS]
    try:
        stored = (keys or _stored_stock_keys)()
    except Exception as e:  # noqa: BLE001 - a row, never a traceback
        return [("stock keys", False, f"could not read the stored keys ({type(e).__name__})")]
    if ask is None:
        from installer.stage2.web.keys import ask as setup_ask

        ask = setup_ask
    rows: list[tuple[str, bool, str]] = []
    for p in sa.PROVIDERS:
        name, key = sa.NAMES[p], stored.get(p)
        if not key:  # not a fault of the install (setup allows none); a video asks for one (NS-E502)
            if any(stored.get(o) for o in sa.PROVIDERS):
                how = "optional: the other site's key is enough"
            else:
                how = f"a video needs one: add a Pixabay key (free) in {sa.KEYS_PLACE}"
            rows.append((f"stock key {p}", True, f"no key stored ({how})"))
            continue
        try:
            a = ask(p, key)
        except Exception as e:  # noqa: BLE001 - a row, never a traceback
            rows.append((f"stock key {p}", False, f"could not be checked ({type(e).__name__})"))
            continue
        if a.ok:
            rows.append((f"stock key {p}", True, f"{name} accepted the key"))
        elif a.kind == sa.KEY_WRONG:
            rows.append((f"stock key {p}", False, f"{a.code}: {a.sentence} {a.way_out}"))
        else:
            rows.append((f"stock key {p}", True, f"could not be checked now ({a.code}): {a.sentence}"))
    return rows


#: The row that says when the model files were last read by hash (card model-file-damaged, M2).
HASH_ROW = "model files last checked by hash"


def model_rows(paths: Paths, app: Path) -> list[tuple[str, bool, str]]:
    """M1, M2 (card model-file-damaged): one row per kind of model file of this install (language, story,
    embed, voice, whisper, face finder), from stat calls and the check record only, so plain `ns doctor` stays
    fast: missing, a stopped download (it continues), the wrong size, or recorded damaged are FAIL rows with
    the way out; then when the files were last read by hash, with how long `ns verify --downloads` takes."""
    from installer.stage2 import verify_downloads as vd
    from installer.stage2.downloader import human_bytes
    from installer.stage2.web import modelmarks

    try:
        items = vd.model_items(paths, app)
    except Exception as e:  # noqa: BLE001 - a row, never a traceback (no answers yet, an unreadable manifest)
        return [("model files", True, f"{WARN}not checked: {e}")]
    damaged = modelmarks.recorded_damage(paths.home)
    groups: dict[str, list[vd.DownloadItem]] = {}
    for item in items:
        groups.setdefault(vd.role(item, paths), []).append(item)
    rows: list[tuple[str, bool, str]] = []
    for label, group in groups.items():
        bad: list[str] = []
        notes: list[str] = []
        size = present = 0
        for item in group:
            row = vd.check_item(item, quick=True)
            present += row.size is not None
            where = modelmarks.key_of(paths.home, Path(item.dest)).replace("/", "\\")
            found = damaged.get(modelmarks.key_of(paths.home, Path(item.dest)))
            if row.status in (vd.MISSING, vd.BAD):
                bad.append(f"{row.status} ({where}{f': {row.detail}' if row.detail else ''}). {row.fix}")
            elif found is not None:
                bad.append(f"damaged ({where}: its SHA256 did not match at the check of "
                           f"{found.get('checked_at')}). {vd.REPAIR.format(what=vd.what(item))}")
            elif row.status == vd.NO_PIN and item.size:
                notes.append(f"{Path(item.dest).name}: its size is right; it has no pinned SHA256, so it "
                             "cannot be checked by hash (NS-W113)")
            size += row.size or 0
        ok = f"{present} file{'s' if present != 1 else ''} present, {human_bytes(size)}, sizes as downloaded"
        rows.append((label, not bad, " ".join(bad) or "; ".join([ok, *notes])))
    rows.append(hash_row(paths, items))
    return rows


def hash_row(paths: Paths, items: list[Any]) -> tuple[str, bool, str]:
    """M2: when the model files were last read by hash (the oldest of them), or never, with the estimate of
    a full check (the speed measured here, else 500 MB/s on an SSD, 120 MB/s on a hard disk)."""
    from installer.stage2 import verify_downloads as vd
    from installer.stage2.downloader import human_bytes, pinned_sha256

    pinned = [i for i in items if pinned_sha256(i.sha256) and not i.stub and Path(i.dest).is_file()]
    if not pinned:
        return (HASH_ROW, True, "no model file with a pinned SHA256 here")
    total = sum(vd._size(Path(i.dest)) for i in pinned)
    bps, where = vd.default_speed(paths)
    estimate = f"about {vd.duration_text(total / bps)} for {human_bytes(total)} here, at {vd.speed_text(bps)}"
    seen = [vd.last_hashed(paths, i) for i in pinned]
    never = sum(1 for s in seen if s is None)
    if never:
        part = "" if never == len(pinned) else f" for {never} of {len(pinned)} files"
        return (HASH_ROW, True, f"{WARN}never{part}. Run 'ns verify --downloads' ({estimate}, {where}).")
    oldest = min(s for s in seen if s)
    return (HASH_ROW, True, f"{oldest} (the oldest of {len(pinned)} files); 'ns verify --downloads' checks "
                            f"them again ({estimate}, {where})")


def runtime_rows_of(paths: Paths) -> tuple[Runtime | None, list[tuple[str, bool, str]]]:
    """(the settings or None, the rows "runtime.json", "llm model" and "embed model"). A file that cannot be
    used is ONE failed row with NS-E125 naming the file and what is wrong (`read_runtime`, the classifier
    setup uses), and its hint, the way out: never a traceback."""
    try:
        rt = read_runtime(paths)
    except NsError as e:
        return None, [("runtime.json", False, f"{e} | Hint: {e.definition.hint}")]
    rows = [("runtime.json", True, str(paths.runtime_file))]
    for label, model in (("llm model", rt.llama.model_path), ("embed model", rt.embed.model_path)):
        ok = bool(model) and Path(model).is_file()
        rows.append((label, ok, model or "not configured; run the installer or 'ns tune'"))
    return rt, rows


#: The way out of an install_state.json that does not name the program folder.
STATE_WAY_OUT = "run Nightshift setup again (INSTALL.cmd) and choose this install's folder"


def install_state_row(paths: Paths) -> tuple[InstallState, tuple[str, bool, str]]:
    """(the state, the row "install_state"). `load_install_state` already takes the last good copy of a
    damaged file and keeps the damaged one as `.bad`; a file that cannot be opened (another program holds it,
    a folder of that name) is a failed row naming it, not a traceback."""
    f = paths.install_state_file
    try:
        st = load_install_state(paths)
    except OSError as e:
        why = f"{type(e).__name__}: {e}"
        return InstallState(), ("install_state", False, f"{f} could not be read ({why}); if another program "
                                                        f"holds it, close that program, else {STATE_WAY_OUT}")
    if st.app_path:
        return st, ("install_state", True, st.app_path)
    if not f.exists() and not f.with_name(f.name + ".bad").exists():
        return st, ("install_state", False, f"{f} does not exist: {STATE_WAY_OUT}")
    kept = f.with_name(f.name + ".bad")
    damaged = f" (a damaged copy was kept as {kept.name})" if kept.exists() else ""
    return st, ("install_state", False, f"{f} does not name the program folder{damaged}: {STATE_WAY_OUT}")


def bundle_notes(paths: Paths) -> list[str]:
    """What `ns doctor --bundle` says after the zip: the rows of the two files every check starts from that
    failed (runtime.json, install_state.json), so the owner reads the way out and not only the zip's name."""
    _, rows = runtime_rows_of(paths)
    rows = [r for r in rows if r[0] == "runtime.json"] + [install_state_row(paths)[1]]
    return [f"[FAIL] {name}: {detail}" for name, ok, detail in rows if not ok]


#: The first line of `nvidia-smi.txt` in a diagnostics zip made on the simulated graphics card.
def simulated_header(mark: str) -> str:
    """The first line of `nvidia-smi.txt` in a diagnostics zip made on the simulated graphics card."""
    return (
        f"{mark}: not a real graphics card. These numbers come from run\\fake_gpu.json "
        "(docs/DEV_MODE.md, 'A simulated graphics card').\n"
    )


def fake_gpu_row(paths: Paths) -> tuple[str, bool, str] | None:
    """The row "fake_gpu" (H-10), only when runtime.json sets the switch: ok on a dev or engine-test install
    (the simulated card is on, and says where its numbers come from), FAIL with NS-W121 on any other install
    (the switch is ignored there). None when the switch is not set."""
    if devmode.fake_gpu(paths):
        return ("fake_gpu", True,
                f"on: the simulated graphics card; nvidia-smi is answered from "
                f"{devmode.fake_gpu_file(paths)} (docs/DEV_MODE.md)")  # fmt: skip
    if devmode.fake_gpu_ignored(paths):
        info = describe("NS-W121")
        return ("fake_gpu", False, f"NS-W121: {info.message} {info.hint}")
    return None


ENV_ROW = "python environments"


def python_environments(paths: Paths, app: Path) -> tuple[str, bool, str]:
    """The row "python environments" (D-39b): an update, rollback or resync left a venv unfinished (its
    repair record waits for the next start), or a venv holds another Nightshift than the program in `app`
    (an older updater left it, with no record). Read from the disk only; it changes nothing."""
    try:
        record = venvrepair.read(paths.home)
    except ValueError as e:     # the launch script treats it as present: nothing starts until it is gone
        return (ENV_ROW, False, venvrepair.pending_sentence(paths.home, app, None, str(e)))
    if record is not None and venvrepair.matches(record, app):
        return (ENV_ROW, False, venvrepair.pending_sentence(paths.home, app, record))
    found = venvrepair.mixed(paths.home, app)
    if found:
        return (ENV_ROW, False, venvrepair.mixed_sentence(found, app))
    stale = " (a repair record of another program is left; the next start deletes it)" if record else ""
    return (ENV_ROW, True, f"ok: each environment holds Nightshift {venvrepair.program_version(app)}{stale}")


#: A row that is not a failure but needs the owner: printed as `[WARN]` by `ns doctor`.
WARN = "WARN: "


def commands_row(
    paths: Paths,
    *,
    read_user: Callable[[], tuple[str, str] | None] = commands.registry_user_path,
    read_machine: Callable[[], str] = commands.registry_machine_path,
) -> tuple[str, bool, str]:
    """The row "commands" (card FN-3): which install a bare `ns` and `studio` open in a new terminal, read
    from the command files and the PATH as Windows has it (read only; a test run reads its stand-ins)."""
    row, home = "commands", paths.home
    shims = commands.shim_paths(home)
    unfit = commands.shim_problem(home)
    if unfit is not None:               # (a hand-set folder: setup refuses such a name) no Repair changes it
        return (row, True, f"{WARN}none: {unfit} ({commands.CODE}); start Nightshift from its shortcuts")
    if not all(p.is_file() for p in shims):
        missing = f"{shims[0]} or {shims[1].name} is missing ({commands.CODE})"
        return (row, True, f"{WARN}{missing}: run INSTALL.cmd and choose Repair")
    for _, venv, exe, _ in commands.SHIMS:
        if not (home / "venvs" / venv / "Scripts" / exe).is_file():
            return (row, False, commands.missing_line(home))
    if is_dev(paths):
        return (row, True, commands.dev_line(home))
    now = read_user()
    if now is None:
        return (row, True, f"{WARN}Windows did not say what your PATH holds; run ns doctor again")
    known = [] if testguard.under_test() else so.read_list(so.default_list())
    found = commands.plan(now[0], home, False, known)
    if found.state in (so.OTHER, so.UNREACHABLE):
        kept = commands.kept_line(home, str(found.other), found.state == so.UNREACHABLE)
        return (row, True, WARN + kept.split(" This install")[0].removeprefix("Commands: "))
    if found.state != so.MINE:
        return (row, True, f"{WARN}not on your PATH ({commands.CODE}): run INSTALL.cmd and choose Repair")
    # what a new window reaches (the one check setup and every message use: `ns_common.commandline.reach`)
    found = commandline.reach(home, user_value=now[0], machine_value=read_machine())
    if found.ok is False:
        return (row, True, f"{WARN}{found.why}: rename or remove it from your PATH")
    return (row, True, "'ns' and 'studio' open this install")


LIMIT_ROW = "nightly task limit"


def _hours(seconds: int) -> str:
    return f"{seconds // 3600} h" if seconds % 3600 == 0 else f"{round(seconds / 60)} min"


def task_limit_row(
    paths: Paths, read: Callable[[], schedule.TaskState | None] | None = None
) -> tuple[str, bool, str]:
    """The row "nightly task limit" (card night-fits): how long Windows lets the nightly task run, read the
    way `ns schedule` reads the task, against `schedule_cfg.TASK_TIME_LIMIT_S`, the time every night plans
    inside. A task whose limit was changed in Task Scheduler is said with the way back (read only). A test run
    that gave no reader does not ask Windows (`testguard`)."""
    planned = int(schedule_cfg.TASK_TIME_LIMIT_S)
    if read is None:
        if testguard.under_test():
            return (LIMIT_ROW, True, "not read: a test run does not ask Task Scheduler")
        read = partial(schedule.read_task, timeout_s=schedule.READ_TIMEOUT_S)
    try:
        task = read()
    except schedule.ScheduleError as e:
        return (LIMIT_ROW, True, f"{WARN}Windows did not say ({e}); run ns doctor again")
    if task is None or task.limit_s is None:
        return (LIMIT_ROW, True, f"{_hours(planned)} when the nightly task is registered")
    if task.limit_s == planned:
        return (LIMIT_ROW, True, f"{_hours(planned)}, the time each night is planned inside")
    way = (
        "Switch Automatic runs off and on again in Settings > Schedule: Nightshift registers the task again "
        f"with {_hours(planned)}"
    )
    if task.limit_s == 0 or task.limit_s > planned:     # longer than planned: a night ends before it
        allows = "never stops it" if task.limit_s == 0 else f"lets it run {_hours(task.limit_s)}"
        return (LIMIT_ROW, True, f"Windows {allows}; each night is still planned inside {_hours(planned)}")
    return (LIMIT_ROW, True, f"{WARN}Windows stops the nightly task after {_hours(task.limit_s)}, but each "
            f"night is planned inside {_hours(planned)}, so a long night can be stopped half way. {way}")


#: The log events that say the start check, an update or a rollback changed something beside the program.
DONE_EVENTS = (first_start.SWAP_EVENT, "rollback_recovered")


def _last_done(paths: Paths) -> str:
    """The newest thing a start, an update or a rollback did about an interrupted rollback: a rename of a
    leftover `app.swap` (`first_start.SWAP_EVENT` with `settled` true) or a rollback finished, undone or its
    stale record removed (`rollback_recovered`); from the three newest log files of each, with its time.
    Empty when there is none."""
    newest: tuple[str, str] = ("", "")
    for component in (first_start.LOG_COMPONENT, "update"):
        try:
            files = sorted(paths.logs.glob(f"{component}-*.jsonl"))[-3:]
        except OSError:
            continue
        for f in files:
            try:
                lines = read_text_retry(f).splitlines()
            except OSError:
                continue
            for line in lines:
                if not any(e in line for e in DONE_EVENTS):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or rec.get("event") not in DONE_EVENTS:
                    continue
                data = rec.get("data")
                held = isinstance(data, dict) and data.get("settled") is False  # (a rename that failed)
                if not held:
                    newest = max(newest, (str(rec.get("ts", "")), str(rec.get("msg", ""))))
    return f"{newest[1]} ({newest[0][:16].replace('T', ' ')} UTC)" if newest[1] else ""


def program_folders(
    paths: Paths, app: Path, on_wait: Callable[[], None] | None = None
) -> tuple[str, bool, str]:
    """The row "program folders": what an interrupted `ns update --rollback` left beside the program, and
    everything this run did about it.

    `ns doctor` is a start of the program like any other, so the start's own check runs first
    (`first_start.ensure`): a leftover `app.swap` is given its place, an interrupted rollback is finished or
    undone (settings files rewritten, its record deleted), a missing settings copy is taken. Every sentence
    of that check is in the row ("This check: ..."), not only in the log. While another program holds the
    copy's lock the check waits up to 60 s (`on_wait` is called once, so the terminal is not silent), and
    the row then says it did not look. FAIL while `app.swap` is still there (held open, or not a program
    folder), with the way out, or when a rollback could not be finished or undone; ok, naming each folder,
    while a folder that was set aside (`app.swap-left-<stamp>`) waits for the owner to delete it; else ok,
    with the last thing that was done before this run, if any."""
    told: list[tuple[str, str]] = []
    first_start.ensure(paths.config, told=told, on_wait=on_wait)
    if any(event == "first_start_lock_busy" for event, _ in told):
        return (
            "program folders",
            False,
            f"Another Nightshift program (an update or a rollback) kept the settings copy busy for "
            f"{config_snapshot.LOCK_WAIT_S:.0f} s, so this check did not look at the program folders and "
            "changed nothing. Run ns doctor again when that program has finished.",
        )
    did = " ".join(msg for _, msg in told)
    failed = any(event in ("rollback_recovery_failed", "first_start_check_failed") for event, _ in told)
    waiting = rollback_intent.swap_note(app)
    left = [rollback_intent.left_sentence(f) for f in rollback_intent.left_folders(app) if str(f) not in did]
    # (and the settings a stale rollback record held, saved beside the program before the record went)
    left += [
        rollback_intent.saved_settings_sentence(f, paths.config)
        for f in rollback_intent.saved_settings(app) if str(f) not in did
    ]
    now = f"This check: {did}" if did else ""
    if waiting or left or failed:
        said = [waiting, *left, now]
        return ("program folders", not (waiting or failed), " ".join(part for part in said if part))
    if now:
        return ("program folders", True, f"ok. {now}")
    last = _last_done(paths)
    return ("program folders", True, f"ok. Last: {last}" if last else "ok")
