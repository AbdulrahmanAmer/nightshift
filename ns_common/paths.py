from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def contracts_dir() -> Path:
    return repo_root() / "contracts"


@dataclass(frozen=True)
class Paths:
    home: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Paths:
        e = os.environ if env is None else env
        raw = e.get("NIGHTSHIFT_HOME")
        if raw:
            return cls(Path(raw))
        local = e.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return cls(Path(local) / "Nightshift")

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
    def imports_analytics(self) -> Path: return self.home / "imports" / "analytics"
    @property
    def outbox(self) -> Path: return self.home / "outbox"
    @property
    def rejected(self) -> Path: return self.home / "rejected"
    @property
    def stories(self) -> Path: return self.home / "stories"
    @property
    def exemplars(self) -> Path: return self.home / "exemplars"            # the owner's own example stories
    @property
    def prompt_overrides(self) -> Path: return self.home / "prompt_overrides"  # prompt files that win over ours
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
    @property
    def clips_index(self) -> Path: return self.cache_clips / "index.sqlite"

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
            self.stories, self.logs,
        ]

    def ensure(self) -> None:
        for d in self.all_dirs():
            d.mkdir(parents=True, exist_ok=True)
