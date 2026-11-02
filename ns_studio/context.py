"""Job context and service wiring for the studio (component C7)."""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ns_common.acquire import Acquirer, DevAcquirer
from ns_common.db import Db
from ns_common.embed_client import Embed, EmbedClient, FakeEmbed
from ns_common.llm_client import FakeLlm, Llm, LlmClient
from ns_common.logging import NsLogger
from ns_common.models import Channel, Combo, Runtime
from ns_common.paths import Paths, repo_root
from ns_common.tts_client import FakeTts, Tts, TtsClient
from ns_studio.acquirer import StudioAcquirer, wants_supervisor

if TYPE_CHECKING:
    from ns_studio.media.clip_sources import ClipSource
    from ns_studio.media.transcribe import Transcriber

PROMPT_VERSIONS_FILE = "prompt_versions.json"
FAKEABLE: frozenset[str] = frozenset({"llm", "tts", "whisper", "embed", "clips"})
FIXTURES_LLM = repo_root() / "tests" / "fixtures" / "llm"


@dataclass
class Services:
    """Every collaborator a stage may call, selected by `build_services`."""

    llm: Llm
    tts: Tts
    embed: Embed
    transcriber: Transcriber
    clip_sources: list[ClipSource]
    acquirer: Acquirer


def build_services(
    runtime: Runtime,
    *,
    fake: Collection[str],
    paths: Paths,
    keys: dict[str, str] | None = None,
    llm_fixtures: Path | None = None,
    acquirer: Acquirer | None = None,
) -> Services:
    """Build the service bundle; names in `fake` (subset of FAKEABLE) get in-process fakes.

    Real clip sources use `keys["pexels"]` and `keys["pixabay"]` (empty when absent; the sources
    themselves raise a coded error on first use).

    `acquirer` defaults to the supervisor-backed `StudioAcquirer` when no model service is faked (it connects
    on first use, so building the bundle never contacts anything) and to `DevAcquirer` when any of llm, tts,
    whisper or embed is faked: the in-process fakes need no process, and the dev acquirer only hands out the
    configured ports.
    """
    from ns_studio.media.clip_sources import FakeClipSource, PexelsSource, PixabaySource
    from ns_studio.media.transcribe import FakeTranscriber, WhisperTranscriber

    keys = keys or {}
    llm: Llm = (
        FakeLlm(llm_fixtures or FIXTURES_LLM) if "llm" in fake else LlmClient(runtime.llama.base_url, "story")
    )
    tts: Tts = FakeTts() if "tts" in fake else TtsClient(runtime.tts.base_url)
    embed: Embed = FakeEmbed() if "embed" in fake else EmbedClient(runtime.embed.base_url)
    transcriber: Transcriber = (
        FakeTranscriber() if "whisper" in fake else WhisperTranscriber(runtime.whisper, paths)
    )
    sources: list[ClipSource]
    if "clips" in fake:
        sources = [FakeClipSource(paths.cache_clips / "fake")]
    else:
        sources = [
            PexelsSource(keys.get("pexels", ""), paths.cache_search),
            PixabaySource(keys.get("pixabay", ""), paths.cache_search),
        ]
    return Services(
        llm=llm,
        tts=tts,
        embed=embed,
        transcriber=transcriber,
        clip_sources=sources,
        acquirer=acquirer
        if acquirer is not None
        else (StudioAcquirer(runtime, paths) if wants_supervisor(set(fake)) else DevAcquirer(runtime)),
    )


@dataclass
class JobContext:
    """Everything a stage needs: identity, paths, DB, services and the job directory helpers."""

    job_id: str
    channel: Channel
    combo: Combo
    fmt: Literal["master_first", "shorts_only", "series_compilation", "clip", "master_from_script"]
    paths: Paths
    job_dir: Path
    db: Db
    runtime: Runtime
    services: Services
    log: NsLogger
    prompt_versions: dict[str, int] = field(default_factory=dict)

    def _saved_prompt_versions(self) -> dict[str, int]:
        f = self.job_dir / PROMPT_VERSIONS_FILE
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): int(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def note_prompt(self, name: str, version: int) -> None:
        """Remember that prompt `name` at `version` produced output for this job, in memory and on disk.

        `prompt_versions.json` keeps the lineage across `--resume` and regeneration, when the process that
        called the earlier prompts is gone.
        """
        saved = self._saved_prompt_versions()
        self.prompt_versions[name] = version
        if saved.get(name) != version:
            self.write_json(PROMPT_VERSIONS_FILE, {**saved, **self.prompt_versions})

    def versions(self) -> dict[str, int]:
        """Every prompt version used by this job so far: the saved ones plus this process's."""
        return {**self._saved_prompt_versions(), **self.prompt_versions}

    @property
    def series(self) -> dict[str, Any]:
        """`series_context.json` of a series part (title, part numbers, end card, design); empty otherwise."""
        f = self.job_dir / "series_context.json"
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @property
    def attempt(self) -> int:
        """How many times the policy gate has already sent this job back (0 on the first pass)."""
        f = self.job_dir / "policy_attempts.json"
        try:
            return int(json.loads(f.read_text(encoding="utf-8")).get("regenerations", 0))
        except (OSError, ValueError, AttributeError):
            return 0

    def file(self, name: str) -> Path:
        """Path of `name` inside the job directory."""
        return self.job_dir / name

    def read_json(self, name: str) -> Any:
        """Read and parse a JSON file from the job directory."""
        return json.loads(self.file(name).read_text(encoding="utf-8"))

    def write_json(self, name: str, data: Any) -> Path:
        """Write `data` as pretty JSON into the job directory and return the path."""
        p = self.file(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return p

    def subdir(self, name: str) -> Path:
        """Create (if needed) and return a subdirectory of the job directory."""
        d = self.job_dir / name
        d.mkdir(parents=True, exist_ok=True)
        return d
