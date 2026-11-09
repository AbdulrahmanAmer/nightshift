"""The PC check: stage 1's hand-over JSON turned into screen data (docs/design/installer-ui.md 2.2, 5.6).

Standard library only. Stage 1 (``stage1.ps1``) decides what blocks; this module only words it for the window
and attaches the fix button.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from installer.stage2.devmode import MIN_FREE_GB, MIN_RAM_GB, relax_checks
from installer.stage2.sysinfo import is_blackwell
from installer.stage2.web.pathcheck import (
    FOLDER_FOREIGN,
    FOLDER_KEPT,
    foreign_warning,
    home_problem,
)
from installer.stage2.web.plan import pinned, required_free_gb

COPY_FILE = Path(__file__).with_name("operator_copy.json")
DRIVER_URL = "https://www.nvidia.com/Download/index.aspx"
STORE_URL = "ms-windows-store://pdp/?ProductId=9NBLGGH4NNS1"
CHECK_IDS = (
    "os", "gpu", "driver", "ram", "disk", "disktype", "network", "winget", "vcruntime", "longpaths",
    "waketimers",
)
DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")

_copy_cache: dict[str, dict[str, str]] | None = None


def operator_copy() -> dict[str, dict[str, str]]:
    """Code -> {title, whatToDo} (docs/design/installer-ui.md 3.6)."""
    global _copy_cache
    if _copy_cache is None:
        _copy_cache = json.loads(COPY_FILE.read_text(encoding="utf-8"))
    return _copy_cache


def copy_for(code: str, message: str = "", hint: str = "") -> tuple[str, str]:
    """Plain title and next step for a code; falls back to the errors.json text."""
    entry = operator_copy().get(code)
    if entry:
        return entry["title"], entry["whatToDo"]
    return message or code, hint or "Press Save a help file and send that file"


def load(path: Path | str) -> dict[str, Any] | None:
    """Read stage 1's hand-over JSON (None when absent or broken)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------------------------------------
# dev mode (docs/DEV_MODE.md)
# ---------------------------------------------------------------------------------------------------------
def apply_dev(pf: dict[str, Any]) -> dict[str, Any]:
    """``devmode.relax_checks`` with the disk rows worked out here (``raw_disk_rows`` knows the 10 GB)."""
    return relax_checks(pf, raw_disk_rows)


# ---------------------------------------------------------------------------------------------------------
# check rows
# ---------------------------------------------------------------------------------------------------------
def _fix(kind: str, label: str, url: str | None = None) -> dict[str, Any]:
    fix: dict[str, Any] = {"kind": kind, "label": label}
    if url:
        fix["url"] = url
    return fix


# (row id, status) -> (title, what to do) for rows whose stage 1 line carries no code
ROW_COPY: dict[tuple[str, str], tuple[str, str]] = {
    ("gpu", "warn"): (
        "The graphics card has less than 16 GB",
        "Setup can continue, but only the Small model fits and videos take longer",
    ),
    # Stage 1 gives a driver warning without a code only when it did not look at the driver at all
    # (no graphics card found, or the cpu-dev profile); a driver that is too old is NS-E103 and an unreadable
    # one is NS-W115, each with its own copy.
    ("driver", "warn"): (
        "The driver was not checked",
        "There is no NVIDIA graphics card to check a driver for. Fix the graphics card row first",
    ),
    ("ram", "warn"): ("Less memory than recommended", "Setup continues with the Small model only"),
    ("disk", "warn"): (
        "Less free space than recommended",
        "Setup fits, but leaves little room for videos. 60 GB keeps about a month of them",
    ),
    ("winget", "warn"): (
        "winget is not available",
        "Nothing to do: setup downloads FFmpeg and the Visual C++ runtime directly instead",
    ),
    ("vcruntime", "warn"): (
        "The Visual C++ runtime is missing",
        "Setup installs it for you. Windows asks for permission once: click Yes",
    ),
    ("network", "warn"): (
        "A download site cannot be reached",
        "Setup can continue. If Hugging Face is blocked, choose \"A folder on this PC\" or "
        "\"A zip from Google "
        "Drive\" for the model files in the next step",
    ),
}
BLACKWELL_W115_TEXT = (
    "Setup works and uses the CUDA 13 build, which RTX 50-series cards need. If a video fails on the "
    "graphics card later, install the newest driver (580 or newer) from nvidia.com/drivers"
)
NETWORK_FAIL_TEXT = (
    "Check the internet, switch off any VPN or proxy, then press Check again. "
    "If only Hugging Face is blocked: "
    "in the next step, choose \"A folder on this PC\" or \"A zip from Google Drive\" for the model files"
)


def _row_fix(row_id: str, status: str, code: str) -> dict[str, Any] | None:
    if row_id in ("gpu", "driver") and code in ("NS-E102", "NS-E103", "NS-W115"):
        return _fix("open-url", "Open the driver page", DRIVER_URL)
    if row_id in ("disk", "disktype"):
        return _fix("choose-drive", "Pick another drive")
    if row_id == "network":
        return _fix("recheck", "Check again")
    if row_id == "winget" and status == "fail":
        return _fix("open-url", "Open the Microsoft Store", STORE_URL)
    if row_id == "waketimers":
        return {"kind": "open-windows-page", "label": "Open power settings", "page": "power"}
    if row_id == "longpaths":
        return _fix("elevate-longpaths", "Fix it")
    return None


def folder_bytes(folder: str | Path) -> int:
    """Bytes of the files under ``folder`` (0 when it is not there): what setup already put on a drive."""
    total = 0
    for current, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += os.stat(os.path.join(current, name)).st_size
            except OSError:
                continue
    return total


def raw_disk_rows(
    pf: dict[str, Any], home: str, held_gb: Callable[[], float] | None = None
) -> list[dict[str, Any]]:
    """The disk and disktype lines for ``home``, with the same rules as stage 1's Get-PreflightRows.

    ``held_gb`` (a locked resume) says how much of the install folder is setup's own work. Below the hard
    limit the row is still only a warning when free space plus that amount reaches the limit: the shortfall
    is the models, programs and environments already on the drive, and the 2 GB guard during downloads and
    NS-E104 from the steps still protect the run.
    """
    letter = home[:1].upper() if len(home) > 1 and home[1] == ":" else ""
    drive = next((d for d in drives_view(pf) if d["letter"] == letter), None)
    base = {"id": "disk", "label": "Free disk space", "code": "", "hint": ""}
    if drive is None:
        return [{**base, "status": "fail", "code": "NS-E104", "value": f"drive of {home} not found"},
                {"id": "disktype", "label": "Disk type", "status": "ok", "code": "", "value": "unknown"}]
    free = drive["freeGb"]
    held = held_gb() if free < 40 and held_gb is not None else 0.0
    if pf.get("dev_mode"):
        base = {**base, "label": "Free disk space"}
        if free < MIN_FREE_GB:
            disk = {**base, "status": "fail", "code": "NS-E104",
                    "value": f"{free:g} GB free on {letter}: (dev mode needs {MIN_FREE_GB} GB)"}
        else:
            disk = {**base, "status": "ok", "value": f"{free:g} GB free on {letter}:"}
    elif free < 40 and free + held >= 40:
        disk = {**base, "status": "warn",
                "value": f"{free:g} GB free on {letter}:; setup already holds {held:.0f} GB here"}
    elif free < 40:
        disk = {**base, "status": "fail", "code": "NS-E104", "value": f"{free:g} GB free on {letter}:"}
    elif free < 60:
        disk = {**base, "status": "warn", "value": f"{free:g} GB free on {letter}: (60 GB recommended)"}
    else:
        disk = {**base, "status": "ok", "value": f"{free:g} GB free on {letter}:"}
    if drive["type"] == "HDD":
        kind = {"id": "disktype", "label": "Disk type", "status": "warn", "code": "NS-W106",
                "value": f"{letter}: is a hard disk"}
    else:
        kind = {"id": "disktype", "label": "Disk type", "status": "ok", "code": "",
                "value": f"{letter}: {drive['type']}"}
    return [disk, kind]


def check_rows(
    pf: dict[str, Any], home: str | None = None, held_gb: Callable[[], float] | None = None
) -> list[dict[str, Any]]:
    """One CheckRow per line of the hand-over JSON, in screen order.

    With ``home`` the disk lines are worked out again for that install folder (drive picker, typed path).
    ``held_gb`` is given on a locked resume (see ``raw_disk_rows``).
    """
    rows: list[dict[str, Any]] = []
    source = list(pf.get("checks") or [])
    if home:
        source = [c for c in source if c.get("id") not in ("disk", "disktype")]
        source += raw_disk_rows(pf, home, held_gb)
    for c in source:
        row_id = str(c.get("id", ""))
        if row_id not in CHECK_IDS:
            continue
        status = str(c.get("status", "ok"))
        code = str(c.get("code") or "") or None
        title: str | None = None
        what: str | None = None
        if status != "ok":
            if code:
                title, what = copy_for(code, hint=str(c.get("hint") or ""))
                if code == "NS-W115" and is_blackwell(str((pf.get("gpu") or {}).get("name") or "")):
                    # s02 choose_pair hands an RTX 50-series card the CUDA 13 build, never the 12.4 one
                    what = BLACKWELL_W115_TEXT
                if row_id == "network":
                    what = NETWORK_FAIL_TEXT if status == "fail" else ROW_COPY[("network", "warn")][1]
                if row_id == "disk" and status == "fail":
                    need = MIN_FREE_GB if pf.get("dev_mode") else 60
                    what = f"Free {need} GB on this drive, or pick another drive below"
            elif (row_id, status) in ROW_COPY:
                title, what = ROW_COPY[(row_id, status)]
            else:
                title, what = str(c.get("label", row_id)), str(c.get("hint") or "Press Check again")
        rows.append({
            "id": row_id,
            "label": _short_label(row_id, str(c.get("label", row_id))),
            "status": status,
            "blocking": status == "fail",
            "value": str(c.get("value", "")),
            "code": code if status != "ok" else None,
            "title": title,
            "whatToDo": what,
            "fix": _row_fix(row_id, status, code or "") if status != "ok" else None,
        })
    order = {k: i for i, k in enumerate(CHECK_IDS)}
    rows.sort(key=lambda r: order[r["id"]])
    return rows


SHORT_LABELS = {
    "os": "Windows 10/11, 64-bit", "gpu": "Graphics card", "driver": "NVIDIA driver", "ram": "Memory",
    "disk": "Free disk space", "disktype": "Disk type", "network": "Internet",
    "winget": "Windows app installer (optional)", "vcruntime": "Visual C++ runtime",
    "longpaths": "Very long file names", "waketimers": "Wake timers",
}


def _short_label(row_id: str, fallback: str) -> str:
    return SHORT_LABELS.get(row_id, fallback)


# ---------------------------------------------------------------------------------------------------------
# machine summary
# ---------------------------------------------------------------------------------------------------------
def drives_view(pf: dict[str, Any]) -> list[dict[str, Any]]:
    """Fixed drives from stage 1 as DriveView dicts."""
    out = []
    for d in pf.get("drives") or []:
        kind = str(d.get("type", "Unknown"))
        out.append({
            "letter": str(d.get("letter", "")).upper()[:1],
            "freeGb": round(float(d.get("free_gb", 0)), 1),
            "type": kind if kind in ("SSD", "HDD") else "Unknown",
        })
    return out


def _gb(mb: float | None) -> float:
    return round((mb or 0) / 1024.0, 1)


def machine_summary(pf: dict[str, Any], home: str, tier: str) -> dict[str, Any]:
    """MachineSummary for the Hardware screen and its three meters."""
    gpu = pf.get("gpu") or {}
    drives = drives_view(pf)
    letter = home[:1].upper() if len(home) > 1 and home[1] == ":" else ""
    free = next((d["freeGb"] for d in drives if d["letter"] == letter), 0.0)
    ram = float(pf.get("ram_gb") or 0)
    vram_mb = gpu.get("vram_mb")
    caption = str(pf.get("os_caption") or "").strip()
    os_row = next((c for c in pf.get("checks") or [] if c.get("id") == "os"), {})
    default_os = str(os_row.get("value", "Windows"))
    windows = f"{caption} {pf.get('os_version', '')}".strip() if caption else default_os
    dev = bool(pf.get("dev_mode"))
    return {
        "windows": windows,
        "gpuName": gpu.get("name") or None,
        "vramMb": int(vram_mb) if vram_mb else None,
        "driver": gpu.get("driver") or None,
        "cuda": str(pf.get("cuda")) if pf.get("cuda") else None,
        "ramGb": ram,
        "profile": pf.get("profile", "gpu"),
        "isAdmin": bool(pf.get("is_admin", False)),
        "drives": drives,
        "meters": {
            "vram": {"haveGb": _gb(vram_mb), "needGb": 16, "hardMinGb": 8},
            "ram": {"haveGb": round(ram, 1), "needGb": MIN_RAM_GB if dev else 16,
                    "hardMinGb": MIN_RAM_GB if dev else 8},
            "disk": {"haveGb": free, "needGb": required_free_gb(tier, dev=dev),
                     "hardMinGb": MIN_FREE_GB if dev else 40, "path": home},
        },
    }


# ---------------------------------------------------------------------------------------------------------
# re-probe and disk check
# ---------------------------------------------------------------------------------------------------------
def reprobe_argv(app_path: Path, out: Path, home: str | None) -> list[str]:
    """Command line that runs stage 1's probe again and writes the hand-over JSON to ``out``."""
    argv = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        str(app_path / "installer" / "stage1.ps1"), "-AppPath", str(app_path), "-ProbeOnly", "-Out", str(out),
    ]
    if home:
        argv += ["-HomePath", home]
    return argv


def reprobe(
    app_path: Path, out: Path, home: str | None,
    run: Callable[[Sequence[str]], int] | None = None,
) -> dict[str, Any] | None:
    """Run the probe again (``run`` is injectable for tests). None when it failed."""
    argv = reprobe_argv(app_path, out, home)

    def default_run(a: Sequence[str]) -> int:
        return subprocess.run(list(a), capture_output=True, timeout=300, check=False).returncode

    out.unlink(missing_ok=True)
    if (run or default_run)(argv) != 0:
        return None
    return load(out)


def expected_model_files(manifest: dict[str, Any], profile: str, tier: str) -> list[str]:
    """The file names a models folder can supply."""
    pm = manifest["profiles"][profile]
    llm = pm.get({"balanced": "llm_balanced", "best": "llm_best"}.get(tier, "llm")) or pm["llm"]
    names = [Path(llm["file"]).name, Path(pm["embed"]["file"]).name]
    names += [Path(f["file"]).name for f in pm["tts"]]
    names += [*pm["whisper_repo"].get("files_sha256", {}), "config.json", "tokenizer.json"]
    yunet = (manifest.get("bins") or {}).get("yunet")
    if yunet:
        names.append(Path(yunet["url"]).name)
    return list(dict.fromkeys(names))


def scan_models_source(folder: Path, expected: Sequence[str], max_depth: int = 4) -> dict[str, int]:
    """Count the expected model files found in a folder tree and inside the *.zip files in it."""
    wanted = set(expected)
    found: dict[str, int] = {}
    if not folder.is_dir():
        return {"files": 0, "of": len(wanted), "bytes": 0}
    base = len(folder.parts)
    for current, dirs, files in os.walk(folder):
        if len(Path(current).parts) - base >= max_depth:
            dirs[:] = []
        for name in files:
            full = Path(current) / name
            if name in wanted:
                found.setdefault(name, full.stat().st_size)
            elif name.lower().endswith(".zip"):
                try:
                    with zipfile.ZipFile(full) as zf:
                        for info in zf.infolist():
                            leaf = Path(info.filename).name
                            if leaf in wanted:
                                found.setdefault(leaf, info.file_size)
                except (OSError, zipfile.BadZipFile):
                    continue
    return {"files": len(found), "of": len(wanted), "bytes": sum(found.values())}


DRIVE_REMOVABLE, DRIVE_REMOTE, DRIVE_CDROM = 2, 4, 5


def default_drive_type(letter: str) -> int:
    """Windows GetDriveTypeW of the drive (3 fixed, 2 removable, 4 network); 0 when it cannot be asked."""
    if os.name != "nt":
        return 0
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\"))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return 0


def disk_check(
    path: str,
    tier: str,
    drives: list[dict[str, Any]],
    free_fn: Callable[[str], float],
    *,
    models: dict[str, int] | None = None,
    drive_type_fn: Callable[[str], int] = default_drive_type,
    isfile_fn: Callable[[str], bool] = os.path.isfile,
    layout_fn: Callable[[str], str | None] | None = None,
    existing_fn: Callable[[str], str | None] | None = None,
    kind_fn: Callable[[str], str] | None = None,
    dev: bool = False,
) -> dict[str, Any]:
    """DiskCheck for an install folder (or, with ``models``, for a models folder).

    The install folder must be an absolute path on a local fixed drive (no network or removable drives),
    at most 120 characters, and the drive needs the plan's free space. Spaces and non-ASCII letters are fine.
    Nothing is created: setup makes the folder when Install starts.

    ``layout_fn`` says why the folder clashes with where the program was extracted or lives in Downloads or
    OneDrive; ``existing_fn`` names an install that is already in the folder (setup then updates it).
    ``kind_fn`` says what is in the folder (the setup window passes ``pathcheck.folder_kind``; without it the
    folder is not looked at): ``folder`` carries the answer, and a foreign folder (other files, not
    Nightshift) gets a ``warning`` the window shows and the person confirms (D-21). It stays ``ok``.
    """
    need = required_free_gb(tier, dev=dev)
    result: dict[str, Any] = {
        "path": path, "exists": os.path.exists(path), "drive": None, "freeGb": None, "type": "Unknown",
        "needGb": need, "ok": False, "message": None, "modelsFound": models,
        "folder": None, "warning": None,
    }
    if models is not None:
        result["ok"] = models["files"] > 0
        if result["exists"]:
            size_gb = models["bytes"] / 1024 ** 3
            result["message"] = f"Found {models['files']} of {models['of']} model files, {size_gb:.1f} GB"
        else:
            result["message"] = "That folder was not found"
        return result
    problem = home_problem(path) or (layout_fn(path) if layout_fn else None)
    if problem:
        result["message"] = problem
        return result
    path = path.strip()
    letter = path[0].upper()
    result["drive"] = letter
    if isfile_fn(path):
        result["message"] = "That is a file, not a folder"
        return result
    result["folder"] = what = kind_fn(path) if kind_fn else None
    known = next((d for d in drives if d["letter"] == letter), None)
    if known:
        result["freeGb"] = known["freeGb"]
        result["type"] = known["type"]
    else:
        if drive_type_fn(letter) in (DRIVE_REMOVABLE, DRIVE_REMOTE, DRIVE_CDROM):
            result["message"] = f"{letter}: is a removable, network or disc drive. Pick a drive in this PC"
            return result
        try:
            result["freeGb"] = round(free_fn(path), 1)
        except OSError:
            result["message"] = f"Drive {letter}: was not found"
            return result
    free = float(result["freeGb"])
    result["ok"] = free >= need
    if result["ok"]:
        kind = f" ({result['type']})" if result["type"] != "Unknown" else ""
        result["message"] = f"{free:.0f} GB free on {letter}:{kind}"
        existing = existing_fn(path) if existing_fn else None
        if existing:
            result["message"] += f". {existing}"
        if what == FOLDER_FOREIGN:
            result["warning"] = foreign_warning(path)
            result["message"] += f". {result['warning']}"
        elif what == FOLDER_KEPT:
            result["message"] += ". The data an earlier Nightshift left here is kept and used"
    else:
        result["message"] = f"Only {free:.0f} GB free on {letter}:. Nightshift needs {need} GB"
    return result


def hf_only_blocked(pf: dict[str, Any]) -> bool:
    """True when the only unreachable download site is Hugging Face (the source then defaults to a folder)."""
    down = [str(h) for h in pf.get("unreachable") or []]
    return bool(down) and all(h.endswith("huggingface.co") for h in down)


def webview2_pinned(manifest: dict[str, Any]) -> bool:
    """True when manifest.json pins a real hash for the window files."""
    return pinned(((manifest.get("bins") or {}).get("webview2") or {}).get("sha256"))
