"""C2 `ns update <zip> | --rollback` (spec J8, ENGINEERING 10): verified, swappable, reversible.

Order: verify VERSION + `.sha256` sidecar (`NS-E701`, nothing touched) -> stop the engine -> extract to
`<app>.new` -> carry over `.git`, `config`, `answers.json` and untracked data -> swap with `<app>.prev` ->
`uv pip install -e` per venv -> `ns verify --quick` -> re-register the scheduled task. Any failure after
the swap restores `<app>.prev` and raises `NS-E702`.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ns_common import __version__
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_common.uv import find_uv, home_uv

PRESERVE = (".git", "config", "answers.json")
VENV_EXTRAS = (("studio", "[studio]", False), ("supervisor", "[supervisor]", False), ("tts", "[tts]", True))


@dataclass
class UpdateEnv:
    """Side effects of an update; replaced in tests."""

    stop: Callable[[], None]
    sync: Callable[[Path], None]
    verify: Callable[[Path], bool]
    reregister: Callable[[Path], list[str]]
    untracked: Callable[[Path], list[str]] = field(default=lambda app: git_untracked(app))


@dataclass
class UpdateResult:
    """What changed."""

    old_version: str
    new_version: str
    notes: list[str] = field(default_factory=list)


def sha256_file(path: Path) -> str:
    """Hex SHA256 of a file."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_sidecar(zip_path: Path) -> str | None:
    """The expected digest from `<zip>.sha256` (`<hex>  <name>` or just `<hex>`), or None."""
    side = zip_path.with_name(zip_path.name + ".sha256")
    try:
        token = side.read_text(encoding="utf-8").split()[0].lower()
    except (OSError, IndexError):
        return None
    return token if len(token) == 64 and all(c in "0123456789abcdef" for c in token) else None


def verify_package(zip_path: Path) -> tuple[str, str]:
    """Check sidecar digest and VERSION; returns (version, member prefix). Raises `NS-E701`."""
    if not zip_path.is_file():
        raise NsError("NS-E701", f"{zip_path} does not exist")
    want = read_sidecar(zip_path)
    if want is None:
        raise NsError("NS-E701", f"missing or unreadable {zip_path.name}.sha256")
    got = sha256_file(zip_path)
    if got != want:
        raise NsError("NS-E701", f"SHA256 mismatch for {zip_path.name}: expected {want[:12]}, got {got[:12]}")
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            prefix = ""
            if "VERSION" not in names:
                tops = {n.split("/", 1)[0] for n in names if "/" in n}
                if len(tops) == 1 and f"{next(iter(tops))}/VERSION" in names:
                    prefix = next(iter(tops)) + "/"
                else:
                    raise NsError("NS-E701", "the zip has no VERSION file at its root")
            version = z.read(prefix + "VERSION").decode("utf-8").strip()
    except zipfile.BadZipFile as e:
        raise NsError("NS-E701", f"{zip_path.name} is not a valid zip: {e}") from e
    if not version:
        raise NsError("NS-E701", "VERSION is empty")
    return version, prefix


def safe_extract(zip_path: Path, dest: Path, prefix: str) -> None:
    """Extract members under `prefix` into `dest`, refusing anything that would land outside it."""
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename
            if not name.startswith(prefix) or name == prefix:
                continue
            rel = PurePosixPath(name[len(prefix) :])
            if rel.is_absolute() or ".." in rel.parts or (rel.parts and ":" in rel.parts[0]):
                raise NsError("NS-E701", f"unsafe path in zip: {name}")
            target = (dest / Path(*rel.parts)).resolve()
            if root != target and root not in target.parents:
                raise NsError("NS-E701", f"unsafe path in zip: {name}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)


def git_untracked(app: Path) -> list[str]:
    """Top-level entries git does not track (ignored included); empty when git or the repo is absent."""
    git = shutil.which("git")
    if git is None or not (app / ".git").exists():
        return []
    try:
        r = subprocess.run(
            [git, "-C", str(app), "ls-files", "--others", "--directory", "--no-empty-directory"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    tops = {line.strip().strip("/").split("/")[0] for line in r.stdout.splitlines() if line.strip()}
    return sorted(t for t in tops if t and t != "__pycache__")


def carry_over(old: Path, new: Path, extra: list[str]) -> list[str]:
    """Copy `.git`, `config`, `answers.json` and untracked data from the old tree into the new one."""
    carried: list[str] = []
    for name in dict.fromkeys([*PRESERVE, *extra]):
        src = old / name
        dst = new / name
        if not src.exists() or dst.exists():
            continue
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        carried.append(name)
    return carried


def _sibling(app: Path, suffix: str) -> Path:
    return app.with_name(app.name + suffix)


def current_version(app: Path) -> str:
    """VERSION file of the app tree, else the package version."""
    try:
        return (app / "VERSION").read_text(encoding="utf-8").strip() or __version__
    except OSError:
        return __version__


def leave_app(app: Path, paths: Paths) -> None:
    """Move this process out of `app` (to the install home, else the temp dir) before it is renamed.

    Windows refuses to rename a directory that is any process's working directory (WinError 32); the
    shortcuts start `ns.exe` with `<app>` as cwd, so without this every update fails with `NS-E702`.
    """
    try:
        here = Path.cwd().resolve()
    except OSError:
        here = None
    root = app.resolve()
    if here is not None and here != root and root not in here.parents:
        return
    target = paths.home if paths.home.is_dir() else Path(tempfile.gettempdir())
    os.chdir(target)


def _restore(app: Path, prev: Path, log: NsLogger) -> None:
    """Put `<app>.prev` back in place of a failed new tree."""
    failed = _sibling(app, ".failed")
    shutil.rmtree(failed, ignore_errors=True)
    if app.exists():
        app.rename(failed)
    prev.rename(app)
    shutil.rmtree(failed, ignore_errors=True)
    log.warn("update_restored", app=str(app))


def update_app(zip_path: Path, app: Path, env: UpdateEnv, *, paths: Paths) -> UpdateResult:
    """Apply a release zip to `app`. `NS-E701` before any change, `NS-E702` (rolled back) after."""
    zip_path, app = zip_path.resolve(), app.absolute()
    log = NsLogger("update", paths.logs)
    leave_app(app, paths)
    new_version, prefix = verify_package(zip_path)
    old_version = current_version(app)
    new, prev = _sibling(app, ".new"), _sibling(app, ".prev")
    log.info("update_start", old=old_version, new=new_version, zip=str(zip_path))
    env.stop()
    try:
        shutil.rmtree(new, ignore_errors=True)
        new.mkdir(parents=True)
        safe_extract(zip_path, new, prefix)
        notes = [f"kept {n}" for n in carry_over(app, new, env.untracked(app))]
        shutil.rmtree(prev, ignore_errors=True)
        app.rename(prev)
    except NsError:
        shutil.rmtree(new, ignore_errors=True)
        raise
    except OSError as e:
        shutil.rmtree(new, ignore_errors=True)
        raise NsError("NS-E702", f"could not stage the update: {e}") from e
    try:
        new.rename(app)
    except OSError as e:
        prev.rename(app)
        raise NsError("NS-E702", f"could not swap directories: {e}") from e
    try:
        env.sync(app)
        if not env.verify(app):
            raise NsError("NS-E702", "ns verify --quick failed on the new version")
        notes += env.reregister(app)
    except Exception as e:  # any step failing after the swap rolls back
        log.error("update_failed", str(e))
        _restore(app, prev, log)
        try:
            env.sync(app)
        except Exception as e2:  # best effort: report, the old tree is already back
            log.error("rollback_sync_failed", str(e2))
        detail = e.detail if isinstance(e, NsError) else f"{type(e).__name__}: {e}"
        raise NsError("NS-E702", detail) from e
    log.info("update_done", old=old_version, new=new_version)
    return UpdateResult(old_version, new_version, notes)


def rollback_app(app: Path, env: UpdateEnv, *, paths: Paths) -> UpdateResult:
    """Swap `<app>` and `<app>.prev` (so a second rollback rolls forward) and re-sync the venvs."""
    app = app.absolute()
    leave_app(app, paths)
    log = NsLogger("update", paths.logs)
    prev = _sibling(app, ".prev")
    if not prev.is_dir():
        raise NsError("NS-E702", f"no previous version at {prev}")
    old_version = current_version(app)
    env.stop()
    tmp = _sibling(app, ".swap")
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        app.rename(tmp)
        prev.rename(app)
        tmp.rename(prev)
    except OSError as e:
        raise NsError("NS-E702", f"could not swap directories: {e}") from e
    env.sync(app)
    log.info("rollback_done", old=old_version, restored=current_version(app))
    return UpdateResult(old_version, current_version(app), ["rolled back"])


def resync_venvs(
    app: Path,
    paths: Paths,
    *,
    uv: str | None = None,
    run: Callable[[list[str]], tuple[int, str]] | None = None,
) -> list[str]:
    """`uv pip install -e <app>[extras]` in every venv that exists; raises `NS-E702` on failure."""
    runner = run or _run
    exe = uv if uv is not None else find_uv(paths)
    done: list[str] = []
    for venv, extras, no_deps in VENV_EXTRAS:
        py = paths.venvs / venv / "Scripts" / "python.exe"
        if not py.exists():
            continue
        if exe is None:
            raise NsError(
                "NS-E702", f"uv was not found at {home_uv(paths.home)} or on PATH; cannot re-sync the venvs"
            )
        cmd = [exe, "pip", "install", "-e", f"{app}{extras}", "--python", str(py)]
        if no_deps:
            cmd.append("--no-deps")
        rc, tail = runner(cmd)
        if rc != 0:
            raise NsError("NS-E702", f"uv pip install failed for venv {venv}: {tail}")
        done.append(venv)
    return done


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return r.returncode, " | ".join((r.stderr or r.stdout).strip().splitlines()[-3:])


def verify_quick(app: Path) -> bool:
    """`ns verify --quick` with the new code (a fresh interpreter in the app directory)."""
    r = subprocess.run(
        [sys.executable, "-m", "ns_supervisor.cli", "verify", "--quick"], cwd=str(app), check=False
    )
    return r.returncode == 0
