"""Dev install switches for the installer (docs/DEV_MODE.md). Standard library only.

`INSTALL.cmd --dev` (or the environment variable ``NIGHTSHIFT_DEV_MODE=1``) runs the real installer on a PC
with no graphics card and no room for the models: the programs that are small are downloaded for real, the
model files are replaced by stubs, the environments get no GPU or speech wheels and the engines are fakes.
This module holds the numbers and the stub files; the steps decide with ``ctx.dev_mode``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import ns_common.first_start as first_start
from ns_common.atomic import write_text_atomic
from ns_common.devmode import BANNER

__all__ = [
    "BANNER", "ENV_VAR", "HEADER", "MIN_FREE_GB", "MIN_RAM_GB", "PROFILE", "SKIPPED_BINS", "STUB_BYTES",
    "STUB_DIGEST", "STUB_SUFFIX", "env_enabled", "is_stub", "relax_checks", "remove_stubs",
    "reset_for_mode_switch", "stub_body", "stub_marker", "write_stub",
]

ENV_VAR = "NIGHTSHIFT_DEV_MODE"
PROFILE = "cpu-dev"
MIN_FREE_GB = 10                    # a dev install needs this much free space on the install drive
MIN_RAM_GB = 4                      # below this the Memory row warns
STUB_BYTES = 4096                   # size of every stub model file
STUB_SUFFIX = ".dev-stub"           # next to a stub file: the marker "this is not a model"
STUB_DIGEST = "dev-stub"            # what the download marker (<file>.ok) records instead of a SHA256
HEADER = b"NIGHTSHIFT-DEV-STUB v1\n"
#: Programs a dev install does not download (s02 says so): the CUDA runtimes, the voice engine, OpenCode.
SKIPPED_BINS = ("cudart", "cudart13", "qwentts", "opencode")


def env_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """True when ``NIGHTSHIFT_DEV_MODE`` is 1, true or yes."""
    value = (environ if environ is not None else os.environ).get(ENV_VAR, "")
    return value.strip().lower() in ("1", "true", "yes")


DEV_NOTE = "dev mode, not needed"


def relax_checks(
    pf: dict[str, Any], disk_rows: Callable[[dict[str, Any], str], list[dict[str, Any]]] | None = None
) -> dict[str, Any]:
    """The hand-over JSON of a dev install: profile ``cpu-dev``, no llama.cpp pair, and the checks that dev
    mode does not need turned into information rows.

    The graphics card and driver rows are ``ok`` with a value that says what was found and that dev mode does
    not need it; the memory row only warns under ``MIN_RAM_GB``. ``disk_rows(pf, home)`` (the setup server
    passes ``preflight.raw_disk_rows``) replaces the disk rows, which stage 1 may have judged as for a
    real install; without it they are left alone (step s01 checks the 10 GB itself).
    Idempotent, so a hand-over that stage 1 already wrote for ``-Dev`` comes out the same.
    """
    out = {**pf, "dev_mode": True, "profile": PROFILE, "llama_pair": ""}
    gpu = pf.get("gpu") or {}
    found = f"{gpu['name']}" if gpu.get("name") else "no NVIDIA graphics card"
    ram = float(pf.get("ram_gb") or 0)
    rows: list[dict[str, Any]] = []
    for c in pf.get("checks") or []:
        c = dict(c)
        if c.get("id") == "gpu":
            c.update(status="ok", code="", hint="", value=f"{found} ({DEV_NOTE})")
        elif c.get("id") == "driver":
            c.update(status="ok", code="", hint="", value=f"not checked ({DEV_NOTE})")
        elif c.get("id") == "ram":
            if ram >= MIN_RAM_GB:
                c.update(status="ok", code="", hint="", value=f"{ram:g} GB (dev mode needs {MIN_RAM_GB} GB)")
            else:
                c.update(status="warn", value=f"{ram:g} GB is below the {MIN_RAM_GB} GB dev mode needs")
        rows.append(c)
    home = str(pf.get("home") or "")
    if disk_rows is not None and home and pf.get("drives"):
        rows = [c for c in rows if c.get("id") not in ("disk", "disktype")] + disk_rows(out, home)
    out["checks"] = rows
    return out


def stub_marker(dest: Path) -> Path:
    """The marker file that sits next to a stub: ``<name>.dev-stub``."""
    return dest.with_name(dest.name + STUB_SUFFIX)


def is_stub(dest: Path) -> bool:
    """True when ``dest`` was written as a stub by a dev install."""
    return stub_marker(dest).is_file() and dest.is_file()


def stub_body(name: str) -> bytes:
    """Content of the stub called ``name``: a JSON file stays JSON, anything else starts with ``HEADER``."""
    if name.lower().endswith(".json"):
        text = json.dumps({"nightshift_dev_stub": True, "file": name}) + "\n"
        return text.encode("utf-8").ljust(STUB_BYTES, b" ")      # trailing blanks keep it valid JSON
    text = f"file: {name}\nThis is a placeholder written by INSTALL.cmd --dev. It is not a model.\n"
    return (HEADER + text.encode("utf-8")).ljust(STUB_BYTES, b"\0")


def write_stub(dest: Path) -> int:
    """Write the stub for ``dest`` with its download marker and its stub marker. Returns the size written.

    The download marker (``<file>.ok``) records ``STUB_DIGEST`` as the hash, so a real install, which checks
    the pinned SHA256, never takes a stub for a model. The format is ``downloader.write_marker``'s.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    body = stub_body(dest.name)
    dest.write_bytes(body)
    st = dest.stat()
    marker = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": STUB_DIGEST}
    dest.with_name(dest.name + ".ok").write_text(json.dumps(marker), encoding="utf-8")
    stub_marker(dest).write_text("dev mode stub\n", encoding="utf-8")
    return len(body)


def remove_stubs(root: Path) -> list[Path]:
    """Delete every stub under ``root`` (its file, ``.ok`` and ``.dev-stub``); returns the stub files removed.

    A real install calls this on ``models`` so a placeholder left by an earlier ``--dev`` run is replaced by
    the model, and ``verify --downloads`` does not meet it.
    """
    removed: list[Path] = []
    if not root.is_dir():
        return removed
    for marker in sorted(root.rglob("*" + STUB_SUFFIX)):
        target = marker.with_name(marker.name[: -len(STUB_SUFFIX)])
        for victim in (target, target.with_name(target.name + ".ok"), marker):
            victim.unlink(missing_ok=True)
        removed.append(target)
    return removed


#: Wizard flag: the name the old mode's runtime.json was set aside under at a mode switch. The tuning step
#: tells the owner on its row and clears it (``runtime_cfg.ensure_baseline``).
OTHER_FLAG = "runtime_other"


def _set_runtime_aside(path: Path) -> str | None:
    """The runtime.json of the other mode is not deleted: it is renamed to ``runtime.json.other`` (or
    ``.other.1`` ... when that name is taken, never over a file), as the tuning step keeps one it builds again
    (``runtime_cfg.OTHER_NAME``). A file that cannot be renamed (another program holds it), or a folder of
    that name, stays: setup does not stop here with a traceback. The tuning step compares the kind of
    install itself (``runtime_cfg.runtime_settled``): it builds the file again from the answers once it can
    be read, and refuses by name (NS-E125) while it cannot. Returns the name the file now has, or None."""
    if not path.is_file():
        return None
    for n in range(1000):
        target = path.with_name(path.name + ".other" + (f".{n}" if n else ""))
        if target.exists():
            continue
        try:
            os.rename(path, target)     # on Windows it refuses to replace a file that is already there
        except FileExistsError:
            continue
        except OSError:
            return None
        return target.name
    return None


def reset_for_mode_switch(home: Path, dev_mode: bool) -> bool:
    """An install that was started in the other mode (real vs ``--dev``) runs its later steps again.

    Steps after the questions go back to pending, ``wizard_flags.json`` is dropped and ``runtime.json`` is
    set aside as ``runtime.json.other`` (the new mode writes its own; the old one is kept, never deleted),
    so the programs, the environments, the tuning, the smoke test and the shortcuts are redone for the new
    mode (a finished step would otherwise be skipped). The answers and the files on disk are kept.
    Returns True when something was reset. Uninstalling first is still the clean way to switch.
    """
    state_file = home / "config" / "install_state.json"
    try:
        data = json.loads(state_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, dict) or bool(data.get("dev_mode", False)) == dev_mode:
        return False
    keep = ("b0", "s00_", "s01_")
    began = any(
        isinstance(s, dict) and s.get("status") in ("done", "skipped")
        for sid, s in steps.items() if not str(sid).startswith(keep)
    )
    if not began:
        return False
    data["steps"] = {sid: s for sid, s in steps.items() if str(sid).startswith(keep)}
    data["dev_mode"] = dev_mode
    data["completed_at"] = None
    first_start.guard(state_file)
    write_text_atomic(state_file, json.dumps(data, indent=2) + "\n", keep_backup=True)
    flags = home / "config" / "wizard_flags.json"
    flags.unlink(missing_ok=True)
    kept_as = _set_runtime_aside(home / "config" / "runtime.json")
    if kept_as is not None:
        # the only flag of the new mode so far, stamped with this install (``state.INSTALL_KEY``): the tuning
        # step reads it, names the file on its row and clears it
        note = {"_install": data.get("started_at"), OTHER_FLAG: kept_as}
        write_text_atomic(flags, json.dumps(note, indent=2, sort_keys=True) + "\n")
    return True
