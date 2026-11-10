"""Make or check ``uv.lock``, the locked dependency set setup and ``ns update`` install from (P29a).

    python dev/lock_deps.py            make uv.lock again from pyproject.toml (needs the internet)
    python dev/lock_deps.py --check    check the committed lock, without the network

Run it whenever ``pyproject.toml`` changes its dependencies, its extras or its ``version`` (uv writes the
project version into the lock, so every release bump needs it). ``uv lock`` reads package metadata only: no
wheel is downloaded, so this is safe on a machine that must not hold the GPU packages.

The uv it runs must be the version ``manifest.json`` pins (``bins.uv.version``): that uv reads the lock on the
target, and a lock written by another version may use a format it does not know. uv is looked for in
``--uv``, then where Nightshift keeps it (``ns_common.uv.find_uv``: the install folder named by
NIGHTSHIFT_HOME, stage 1's download, PATH). uv's cache is wherever ``UV_CACHE_DIR`` says.

Exit code: 0 done or consistent, 1 the lock is missing or stale, 2 no usable uv.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from installer.stage2.web import deplock  # noqa: E402
from ns_common.uv import find_uv  # noqa: E402


def pinned_uv_version(root: Path = ROOT) -> str:
    """The uv version ``manifest.json`` pins."""
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    return str(manifest["bins"]["uv"]["version"])


def uv_version(exe: str) -> str | None:
    """The version ``<exe> --version`` reports ("uv 0.13.6 (...)" is "0.13.6"); None when it does not run."""
    try:
        res = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    words = res.stdout.split()
    return words[1] if res.returncode == 0 and len(words) >= 2 and words[0] == "uv" else None


def pinned_uv(given: str | None = None, root: Path = ROOT) -> tuple[str | None, str]:
    """(path of a uv of the pinned version, "") or (None, why there is none)."""
    want = pinned_uv_version(root)
    home = os.environ.get("NIGHTSHIFT_HOME")
    exe = given or find_uv(Path(home) if home else root / ".no-install")
    if exe is None:
        return None, f"uv was not found; this needs uv {want} (pass --uv <path to uv.exe>)"
    have = uv_version(exe)
    if have != want:
        return (
            None,
            f"{exe} is uv {have or '(it does not run)'}; manifest.json pins {want} (pass --uv <path>)",
        )
    return exe, ""


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="check the lock without the network; write nothing"
    )
    parser.add_argument("--uv", help="path of the uv to use (it must be the pinned version)")
    args = parser.parse_args(argv)
    out = sys.stdout.write
    exe, why = pinned_uv(args.uv)
    if args.check:
        stale = deplock.problem(ROOT)
        if stale is not None:
            out(f"STALE: {stale}\nRun: {deplock.RELOCK}\n")
            return 1
        if exe is None:
            out(f"uv.lock matches pyproject.toml (checked without uv: {why})\n")
            return 0
        res = subprocess.run([exe, "lock", "--check", "--offline", "--project", str(ROOT)], check=False)
        if res.returncode != 0:
            out(f"STALE: uv does not accept uv.lock for this pyproject.toml.\nRun: {deplock.RELOCK}\n")
            return 1
        out("uv.lock matches pyproject.toml (checked by uv as well)\n")
        return 0
    if exe is None:
        out(why + "\n")
        return 2
    res = subprocess.run([exe, "lock", "--project", str(ROOT)], check=False)
    if res.returncode != 0:
        out("uv lock failed; uv.lock may be unchanged\n")
        return 1
    stale = deplock.problem(ROOT)
    if stale is not None:  # uv and the product's own check disagree: say so instead of shipping it
        out(f"uv wrote uv.lock, but the product's check refuses it: {stale}\n")
        return 1
    out("uv.lock written; commit it with pyproject.toml\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
