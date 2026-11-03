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
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ns_common import __version__
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_common.uv import find_uv, home_uv
from ns_common.venv_extras import NO_DEPS, extras_suffix

PRESERVE = (".git", "config", "answers.json")
RENAME_TRIES = 20
RENAME_PAUSE_S = 1.0
BUSY_HINT = (
    "another program still has the Nightshift folder open (a Dashboard, Studio or terminal window?). "
    "Close those windows and run the update again; your previous version is still in place."
)
RESTORE_HINT = (
    "the previous version could not be put back because another program still has the Nightshift "
    "folder open. "
    "Close every Nightshift window, then run `ns update --rollback` (the previous version is in the folder "
    "ending in .prev)."
)
#: Written into a complete program folder (appcopy.MARKER): INSTALL.cmd trusts <home>pp only with it.
APP_MARKER = ".ns-app.done"
UPDATE_VENVS = ("studio", "supervisor", "tts")      # extras: ns_common.venv_extras (shared with setup)
#: Console scripts the app installs into every venv (`[project.scripts]`). A running copy cannot be replaced.
ENTRY_EXES = ("ns.exe", "studio.exe")
OLD_SUFFIX = ".old"
NO_GIT_HINT = (
    "Git is not installed, and `ns update --git` needs it. Install Git for this "
    "(https://git-scm.com/download/win), or update without it: `ns update --github`, or the Update "
    "shortcut with a Nightshift-Update zip."
)


@dataclass
class UpdateEnv:
    """Side effects of an update; replaced in tests."""

    stop: Callable[[], None]
    sync: Callable[[Path], None]
    verify: Callable[[Path], bool]
    reregister: Callable[[Path], list[str]]
    untracked: Callable[[Path], list[str]] = field(default=lambda app: git_untracked(app))
    #: Called before a rollback renames folders: waits until the processes `verify` started have exited.
    settle: Callable[[], object] = field(default=lambda: None)


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


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "-"


def app_fingerprint(folder: Path) -> str:
    """Version plus short hashes of manifest.json and pyproject.toml (same text as appcopy.fingerprint)."""
    try:
        version = (folder / "VERSION").read_text(encoding="utf-8-sig").strip() or "-"
    except OSError:
        version = "-"
    return f"{version}|{_digest(folder / 'manifest.json')}|{_digest(folder / 'pyproject.toml')}"


def write_app_marker(app: Path, log: NsLogger) -> None:
    """Mark `app` as a finished program folder so a later INSTALL.cmd run swaps it instead of deleting it."""
    try:
        (app / APP_MARKER).write_text(app_fingerprint(app), encoding="utf-8")
    except OSError as e:     # the update itself worked; the next INSTALL.cmd just copies the folder again
        log.warn("app_marker_failed", error=str(e))


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


def rename_with_retry(
    src: Path, dst: Path, *, tries: int | None = None, pause_s: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Rename a folder, retrying while Windows still reports it in use (WinError 32 or 5).

    Processes that were just told to stop (the engine, the dashboard service, a closing window) release the
    folder a moment later, so the first attempt can fail although nothing is wrong.
    """
    tries = RENAME_TRIES if tries is None else tries
    pause_s = RENAME_PAUSE_S if pause_s is None else pause_s
    for attempt in range(1, tries + 1):
        try:
            src.rename(dst)
            return
        except OSError:
            if attempt >= tries:
                raise
            sleep(pause_s)


def _restore(app: Path, prev: Path, log: NsLogger, settle: Callable[[], object] | None = None) -> None:
    """Put `<app>.prev` back in place of a failed new tree.

    The failure that brought us here is usually `ns verify --quick` failing: the engine it started (and its
    llama-server and TTS children) may still be exiting with the new tree open, so wait for them first and
    retry both renames like the forward swap does.
    """
    if settle is not None:
        try:
            settle()
        except Exception as e:  # waiting is best effort; the retrying renames below are the safety net
            log.warn("rollback_settle_failed", error=str(e))
    failed = _sibling(app, ".failed")
    shutil.rmtree(failed, ignore_errors=True)
    if app.exists():
        rename_with_retry(app, failed)
    rename_with_retry(prev, app)
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
        write_app_marker(new, log)
        shutil.rmtree(prev, ignore_errors=True)
        rename_with_retry(app, prev)
    except NsError:
        shutil.rmtree(new, ignore_errors=True)
        raise
    except OSError as e:
        shutil.rmtree(new, ignore_errors=True)
        raise NsError("NS-E702", f"could not stage the update: {e}. {BUSY_HINT}") from e
    try:
        rename_with_retry(new, app)
    except OSError as e:
        try:
            rename_with_retry(prev, app)
        except OSError as e2:
            raise NsError("NS-E702", f"could not swap directories: {e}; {RESTORE_HINT}") from e2
        raise NsError("NS-E702", f"could not swap directories: {e}") from e
    try:
        env.sync(app)
        if not env.verify(app):
            raise NsError("NS-E702", "ns verify --quick failed on the new version")
        notes += env.reregister(app)
    except Exception as e:  # any step failing after the swap rolls back
        log.error("update_failed", str(e))
        detail = e.detail if isinstance(e, NsError) else f"{type(e).__name__}: {e}"
        try:
            _restore(app, prev, log, env.settle)
        except OSError as e3:   # keep the cause of the failure: it is what the owner has to report
            raise NsError("NS-E702", f"{detail}. {RESTORE_HINT} ({e3})") from e
        try:
            env.sync(app)
        except Exception as e2:  # best effort: report, the old tree is already back
            log.error("rollback_sync_failed", str(e2))
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
        rename_with_retry(app, tmp)
        rename_with_retry(prev, app)
        rename_with_retry(tmp, prev)
    except OSError as e:
        raise NsError("NS-E702", f"could not swap directories: {e}. {BUSY_HINT}") from e
    write_app_marker(app, log)
    env.sync(app)
    log.info("rollback_done", old=old_version, restored=current_version(app))
    return UpdateResult(old_version, current_version(app), ["rolled back"])


def free_entry_points(venv: Path) -> list[tuple[Path, Path]]:
    """Move `ns.exe` and `studio.exe` of `venv` out of the way so uv can write fresh ones.

    Windows refuses to delete or overwrite a running program, but it does allow renaming one. A terminal that
    is following `ns update` (or any Studio window) still runs the old `Scripts\\ns.exe`; without this, uv
    fails with "Access is denied" while replacing it. Returns (original, moved-to) pairs.
    """
    scripts = venv / "Scripts"
    moved: list[tuple[Path, Path]] = []
    for name in ENTRY_EXES:
        exe = scripts / name
        if not exe.is_file():
            continue
        away = scripts / (name + OLD_SUFFIX)
        try:
            away.unlink(missing_ok=True)          # left by the last update; fails while that one still runs
        except OSError:
            away = scripts / f"{name}{OLD_SUFFIX}-{os.getpid()}"
        try:
            os.replace(exe, away)
        except OSError:
            continue                              # not renamable either: uv will report it
        moved.append((exe, away))
    return moved


def restore_entry_points(moved: list[tuple[Path, Path]]) -> None:
    """Put moved launchers back where uv did not write a new one (the install failed half way)."""
    for exe, away in moved:
        if not exe.exists() and away.exists():
            try:
                os.replace(away, exe)
            except OSError:
                pass


def discard_entry_points(moved: list[tuple[Path, Path]]) -> None:
    """Delete the moved launchers; one that is still running stays until the next update."""
    for _exe, away in moved:
        try:
            away.unlink(missing_ok=True)
        except OSError:
            pass


def finish_entry_points(moved: list[tuple[Path, Path]]) -> None:
    """After a successful install: a launcher uv did not write again (nothing to reinstall) comes back, the
    others lose their moved-aside copy."""
    restore_entry_points(moved)
    discard_entry_points(moved)


def resync_venvs(
    app: Path,
    paths: Paths,
    *,
    uv: str | None = None,
    run: Callable[[list[str]], tuple[int, str]] | None = None,
    free: Callable[[Path], list[tuple[Path, Path]]] | None = None,
) -> list[str]:
    """`uv pip install -e <app>[extras]` in every venv that exists; raises `NS-E702` on failure."""
    runner = run or _run
    free_up = free or free_entry_points
    exe = uv if uv is not None else find_uv(paths)
    done: list[str] = []
    for venv in UPDATE_VENVS:
        extras, no_deps = extras_suffix(venv), venv in NO_DEPS
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
        moved = free_up(paths.venvs / venv)
        rc, tail = runner(cmd)
        if rc != 0:
            restore_entry_points(moved)
            raise NsError("NS-E702", f"uv pip install failed for venv {venv}: {tail}")
        finish_entry_points(moved)
        done.append(venv)
    return done


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return r.returncode, " | ".join((r.stderr or r.stdout).strip().splitlines()[-3:])


def verify_quick(app: Path, python: str | None = None) -> bool:
    """`ns verify --quick` with the new code (a fresh interpreter in the app directory).

    `python` is the supervisor venv's interpreter, which the re-sync just brought up to date. The default,
    this process's own interpreter, is wrong in the update worker: that runs from a copy of the venv made
    before the re-sync and would not see a dependency the new version added.
    """
    r = subprocess.run(
        [python or sys.executable, "-m", "ns_supervisor.cli", "verify", "--quick"], cwd=str(app), check=False
    )
    return r.returncode == 0


# --------------------------------------------------------------------------------------------------------
# a cloned-repo install: `ns update --git [--channel stable|main]`
# --------------------------------------------------------------------------------------------------------
CHANNELS = ("stable", "main")
GitRun = Callable[[list[str]], tuple[int, str]]


def _git_run(app: Path) -> GitRun:
    exe = shutil.which("git")

    def run(args: list[str]) -> tuple[int, str]:
        if exe is None:
            return 127, "git was not found on PATH"
        try:
            r = subprocess.run(
                [exe, "-C", str(app), *args], capture_output=True, text=True, timeout=900, check=False
            )
        except (OSError, subprocess.SubprocessError) as e:
            return 1, str(e)
        return r.returncode, (r.stdout.strip() or r.stderr.strip())

    return run


def git_update(
    app: Path,
    env: UpdateEnv,
    *,
    paths: Paths,
    channel: str = "stable",
    latest_tag: Callable[[], str] | None = None,
    git: GitRun | None = None,
) -> UpdateResult:
    """Update a cloned-repo install: fetch, move to the newest release tag (`stable`) or `origin/main`
    (`main`), re-sync the venvs, verify, and go back to the previous commit if any step fails (`NS-E702`).

    Refuses when the working tree has local changes to tracked files (nothing is touched then).
    """
    if channel not in CHANNELS:
        raise NsError("NS-E702", f"unknown channel {channel!r}; choose stable or main")
    app = app.absolute()
    if not (app / ".git").exists():
        raise NsError("NS-E702", f"{app} is not a git clone; use --github or an update zip instead")
    if git is None and shutil.which("git") is None:
        raise NsError("NS-E702", NO_GIT_HINT)
    run = git or _git_run(app)
    log = NsLogger("update", paths.logs)
    old_version = current_version(app)
    code, head = run(["rev-parse", "HEAD"])
    if code != 0:
        raise NsError("NS-E702", f"git rev-parse failed: {head}")
    code, dirty = run(["status", "--porcelain", "--untracked-files=no"])
    if code != 0 or dirty:
        raise NsError("NS-E702", "this copy has local changes to tracked files; commit or discard them first")
    code, out = run(["fetch", "--tags", "--prune", "origin"])
    if code != 0:
        raise NsError("NS-E105", f"git fetch failed: {out}")
    if channel == "stable":
        tag = (latest_tag or (lambda: _latest_release_tag()))()
        target = ["checkout", "--detach", tag]
    else:
        target = ["checkout", "--detach", "origin/main"]
    leave_app(app, paths)
    log.info("git_update_start", channel=channel, old=old_version, head=head[:12])
    env.stop()
    code, out = run(target)
    if code != 0:
        raise NsError("NS-E702", f"git {' '.join(target)} failed: {out}")
    try:
        env.sync(app)
        if not env.verify(app):
            raise NsError("NS-E702", "ns verify --quick failed on the new version")
        notes = env.reregister(app)
    except Exception as e:  # any step failing after the move goes back to the previous commit
        log.error("git_update_failed", str(e))
        run(["checkout", "--detach", head])
        try:
            env.sync(app)
        except Exception as e2:  # best effort: the old tree is back
            log.error("rollback_sync_failed", str(e2))
        detail = e.detail if isinstance(e, NsError) else f"{type(e).__name__}: {e}"
        raise NsError("NS-E702", detail) from e
    new_version = current_version(app)
    log.info("git_update_done", old=old_version, new=new_version)
    return UpdateResult(old_version, new_version, [f"channel {channel}", *notes])


def _latest_release_tag() -> str:
    from ns_supervisor import releases

    return releases.fetch_latest().tag
