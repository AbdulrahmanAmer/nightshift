"""Adapters between the dashboard and collaborators owned by other components (component C9).

Every real adapter imports its collaborator lazily and raises `AdapterUnavailable` naming the missing
symbol, so the dashboard starts (and every other page works) before C2, C7 or C8 have landed.
Fakes that write real files live in `ns_dashboard.fakes`.
"""

from __future__ import annotations

import importlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ns_common.config import load_runtime
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_dashboard.errors import AdapterUnavailable
from ns_dashboard.jobs import JobControl
from ns_dashboard.supervisor_link import SupervisorLink, make_acquirer
from ns_dashboard.updater import RealUpdateAdapter

KEYRING_SERVICE = "Nightshift"
KEY_NAMES = ("pexels_api_key", "pixabay_api_key")
PRESET_VOICES = [{"name": "Ryan", "mode": "preset", "source": "builtin"}]


class RunAdapter(Protocol):
    """Runs one studio job (spec 15.9 `studio run`) and returns the new output ids."""

    def run(self, ctl: JobControl, channel: str, fmt: str, count: int | None) -> list[str]: ...


class ClipAdapter(Protocol):
    """Runs the Clipper pipeline (spec 19.2) for one source video and returns the new output ids."""

    def clip(
        self, ctl: JobControl, source_path: Path, source_id: str, params: dict[str, Any]
    ) -> list[str]: ...

    def fetch_url(self, ctl: JobControl, url: str, source_id: str) -> Path:
        """Download a video from an http(s) link (C8 `import_url`); reports `ctl.download(percent)`."""
        ...


class LearnAdapter(Protocol):
    """Imports a YouTube Analytics CSV and summarises what the weights learned."""

    def import_csv(self, ctl: JobControl, csv_path: Path) -> dict[str, Any]: ...

    def summary(self) -> dict[str, Any]: ...


class VerifyAdapter(Protocol):
    """`ns verify` on demand."""

    def verify(self, ctl: JobControl, quick: bool) -> dict[str, Any]: ...


class DoctorAdapter(Protocol):
    """`ns doctor --bundle`: returns the path of the diagnostics zip."""

    def bundle(self) -> Path: ...


class KeyringAdapter(Protocol):
    """Windows Credential Manager access for the stock-footage keys."""

    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...


class TtsPreviewAdapter(Protocol):
    """Voice list and short audio previews."""

    def voices(self) -> tuple[list[dict[str, Any]], bool]: ...

    def preview(self, voice: str, text: str, speed: float) -> bytes: ...


class FolderOpener(Protocol):
    """Opens a folder in Explorer."""

    def open(self, path: Path) -> None: ...


class UpdateAdapter(Protocol):
    """GitHub update check and apply (`ns update`)."""

    def check(self) -> dict[str, Any]: ...

    def apply(self) -> dict[str, Any]: ...


class ScheduleAdapter(Protocol):
    """Applies the nightly run time to the scheduled task."""

    def apply(self, run_time: str) -> dict[str, Any]: ...


@dataclass
class Adapters:
    """The ten collaborators the service layer and the job handlers use."""

    run: RunAdapter
    clip: ClipAdapter
    learn: LearnAdapter
    verify: VerifyAdapter
    doctor: DoctorAdapter
    keyring: KeyringAdapter
    tts: TtsPreviewAdapter
    folder: FolderOpener
    schedule: ScheduleAdapter
    update: UpdateAdapter


def lazy(module: str, attr: str) -> Callable[..., Any]:
    """Resolve `module.attr` or raise `AdapterUnavailable` with a message naming it."""
    try:
        mod = importlib.import_module(module)
        return getattr(mod, attr)
    except (ImportError, AttributeError) as e:
        raise AdapterUnavailable(f"{module}.{attr} is not installed yet ({e})") from e


def stage_reporter(ctl: JobControl) -> Callable[[str, int, int], None]:
    """`on_stage(name, index, total)` callback: checks cancel, then records progress.

    Raising `JobCancelled` from the callback unwinds the orchestrator between stages.
    """

    def on_stage(name: str, index: int, total: int) -> None:
        ctl.check_cancel()
        ctl.progress(index / max(1, total), f"{name} ({index + 1}/{total})")

    return on_stage


class RealRunAdapter:
    """Expects `ns_studio.orchestrator.run_job_with_progress` (C7)."""

    def __init__(self, paths: Paths, link: SupervisorLink) -> None:
        self.paths = paths
        self.link = link

    def run(self, ctl: JobControl, channel: str, fmt: str, count: int | None) -> list[str]:
        """Run one studio job; progress per stage, tenants acquired through the supervisor."""
        fn = lazy("ns_studio.orchestrator", "run_job_with_progress")
        ids = fn(
            paths=self.paths,
            channel=channel,
            fmt=fmt,
            count=count,
            on_stage=stage_reporter(ctl),
            acquirer=make_acquirer(self.link.base_url),
        )
        return [str(i) for i in ids]


class RealClipAdapter:
    """Expects `ns_studio.clipper.run_clip_job` (C8)."""

    def __init__(self, paths: Paths, link: SupervisorLink) -> None:
        self.paths = paths
        self.link = link

    def clip(self, ctl: JobControl, source_path: Path, source_id: str, params: dict[str, Any]) -> list[str]:
        """Run the Clipper for `source_path`; returns the output ids it inserted."""
        fn = lazy("ns_studio.clipper", "run_clip_job")
        ids = fn(
            paths=self.paths,
            source_path=source_path,
            source_id=source_id,
            params=params,
            on_stage=stage_reporter(ctl),
            acquirer=make_acquirer(self.link.base_url),
        )
        return [str(i) for i in ids]

    def fetch_url(self, ctl: JobControl, url: str, source_id: str) -> Path:
        """Download `url` into `imports/clipper/<source_id>/` with C8's `import_url`, as `Downloading N%`.

        Expects `ns_studio.clipper.import_url(paths=, url=, source_id=, on_progress=) -> Path`, where
        `on_progress(percent)` may raise to cancel.
        """
        try:
            fn = lazy("ns_studio.clipper", "import_url")
        except AdapterUnavailable as e:
            raise AdapterUnavailable("URL import not available yet") from e
        return Path(fn(paths=self.paths, url=url, source_id=source_id, on_progress=ctl.download))


class RealLearnAdapter:
    """Expects `ns_studio.learn.import_csv` and `ns_studio.learn.summary` (C7)."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths

    def import_csv(self, ctl: JobControl, csv_path: Path) -> dict[str, Any]:
        """Import `csv_path`; returns the import summary."""
        fn = lazy("ns_studio.learn", "import_csv")
        ctl.progress(0.1, "Reading the analytics file")
        out = fn(self.paths, csv_path)
        return out if isinstance(out, dict) else {"result": out}

    def summary(self) -> dict[str, Any]:
        """Summary of learned weights."""
        fn = lazy("ns_studio.learn", "summary")
        out = fn(self.paths)
        return out if isinstance(out, dict) else {}


class RealVerifyAdapter:
    """Expects `ns_supervisor.verify.run_verify` (C2)."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths

    def verify(self, ctl: JobControl, quick: bool) -> dict[str, Any]:
        """Run `ns verify`; returns `{checks: [...]}`."""
        fn = lazy("ns_supervisor.verify", "run_verify")
        out = fn(paths=self.paths, quick=quick, on_stage=stage_reporter(ctl))
        return out if isinstance(out, dict) else {"checks": out}


class RealDoctorAdapter:
    """Calls C2's doctor bundle in-process (`ns_supervisor.doctor.make_bundle(paths) -> Path`)."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths

    def bundle(self) -> Path:
        """Create the diagnostics zip and return its path."""
        fn = lazy("ns_supervisor.doctor", "make_bundle")
        return Path(fn(self.paths))


class RealKeyringAdapter:
    """Windows Credential Manager through the `keyring` package (service `Nightshift`)."""

    def _module(self) -> Any:
        try:
            return importlib.import_module("keyring")
        except ImportError as e:
            raise AdapterUnavailable("the keyring package is not installed") from e

    def get(self, name: str) -> str | None:
        """Stored secret or None."""
        return self._module().get_password(KEYRING_SERVICE, name)

    def set(self, name: str, value: str) -> None:
        """Store or replace a secret."""
        self._module().set_password(KEYRING_SERVICE, name, value)


def scan_voice_library(paths: Paths) -> list[dict[str, Any]]:
    """Voices found on disk: `models/tts/voices/<name>/voice.json` plus the built-in preset."""
    out = list(PRESET_VOICES)
    root = paths.models_tts_voices
    if root.exists():
        for meta in sorted(root.glob("*/voice.json")):
            try:
                raw = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
            mode = raw.get("mode") if isinstance(raw, dict) else None
            out.append(
                {"name": meta.parent.name, "mode": mode or "clone", "source": "library"}
            )
    return out


class RealTtsAdapter:
    """Lists voices from the running TTS service (library scan when it is off) and renders previews."""

    def __init__(self, paths: Paths, link: SupervisorLink) -> None:
        self.paths = paths
        self.link = link

    def _base_url(self) -> str:
        try:
            return load_runtime(self.paths).tts.base_url
        except Exception:  # missing or invalid runtime.json: fall back to the default port
            return "http://127.0.0.1:8091"

    def voices(self) -> tuple[list[dict[str, Any]], bool]:
        """(voices, live) where live says the TTS service answered."""
        from ns_common.tts_client import TtsClient

        client = TtsClient(self._base_url(), timeout_s=3)
        try:
            if client.health():
                return client.voices(), True
        except Exception:  # service not running: not an error for the voice picker
            pass
        return scan_voice_library(self.paths), False

    def preview(self, voice: str, text: str, speed: float) -> bytes:
        """Acquire the TTS service, speak `text`, release, return WAV bytes."""
        from ns_common.tts_client import TtsClient

        acquirer = make_acquirer(self.link.base_url)
        owner = f"dashboard:preview-{int(time.time())}"
        endpoint = acquirer.acquire("tts", owner, timeout_s=300)
        try:
            out = self.paths.cache_search.parent / "dashboard" / "voice-preview.wav"
            out.parent.mkdir(parents=True, exist_ok=True)
            TtsClient(endpoint.base_url, timeout_s=300).speak(text, voice, speed, out)
            return out.read_bytes()
        finally:
            acquirer.release("tts", owner)


class RealFolderOpener:
    """`os.startfile` (Windows Explorer)."""

    def open(self, path: Path) -> None:
        """Open `path` (a folder) in Explorer."""
        starter = getattr(os, "startfile", None)
        if starter is None:
            raise AdapterUnavailable("opening folders needs Windows")
        starter(str(path))


class RealScheduleAdapter:
    """Applies the run time to the scheduled task when C2 provides `ns_supervisor.scheduler`."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths

    def apply(self, run_time: str) -> dict[str, Any]:
        """Update the 'Nightshift Daily' trigger; reports `applied: False` while C2 has no hook."""
        try:
            fn = lazy("ns_supervisor.scheduler", "set_daily_time")
        except AdapterUnavailable:
            return {
                "applied": False,
                "note": "Saved. The scheduled task picks the new time up after the next update.",
            }
        fn(self.paths, run_time)
        return {"applied": True, "note": "Scheduled task updated."}


def real_adapters(paths: Paths, link: SupervisorLink, log: NsLogger) -> Adapters:
    """Adapters wired to the real collaborators (each fails softly when its component is missing)."""
    log.debug("adapters_real")
    return Adapters(
        run=RealRunAdapter(paths, link),
        clip=RealClipAdapter(paths, link),
        learn=RealLearnAdapter(paths),
        verify=RealVerifyAdapter(paths),
        doctor=RealDoctorAdapter(paths),
        keyring=RealKeyringAdapter(),
        tts=RealTtsAdapter(paths, link),
        folder=RealFolderOpener(),
        schedule=RealScheduleAdapter(paths),
        update=RealUpdateAdapter(paths),
    )
