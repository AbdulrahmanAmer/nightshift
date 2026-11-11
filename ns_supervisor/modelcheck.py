"""Which model file a failed load used, and whether that file is damaged (card model-file-damaged, M6, M7).

A model that does not load says nothing the owner can act on. llama-server, the voice service and whisper
print their own words for a truncated or corrupted file, and those words were never captured on this
project's machines (no test or fake holds one), so nothing here reads them. The file itself decides: after a
load failed, the service's model files are checked by hash with the one checker
(``installer.stage2.verify_downloads.check_item``), and its verdict is the evidence:

* a SHA256 or a size that is not the manifest's: ``NS-E127`` naming the file, its size and the way out
  (setup's Repair), recorded in ``logs/model-check.json``;
* the file matches: not damage. The caller keeps its own classification (too little memory: NS-E208 or
  NS-E203; else the original NS-E202);
* the file is missing: a sentence for the caller's error (a ``.part`` beside it is an interrupted download,
  which INSTALL.cmd continues: not damage);
* it could not be read (another program holds it) or the hash did not finish within ``BOUND_S``: not damage;
  the sentence says so and names ``ns verify --downloads``.

Each file is hashed at most once per process for one size and date (``FileCheck``): a second failure of the
same file is answered from that verdict at once. A custom story model has no pinned hash: only its size is
checked, and the sentence says so.

``precheck`` is the cheap check at a night's start (stat calls and the record, no hash).
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ns_common.errors import NsError
from ns_common.paths import Paths

if TYPE_CHECKING:
    from installer.stage2.downloader import DownloadItem
    from installer.stage2.verify_downloads import Row

#: The longest a hash after a failed load may take; past it the original error stands, with a sentence.
BOUND_S = 240.0
CODE = "NS-E127"
NOT_CHECKED = "not checked"


@dataclass(frozen=True)
class Verdict:
    """What the check of one service's files found: ``damaged`` (NS-E127), else a sentence for the caller."""

    damaged: NsError | None = None
    note: str = ""


class _OutOfTime(Exception):
    pass


#: The talker that only "design a voice" loads (``ns_tts.engine.TALKER_KIND["design"]``, the word in its file
#: name by ``TALKER_TEMPLATE``; a test holds the two together). The voice service's start and every
#: synthesis work without it (``ns_tts.voices``: a designed voice is stored as a Base prompt), so it is no
#: file of a failed voice start and no file of a night.
DESIGN_KIND = "voicedesign"


def design_only(item: DownloadItem) -> bool:
    """True for the VoiceDesign talker: loaded when a voice is designed, never by a start or a synthesis."""
    dest = Path(item.dest)
    return dest.parent.name == "tts" and DESIGN_KIND in dest.name.lower()


def night_files(paths: Paths, items: list[DownloadItem]) -> list[DownloadItem]:
    """The planned model files a night's jobs load, taken from where the stages take them: llama's ``-m``
    files as ``runtime.json`` names them (the language model and, with two models, the story model), the
    voice files a synthesis uses (the talkers and the tokenizer, not the design talker) and whisper's files.
    Not among them: the design talker, the embed model and the face finder; should a job load one of those
    after all, the engine's check after the failed load names it (``FileCheck``). When ``runtime.json``
    cannot be read or names no planned file, every file of ``models\\llm`` counts."""
    from ns_common.config import load_runtime

    try:
        llama = load_runtime(paths).llama
        cmd = [x for p in (llama.model_path, llama.story_model_path) if p for x in ("-m", str(p))]
        language = files_for("llama", cmd, items)
    except Exception:  # noqa: BLE001 - no usable runtime.json: the folder decides
        language = []
    if not language:    # runtime.json names no planned file (or cannot be read): the safe side, every one
        language = [i for i in items if Path(i.dest).parent.name == "llm"]
    return language + files_for("tts", [], items) + files_for("whisper", [], items)


def files_for(service: str, cmd: list[str], items: list[DownloadItem]) -> list[DownloadItem]:
    """The planned model files a service loads: the ``-m`` file of llama-server (llama, embed), the voice
    models a start and a synthesis of a real voice engine use (tts: not the design talker, ``design_only``),
    whisper's files (whisper). A file outside the plan is not checked."""
    if service in ("llama", "embed"):
        # runtime.json may spell the path with forward slashes or another case: compare normalised paths
        wanted = {os.path.normcase(str(Path(cmd[i + 1]))) for i, a in enumerate(cmd[:-1]) if a == "-m"}
        return [i for i in items if os.path.normcase(str(Path(i.dest))) in wanted]
    if service == "tts":
        if "--engine" in cmd and cmd[cmd.index("--engine") + 1:][:1] == ["fake"]:
            return []
        return [i for i in items if Path(i.dest).parent.name == "tts" and not design_only(i)]
    if service == "whisper":
        return [i for i in items if "whisper" in Path(i.dest).parts]
    return []


def damage_sentence(paths: Paths, item: DownloadItem, row: Row) -> str:
    """``The story model file is damaged (<path>, <name> (<size>)): <why>``."""
    from installer.stage2 import verify_downloads as vd

    if row.detail.startswith("size "):   # "size N bytes, expected M" / "..., the download was M bytes"
        why = f"its {row.detail}"
    else:
        why = "its SHA256 does not match the one Nightshift downloaded"
    return f"The {vd.role(item, paths)} file is damaged ({item.dest}, {vd.what(item)}): {why}"


class FileCheck:
    """The check after a failed load, once per file (path, size, date) per process; thread-safe."""

    def __init__(self, paths: Paths, *, app: Path | None = None, hasher: Callable[..., str] | None = None,
                 clock: Callable[[], float] = time.monotonic, bound_s: float = BOUND_S) -> None:
        self.paths, self.app, self.hasher, self.clock, self.bound_s = paths, app, hasher, clock, bound_s
        self._seen: dict[tuple[str, int, int], Row] = {}
        self._lock = threading.Lock()
        self.hashes = 0                 # how many files this process hashed

    def _app(self) -> Path:
        if self.app is not None:
            return self.app
        from ns_supervisor.control import app_path

        return app_path(self.paths)

    def after_failure(self, service: str, cmd: list[str]) -> Verdict:
        """Blocking (run it in a worker thread): check the files `service` loads. Never raises."""
        try:
            return self._check(service, cmd)
        except Exception as e:  # noqa: BLE001 - the caller's own error stands; this only adds evidence
            return Verdict(note=f"its model files could not be checked ({type(e).__name__}: {e})")

    def _check(self, service: str, cmd: list[str]) -> Verdict:
        from installer.stage2 import verify_downloads as vd

        try:
            items = vd.model_items(self.paths, self._app())
        except vd.AnswersError:
            return Verdict()
        notes: list[str] = []
        for item in files_for(service, cmd, items):
            row = self._row(item)
            name = Path(item.dest).name
            if row.status == vd.BAD:
                return Verdict(damaged=NsError(CODE, damage_sentence(self.paths, item, row)))
            if row.status in (vd.MISSING, vd.UNREADABLE):
                notes.append(f"{name}: {row.status} ({row.detail or 'not there'}). {row.fix}")
            elif row.status == vd.NO_PIN and item.size:    # a custom story model (whisper's side files: none)
                notes.append(f"{name}: its size is right; it has no pinned SHA256, so it cannot be checked "
                             "by hash")
            elif row.status == NOT_CHECKED:
                notes.append(row.detail)
        return Verdict(note="; ".join(notes))

    def _row(self, item: DownloadItem) -> Row:
        from installer.stage2 import verify_downloads as vd

        dest = Path(item.dest)
        try:
            st = dest.stat()
        except OSError:
            return vd.check_item(item, quick=True)          # not there: nothing to hash
        key = (str(dest), st.st_size, st.st_mtime_ns)
        with self._lock:
            known = self._seen.get(key)
        if known is not None:
            return known
        deadline = self.clock() + self.bound_s

        def progress(done: int, total: int | None) -> None:
            if self.clock() > deadline:
                raise _OutOfTime()

        t0 = self.clock()
        try:
            row = vd.check_item(item, hasher=self.hasher or vd.sha256_file, progress=progress)
        except _OutOfTime:
            return vd.Row(item.name, str(dest), NOT_CHECKED,
                          f"{dest.name} could not be checked by hash within {int(self.bound_s)} s: run "
                          "'ns verify --downloads' to check it")
        if row.hashed:
            self.hashes += 1
        try:
            vd.record(self.paths, item, row, self.clock() - t0)
        except OSError:
            pass                        # the verdict stands; only the record misses it
        if row.status != vd.UNREADABLE:
            with self._lock:
                self._seen[key] = row
        return row


def precheck(paths: Paths, app: Path | None = None, *, others: list[str] | None = None) -> list[str]:
    """A night's start: one sentence per model file a night's job loads (``night_files``) that cannot load
    (missing, the wrong size, or recorded damaged for its present size and date); the morning note adds the
    way out (Repair), a stopped download says that it continues. The same sentence about a file no night job
    loads (the design talker, the embed model, the face finder) goes to ``others``: it stops no night and is
    said beside it. No hash. Nothing when setup never verified the models (its models step is not done: a
    test home, an unfinished install)."""
    from installer.stage2 import verify_downloads as vd
    from installer.stage2.web import modelmarks
    from ns_common.config import load_install_state

    step = load_install_state(paths).steps.get("s03_download_models")
    if step is None or step.status != "done":
        return []                       # setup never verified this install's models: nothing to compare with
    if app is None:
        from ns_supervisor.control import app_path

        app = app_path(paths)
    try:
        items = vd.model_items(paths, app)
    except vd.AnswersError:
        return []
    damaged = modelmarks.recorded_damage(paths.home)
    tonight = {str(i.dest) for i in night_files(paths, items)}
    out: list[str] = []
    aside: list[str] = [] if others is None else others
    for item in items:
        row = vd.check_item(item, quick=True)
        found = damaged.get(modelmarks.key_of(paths.home, Path(item.dest)))
        said = out if str(item.dest) in tonight else aside
        if row.status == vd.BAD or (found is not None and row.status == vd.OK):
            recorded = found is not None and row.status == vd.OK
            when = f" (found at {found.get('checked_at')})" if found is not None and recorded else ""
            said.append(f"{CODE}: {damage_sentence(paths, item, row)}{when}.")
        elif row.status == vd.MISSING:      # a stopped download says that it continues; the rest is Repair
            said.append(f"The {vd.role(item, paths)} file {item.dest} is missing"
                        + (f" ({row.detail}). {row.fix}" if row.detail else "."))
    return out
