"""`ns verify --downloads`: re-hash every downloaded model and program against manifest.json (component C1).

Reads what the install asked for (``config/answers.json``: LLM tier or custom model, voice design, OpenCode;
``config/wizard_flags.json``: the llama.cpp build; ``config/install_state.json``: the profile), plans the same
files setup downloads, and checks each one on disk:

* ``ok``        the file is there and its SHA256 equals the manifest pin
* ``missing``   the file is not there (a ``.part`` next to it says how far a download got)
* ``bad hash``  the file is there but its SHA256 is not the pin (or its size is wrong)
* ``ok (no pin)`` a file with no pinned hash (the owner's own story model): present and the right size
* ``unpacked``  a zip that setup extracted (the archive is not kept, so only the unpacked files are checked)
* ``optional``  an optional side file the server does not publish

It never changes anything and never touches the network: the fix for every line is printed with it and is
almost always "run INSTALL.cmd again", which downloads only what is missing or wrong.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from installer.stage2.answers import AnswersError, build_answers, load_raw, wants_voicedesign
from installer.stage2.downloader import (
    DownloadItem,
    _marker,
    _part,
    human_bytes,
    is_done,
    pinned_sha256,
    plan_bins,
    plan_models,
    sha256_file,
)
from installer.stage2.state import load_flags
from ns_common.config import load_install_state, load_manifest
from ns_common.paths import Paths, repo_root

OK = "ok"
MISSING = "missing"
BAD = "bad hash"
NO_PIN = "ok (no pin)"
UNPACKED = "unpacked"
OPTIONAL = "optional"
FAILING = (MISSING, BAD)

RERUN = "Run INSTALL.cmd again: it downloads only what is missing and keeps what is verified."


@dataclass(frozen=True)
class Row:
    """One checked file."""

    name: str
    path: str
    status: str
    detail: str = ""
    fix: str = ""

    @property
    def failing(self) -> bool:
        """True for ``missing`` and ``bad hash``."""
        return self.status in FAILING


Hasher = Callable[[Path], str]


def plan_for_install(paths: Paths, app_path: Path | None = None) -> list[DownloadItem]:
    """The files setup downloaded for this install. Raises ``AnswersError`` when setup never got that far."""
    answers_file = paths.config / "answers.json"
    if not answers_file.is_file():
        raise AnswersError(f"{answers_file} does not exist: setup has not got as far as choosing the models")
    answers = build_answers(load_raw(answers_file))
    manifest = load_manifest((app_path or repo_root()) / "manifest.json")
    state = load_install_state(paths)
    profile = state.profile or "gpu"
    flags = load_flags(paths)
    pair = flags.get("llama_pair")
    return plan_bins(
        manifest, profile, paths.home, include_code=answers.install_code,
        llama_pair=pair if isinstance(pair, str) else None,
    ) + plan_models(
        manifest, profile, answers.llm_tier, paths.home, custom=answers.llm_custom,
        include_voicedesign=wants_voicedesign(answers),
    )


def check_item(item: DownloadItem, *, hasher: Hasher = sha256_file) -> Row:
    """Check one planned file on disk."""
    dest = Path(item.dest)
    if item.kind in ("zip", "nupkg"):
        if is_done(item):
            return Row(item.name, str(item.extract_to), UNPACKED, "archive not kept, unpacked files present")
        return Row(item.name, str(item.extract_to), MISSING, "not unpacked", RERUN)
    if not dest.is_file():
        part = _part(dest)
        if part.is_file():
            have = part.stat().st_size
            of = f" of {human_bytes(item.size)}" if item.size else ""
            return Row(item.name, str(dest), MISSING, f"download stopped at {human_bytes(have)}{of}",
                       "Run INSTALL.cmd again: the partial download continues from where it stopped.")
        if item.optional:
            return Row(item.name, str(dest), OPTIONAL, "optional file, not installed")
        return Row(item.name, str(dest), MISSING, "", RERUN)
    size = dest.stat().st_size
    pin = pinned_sha256(item.sha256)
    cleanup = (
        f"Delete {dest.name} and {_marker(dest).name} in {dest.parent}, then run INSTALL.cmd again. "
        "If it fails the same way again, the pin in manifest.json is wrong: send the report."
    )
    if item.size and size != item.size:
        return Row(item.name, str(dest), BAD, f"size {size} bytes, expected {item.size}", cleanup)
    if pin is None:
        return Row(item.name, str(dest), NO_PIN, f"{human_bytes(size)}, the manifest pins no hash for it")
    digest = hasher(dest)
    if digest != pin:
        return Row(item.name, str(dest), BAD, f"sha256 {digest[:12]}..., expected {pin[:12]}...", cleanup)
    return Row(item.name, str(dest), OK, human_bytes(size))


def verify_items(
    items: list[DownloadItem],
    *,
    hasher: Hasher = sha256_file,
    say: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[Row]:
    """Check every item; ``say`` hears one progress line per file (hashing 14 GB takes a while)."""
    rows: list[Row] = []
    for n, item in enumerate(items, 1):
        if say is not None:
            say(f"[{n}/{len(items)}] checking {item.name} ...")
        t0 = clock()
        rows.append(check_item(item, hasher=hasher))
        if say is not None:
            say(f"      {rows[-1].status} ({clock() - t0:.1f} s)")
    return rows


def format_table(rows: list[Row]) -> list[str]:
    """The result table, then the fix for each file that is not fine."""
    width = max([len(r.name) for r in rows] + [4])
    status_w = max([len(r.status) for r in rows] + [6])
    lines = [f"{'file'.ljust(width)}  {'result'.ljust(status_w)}  detail"]
    lines += [f"{r.name.ljust(width)}  {r.status.ljust(status_w)}  {r.detail}".rstrip() for r in rows]
    bad = [r for r in rows if r.failing]
    if bad:
        lines += ["", "What to do:"]
        for r in bad:
            lines += [f"  {r.name} ({r.status}): {r.fix}", f"    {r.path}"]
    good = sum(1 for r in rows if not r.failing)
    lines += ["", f"{good} of {len(rows)} files fine, {len(bad)} to fix."]
    return lines


def run(
    paths: Paths,
    app_path: Path | None = None,
    echo: Callable[[str], None] = print,
    *,
    hasher: Hasher = sha256_file,
) -> int:
    """Check the whole install and print the table. Exit: 0 all fine, 1 something to fix, 2 no install."""
    try:
        items = plan_for_install(paths, app_path)
    except AnswersError as e:
        echo(f"Cannot tell which files this install needs: {e}")
        echo("Run INSTALL.cmd to finish the setup, then try again.")
        return 2
    rows = verify_items(items, hasher=hasher, say=lambda m: print(m, file=sys.stderr))
    for line in format_table(rows):
        echo(line)
    return 1 if any(r.failing for r in rows) else 0


if __name__ == "__main__":
    sys.exit(run(Paths.from_env()))
