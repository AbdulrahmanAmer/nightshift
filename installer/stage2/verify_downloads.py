"""`ns verify --downloads`: re-hash every downloaded model and program against manifest.json (component C1).

Reads what the install asked for (``config/answers.json``: LLM tier or custom model, voice design, OpenCode;
``config/wizard_flags.json``: the llama.cpp build; ``config/install_state.json``: the profile;
``config/runtime.json``: the story model ``ns models get story_llm`` added), plans the same files setup
downloads, and checks each one on disk:

* ``ok``        the file is there and its SHA256 equals the manifest pin
* ``missing``   the file is not there (a ``.part`` next to it says how far a download got: that is an
  interrupted download, which INSTALL.cmd continues, not a damaged file)
* ``bad hash``  the file is there but its SHA256 is not the pin (or its size is wrong): damaged
* ``unreadable`` the file could not be read (another program holds it, a read error): NOT called damaged
* ``ok (no pin)`` a file with no pinned hash (the owner's own story model): present and the right size
* ``unpacked``  a zip that setup extracted (the archive is not kept, so only the unpacked files are checked)
* ``optional``  an optional side file the server does not publish
* ``stub (dev)`` a placeholder written by ``INSTALL.cmd --dev`` (docs/DEV_MODE.md): fine in a dev install

``check_item`` is the ONE checker of a model file (card model-file-damaged): ``ns verify --downloads``,
setup's Repair (s03) and the engine after a model failed to load (``ns_supervisor.modelcheck``) all call
it, and each writes what it found into ``logs/model-check.json`` (``installer.stage2.web.modelmarks``).
It never changes a model file and never touches the network: the fix for every line is printed with it. A
damaged model file's fix is setup's Repair, which checks every model by hash and downloads it again.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from installer.stage2 import devmode
from installer.stage2.answers import AnswersError, build_answers, load_raw, wants_voicedesign
from installer.stage2.downloader import (
    DownloadItem,
    _hf_item,
    _marker,
    _part,
    hashed_at,
    human_bytes,
    is_done,
    pinned_sha256,
    plan_bins,
    plan_models,
    sha256_file,
)
from installer.stage2.state import load_flags
from installer.stage2.web import modelmarks
from ns_common.config import load_install_state, load_manifest, load_runtime
from ns_common.models import Manifest
from ns_common.paths import Paths, repo_root

OK = "ok"
MISSING = "missing"
BAD = modelmarks.BAD
UNREADABLE = "unreadable"
NO_PIN = "ok (no pin)"
UNPACKED = "unpacked"
OPTIONAL = "optional"
STUB = "stub (dev)"
FAILING = (MISSING, BAD, UNREADABLE)

RERUN = "Run INSTALL.cmd again: it downloads only what is missing and keeps what is verified."
#: The one way out of a damaged or missing model file (``{what}`` names it and its size).
REPAIR = "Run INSTALL.cmd again and choose Repair: it downloads {what} again."
HELD = (
    "Another program has it open or it could not be read (Nightshift's engine, an antivirus scan): this is "
    "not damage. Stop All (or close that program), then run 'ns verify --downloads' again."
)
#: Read speeds for the time a check takes when none was measured here yet (bytes per second).
SSD_BPS = 500 * 1024 * 1024
HDD_BPS = 120 * 1024 * 1024
#: The progress line of one file is said at most this often (seconds).
SAY_EVERY_S = 1.0


@dataclass(frozen=True)
class Row:
    """One checked file. ``size``: its bytes on disk (None when it is not there); ``hashed``: its SHA256 was
    read in this check."""

    name: str
    path: str
    status: str
    detail: str = ""
    fix: str = ""
    size: int | None = None
    hashed: bool = False

    @property
    def failing(self) -> bool:
        """True for ``missing``, ``bad hash`` and ``unreadable``."""
        return self.status in FAILING


Hasher = Callable[..., str]
ProgressFn = Callable[[int, "int | None"], None]


def _stdout(line: str) -> None:
    sys.stdout.write(line + "\n")


def _stderr(line: str) -> None:
    sys.stderr.write(line + "\n")


def _now() -> float:
    """The clock of a check (the seam a test replaces)."""
    return time.monotonic()


def _size(path: Path) -> int:
    """A file's size (the seam a test gives a model's real size through, without writing gigabytes)."""
    return path.stat().st_size


def _marker_size(dest: Path) -> int | None:
    """The size setup's ``.ok`` marker recorded for ``dest`` (None: no marker, or not one setup can read)."""
    try:
        size = json.loads(_marker(dest).read_text(encoding="utf-8")).get("size")
    except (OSError, ValueError, AttributeError):
        return None
    return size if isinstance(size, int) else None


def is_model(item: DownloadItem) -> bool:
    """A model file (under ``models``), as opposed to a program or an archive."""
    return item.kind == "file" and "models" in Path(item.dest).parts


def story_item(manifest: Manifest, profile: str, paths: Paths) -> DownloadItem | None:
    """The story model ``ns models get story_llm`` downloaded, when ``runtime.json`` uses it (two models)."""
    pm = manifest.profiles.get(profile)
    if pm is None or pm.story_llm is None:
        return None
    try:
        story = load_runtime(paths).llama.story_model_path
    except Exception:  # noqa: BLE001 - no usable runtime.json: no story model to check
        return None
    item = _hf_item(pm.story_llm, paths.models_llm)
    return item if story and Path(story).name == item.dest.name else None


def plan_for_install(paths: Paths, app_path: Path | None = None) -> list[DownloadItem]:
    """The files setup downloaded for this install. Raises ``AnswersError`` when setup never got that far."""
    answers_file = paths.config / "answers.json"
    if not answers_file.is_file():
        raise AnswersError(f"{answers_file} does not exist: setup has not got as far as choosing the models")
    answers = build_answers(load_raw(answers_file), saved=True)
    manifest = load_manifest((app_path or repo_root()) / "manifest.json")
    state = load_install_state(paths)
    profile = state.profile or "gpu"
    flags = load_flags(paths)
    pair = flags.get("llama_pair")
    story = story_item(manifest, profile, paths) if not state.dev_mode else None
    return plan_bins(
        manifest, profile, paths.home, include_code=answers.install_code,
        llama_pair=pair if isinstance(pair, str) else None,
    ) + plan_models(
        manifest, profile, answers.llm_tier, paths.home, custom=answers.llm_custom,
        include_voicedesign=wants_voicedesign(answers), dev=state.dev_mode,
        include_voice=not (answers.fake_voice and not state.dev_mode),
    ) + ([story] if story is not None else [])


def model_items(paths: Paths, app_path: Path | None = None) -> list[DownloadItem]:
    """The model files of this install (``plan_for_install`` without programs and archives)."""
    return [i for i in plan_for_install(paths, app_path) if is_model(i)]


def role(item: DownloadItem, paths: Paths | None = None) -> str:
    """What the file is, in the owner's words: language model, story model (the one ``runtime.json`` names as
    ``story_model_path``, read from ``paths``), embed model, voice models, whisper model or face finder."""
    folder = Path(item.dest).parent.name
    if folder == "llm":
        try:
            story = load_runtime(paths).llama.story_model_path if paths is not None else None
        except Exception:  # noqa: BLE001 - no usable runtime.json: no story model
            story = None
        return "story model" if story and Path(story).name == Path(item.dest).name else "language model"
    return {"embed": "embed model", "tts": "voice models", "cv": "face finder"}.get(
        folder, "whisper model" if "whisper" in Path(item.dest).parts else "model files"
    )


def what(item: DownloadItem) -> str:
    """``<file name> (<size>)`` for the sentences that name a file."""
    size = item.size or (_size(Path(item.dest)) if Path(item.dest).is_file() else None)
    return f"{Path(item.dest).name} ({human_bytes(size)})" if size else Path(item.dest).name


def check_item(item: DownloadItem, *, hasher: Hasher | None = None, progress: ProgressFn | None = None,
               quick: bool = False) -> Row:
    """Check one planned file on disk. ``quick``: size and presence only (``ns doctor``, a night's start), no
    hash; ``progress(bytes read, total)`` while the hash runs. A ``KeyboardInterrupt`` from the hasher goes
    through: the caller says what was checked."""
    dest = Path(item.dest)
    model = is_model(item)
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
        return Row(item.name, str(dest), MISSING, "", REPAIR.format(what=what(item)) if model else RERUN)
    size = _size(dest)
    if item.stub:                       # a dev install: the placeholder is the right file
        if devmode.is_stub(dest):
            return Row(item.name, str(dest), STUB, "dev mode placeholder, not a model", size=size)
        return Row(item.name, str(dest), NO_PIN, f"{human_bytes(size)}, a real file kept in a dev install",
                   size=size)
    pin = pinned_sha256(item.sha256)
    cleanup = REPAIR.format(what=what(item)) if model else (
        f"Delete {dest.name} and {_marker(dest).name} in {dest.parent}, then run INSTALL.cmd again. "
        "If it fails the same way again, the pin in manifest.json is wrong: send the report."
    )
    if item.size and size != item.size:
        return Row(item.name, str(dest), BAD, f"size {size} bytes, expected {item.size}", cleanup, size=size)
    was = None if item.size else _marker_size(dest)    # no size in the manifest: setup's marker kept it
    if was is not None and size != was:
        return Row(item.name, str(dest), BAD, f"size {size} bytes, the download was {was} bytes", cleanup,
                   size=size)
    if pin is None:
        return Row(item.name, str(dest), NO_PIN,
                   f"{human_bytes(size)}, the manifest pins no hash for it, so it cannot be checked by hash",
                   size=size)
    if quick:
        return Row(item.name, str(dest), OK, f"{human_bytes(size)}, size checked", size=size)
    try:
        read = hasher or sha256_file    # looked up now: the seam a test replaces
        digest = read(dest) if progress is None else read(dest, progress=progress)
    except OSError as e:                # held open without sharing, a read error: not evidence of damage
        return Row(item.name, str(dest), UNREADABLE, f"could not be read ({type(e).__name__}: {e})", HELD,
                   size=size)
    if digest != pin:
        return Row(item.name, str(dest), BAD, f"sha256 {digest[:12]}..., expected {pin[:12]}...", cleanup,
                   size=size, hashed=True)
    return Row(item.name, str(dest), OK, human_bytes(size), size=size, hashed=True)


def record(
    paths: Paths, item: DownloadItem, row: Row, seconds: float, *, now: datetime | None = None
) -> None:
    """Write one model file's verdict into ``logs/model-check.json`` (nothing for programs and archives)."""
    if not is_model(item) or row.status in (OPTIONAL, STUB):
        return
    dest = Path(item.dest)
    try:
        st = dest.stat()
        on_disk: dict[str, Any] = {"bytes": st.st_size, "mtime_ns": st.st_mtime_ns}
    except OSError:
        on_disk = {"bytes": None, "mtime_ns": None}
    entry = {
        "verdict": row.status, "sha256_ok": (row.status == OK) if row.hashed else None, **on_disk,
        "checked_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"), "seconds": round(seconds, 2),
    }
    modelmarks.write_entry(paths.home, modelmarks.key_of(paths.home, dest), entry)


def last_hashed(paths: Paths, item: DownloadItem) -> str | None:
    """When this file's SHA256 was last read and matched, while it is still that file: the setup marker's
    ``hashed_at`` (a download, a Repair) or the record's ``ok`` entry (``ns verify --downloads``, the engine),
    whichever is newer; None when neither says so."""
    dest = Path(item.dest)
    seen = [hashed_at(dest)]
    entry = modelmarks.read_record(paths.home).get(modelmarks.key_of(paths.home, dest))
    if entry and entry.get("verdict") == OK and entry.get("sha256_ok") and modelmarks.still_same(entry, dest):
        seen.append(str(entry.get("checked_at") or "") or None)
    found = [s for s in seen if s]
    return max(found) if found else None


def default_speed(paths: Paths) -> tuple[float, str]:
    """(bytes per second, where the number comes from) for a check: the speed measured by the last checks
    here, else 500 MB/s when setup found an SSD (``config/supervisor.json`` holds the SSD wait), else 120 MB/s
    (a hard disk, or a drive setup could not name)."""
    total_b = total_s = 0.0
    for entry in modelmarks.read_record(paths.home).values():
        if entry.get("sha256_ok") is not None and isinstance(entry.get("bytes"), int) \
                and float(entry.get("seconds") or 0) >= 0.5:
            total_b += entry["bytes"]
            total_s += float(entry["seconds"])
    if total_s > 0 and total_b > 0:
        return total_b / total_s, "measured here"
    from ns_supervisor.health_cfg import DEFAULT_HEALTH_TIMEOUT_S, health_file, read_health_timeout

    if health_file(paths).is_file() and read_health_timeout(paths) == DEFAULT_HEALTH_TIMEOUT_S:
        return SSD_BPS, "an SSD"
    return HDD_BPS, "a hard disk"


def duration_text(seconds: float) -> str:
    """``7 s``, ``1 minute``, ``12 minutes``."""
    if seconds < 60:
        return f"{max(1, round(seconds))} s"
    minutes = max(1, round(seconds / 60))
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


def speed_text(bps: float) -> str:
    """``1.2 GB/s`` or ``120 MB/s``."""
    gb = bps / 1024 ** 3
    return f"{gb:.1f} GB/s" if gb >= 1 else f"{bps / 1024 ** 2:.0f} MB/s"


class Progress:
    """The progress of one file's hash: ``checking <name> (<size>): 40% at 1.2 GB/s, about 7 s left``, said
    at most once every ``every`` seconds of ``clock``."""

    def __init__(self, say: Callable[[str], None], name: str, total: int | None,
                 clock: Callable[[], float], every: float = SAY_EVERY_S) -> None:
        self.say, self.name, self.total, self.clock, self.every = say, name, total, clock, every
        self.t0 = clock()
        self.last: float | None = None

    def __call__(self, done: int, total: int | None = None) -> None:
        """Bytes read so far."""
        total = total or self.total
        now = self.clock()
        if not total or (self.last is not None and now - self.last < self.every) or now <= self.t0:
            return
        self.last = now
        bps = done / (now - self.t0)
        left = (total - done) / bps if bps > 0 else 0.0
        self.say(f"checking {self.name} ({human_bytes(total)}): {int(done * 100 / total)}% at "
                 f"{speed_text(bps)}, about {duration_text(left)} left")


def verify_items(
    items: list[DownloadItem],
    *,
    hasher: Hasher | None = None,
    say: Callable[[str], None] | None = None,
    clock: Callable[[], float] | None = None,
    paths: Paths | None = None,
    done: list[Row] | None = None,
    every: float = SAY_EVERY_S,
) -> list[Row]:
    """Check every item; ``say`` hears a line per file and its progress (hashing 14 GB takes a while). With
    ``paths`` each model file's verdict goes into ``logs/model-check.json`` as soon as it is known; ``done``
    receives the rows as they come (an interrupted check still knows what it finished)."""
    rows: list[Row] = [] if done is None else done
    clock = clock or _now
    for n, item in enumerate(items, 1):
        size = item.size or (_size(Path(item.dest)) if Path(item.dest).is_file() else None)
        if say is not None:
            of = f" ({human_bytes(size)})" if size else ""
            say(f"[{n}/{len(items)}] checking {item.name}{of} ...")
        t0 = clock()
        progress = Progress(say, item.name, size, clock, every) if say is not None else None
        row = check_item(item, hasher=hasher, progress=progress)
        seconds = clock() - t0
        rows.append(row)
        if paths is not None:
            try:
                record(paths, item, row, seconds)
            except OSError as e:        # the verdict is still printed; only the record misses it
                if say is not None:
                    say(f"      (could not write {modelmarks.record_path(paths.home)}: {e})")
        if say is not None:
            say(f"      {row.status} ({seconds:.1f} s)")
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
    echo: Callable[[str], None] = _stdout,
    *,
    hasher: Hasher | None = None,
    say: Callable[[str], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> int:
    """Check the whole install and print the table. Exit: 0 all fine, 1 something to fix, 2 no install, 130
    stopped (Ctrl+C): no model file was changed, the files finished so far are in the record."""
    try:
        items = plan_for_install(paths, app_path)
    except AnswersError as e:
        echo(f"Cannot tell which files this install needs: {e}")
        echo("Run INSTALL.cmd to finish the setup, then try again.")
        return 2
    say = say or _stderr
    to_hash = sum(i.size or 0 for i in items if is_model(i) and pinned_sha256(i.sha256) and not i.stub)
    bps, where = default_speed(paths)
    say(f"Checking {len(items)} files; {human_bytes(to_hash)} of models are read by hash (about "
        f"{duration_text(to_hash / bps)} at {speed_text(bps)}, {where}). Ctrl+C stops it; nothing is "
        "changed.")
    rows: list[Row] = []
    try:
        verify_items(items, hasher=hasher, say=say, clock=clock, paths=paths, done=rows)
    except KeyboardInterrupt:
        echo(f"stopped: nothing was changed (no model file and no marker); {len(rows)} of {len(items)} "
             f"files were checked, their results are in {modelmarks.record_path(paths.home)}.")
        return 130
    for line in format_table(rows):
        echo(line)
    return 1 if any(r.failing for r in rows) else 0


if __name__ == "__main__":
    sys.exit(run(Paths.from_env()))
