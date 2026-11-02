"""Adapters between the dashboard and collaborators owned by other components (component C9).

Every real adapter imports its collaborator lazily and raises `AdapterUnavailable` naming the missing
symbol, so the dashboard starts (and every other page works) before C2, C7 or C8 have landed.
Fakes that write real files live in `ns_dashboard.fakes`.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import time
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from ns_common.acquire import Acquirer, Service
from ns_common.config import load_runtime
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_dashboard import studio_bridge
from ns_dashboard.errors import AdapterUnavailable, ApiError
from ns_dashboard.jobs import JobControl
from ns_dashboard.supervisor_link import SupervisorLink, make_acquirer, supervisor_url
from ns_dashboard.updater import RealBackupAdapter, RealUpdateAdapter

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

    def rerun(self, ctl: JobControl, studio_job_id: str, changes: dict[str, Any]) -> list[str]:
        """Run the later stages of an earlier clip job again with other options (transcript kept)."""
        ...

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


@dataclass(frozen=True)
class MadeVoice:
    """A voice that now exists in the Library bank, with the rendered test line next to it."""

    voice_id: str
    preview_wav_path: str


class VoiceMakerAdapter(Protocol):
    """Designs or clones a narration voice and renders a preview of `test_line` (CR-C3-UI-7).

    `sample_wav` is a checked WAV (3-30 s, see `ns_tts.sample`). Both calls block until the voice and its
    `preview.wav` exist under `models/tts/voices/<name>/`.
    """

    def design(self, name: str, description: str, test_line: str) -> MadeVoice: ...

    def clone(self, name: str, sample_wav: Path, test_line: str) -> MadeVoice: ...


class FolderOpener(Protocol):
    """Opens a folder in Explorer."""

    def open(self, path: Path) -> None: ...


class UpdateAdapter(Protocol):
    """GitHub update check and apply (`ns update`)."""

    def check(self) -> dict[str, Any]: ...

    def apply(self) -> dict[str, Any]: ...


class SeriesAdapter(Protocol):
    """Series Workshop production: runs one approved part as a studio job (kind `series_part`)."""

    def produce(self, ctl: JobControl, params: dict[str, Any]) -> list[str]: ...

    def listing(self) -> list[dict[str, Any]]:
        """Every workshop story with its parts and next step (read-only)."""
        ...

    def queue_next(self, slug: str) -> list[dict[str, Any]]:
        """Queue the next approved part of `slug`: `[{part, job_id, title}]`; 409 when none is ready."""
        ...


class BackupAdapter(Protocol):
    """Export and import of settings (`ns backup`, `ns restore`) and where UNINSTALL.cmd is."""

    def export(self) -> Path:
        """Write the backup zip and return its path."""
        ...

    def restore(self, zip_path: Path) -> list[str]:
        """Restore a backup zip; returns the lines the command printed."""
        ...

    def uninstall_path(self) -> Path:
        """Full path of UNINSTALL.cmd."""
        ...


class ScheduleAdapter(Protocol):
    """Applies the nightly run time to the scheduled task."""

    def apply(self, run_time: str) -> dict[str, Any]: ...


@dataclass
class Adapters:
    """The thirteen collaborators the service layer and the job handlers use."""

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
    voice_maker: VoiceMakerAdapter
    series: SeriesAdapter
    backup: BackupAdapter


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


def job_reporter(ctl: JobControl) -> studio_bridge.Report:
    """`report(stage, index, total, job_index, job_total)` for the studio bridge: cancel check, then progress.

    Progress is the share of finished stages across all jobs of the request, shown as `name (i/n)`.
    """

    def report(stage: str, index: int, total: int, job_index: int, job_total: int) -> None:
        ctl.check_cancel()
        done = job_index + index / max(1, total)
        ctl.progress(done / max(1, job_total), f"{stage} ({index + 1}/{total})")

    return report


class StudioAdapter:
    """Shared wiring of the adapters that run studio code in-process: home, link, fakes, GPU owner."""

    def __init__(
        self,
        paths: Paths,
        link: SupervisorLink,
        *,
        fake: Collection[str] = (),
        acquirer: Acquirer | None = None,
    ) -> None:
        self.paths = paths
        self.link = link
        self.fake = frozenset(fake)
        self._acquirer = acquirer

    def acquirer(self) -> Acquirer:
        """The supervisor-backed acquirer (tests inject their own)."""
        return self._acquirer or make_acquirer(self.link.base_url)


class RealRunAdapter(StudioAdapter):
    """Runs `studio run` jobs in-process through `ns_dashboard.studio_bridge.run_story_jobs` (C7).

    The bridge calls `ns_studio.jobs` and `run_job_guarded`; progress comes from the runner's log events,
    and the GPU tenants are acquired through the supervisor.
    """

    def run(self, ctl: JobControl, channel: str, fmt: str, count: int | None) -> list[str]:
        """Run `count` studio jobs for `channel`; returns the output ids and notes the studio job ids."""
        seen: list[str] = []
        ids = studio_bridge.run_story_jobs(
            channel,
            fmt,
            count,
            job_reporter(ctl),
            acquirer=self.acquirer(),
            fake=self.fake,
            paths=self.paths,
            seen=seen,
        )
        ctl.extras["studio_job_ids"] = seen
        return ids


class RealClipAdapter(StudioAdapter):
    """Runs the Clipper in-process: the clip job's stages (`studio clip`) and `import_url` for links."""

    def clip(self, ctl: JobControl, source_path: Path, source_id: str, params: dict[str, Any]) -> list[str]:
        """Run the Clipper for `source_path`; returns the output ids it inserted."""
        seen: list[str] = []
        ids = studio_bridge.run_clip(
            source_path,
            {**params, "channel": params.get("channel") or ctl.channel},
            job_reporter(ctl),
            acquirer=self.acquirer(),
            fake=self.fake,
            paths=self.paths,
            seen=seen,
        )
        ctl.extras["studio_job_id"] = seen[0] if seen else None
        return ids

    def rerun(self, ctl: JobControl, studio_job_id: str, changes: dict[str, Any]) -> list[str]:
        """Run the later stages of `studio_job_id` again with other options; keeps the transcript."""
        ids = studio_bridge.rerun_clip(
            studio_job_id,
            changes,
            job_reporter(ctl),
            acquirer=self.acquirer(),
            fake=self.fake,
            paths=self.paths,
        )
        ctl.extras["studio_job_id"] = studio_job_id
        return ids

    def fetch_url(self, ctl: JobControl, url: str, source_id: str) -> Path:
        """Download `url` with the pinned yt-dlp (`clipper.source.import_url`), shown as `Downloading N%`."""
        return studio_bridge.download_source(self.paths, url, source_id, ctl.download)


class RealSeriesAdapter(StudioAdapter):
    """Produces a series part: the job `Workshop.produce` prepared, run with the `master_from_script` plan."""

    def produce(self, ctl: JobControl, params: dict[str, Any]) -> list[str]:
        """Run the part's job; a failed or cancelled part goes back to `approved` to be queued again."""
        try:
            return studio_bridge.run_series_part(
                {**params, "job_id": ctl.job_id},
                job_reporter(ctl),
                acquirer=self.acquirer(),
                fake=self.fake,
                paths=self.paths,
            )
        except BaseException:
            studio_bridge.release_part({**params, "job_id": ctl.job_id})
            raise

    def listing(self) -> list[dict[str, Any]]:
        """Stories and parts read from the workshop files under `stories/`."""
        return studio_bridge.list_series()

    def queue_next(self, slug: str) -> list[dict[str, Any]]:
        """Queue the next approved part (`Workshop.produce`); workshop usage errors become 409/404 answers."""
        from ns_studio.series.store import WorkshopError

        try:
            return studio_bridge.queue_next_part(slug)
        except WorkshopError as e:
            missing = str(e).startswith("no story")
            code = "not_found" if missing else "cannot_produce"
            raise ApiError(404 if missing else 409, code, str(e)) from e


class RealLearnAdapter:
    """Imports an analytics CSV (`learn.importer.read_csv`, `import_rows`) and reweights the pools."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths

    def import_csv(self, ctl: JobControl, csv_path: Path) -> dict[str, Any]:
        """Match the rows to uploads, store them, update `pool_weights`; returns the plain-English counts."""
        from ns_common.db import Db
        from ns_studio.channels.crud import get_channel
        from ns_studio.errors import StudioError
        from ns_studio.learn.importer import import_rows, read_csv
        from ns_studio.learn.reweight import reweight, save_snapshot

        ctl.progress(0.1, "Reading the analytics file")
        db = Db(self.paths.db_path)
        db.migrate()
        try:
            try:
                rows = read_csv(csv_path)
                ctl.progress(0.4, "Matching videos")
                result = import_rows(db, rows, datetime.now(UTC).date().isoformat())
            except StudioError as e:  # NS-E323 and friends: show the code and the fix hint like the CLI
                raise NsError(e.code, e.detail) from e
            ctl.progress(0.7, "Updating what Nightshift learns")
            changes: list[dict[str, Any]] = []
            for slug in sorted(set(result.matched.values())):
                moved = reweight(db, get_channel(self.paths, slug))
                save_snapshot(self.paths, slug, moved)
                for pool, options in moved.items():
                    changes.extend(
                        {"pool": pool, "option": o, "delta": new - old} for o, (old, new) in options.items()
                    )
        finally:
            db.close()
        return {
            "rows": len(rows),
            "matched": len(result.matched),
            "unmatched": [r.title or r.video_id or r.url for r in result.unmatched],
            "changes": changes,
        }

    def summary(self) -> dict[str, Any]:
        """Nothing extra: the Results page computes the summary from `pool_weights` (SettingsService)."""
        return {}


class RealVerifyAdapter:
    """`ns verify` in-process: `run_verify(paths, rt, controller, quick=, deps=)`, then the report file."""

    def __init__(
        self, paths: Paths, link: SupervisorLink | None = None, *, controller: Any = None, deps: Any = None
    ) -> None:
        self.paths = paths
        self.link = link
        self.controller = controller  # tests pass a stub supervisor
        self.deps = deps  # tests pass fake machine probes

    def _controller(self) -> Any:
        if self.controller is not None:
            return self.controller
        client = lazy("ns_supervisor.client", "SupervisorClient")
        url = self.link.base_url if self.link is not None else supervisor_url(self.paths)
        return client(url)

    def verify(self, ctl: JobControl, quick: bool) -> dict[str, Any]:
        """Run the checks and write `logs\verify-report.json`; returns the checks, benchmarks and verdict."""
        run = lazy("ns_supervisor.verify", "run_verify")
        write = lazy("ns_supervisor.verify", "write_report")
        runtime = lazy("ns_supervisor.control", "runtime_or_default")
        ctl.check_cancel()
        ctl.progress(0.05, "Running checks")
        report = run(self.paths, runtime(self.paths), self._controller(), quick=quick, deps=self.deps)
        report_file = write(self.paths, report)
        return {
            "checks": [c.model_dump(mode="json") for c in report.checks],
            "passed": bool(report.passed),
            "quick": quick,
            "benchmarks": dict(report.benchmarks),
            "report": str(report_file),
        }


class RealDoctorAdapter:
    """Calls C2's doctor in-process: `ns_supervisor.doctor.build_bundle` (what `ns doctor --bundle` runs)."""

    def __init__(
        self, paths: Paths, *, out_dir: Path | None = None, options: dict[str, Any] | None = None
    ) -> None:
        self.paths = paths
        self.out_dir = out_dir  # None: the Desktop, like the command line
        self.options = options or {}

    def bundle(self) -> Path:
        """Create the diagnostics zip (logs, reports, settings without keys) and return its path."""
        build = lazy("ns_supervisor.doctor", "build_bundle")
        app_path = lazy("ns_supervisor.control", "app_path")
        return Path(build(self.paths, out_dir=self.out_dir, app=app_path(self.paths), **self.options))


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


class RealVoiceMaker:
    """Talks to the TTS service (`/v1/voices/design`, `/v1/voices/clone`) through the supervisor.

    Design needs the VoiceDesign talker; clone first transcribes the sample with whisper (the Base model
    wants the words that are spoken in the reference) while holding the `whisper` tenant, then holds `tts`.
    """

    def __init__(
        self,
        paths: Paths,
        link: SupervisorLink,
        *,
        acquirer: Acquirer | None = None,
        http: httpx.Client | None = None,
        transcribe: Callable[[Path], str] | None = None,
    ) -> None:
        self.paths = paths
        self.link = link
        self._acquirer = acquirer
        self._http = http
        self._transcribe = transcribe

    def _acq(self) -> Acquirer:
        return self._acquirer or make_acquirer(self.link.base_url)

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=1800)
        return self._http

    @contextlib.contextmanager
    def _tenant(self, service: Service, timeout_s: int = 600) -> Iterator[str]:
        acq = self._acq()
        owner = f"dashboard:voice-{int(time.time() * 1000)}"
        endpoint = acq.acquire(service, owner, timeout_s=timeout_s)
        try:
            yield endpoint.base_url
        finally:
            acq.release(service, owner)

    def _post(self, base_url: str, route: str, body: dict[str, Any]) -> None:
        try:
            r = self._client().post(f"{base_url.rstrip('/')}{route}", json=body)
        except httpx.HTTPError as e:
            raise NsError("NS-E310", f"voice service: {e}") from e
        if r.status_code == 409 and "missing_model" in r.text:
            raise ApiError(
                409,
                "model_missing",
                "The model for this voice tool is not downloaded yet.",
                hint="Run the installer's model step again with the voice design option ticked.",
            )
        if r.status_code == 400:
            raise ApiError(400, "voice_rejected", _detail(r))
        if r.status_code >= 400:
            raise NsError("NS-E310", f"voice service answered {r.status_code}: {_detail(r)}")

    def _preview(self, base_url: str, name: str, test_line: str) -> MadeVoice:
        from ns_common.tts_client import TtsClient

        out = self.paths.models_tts_voices / name / "preview.wav"
        TtsClient(base_url, client=self._client()).speak(test_line, name, 1.0, out)
        return MadeVoice(voice_id=name, preview_wav_path=str(out))

    def design(self, name: str, description: str, test_line: str) -> MadeVoice:
        """VoiceDesign from the description, stored as an embedding, then the test line spoken."""
        with self._tenant("tts") as url:
            self._post(url, "/v1/voices/design", {"name": name, "description": description})
            return self._preview(url, name, test_line)

    def _words_in(self, wav: Path) -> str:
        if self._transcribe is not None:
            return self._transcribe(wav)
        from ns_studio.media.transcribe import WhisperTranscriber

        try:
            cfg = load_runtime(self.paths).whisper
        except Exception:  # no runtime.json yet: the default whisper settings
            from ns_common.models import Runtime

            cfg = Runtime.default("gpu").whisper
        with self._tenant("whisper"):
            segs = WhisperTranscriber(cfg, self.paths).segments(wav, "")
        return " ".join(seg.text for seg in segs).strip()

    def clone(self, name: str, sample_wav: Path, test_line: str) -> MadeVoice:
        """Base-model embedding from the sample (its words found by whisper), then the test line spoken."""
        ref_text = self._words_in(sample_wav)
        if len(ref_text.split()) < 2:
            raise NsError("NS-E312", "no speech could be found in the recording")
        body = {
            "name": name,
            "ref_audio_path": str(sample_wav),
            "ref_text": ref_text,
            "rights_confirmed": True,
        }
        with self._tenant("tts") as url:
            self._post(url, "/v1/voices/clone", body)
            return self._preview(url, name, test_line)


def _detail(r: httpx.Response) -> str:
    """Error text of a TTS service answer (FastAPI `{"detail": ...}`), shortened."""
    try:
        data = r.json()
        text = data.get("detail") or data.get("error") or r.text
    except ValueError:
        text = r.text
    return str(text)[:300]


class RealFolderOpener:
    """`os.startfile` (Windows Explorer)."""

    def open(self, path: Path) -> None:
        """Open `path` (a folder) in Explorer."""
        starter = getattr(os, "startfile", None)
        if starter is None:
            raise AdapterUnavailable("opening folders needs Windows")
        starter(str(path))


class RealScheduleAdapter:
    """Re-registers the 'Nightshift Daily' task at the new time (`ns_supervisor.schedule.install`)."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths

    def apply(self, run_time: str) -> dict[str, Any]:
        """Register the task at `run_time`; when Windows refuses, the time stays saved and the note says why."""
        from ns_supervisor.control import app_path
        from ns_supervisor.schedule import ScheduleError, install

        try:
            message = install(self.paths, run_time, app_path(self.paths))
        except ScheduleError as e:
            return {"applied": False, "note": f"Saved, but Windows did not update the nightly task: {e}"}
        return {"applied": True, "note": f"Scheduled task updated. {message}"}


def real_adapters(paths: Paths, link: SupervisorLink, log: NsLogger) -> Adapters:
    """Adapters wired to the real collaborators (each fails softly when its component is missing)."""
    log.debug("adapters_real")
    return Adapters(
        run=RealRunAdapter(paths, link),
        clip=RealClipAdapter(paths, link),
        learn=RealLearnAdapter(paths),
        verify=RealVerifyAdapter(paths, link),
        doctor=RealDoctorAdapter(paths),
        keyring=RealKeyringAdapter(),
        tts=RealTtsAdapter(paths, link),
        folder=RealFolderOpener(),
        schedule=RealScheduleAdapter(paths),
        update=RealUpdateAdapter(paths),
        voice_maker=RealVoiceMaker(paths, link),
        series=RealSeriesAdapter(paths, link),
        backup=RealBackupAdapter(paths),
    )
