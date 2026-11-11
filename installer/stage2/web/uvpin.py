"""The uv a program pins, put in place before that program's lock is read (item uv-pin-on-update, F-E28).

``<home>\\bin\\uv`` holds the uv of the program in ``<app>`` and ``<home>\\bin\\uv.prev`` the uv of the
program in ``<app>.prev``, mirroring the program folders. An update or a rollback calls ``prepare`` before it
changes anything (U1 read the pin, U2 compare, U3 get the zip, U4 unpack to ``bin\\uv.next`` and run it),
``Plan.swap`` right before the repair record and the program swap (U5), and ``Plan.undo`` when the program
did not get to ``<app>`` or was put back (U6, U7).

Invariant: every uv command that fills a venv of this install runs with the uv version the program it fills
pins (or, for a program that pins none, the install's uv); an update that cannot have that uv changes nothing.

Folders and what a kill leaves (``settle`` runs first in every ``prepare``):

- ``bin\\uv.next``: a uv that was unpacked and not yet swapped in. Never read by anything; removed.
- ``bin\\uv`` missing and ``bin\\uv.prev`` there: a swap was cut between its two renames. ``bin\\uv.prev``
  goes back to ``bin\\uv`` (the program swap comes after the uv swap, so the older uv is the right one, and
  when it is not, the compare that follows sees it).
- ``bin\\uv.swap``: an exchange with ``bin\\uv.prev`` was cut. It becomes ``bin\\uv.prev``.

Standard library only at import (setup's own server imports this package without httpx); the download
imports httpx and ``ns_supervisor.releases`` when it is called, which only the updater does.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from installer.stage2.web import filesdir

MANIFEST = "manifest.json"
EXE = "uv.exe"
UV_VERSION = re.compile(r"(?:^|\s)uv\s+(\d+(?:\.\d+)+\S*)")  # "uv 0.13.6 (abc 2025-11-01)"
VERSION_TIMEOUT_S = 60
REPAIR = "Run INSTALL.cmd again and choose Repair to put the pinned uv back."

Rename = Callable[[Path, Path], None]
Fetch = Callable[[str, Path], None]


class NotHad(Exception):
    """The pinned uv could not be had; ``str()`` is the owner's sentence (the detail of NS-E708)."""


class Held(Exception):
    """The install's uv could not be looked at or replaced because another program holds it or it is running:
    never taken for a damaged uv (nothing is fetched or replaced for it). ``str()`` is the owner's text."""


def held(exe: Path) -> str | None:
    """Why ``exe`` cannot be opened for reading (another program holds it); None when it can be."""
    try:
        with exe.open("rb") as fh:
            fh.read(2)
    except OSError as e:
        return f"{type(e).__name__}: {e}"
    return None


def uv_dir(home: Path, suffix: str = "") -> Path:
    """``<home>\\bin\\uv`` (suffix ``.prev``, ``.next`` or ``.swap`` for its siblings)."""
    return home / "bin" / f"uv{suffix}"


def zip_name(version: str) -> str:
    """The name the install's cache keeps a uv zip under: ``uv-<version>.zip``."""
    return f"uv-{version}.zip"


def _pin(data: object) -> dict[str, str]:
    bins = data.get("bins") if isinstance(data, dict) else None
    uv = bins.get("uv") if isinstance(bins, dict) else None
    if not isinstance(uv, dict):
        return {}
    return {k: uv[k] for k in ("version", "url", "sha256") if isinstance(uv.get(k), str) and uv[k]}


def pin_of(program: Path) -> dict[str, str]:
    """``bins.uv`` of the ``manifest.json`` in a program folder; empty when there is none or it is unreadable.

    A program whose manifest cannot be read is treated as one that pins no uv: the install's uv is kept."""
    try:
        return _pin(json.loads((program / MANIFEST).read_text(encoding="utf-8-sig")))
    except (OSError, ValueError):
        return {}


def pin_in_zip(zip_path: Path, prefix: str = "") -> dict[str, str]:
    """``bins.uv`` of the ``manifest.json`` inside an update zip, read without unpacking the zip."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            return _pin(json.loads(z.read(prefix + MANIFEST).decode("utf-8-sig")))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return {}


def run_version(exe: Path) -> tuple[int, str]:
    """``<exe> --version``: its exit code and what it printed (bounded; a uv that hangs counts as failed)."""
    try:
        res = subprocess.run(
            [str(exe), "--version"], capture_output=True, text=True, timeout=VERSION_TIMEOUT_S, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return res.returncode, res.stdout or ""


def report(exe: Path, run: Callable[[Path], tuple[int, str]] | None = None) -> tuple[bool, str | None]:
    """Whether the uv at ``exe`` runs, and the version it prints (None when it prints none).

    The one reader of ``uv --version`` for setup (B02, which passes its own runner) and the updater."""
    if not exe.is_file():
        return False, None
    code, out = (run or run_version)(exe)
    if code != 0:
        return False, None
    found = UV_VERSION.search(out or "")
    return True, found.group(1) if found else None


def is_pinned(runs: bool, version: str | None, want: str) -> bool:
    """The one decision of setup and the updater: this uv runs and is the version the manifest pins.

    A pin without a version leaves nothing to compare with, so a uv that runs is accepted."""
    return runs and (not want or version == want)


def mismatch(runs: bool, version: str | None, want: str | None) -> str:
    """Why a uv is not accepted: it does not run, or it reports another version."""
    if not runs:
        return "does not run from the install folder"
    return f"reports {version or 'no version'}, not the pinned {want}"


def doctor_line(home: Path, program: Path) -> tuple[bool, str]:
    """The ``uv`` row of ``ns doctor``: (ok, sentence) for the install's uv against the program's pin.

    A home with no ``bin\\uv\\uv.exe`` at all (a development checkout, a test home) is not a difference."""
    want = pin_of(program).get("version", "")
    exe = uv_dir(home) / EXE
    if not exe.is_file():
        return True, f"none at {exe} (this program pins {want or 'no version'})"
    runs, have = report(exe)
    if not runs:
        return False, f"{exe} does not run. {REPAIR}"
    if want and have != want:
        return False, f"uv {have or 'of no version'}, but this program pins {want}. {REPAIR}"
    return True, f"uv {have or 'of no version'}" + (", the version this program pins" if want else "")


def _sha256(path: Path) -> str:
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass        # never read under this name without its SHA256 being checked again


def download(url: str, dest: Path) -> None:
    """The pin's URL into ``dest`` (trusted host only: GitHub, or the test source in force)."""
    import httpx

    from ns_supervisor import releases

    if not releases.trusted(url):
        raise OSError(f"{urlsplit(url).netloc or url} is not an address Nightshift downloads from")
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60, read=300)) as http:
        with http.stream("GET", url) as resp:
            resp.raise_for_status()
            with dest.open("wb") as out:
                for block in resp.iter_bytes(1 << 20):
                    out.write(block)


def obtain(
    pin: Mapping[str, str], home: Path, files_root: Path | None, say: Callable[[str], None],
    fetch: Fetch | None = None,
) -> tuple[Path | None, str]:
    """U3: the pinned uv zip, SHA256 checked, at ``<home>\\cache\\uv-<version>.zip`` (kept for a rollback).

    Sources in order: a ``files`` folder beside the update zip, the cache, the pin's URL. A source whose bytes
    do not match the pin is skipped with one line and the next is tried. ``(None, why)`` when none has it.
    """
    version, url, sha = pin.get("version", ""), pin.get("url", ""), pin.get("sha256", "")
    cached = home / "cache" / zip_name(version)
    if not filesdir.is_pin(sha):
        return None, "this release names no SHA256 for it, so no copy of it can be checked"
    names = [filesdir.url_leaf(url), cached.name] if url else [cached.name]
    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return None, f"the folder {cached.parent} could not be made ({e})"
    if filesdir.take(files_root, names, sha, cached, say, then="trying the next source") is not None:
        return cached, ""
    if cached.is_file():
        try:
            if _sha256(cached) == sha.lower():
                return cached, ""
            say(f"cache\\{cached.name} does not match its pin; trying the next source")
        except OSError as e:
            say(f"cache\\{cached.name} could not be read ({e}); trying the next source")
        _unlink(cached)
    # the folders are named by their paths: the Dashboard's "From a file" and `--github` keep the update zip
    # in <home>\data\updates, so "next to the zip you picked" would name a folder this route never sees
    where = f"no checked copy of it is in {cached.parent} and "
    if files_root is not None:
        where = f"no checked copy of it is in {files_root} or in {cached.parent} and "
    if not url:
        return None, f"{where}this release names no address for it"
    part = cached.with_name(cached.name + ".part")
    try:
        (fetch or download)(url, part)
        if _sha256(part) != sha.lower():
            say(f"the download of {names[0]} does not match its pin")
            return None, f"{where}the file GitHub sent does not match its SHA256"
        os.replace(part, cached)
        return cached, ""
    except Exception as e:  # no network, a refused host, a cut connection, a full disk: one reason, said
        return None, f"{where}GitHub could not be reached ({type(e).__name__}: {e})"
    finally:
        _unlink(part)


def extract(zip_path: Path, dest: Path) -> None:
    """Every ``uv*.exe`` of the zip into the fresh folder ``dest``."""
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            leaf = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
            if info.is_dir() or not (leaf.lower().startswith("uv") and leaf.lower().endswith(".exe")):
                continue
            with z.open(info) as src, (dest / leaf).open("wb") as out:
                shutil.copyfileobj(src, out)


def settle(home: Path) -> None:
    """Finish what a run killed inside a swap left (the module text lists the three states). Never raises:
    what could not be moved is seen by the compare that follows."""
    uv, prev, nxt, tmp = (uv_dir(home, s) for s in ("", ".prev", ".next", ".swap"))
    try:
        if not (uv / EXE).is_file() and (prev / EXE).is_file():
            shutil.rmtree(uv, ignore_errors=True)
            os.rename(prev, uv)
        if tmp.exists():
            if prev.exists():
                shutil.rmtree(tmp, ignore_errors=True)
            else:
                os.rename(tmp, prev)
    except OSError:
        pass
    shutil.rmtree(nxt, ignore_errors=True)


@dataclass
class Plan:
    """What ``prepare`` found: nothing to do (``how == ""``), a checked uv in ``bin\\uv.next`` (``"next"``),
    or the wanted version already in ``bin\\uv.prev`` (``"prev"``: the two are exchanged, so a second rollback
    rolls forward)."""

    home: Path
    want: str = ""
    was: str | None = None
    how: str = ""
    swapped: bool = False
    #: A rollback's sentence when the older program's uv could not be had and the current one stays.
    stays: str = ""

    def swap(self, rename: Rename = os.rename) -> str:
        """U5. ``OSError`` when a rename stays refused (a uv still running): ``bin\\uv`` is as it was then,
        except in the one case ``undo`` and ``settle`` finish. The line for the log when it swapped."""
        uv, prev, nxt, tmp = (uv_dir(self.home, s) for s in ("", ".prev", ".next", ".swap"))
        if self.how == "next":
            shutil.rmtree(prev, ignore_errors=True)
            if uv.exists():
                rename(uv, prev)
            try:
                rename(nxt, uv)
            except OSError:
                if prev.exists() and not uv.exists():
                    os.rename(prev, uv)
                raise
        elif self.how == "prev":
            shutil.rmtree(tmp, ignore_errors=True)
            rename(uv, tmp)
            try:
                rename(prev, uv)
            except OSError:
                os.rename(tmp, uv)
                raise
            self.swapped = True         # (the last rename is finished by `undo` or the next `settle`)
            rename(tmp, prev)
        else:
            return ""
        self.swapped = True
        return f"uv {self.want} put in place (was {self.was or 'none'})"

    def undo(self, rename: Rename = os.rename) -> str:
        """U6, U7: the uv that was there before, back in ``bin\\uv``. Never raises; the sentence says what is
        there now ("" when nothing had been swapped)."""
        uv, prev, nxt, tmp = (uv_dir(self.home, s) for s in ("", ".prev", ".next", ".swap"))
        if not self.swapped:
            shutil.rmtree(nxt, ignore_errors=True)
            return ""
        try:
            if tmp.exists() and not prev.exists():
                os.rename(tmp, prev)
            if self.how == "next":
                shutil.rmtree(nxt, ignore_errors=True)
                rename(uv, nxt)
                if prev.exists():
                    rename(prev, uv)
                shutil.rmtree(nxt, ignore_errors=True)
            else:
                shutil.rmtree(tmp, ignore_errors=True)
                rename(uv, tmp)
                rename(prev, uv)
                rename(tmp, prev)
        except OSError as e:
            settle(self.home)
            runs, have = report(uv / EXE)
            if not (runs and have == self.was):
                return f" uv could not be put back to {self.was or 'what it was'} ({e}). {REPAIR}"
        self.swapped = False
        return f" uv is back at {self.was}." if self.was else ""


def prepare(
    pin: Mapping[str, str], home: Path, *, files_root: Path | None = None,
    say: Callable[[str], None] = lambda line: None, fetch: Fetch | None = None,
) -> Plan:
    """U1 to U4, before anything of the install is changed. ``NotHad`` when the pinned uv cannot be had or
    does not run; ``bin\\uv`` and ``bin\\uv.prev`` are untouched then and ``bin\\uv.next`` is removed."""
    plan = Plan(home)
    settle(home)        # U0: a bin/uv.next or a cut swap that a killed run left goes first, pin or no pin
    want = pin.get("version", "")
    if not want:
        say("this program pins no uv version: the install's uv is kept")
        return plan
    uv, prev, nxt = (uv_dir(home, s) for s in ("", ".prev", ".next"))
    runs, have = report(uv / EXE)
    plan.want, plan.was = want, have
    if is_pinned(runs, have, want):
        return plan
    why = held(uv / EXE) if (uv / EXE).is_file() and not runs else None
    if why is not None:     # "could not look", not "damaged": nothing is fetched or replaced for it
        raise Held(f"Nightshift could not look at its uv, because another program holds {uv / EXE} ({why}).")
    if not (uv / EXE).is_file() and not any((home / "venvs").glob("*/Scripts/python.exe")):
        say("this install has no uv of its own and no Python environment to fill: nothing to bring")
        return plan
    if report(prev / EXE) == (True, want):
        plan.how = "prev"
        return plan
    name = filesdir.url_leaf(pin.get("url", "")) or zip_name(want)
    zip_path, why = obtain(pin, home, files_root, say, fetch)
    if zip_path is None:
        raise NotHad(f"Nightshift could not get uv {want} ({name}): {why}.")
    try:
        extract(zip_path, nxt)
    except (OSError, zipfile.BadZipFile) as e:
        shutil.rmtree(nxt, ignore_errors=True)
        raise NotHad(f"uv {want} ({name}) could not be unpacked from {zip_path} ({e}).") from e
    runs, got = report(nxt / EXE)
    if not is_pinned(runs, got, want):
        shutil.rmtree(nxt, ignore_errors=True)
        said = f"reports {got or 'no version'}, not {want}" if runs else "does not run on this PC"
        raise NotHad(f"The uv in {zip_path} ({name}) {said}.")
    plan.how = "next"
    return plan
