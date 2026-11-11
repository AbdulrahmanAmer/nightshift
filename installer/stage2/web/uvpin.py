"""The uv a program pins, put in place before that program's lock is read (item uv-pin-on-update, F-E28).

``<home>\\bin\\uv`` holds the uv of the program in ``<app>`` and ``<home>\\bin\\uv.prev`` the uv of the
program in ``<app>.prev``, mirroring the program folders. An update or a rollback calls ``prepare`` before it
changes anything (U1 read the pin, U2 compare, U3 get the zip, U4 unpack to ``bin\\uv.next`` and run it),
``Plan.swap`` right before the repair record and the program swap (U5), ``Plan.finish`` right after the
program swap, and ``Plan.undo`` when the program did not get to ``<app>`` or was put back (U6, U7).

Invariant: every uv command that fills a venv of this install runs with the uv version the program it fills
pins (or, for a program that pins none, the install's uv); an update that cannot have that uv changes nothing.
And nothing of ``bin`` is deleted while a program folder may still need it: the uv of ``<app>.prev`` is moved
aside to ``bin\\uv.swap`` by the swap, never deleted by it, and goes only when the program swap is done
(``finish``) or when ``settle`` sees that ``<app>.prev`` does not pin it.

The swap of a fetched uv (``how == "next"``), one rename per step: S1 ``bin\\uv.prev`` -> ``bin\\uv.swap``,
S2 ``bin\\uv`` -> ``bin\\uv.prev``, S3 ``bin\\uv.next`` -> ``bin\\uv``. A refused rename puts back what moved
before it. The exchange (``how == "prev"``): E1 ``bin\\uv`` -> ``bin\\uv.swap``, E2 ``bin\\uv.prev`` ->
``bin\\uv``, E3 ``bin\\uv.swap`` -> ``bin\\uv.prev``.

Folders and what a kill leaves (``settle`` runs first in every ``prepare``, with the pin of ``<app>.prev``):

- ``bin\\uv.next``: a uv that was unpacked and not yet swapped in. Never read by anything; removed.
- ``bin\\uv`` missing and ``bin\\uv.prev`` there (killed after S2 or E1): ``bin\\uv.prev`` goes back to
  ``bin\\uv`` (the program swap comes after the uv swap, so the older uv is the right one, and when it is
  not, the compare that follows sees it).
- ``bin\\uv.swap`` and no ``bin\\uv.prev`` (killed after S1, S2 or E2): it becomes ``bin\\uv.prev``.
- ``bin\\uv.swap`` and ``bin\\uv.prev`` both (killed after S3, before ``finish``): when ``bin\\uv.swap`` is
  the uv ``<app>.prev`` pins and ``bin\\uv.prev`` is not, the program swap did not happen and the uv swap is
  put back whole (the fetched uv stays in the cache); otherwise ``bin\\uv.swap`` is not needed and goes. With
  no pin of ``<app>.prev`` to go by (``older`` None) it is left.

Standard library only at import (setup's own server imports this package without httpx); the download
imports httpx and ``ns_supervisor.releases`` when it is called, which only the updater does.
"""

from __future__ import annotations

import hashlib
import io
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


def pin_in_bytes(data: bytes, prefix: str = "") -> dict[str, str]:
    """``bins.uv`` of the ``manifest.json`` inside an update zip given as its bytes: the bytes the updater
    hashed and checked (``update._check_package``), never the file read a second time."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
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
        kept = uv_dir(home, ".prev")
        if (kept / EXE).is_file():      # a swap cut between its two renames: this install has no uv in place
            return False, (
                f"none at {exe}, but one is kept in {kept}: an update or rollback was stopped while it "
                f"exchanged them. Run that update or rollback again (it puts uv back first), or: {REPAIR}"
            )
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


def _serves(folder: Path, pin: Mapping[str, str]) -> bool:
    """The uv in ``folder`` runs and is the version ``pin`` names (False for a pin without a version)."""
    want = pin.get("version", "")
    return bool(want) and report(folder / EXE) == (True, want)


def settle(home: Path, older: Mapping[str, str] | None = None) -> None:
    """Finish what a run killed inside a swap left (the module text lists the states). ``older`` is the pin
    of the program in ``<app>.prev`` ({} when there is none or it pins none; None: not known, so a kept
    ``bin\\uv.swap`` is never deleted). Never raises: what could not be moved is seen by the compare that
    follows, and the next ``settle`` tries again."""
    uv, prev, nxt, tmp = (uv_dir(home, s) for s in ("", ".prev", ".next", ".swap"))
    try:
        if not (uv / EXE).is_file() and (prev / EXE).is_file():
            shutil.rmtree(uv, ignore_errors=True)
            os.rename(prev, uv)
        if tmp.exists():
            if not prev.exists():
                os.rename(tmp, prev)
            elif older is not None and _serves(tmp, older) and not _serves(prev, older):
                # killed after S3 and before the program swap: <app>.prev still needs the kept uv, so the
                # whole uv swap goes back (bin\uv was the uv of the program still in <app>)
                shutil.rmtree(nxt, ignore_errors=True)
                os.rename(uv, nxt)
                os.rename(prev, uv)
                os.rename(tmp, prev)
            elif older is not None:     # bin\uv.prev serves <app>.prev, or <app>.prev does not want this one
                shutil.rmtree(tmp, ignore_errors=True)
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
    #: The pin of the program in ``<app>.prev`` when ``prepare`` ran (``settle`` decides a kept uv by it).
    older: Mapping[str, str] | None = None

    def swap(self, rename: Rename = os.rename) -> str:
        """U5 (S1 to S3, or E1 to E3: the module text). ``OSError`` when a rename stays refused (a uv still
        running): what moved before it is put back, and ``undo`` (which the caller calls next) or the next
        ``settle`` finishes what that could not. ``bin\\uv.prev`` is moved aside to ``bin\\uv.swap``, never
        deleted here: ``finish`` removes it after the program swap. The line for the log when it swapped."""
        uv, prev, nxt, tmp = (uv_dir(self.home, s) for s in ("", ".prev", ".next", ".swap"))
        if self.how and tmp.exists():   # (settle could not decide or move it: nothing deleted to make room)
            raise OSError(f"{tmp} is left from an earlier swap and could not be settled")
        if self.how == "next":
            if prev.exists():
                rename(prev, tmp)               # S1: the uv of <app>.prev aside, kept until the swap is done
            try:
                if uv.exists():
                    rename(uv, prev)            # S2
                rename(nxt, uv)                 # S3
            except OSError:
                try:        # back in the order they moved: bin\uv first, then the kept uv to bin\uv.prev
                    if prev.exists() and not uv.exists():
                        os.rename(prev, uv)
                    if tmp.exists() and not prev.exists():
                        os.rename(tmp, prev)
                except OSError:
                    pass    # (a cut swap: `undo`, called next by the caller, settles it; else the next run)
                raise
        elif self.how == "prev":
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

    def finish(self, older: Mapping[str, str]) -> None:
        """Right after the program swap (or after the uv swap when no program swap follows): ``older`` is the
        pin of the program in ``<app>.prev`` NOW. The uv kept in ``bin\\uv.swap`` by S1 goes, unless it is
        the one ``<app>.prev`` pins and ``bin\\uv.prev`` is not: then it takes the place of ``bin\\uv.prev``.
        Never raises: what stays is decided by the next ``settle`` by the same rule."""
        prev, nxt, tmp = (uv_dir(self.home, s) for s in (".prev", ".next", ".swap"))
        if self.swapped and self.how == "next" and tmp.exists():
            try:
                if _serves(tmp, older) and not _serves(prev, older):
                    # ``bin\\uv.prev`` is moved aside (to the free ``bin\\uv.next``), not deleted first: a
                    # delete that stops half way (a file held) and a refused rename after it would leave half
                    # a uv in ``bin\\uv.prev``, which the next ``settle`` (kept uv there, ``bin\\uv.prev``
                    # not the pinned one) would put into ``bin\\uv``. A kill or a refusal between the two
                    # renames leaves ``bin\\uv.swap`` and no ``bin\\uv.prev``: ``settle`` finishes it.
                    shutil.rmtree(nxt, ignore_errors=True)
                    os.rename(prev, nxt)
                    os.rename(tmp, prev)
                    shutil.rmtree(nxt, ignore_errors=True)
                else:
                    shutil.rmtree(tmp, ignore_errors=True)
            except OSError:
                pass
        self.older = None   # (<app>.prev is another program now: an `undo` after this decides nothing by it)

    def undo(self, rename: Rename = os.rename) -> str:
        """U6, U7: the uv that was there before, back in ``bin\\uv``, and the kept uv back in
        ``bin\\uv.prev``. Never raises; the sentence says what is there now ("" when it is as it was)."""
        uv, prev, nxt, tmp = (uv_dir(self.home, s) for s in ("", ".prev", ".next", ".swap"))
        if not self.swapped:
            if not self.how:
                shutil.rmtree(nxt, ignore_errors=True)
                return ""
            settle(self.home, self.older)   # (a swap refused half way and not put back: finished here)
            if (uv / EXE).is_file() or not self.was:
                return ""
            return f" uv could not be put back to {self.was}: {uv} is missing. {REPAIR}"
        try:
            if self.how == "next":
                shutil.rmtree(nxt, ignore_errors=True)
                rename(uv, nxt)
                if prev.exists():
                    rename(prev, uv)
                if tmp.exists() and not prev.exists():   # (before `finish`: the kept uv of <app>.prev)
                    rename(tmp, prev)
                shutil.rmtree(nxt, ignore_errors=True)
            else:
                if tmp.exists() and not prev.exists():   # (E3 was cut)
                    os.rename(tmp, prev)
                rename(uv, tmp)
                rename(prev, uv)
                rename(tmp, prev)
        except OSError as e:
            settle(self.home, self.older)
            runs, have = report(uv / EXE)
            if not (runs and have == self.was):
                return f" uv could not be put back to {self.was or 'what it was'} ({e}). {REPAIR}"
        self.swapped = False
        return f" uv is back at {self.was}." if self.was else ""


def prepare(
    pin: Mapping[str, str], home: Path, *, files_root: Path | None = None,
    say: Callable[[str], None] = lambda line: None, fetch: Fetch | None = None,
    older: Mapping[str, str] | None = None,
) -> Plan:
    """U1 to U4, before anything of the install is changed. ``NotHad`` when the pinned uv cannot be had or
    does not run; ``bin\\uv`` and ``bin\\uv.prev`` are untouched then and ``bin\\uv.next`` is removed.
    ``older``: the pin of the program in ``<app>.prev`` (``settle``)."""
    plan = Plan(home, older=older)
    settle(home, older)  # U0: a bin/uv.next or a cut swap that a killed run left goes first, pin or no pin
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
