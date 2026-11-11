from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
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


HOME_ENV = "NIGHTSHIFT_HOME"
#: Set beside NIGHTSHIFT_HOME, to the same folder, by a starter that hands its child a home on purpose and
#: whose child may be a program of ANOTHER install (setup's test render in a scratch home, `dev/prove.py`,
#: `ns update --home`). It counts only while it names the folder NIGHTSHIFT_HOME names, so a tree that
#: inherited it and then got another NIGHTSHIFT_HOME is not vouched for by a stale one.
EXPLICIT_ENV = "NS_HOME_EXPLICIT"
#: Under a test run only: what stands for the user-level NIGHTSHIFT_HOME (the registry is not read there).
TEST_USER_HOME_ENV = "NS_TEST_USER_HOME"


def home_env(home: str | Path) -> dict[str, str]:
    """The two variables a starter puts into a child's environment (or its own, `ns update --home`) to say
    "work on THIS home, wherever your program is": NIGHTSHIFT_HOME and the marker that vouches for it."""
    return {HOME_ENV: str(home), EXPLICIT_ENV: str(home)}


#: The user-level variable, read at most once per process: (could it be read, its value or None).
_user_variable: list[tuple[bool, str | None]] = []


def _read_user_variable() -> tuple[bool, str | None]:
    """NIGHTSHIFT_HOME as the user's account has it (HKCU, Environment), read only. (True, None): not set
    (also off Windows, where there is no such level). (False, None): Windows did not say."""
    if sys.platform != "win32":
        return True, None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:     # opened for reading
            value, kind = winreg.QueryValueEx(key, HOME_ENV)
            if isinstance(value, str) and kind == winreg.REG_EXPAND_SZ:
                value = winreg.ExpandEnvironmentStrings(value)
    except FileNotFoundError:
        return True, None                   # no such value: the variable is not set for this user
    except (ImportError, OSError, ValueError):
        return False, None
    if not isinstance(value, str):
        return False, None
    return True, value or None


def _user_variable_once(read: Callable[[], tuple[bool, str | None]]) -> tuple[bool, str | None]:
    if not _user_variable:
        _user_variable.append(read())
    return _user_variable[0]


def user_home_variable() -> tuple[bool, str | None]:
    """(known, value) of the user-level NIGHTSHIFT_HOME. A normal process reads the registry once and keeps
    the answer. A test run never reads it: the answer is `NS_TEST_USER_HOME` (unset: "not set"), so a test
    says what the PC's default is, and a home a test set with `monkeypatch.setenv` is its own."""
    if testguard.under_test():
        return True, os.environ.get(TEST_USER_HOME_ENV) or None
    return _user_variable_once(_read_user_variable)


#: Why a NIGHTSHIFT_HOME that names another folder than the program's own install is, or is not, followed.
VOUCHED, SET_HERE, DEFAULT, UNKNOWN = "vouched", "set-here", "default", "unknown"


def home_variable_kind(raw: str, env: Mapping[str, str]) -> str:
    """Was this process's NIGHTSHIFT_HOME (`raw`) set FOR this process, or only inherited from the user?

    - `VOUCHED`: the starter set the marker `NS_HOME_EXPLICIT` to the same folder.
    - `SET_HERE`: it differs from the user-level variable (which may be unset), so something between the
      user's account and this process set it: `$env:NIGHTSHIFT_HOME = ...` in a terminal, a launcher, the
      nightly task's action, setup, a parent. A window that was opened before the user-level variable
      changed looks the same and is followed as well (as every release before did): the line says so.
    - `DEFAULT`: it equals the user-level variable: the PC's one default, which may name another install.
    - `UNKNOWN`: Windows did not say what the user-level variable is.
    """
    marker = env.get(EXPLICIT_ENV)
    if marker and _same_folder(marker, raw):
        return VOUCHED
    known, user = user_home_variable()
    if not known:
        return UNKNOWN
    return DEFAULT if user and _same_folder(user, raw) else SET_HERE


#: One line per process when the place and the variable disagree (`Paths.from_env`).
_said_place: list[str] = []


def _say_place(place: Path, named: str, kind: str) -> None:
    if _said_place or kind == VOUCHED:      # a starter that vouched knows what it asked for
        return
    _said_place.append(named)
    if kind == SET_HERE:
        line = (
            f"Nightshift: using {named}: NIGHTSHIFT_HOME names it in this window (it is not this PC's "
            f"default), and this program is in the install at {place}. If you did not set it, this window "
            "was opened before the default changed: open a new one."
        )
        from ns_common import homeguard  # here, not at the top: homeguard imports this module

        versions = homeguard.notice(Path(named))    # "" unless that install is another version
        if versions:
            line += (
                f" {versions}: commands that change that install are refused from this program (NS-E128); "
                "looking still works."
            )
    else:
        why = (
            "which is this PC's default and was not set in this window" if kind == DEFAULT
            else "and Windows did not say whether that was set in this window"
        )
        line = (
            f"Nightshift: using the install in {place} (this program is in it); NIGHTSHIFT_HOME names "
            f"{named}, {why}. To work on that folder, start its own shortcuts or programs."
        )
    try:
        sys.stderr.write(line + "\n")
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
        raw = e.get(HOME_ENV)
        # A PC can hold several Nightshift folders and has ONE user-level NIGHTSHIFT_HOME, which may name
        # another of them (card second-install-takeover). The rule, for this process's own environment:
        #   1. a NIGHTSHIFT_HOME that was set FOR this process is followed, wherever the program is
        #      (`home_variable_kind`: vouched for by its starter, or different from the user-level variable);
        #   2. one that is only the user-level default gives way to the install the program is in
        #      (`home_of_place`), and so does one Windows cannot tell apart;
        #   3. a program in no install follows the variable, and with no variable nothing changes here
        #      (`ns_supervisor.homecheck` answers that case).
        # One line on stderr, once per process, whenever the folder used and the program's install differ
        # and no starter vouched for it. An `env` handed in is somebody else's process: never second-guessed.
        place = home_of_place() if env is None and raw else None
        if place is not None and raw and not _same_folder(raw, place):
            kind = home_variable_kind(raw, e)
            _say_place(place, raw, kind)
            home = Path(raw) if kind in (VOUCHED, SET_HERE) else place
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
