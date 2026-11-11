"""The locked dependency set of a program folder: ``uv.lock`` beside ``pyproject.toml`` (P29a).

Setup (b03, s04) and ``ns update`` (``resync_venvs``) fill a venv in three uv calls built here:

1. ``uv export --locked --offline --no-emit-project --extra <x> ...`` writes ``logs/pins-<venv>.txt``: every
   package of that venv's extras at the locked version, each with its SHA256 hashes;
2. ``uv pip install --require-hashes -r`` that file (not ``uv sync``: a venv also holds what the lock does not
   know, the cuBLAS/cuDNN wheels and the spaCy model, and ``uv sync`` would remove them);
3. ``uv pip install --no-deps -e <app>``.

So a venv gets the versions that were tested, not the newest ones PyPI has on install day.

``problem`` is the check that goes before all of it, without uv and without the network: the lock exists and
was made for this ``pyproject.toml`` (same project version, same requirements, same extras). A caller refuses
on it before it changes any venv. ``--locked`` makes uv check again; ``--offline`` keeps a lock uv finds
stale from being resolved again from the network.

Limits of ``problem``: a requirement is compared by name, extras, marker and version clauses as written
(``name[extra]>=1,<2; marker``). It does not parse parentheses or URLs; a requirement in such a form shows as
a difference, which the lock test in CI meets before any install does.

Standard library only (the setup server runs on the bare Python). This file's place in the program folder is
read by other releases (``follows_lock``): do not move or rename it.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

LOCK_NAME = "uv.lock"
PROJECT_NAME = "pyproject.toml"
#: This file, as a path inside a program folder (see ``follows_lock``).
SELF = "installer/stage2/web/deplock.py"
#: How the lock is made again (docs/ENGINEERING.md, "The dependency lock").
RELOCK = "python dev/lock_deps.py"
#: True in every state a caller refuses in: the check and the export run before the first venv call.
NO_VENV_CHANGED = "No Python environment was changed."

#: One requirement as the lock compares it: name, its own extras, its own marker, the extra of the project it
#: belongs to ("" for a base dependency), the version clauses.
Requirement = tuple[str, tuple[str, ...], str, str, frozenset[str]]

_REQUIREMENT = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?\s*([^;]*?)\s*(?:;\s*(.*?))?\s*$"
)
_EXTRA_CLAUSE = re.compile(r"(?:^|\s+and\s+)extra\s*==\s*'([^']+)'\s*$")
_PRE_RELEASE = re.compile(r"[-_.]?(a|b|rc)[-_.]?(\d+)$")


def name_key(name: str) -> str:
    """A package or extra name as indexes compare it (PEP 503): lower case, runs of ``-_.`` are one ``-``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def version_key(version: str) -> str:
    """A project version as uv writes it into the lock: ``0.1.15-rc3`` is ``0.1.15rc3``."""
    return _PRE_RELEASE.sub(r"\1\2", version.strip().lower())


def _clauses(specifier: str) -> frozenset[str]:
    return frozenset(c.replace(" ", "") for c in specifier.split(",") if c.strip())


def _marker(text: str) -> str:
    return " ".join(text.replace('"', "'").split())


def _own_extras(names: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(name_key(n) for n in names if n.strip()))


def wanted(project: Mapping[str, Any]) -> set[Requirement]:
    """The requirements ``pyproject.toml`` states: the base dependencies and every extra's."""
    groups: dict[str, list[str]] = {"": list(project.get("dependencies", []))}
    for extra, lines in project.get("optional-dependencies", {}).items():
        groups[name_key(extra)] = list(lines)
    out: set[Requirement] = set()
    for extra, lines in groups.items():
        for line in lines:
            m = _REQUIREMENT.match(line)
            if m is None:  # not a form this module reads: kept whole, so it shows as a difference
                out.add((line.strip(), (), "", extra, frozenset()))
                continue
            name, own, spec, marker = m.group(1), m.group(2) or "", m.group(3) or "", m.group(4) or ""
            out.add((name_key(name), _own_extras(own.split(",")), _marker(marker), extra, _clauses(spec)))
    return out


def locked(requires_dist: Iterable[Mapping[str, Any]]) -> set[Requirement]:
    """The requirements the lock was made for (``[package.metadata] requires-dist`` of the project)."""
    out: set[Requirement] = set()
    for entry in requires_dist:
        marker, extra = str(entry.get("marker", "")), ""
        m = _EXTRA_CLAUSE.search(marker)
        if m is not None:
            extra, marker = name_key(m.group(1)), marker[: m.start()]
        out.add(
            (
                name_key(str(entry.get("name", ""))),
                _own_extras(entry.get("extras", [])),
                _marker(marker),
                extra,
                _clauses(str(entry.get("specifier", ""))),
            )
        )
    return out


def _names(items: Iterable[Requirement]) -> str:
    shown = sorted({f"{r[0]} ({r[3]})" if r[3] else r[0] for r in items})
    return ", ".join(shown[:6]) + (f" and {len(shown) - 6} more" if len(shown) > 6 else "")


def problem(app: Path) -> str | None:
    """None when ``<app>/uv.lock`` was made for ``<app>/pyproject.toml``, else one sentence saying what is
    wrong. Reads two files; runs nothing; never raises (a file that is not what uv writes is a sentence)."""
    lock_file = app / LOCK_NAME
    if not lock_file.is_file():
        return f"{LOCK_NAME} is missing from {app}."
    try:
        lock = tomllib.loads(lock_file.read_text(encoding="utf-8"))
        project = tomllib.loads((app / PROJECT_NAME).read_text(encoding="utf-8"))["project"]
        return _difference(app, lock, project)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        return f"{LOCK_NAME} or {PROJECT_NAME} in {app} cannot be read ({type(e).__name__}: {e})."


def _difference(app: Path, lock: Mapping[str, Any], project: Mapping[str, Any]) -> str | None:
    name = name_key(str(project["name"]))
    version = str(project["version"])
    packages = lock.get("package", [])
    own = [p for p in packages if isinstance(p, dict) and name_key(str(p.get("name", ""))) == name]
    if not own:
        return f"{LOCK_NAME} in {app} does not hold the project {name}."
    entry = own[0]
    if version_key(str(entry.get("version", ""))) != version_key(version):
        return (
            f"{LOCK_NAME} in {app} was made for version {entry.get('version', '?')}, and {PROJECT_NAME} "
            f"says {version}."
        )
    metadata = entry.get("metadata", {})
    want, have = wanted(project), locked(metadata.get("requires-dist", []))
    if want != have:
        gone, added = want - have, have - want
        both = {(r[0], r[3]) for r in gone} & {(r[0], r[3]) for r in added}
        parts = []
        for label, group in (
            ("changed", [r for r in gone if (r[0], r[3]) in both]),
            ("not in the lock", [r for r in gone if (r[0], r[3]) not in both]),
            ("only in the lock", [r for r in added if (r[0], r[3]) not in both]),
        ):
            if group:
                parts.append(f"{label}: {_names(group)}")
        return f"{LOCK_NAME} in {app} does not match {PROJECT_NAME} ({'; '.join(parts)})."
    extras = {name_key(e) for e in project.get("optional-dependencies", {})}
    in_lock = {name_key(str(e)) for e in metadata.get("provides-extras", [])}
    if extras != in_lock:
        odd = ", ".join(sorted(extras ^ in_lock))
        return f"{LOCK_NAME} in {app} does not match {PROJECT_NAME} (the extras differ: {odd})."
    return None


def follows_lock(app: Path) -> bool:
    """True when the program in ``app`` installs from a lock: it has ``uv.lock``, or it has this module.

    An updater also fills the venvs of a program that is not its own (``ns update --rollback`` to the release
    before, a package of an older release). 0.1.14 and the 0.1.15 candidates up to rc3 have neither file and
    are installed their own way, from the ranges in their ``pyproject.toml``. A folder that has this module
    and no ``uv.lock`` is an incomplete copy of a release that needs one.
    """
    return (app / LOCK_NAME).is_file() or (app / SELF).is_file()


def refusal(app: Path) -> str | None:
    """Why the venvs must not be filled from ``app``: ``problem`` for a program that installs from a lock,
    None for an older one (and for a good lock)."""
    return problem(app) if follows_lock(app) else None


#: Words of uv's message when ``uv export --locked --offline`` fails over the lock itself: it is not there,
#: it "needs to be updated", or (offline) uv found "No solution" while resolving again.
LOCK_WORDS = ("lockfile", LOCK_NAME, "--locked", "no solution found")


def blames_lock(text: str) -> bool:
    """True when uv's output says the lock is why it stopped (else the cause is the disk, a blocked file, a
    missing interpreter: the caller keeps the code it gives any failed uv call)."""
    low = text.lower()
    return any(word in low for word in LOCK_WORDS)


def pins_file(logs: Path, venv: str) -> Path:
    """``logs/pins-<venv>.txt``: the exported requirements of one venv, kept for diagnosis."""
    return logs / f"pins-{venv}.txt"


def export_args(app: Path, extras: Iterable[str], out: Path) -> list[str]:
    """Arguments of the ``uv export`` that writes ``out`` from ``<app>/uv.lock`` for ``extras``."""
    args = ["export", "--locked", "--offline", "--no-emit-project", "--format", "requirements-txt"]
    for extra in extras:
        args += ["--extra", extra]
    return [*args, "--project", str(app), "-o", str(out)]


def install_args(python: Path, pins: Path) -> list[str]:
    """Arguments of the ``uv pip install`` that puts the exported set into the venv of ``python``."""
    return ["pip", "install", "--python", str(python), "--require-hashes", "-r", str(pins)]


def app_args(python: Path, app: Path) -> list[str]:
    """Arguments of the ``uv pip install`` that adds the program itself (its packages are already there)."""
    return ["pip", "install", "--python", str(python), "--no-deps", "-e", str(app)]
