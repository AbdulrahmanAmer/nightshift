"""Machine facts for the wizard (component C1): preflight summary, drives, default home, CUDA pair."""

from __future__ import annotations

import json
import shutil
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BASE_FREE_GB = 60
TIER_EXTRA_GB = {"small": 0, "balanced": 14, "best": 21}


@dataclass(frozen=True)
class Drive:
    """A fixed drive with free space and media type (SSD, HDD or Unknown)."""

    letter: str
    free_gb: float
    type: str = "Unknown"


def required_free_gb(tier: str) -> int:
    """Free space the install needs on the home drive (spec 5: 60 GB check, plus the tier's larger model)."""
    return BASE_FREE_GB + TIER_EXTRA_GB.get(tier, 0)


def load_preflight(path: Path) -> dict[str, Any] | None:
    """Read the JSON that stage1.ps1 writes; None when absent or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def sample_preflight(home: Path, app_path: Path, *, cuda: str = "13.0") -> dict[str, Any]:
    """A green, GPU-like summary used by --simulate-downloads when stage1 did not run."""
    pair = llama_pair_for(cuda)
    letter = (home.drive[:1] or "C").upper()
    checks = [
        {"id": i, "label": label, "status": "ok", "code": "", "value": value, "hint": ""}
        for i, label, value in [
            ("os", "Windows 10/11, 64-bit", "simulated"),
            ("gpu", "NVIDIA RTX GPU, 16 GB", "simulated RTX, 16311 MB"),
            ("driver", "NVIDIA driver, CUDA 12.8 or newer", f"CUDA {cuda}"),
            ("ram", "Memory, 16 GB minimum", "32 GB"),
            ("disk", "Free disk space, 60 GB", "500 GB"),
            ("disktype", "Disk type, SSD", "SSD"),
            ("network", "Internet", "all reachable"),
            ("winget", "winget (App Installer)", "available"),
            ("longpaths", "Windows long paths enabled", "yes"),
            ("waketimers", "Wake timers allowed", "enabled"),
        ]
    ]
    return {
        "schema": 1,
        "simulated": True,
        "app_path": str(app_path),
        "home": str(home),
        "profile": "gpu",
        "llama_pair": pair or "",
        "cuda": cuda,
        "ram_gb": 32.0,
        "gpu": {"name": "Simulated RTX 5060 Ti", "vram_mb": 16311, "driver": "572.16"},
        "drives": [{"letter": letter, "free_gb": 500.0, "type": "SSD"}],
        "winget": True,
        "checks": checks,
    }


def llama_pair_for(cuda: str | None) -> str | None:
    """cuda13 for CUDA >= 13.0, cuda12 for 12.4 to 12.x, None below 12.4 (driver too old)."""
    if not cuda:
        return None
    try:
        major, minor = (int(p) for p in cuda.split(".")[:2])
    except ValueError:
        return None
    if major >= 13:
        return "cuda13"
    if (major, minor) >= (12, 4):
        return "cuda12"
    return None


def drives_from_preflight(pf: dict[str, Any] | None) -> list[Drive]:
    """Drives listed in the stage1 summary."""
    out: list[Drive] = []
    for d in (pf or {}).get("drives", []) or []:
        out.append(Drive(str(d["letter"]).upper(), float(d["free_gb"]), str(d.get("type", "Unknown"))))
    return out


def local_drives() -> list[Drive]:
    """Fixed drives seen from Python (no media type). Used only when stage1 left no summary."""
    out: list[Drive] = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        try:
            if Path(root).exists():
                out.append(Drive(letter, shutil.disk_usage(root).free / 1024**3))
        except OSError:
            continue
    return out


def choose_default_home(drives: list[Drive], min_free_gb: float = BASE_FREE_GB) -> Path | None:
    """<drive with most free space, SSD preferred>\\Nightshift; drives under ``min_free_gb`` come last."""
    if not drives:
        return None
    pool = [d for d in drives if d.free_gb >= min_free_gb] or list(drives)
    ssd = [d for d in pool if d.type == "SSD"]
    best = max(ssd or pool, key=lambda d: d.free_gb)
    return Path(f"{best.letter}:\\Nightshift")


def free_gb(path: Path) -> float:
    """Free space in GB on the drive holding ``path`` (nearest existing parent)."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free / 1024**3


def drive_type(pf: dict[str, Any] | None, path: Path) -> str:
    """Media type of ``path``'s drive from the stage1 summary (Unknown when not listed)."""
    letter = (Path(path).drive[:1] or "").upper()
    for d in drives_from_preflight(pf):
        if d.letter == letter:
            return d.type
    return "Unknown"
