"""Setup's Repair of the model files (card model-file-damaged, step M5).

A Repair is a setup run over an install that had finished (``install_state.completed_at`` was set when the
run began: INSTALL.cmd over the install, in the window or unattended). The wizard then sets the flag
``models_recheck`` to the run's start time (``begin``), and s03 is not "already complete" until every pinned
model file was hashed since that time (``unhashed``: its marker's ``hashed_at``). A first install never sets
the flag: its files were hashed by the downloader once, which is enough.

Steps, each safe to interrupt and to run again:

1. ``check_models`` hashes each model whose marker is older than the flag (``verify_downloads.check_item``,
   the one checker), shows its progress on the setup screen every few seconds and writes the verdict into
   ``logs/model-check.json``. A file that matches gets a fresh ``hashed_at``: a run killed half way does not
   hash it again. A file that does not match is set aside as ``<name>.bad`` (after a check that the drive can
   hold the replacement beside it, else NS-E104 and nothing is moved) and its marker removed, so the plan
   fetches it as a missing file (resumed from its ``.part`` when one is there).
2. The download (``ctx.download``) fetches it. Offline or the site gone: the step fails as any download does
   (NS-E105, Retry); the ``.bad`` stays until the replacement verifies, so the owner is never left with
   neither file.
3. ``finish`` deletes every ``.bad`` whose replacement is verified, says what was done and clears the flag.
   Killed before that, the flag and the ``.bad`` are still there: the next run checks again and finishes.

A file another program holds open is not damage: it is left as it is, said on the step's row, and the flag
stays, so the next run checks it. Markers whose file is gone or changed and is no longer part of the plan
(an old tier's model the owner deleted) are removed, so the setup window does not reopen s03 for them.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path

from installer.stage2 import verify_downloads as vd
from installer.stage2.context import StepContext
from installer.stage2.downloader import (
    DownloadItem,
    _marker,
    hashed_at,
    human_bytes,
    is_done,
    pinned_sha256,
    write_marker,
)
from installer.stage2.errors import make_error
from installer.stage2.web import modelmarks
from ns_common.atomic import replace_atomic

RECHECK_FLAG = "models_recheck"
#: Room kept free beyond the replacement's own size while it is downloaded beside the damaged copy.
MARGIN_BYTES = 1024 ** 3
#: The setup screen's progress line of one file's hash is said at most this often (seconds).
SAY_EVERY_S = 5.0


def begin(ctx: StepContext, *, was_complete: bool) -> None:
    """At the start of a wizard run: a run over a finished install is a Repair. An interrupted Repair keeps
    its first stamp (the files it already hashed are not hashed again)."""
    if was_complete and not ctx.flag(RECHECK_FLAG):
        ctx.set_flag(RECHECK_FLAG, datetime.now(UTC).isoformat(timespec="seconds"))
        ctx.log.info("models_recheck", "a run over a finished install: every model file is checked by hash")


def _checkable(items: list[DownloadItem]) -> list[DownloadItem]:
    return [i for i in items if vd.is_model(i) and pinned_sha256(i.sha256) and not i.stub and not i.optional]


def unhashed(ctx: StepContext, items: list[DownloadItem]) -> list[DownloadItem]:
    """On a Repair: the verified model files not hashed since it began (none outside a Repair)."""
    stamp = ctx.flag(RECHECK_FLAG)
    if not isinstance(stamp, str) or ctx.simulate:     # (a simulated download writes no real model bytes)
        return []
    return [i for i in _checkable(items) if is_done(i) and (hashed_at(Path(i.dest)) or "") < stamp]


def set_aside(ctx: StepContext, items: list[DownloadItem]) -> list[Path]:
    """Damaged copies a Repair set aside that are still on disk."""
    root = ctx.paths.home / "models"
    if not root.is_dir():
        return []
    return [Path(d) / n for d, _s, files in os.walk(root) for n in files if n.endswith(modelmarks.SET_ASIDE)]


def _sweep_markers(ctx: StepContext, items: list[DownloadItem]) -> None:
    planned = {Path(i.dest) for i in items}
    root = ctx.paths.home / "models"
    if not root.is_dir():
        return
    for folder, _dirs, files in os.walk(root):
        for name in files:
            marker = Path(folder) / name
            if name.endswith(".ok") and marker.with_name(name[:-3]) not in planned \
                    and modelmarks.marker_moved(marker) is not None:
                marker.unlink(missing_ok=True)
                ctx.log.info("stale_marker_removed", file=str(marker))


def _set_aside(ctx: StepContext, item: vd.DownloadItem, row: vd.Row) -> None:
    dest = Path(item.dest)
    need = (item.size or row.size or 0) + MARGIN_BYTES
    free = ctx.free_gb_fn(dest.parent) * 1024 ** 3
    if free < need:
        raise make_error(
            "NS-E104",
            f"{vd.what(item)} is damaged and is downloaded again beside it (the damaged copy is kept until "
            f"the new one is checked): that needs {human_bytes(need)} free at {dest.parent}, there are "
            f"{human_bytes(free)}. Free some space and run INSTALL.cmd again (Repair); nothing was changed.",
        )
    replace_atomic(dest, dest.with_name(dest.name + modelmarks.SET_ASIDE))
    _marker(dest).unlink(missing_ok=True)
    ctx.log.warn("model_damaged", f"{dest} does not match its SHA256 ({row.detail}): set aside, to fetch")


def check_models(ctx: StepContext, items: list[DownloadItem]) -> list[DownloadItem]:
    """Step 1 (see the module): returns the damaged files set aside in this run."""
    _sweep_markers(ctx, items)
    todo = unhashed(ctx, items)
    if not todo:
        return []
    total = sum(i.size or 0 for i in todo)
    bps, _where = vd.default_speed(ctx.paths)
    ctx.ui.say(f"Checking {len(todo)} model files by hash ({human_bytes(total)}, about "
               f"{vd.duration_text(total / bps)}): a Repair checks every model and downloads a damaged one "
               "again.")
    damaged: list[DownloadItem] = []
    held: list[str] = []
    for item in todo:
        size = item.size or vd._size(Path(item.dest))
        t0 = time.monotonic()
        progress = vd.Progress(ctx.ui.say, item.name, size, time.monotonic, SAY_EVERY_S)
        row = vd.check_item(item, progress=progress)
        try:
            vd.record(ctx.paths, item, row, time.monotonic() - t0)
        except OSError as e:
            ctx.log.warn("model_check_not_recorded", str(e))
        if row.status == vd.OK:
            write_marker(Path(item.dest), pinned_sha256(item.sha256))
        elif row.status == vd.BAD:
            _set_aside(ctx, item, row)
            damaged.append(item)
        elif row.status == vd.UNREADABLE:
            held.append(Path(item.dest).name)
    if held:
        ctx.note_step(f"Not checked: {', '.join(held)} could not be read (another program has it open; that "
                      "is not damage). Close Nightshift (Stop All) and run INSTALL.cmd again to check it.")
    ctx.ui.say(f"{len(damaged)} of {len(todo)} model files {'was' if len(damaged) == 1 else 'were'} damaged"
               + (f" and {'is' if len(damaged) == 1 else 'are'} downloaded again: "
                  f"{', '.join(vd.what(i) for i in damaged)}." if damaged else "."))
    return damaged


def finish(ctx: StepContext, items: list[DownloadItem], damaged: list[DownloadItem]) -> None:
    """Step 3: the replacements are verified (the download returned): drop the damaged copies and the flag."""
    pending = {Path(i.dest) for i in items if not is_done(i)}
    for bad in set_aside(ctx, items):
        if bad.with_name(bad.name[: -len(modelmarks.SET_ASIDE)]) not in pending:
            bad.unlink(missing_ok=True)
            ctx.log.info("damaged_copy_removed", file=str(bad))
    if damaged:
        plural = "s were" if len(damaged) > 1 else " was"
        ctx.note_step(f"{len(damaged)} damaged model file{plural} downloaded again: "
                      f"{', '.join(vd.what(i) for i in damaged)}.")
    if not unhashed(ctx, items):
        ctx.clear_flags(RECHECK_FLAG)
