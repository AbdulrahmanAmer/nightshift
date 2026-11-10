from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import ns_common.testguard as testguard

# Folder under models/tts that holds the PyTorch (reference engine) safetensors checkouts. The installer
# downloads there (installer/stage2/fallback.py) and ns_tts.engine reads there: one constant, both sides.
TTS_HF_SUBDIR = "hf"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _same_folder(a: str | Path, b: str | Path) -> bool:
    try:
        if os.path.samefile(a, b):
            return True
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def home_of_place(program: Path | None = None, executable: str | None = None) -> Path | None:
    r"""The install this process belongs to, told from WHERE IT IS and from nothing else: the folder above
    its program folder when that is `<home>\app`, else the home of the venv its interpreter runs from
    (`<home>\venvs\<name>\Scripts`). None for a program that is in no install (a checkout run by a Python
    of its own; the update worker's private copy under `run`, which is handed its home).

    A folder counts as an install only when it holds `config/runtime.json` (what the launchers ask too), so
    a setup that is still making the folder is not told apart from its own answers.
    """
    app = repo_root() if program is None else Path(program)
    exe = Path(sys.executable if executable is None else executable)
    found: list[Path] = []
    if app.name.lower() == "app":
        found.append(app.parent)
    if exe.parent.name.lower() in ("scripts", "bin") and exe.parent.parent.parent.name.lower() == "venvs":
        found.append(exe.parent.parent.parent.parent)
    for home in found:
        try:
            if (home / "config" / "runtime.json").is_file():
                return home
        except OSError:
            continue
    return None


#: One line per process when the place and the variable disagree (`Paths.from_env`).
_said_place: list[str] = []


def _say_place(place: Path, named: str) -> None:
    if _said_place:
        return
    _said_place.append(named)
    try:
        sys.stderr.write(
            f"Nightshift: using the install in {place} (this program is in it); NIGHTSHIFT_HOME names "
            f"{named}, which is another folder.\n"
        )
    except (OSError, ValueError, AttributeError):       # no stderr (pythonw, a closed pipe): nothing to tell
        pass


def contracts_dir() -> Path:
    return repo_root() / "contracts"


@dataclass(frozen=True)
class Paths:
    home: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Paths:
        e = os.environ if env is None else env
        raw = e.get("NIGHTSHIFT_HOME")
        # A PC can hold several Nightshift folders and has ONE NIGHTSHIFT_HOME, which may name another of
        # them (card second-install-takeover). A program that is inside an install belongs to that install:
        # when the variable names a different folder the place wins, and one line says which was used. With
        # no variable nothing changes here (`ns_supervisor.homecheck` answers that case).
        place = home_of_place() if env is None and raw else None
        if place is not None and raw and not _same_folder(raw, place):
            _say_place(place, raw)
            home = place
        elif raw:
            home = Path(raw)
        else:
            local = e.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            home = Path(local) / "Nightshift"
        testguard.refuse_real_home(home)    # a test run never uses the owner's real home; no-op otherwise
        return cls(home)

    # --- directories ---
    @property
    def bin_llama(self) -> Path: return self.home / "bin" / "llama"
    @property
    def bin_opencode(self) -> Path: return self.home / "bin" / "opencode"
    @property
    def bin_uv(self) -> Path: return self.home / "bin" / "uv"
    @property
    def bin_qwentts(self) -> Path: return self.home / "bin" / "qwentts"
    @property
    def models_llm(self) -> Path: return self.home / "models" / "llm"
    @property
    def models_embed(self) -> Path: return self.home / "models" / "embed"
    @property
    def models_tts(self) -> Path: return self.home / "models" / "tts"
    @property
    def models_tts_voices(self) -> Path: return self.models_tts / "voices"
    @property
    def models_tts_hf(self) -> Path: return self.models_tts / TTS_HF_SUBDIR
    @property
    def models_whisper(self) -> Path: return self.home / "models" / "whisper"
    @property
    def models_spacy(self) -> Path: return self.home / "models" / "spacy"
    @property
    def models_cv(self) -> Path: return self.home / "models" / "cv"
    @property
    def venvs(self) -> Path: return self.home / "venvs"
    @property
    def config(self) -> Path: return self.home / "config"
    @property
    def channels_dir(self) -> Path: return self.config / "channels"
    @property
    def formats_dir(self) -> Path: return self.config / "formats"   # the owner's edits (Library > Formats)
    @property
    def data(self) -> Path: return self.home / "data"
    @property
    def backup_dir(self) -> Path: return self.data / "backup"
    @property
    def jobs_dir(self) -> Path: return self.data / "jobs"
    @property
    def calibration_dir(self) -> Path: return self.data / "calibration"
    @property
    def cache_clips(self) -> Path: return self.home / "cache" / "clips"
    @property
    def cache_search(self) -> Path: return self.home / "cache" / "search"
    @property
    def cache_tts_tmp(self) -> Path: return self.home / "cache" / "tts-tmp"   # scratch for the voice engine
    @property
    def imports_analytics(self) -> Path: return self.home / "imports" / "analytics"
    @property
    def outbox(self) -> Path: return self.home / "outbox"
    @property
    def rejected(self) -> Path: return self.home / "rejected"
    @property
    def exemplars(self) -> Path: return self.home / "exemplars"            # the owner's own example stories
    @property
    def prompt_overrides(self) -> Path: return self.home / "prompt_overrides"  # win over ours
    @property
    def logs(self) -> Path: return self.home / "logs"

    # --- files ---
    @property
    def runtime_file(self) -> Path: return self.config / "runtime.json"
    @property
    def install_state_file(self) -> Path: return self.config / "install_state.json"
    @property
    def opencode_file(self) -> Path: return self.config / "opencode.json"
    @property
    def db_path(self) -> Path: return self.data / "studio.db"
    @property
    def assistant_db(self) -> Path: return self.data / "assistant.db"

    def channel_file(self, slug: str) -> Path:
        return self.channels_dir / f"{slug}.yaml"

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def rejected_dir(self, slug: str, date: str, job_id: str) -> Path:
        return self.rejected / slug / date / job_id

    def all_dirs(self) -> list[Path]:
        return [
            self.bin_llama, self.bin_opencode, self.bin_uv, self.bin_qwentts,
            self.models_llm, self.models_embed, self.models_tts, self.models_tts_voices,
            self.models_whisper, self.models_spacy, self.models_cv, self.venvs, self.config,
            self.channels_dir, self.data, self.backup_dir, self.jobs_dir, self.calibration_dir,
            self.cache_clips, self.cache_search, self.imports_analytics, self.outbox, self.rejected,
            self.exemplars, self.prompt_overrides, self.logs,
        ]

    def ensure(self) -> None:
        for d in self.all_dirs():
            d.mkdir(parents=True, exist_ok=True)
