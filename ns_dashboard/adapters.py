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
import shutil
import time
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from ns_common.acquire import Acquirer, Service
from ns_common.atomic import read_text_retry
from ns_common.config import load_runtime
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_common.secrets_names import KEYRING_SERVICE
from ns_dashboard import studio_bridge
from ns_dashboard.errors import AdapterUnavailable, ApiError
from ns_dashboard.jobs import JobControl
from ns_dashboard.lazyimport import lazy
from ns_dashboard.studio_runner import StudioRunner
from ns_dashboard.supervisor_link import SupervisorLink, make_acquirer, supervisor_url
from ns_dashboard.updater import RealBackupAdapter, RealUpdateAdapter
from ns_dashboard.uploads import adopt_upload, tmp_dir

PRESET_VOICES = [{"name": "Ryan", "mode": "preset", "source": "builtin"}]


class RunAdapter(Protocol):
    """Runs one studio job (spec 15.9 `studio run`) and returns the new output ids."""

    def run(
        self, ctl: JobControl, channel: str, fmt: str, count: int | None, *, resume_job_id: str | None = None,
        script: str | None = None, script_title: str | None = None,
    ) -> list[str]:
        """Run `count` new jobs, or (with `resume_job_id`) continue that failed studio job from its stage.

        With `script` one job is made from that text (the owner's story) instead of one the model writes."""
        ...


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

    def available(self) -> None:
        """Raise `AdapterUnavailable` when `bundle` could not run; writes nothing."""
        ...

    def bundle(self) -> Path: ...


class KeyringAdapter(Protocol):
    """Windows Credential Manager access for the stock-footage keys."""

    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None: ...


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

    def pending(self) -> dict[str, Any] | None:
        """The voice setup asked for (`config/pending_voice.json`) while it does not exist yet, else None."""
        ...

    def apply_pending(self, ctl: JobControl | None = None) -> str | None:
        """Create that voice now (inside an `apply_voice` job when `ctl` is given); its name, or None."""
        ...


class FolderOpener(Protocol):
    """Opens a folder in Explorer."""

    def open(self, path: Path) -> None: ...


class UpdateAdapter(Protocol):
    """GitHub update check and apply (`ns update`), and the on-demand model downloads (`ns models get`)."""

    def check(self) -> dict[str, Any]: ...

    def apply(self) -> dict[str, Any]: ...

    def apply_file(self, zip_path: Path) -> dict[str, Any]:
        """Install an update zip from disk (`ns update <zip>`, its `.sha256` beside it)."""
        ...

    def models_available(self) -> bool:
        """False until `ns models get` exists."""
        ...

    def get_model(self, name: str, on_line: Callable[[str], None] = ...) -> tuple[int, str]:
        """Download the model `name` (blocks); progress lines go to `on_line`. Returns (code, last line)."""
        ...


class SeriesAdapter(Protocol):
    """Series Workshop production: runs one approved part as a studio job (kind `series_part`)."""

    def produce(self, ctl: JobControl, params: dict[str, Any]) -> list[str]: ...

    def listing(self) -> list[dict[str, Any]]:
        """Every workshop story with its parts and next step (read-only)."""
        ...

    def queue_next(self, slug: str) -> list[dict[str, Any]]:
        """Queue the next approved part of `slug`: `[{part, job_id, title}]`; 409 when none is ready."""
        ...

    def release(self, params: dict[str, Any]) -> None:
        """Put a part whose job was interrupted back to `approved` so it can be queued again."""
        ...

    def sweep(
        self, active_job_ids: Collection[str], job_statuses: Mapping[str, str] | None = None
    ) -> int:
        """Settle every `producing` part with no queued or running job: `produced` when its job is `done`,
        else `approved` (the job is missing, failed or cancelled); returns how many."""
        ...

    def finish(self, params: dict[str, Any]) -> None:
        """A part whose job finished goes from `producing` to `produced`."""
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


def studio_owners(ctl: JobControl, args: dict[str, Any]) -> list[str]:
    """Every GPU owner a job may hold: the owners its child announced (`lease_owners`: a setup voice made at
    the start of a run, a helper), `studio:<job id>` for the announced ids and those the arguments name."""
    ids: list[Any] = list(ctl.extras.get("studio_job_ids") or [])
    params = args.get("params")
    ids += [
        ctl.extras.get("studio_job_id"),
        args.get("studio_job_id"),  # a re-run
        params.get("job_id") if isinstance(params, dict) else None,  # a series part
    ]
    named = {f"studio:{i}" for i in ids if i}
    return sorted(named | {str(o) for o in ctl.extras.get("lease_owners") or [] if o})


class StudioAdapter:
    """Shared wiring of the adapters that run studio work: home, link, fakes, and the child process.

    The work itself runs in the studio venv as a child process (`StudioRunner`, entry
    `ns_dashboard.studio_job`), because the dashboard's own venv has no faster-whisper, GPU DLL wheels, spaCy
    or OpenCV. The GPU tenants are still acquired through the supervisor, from inside the child.
    """

    def __init__(
        self,
        paths: Paths,
        link: SupervisorLink,
        *,
        fake: Collection[str] = (),
        runner: StudioRunner | None = None,
    ) -> None:
        self.paths = paths
        self.link = link
        self.fake = frozenset(fake)
        self.runner = runner or StudioRunner(paths)

    def child(
        self,
        ctl: JobControl,
        kind: str,
        args: dict[str, Any],
        on_cancel: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Run `kind` in the studio venv for the job `ctl`; progress and cancel flow through `ctl`.

        The studio job ids announced by the child are kept in `ctl.extras["studio_job_ids"]` as they appear,
        so a failed job still says which studio job to look at; the GPU owners it announces go to
        `ctl.extras["lease_owners"]`, and `release_leases` frees them all when the child is gone.
        """
        report = job_reporter(ctl)
        ended = False  # set once the run is over: an owner announced after that is released on arrival

        def on_event(event: dict[str, Any]) -> None:
            name = event.get("ev")
            if name == "stage":
                report(
                    str(event.get("stage", "")),
                    int(event.get("index", 0)),
                    int(event.get("total", 1)),
                    int(event.get("job_index", 0)),
                    int(event.get("job_total", 1)),
                )
            elif name == "owner":
                owners = ctl.extras.setdefault("lease_owners", [])
                owner = str(event.get("owner", ""))
                if owner and owner not in owners:
                    owners.append(owner)
                    ctl.note(lease_owners=list(owners))  # written now, like the job ids
                    if ended or ctl.cancelled():  # late: the job was cancelled before this arrived
                        self._release_late(ctl, owner)
            elif name == "download":
                ctl.download(float(event.get("percent", 0)))
            elif name == "job":
                ids = ctl.extras.setdefault("studio_job_ids", [])
                ids.append(str(event.get("job_id", "")))
                ctl.note(studio_job_ids=list(ids))   # written now: a restart mid-job must not lose them

        full = {**args, "fake": sorted(self.fake), "supervisor_url": self.link.base_url}
        try:
            return self.runner.run(
                kind, full, on_event=on_event, check_cancel=ctl.check_cancel, on_cancel=on_cancel
            )
        except BaseException:
            ended = True
            self.release_leases(ctl, full)  # the child is gone (stopped itself or killed): free its lease
            raise

    def _release_late(self, ctl: JobControl, owner: str) -> None:
        """Free `owner` at once: it was announced after its job was cancelled or had ended, so nothing else
        would ever release it and the lease would sit there until it expired."""
        NsLogger("dashboard", self.paths.logs).info(
            "lease_owner_released_late", job_id=ctl.job_id, owner=owner
        )
        with contextlib.suppress(Exception):  # an engine that is down holds nothing
            self.link.release_owner(owner)

    def release_leases(self, ctl: JobControl, args: dict[str, Any]) -> None:
        """Tell the supervisor that the studio owners of this job are gone, whatever they still held.

        A child that stopped between stages already released its services; one that was killed in the middle
        of a stage did not, and the lock would stay with a dead owner. Releasing is harmless when nothing is
        held.
        """
        for owner in studio_owners(ctl, args):
            with contextlib.suppress(Exception):  # an engine that is down holds nothing
                self.link.release_owner(owner)


class RealRunAdapter(StudioAdapter):
    """Runs `studio run` jobs in the studio venv (`studio_job run` -> `studio_bridge.run_story_jobs`, C7).

    The orchestrator runs with the regenerate-once policy; progress comes from the runner's stage events, and
    the GPU tenants are acquired through the supervisor.
    """

    def run(
        self, ctl: JobControl, channel: str, fmt: str, count: int | None, *, resume_job_id: str | None = None,
        script: str | None = None, script_title: str | None = None,
    ) -> list[str]:
        """Run `count` studio jobs for `channel` (or resume `resume_job_id`, or make one from `script`);
        returns the output ids and notes the studio job ids."""
        args: dict[str, Any] = {"channel": channel, "fmt": fmt, "count": count}
        if resume_job_id:
            args["resume_job_id"] = resume_job_id
        if script:
            args["script"] = script
            args["script_title"] = script_title
        done = self.child(ctl, "run", args)
        ctl.extras["studio_job_ids"] = list(done.get("job_ids") or ctl.extras.get("studio_job_ids") or [])
        return [str(i) for i in done.get("output_ids") or []]


class RealClipAdapter(StudioAdapter):
    """Runs the Clipper in the studio venv: the clip job's stages (`studio clip`) and `import_url`."""

    def clip(self, ctl: JobControl, source_path: Path, source_id: str, params: dict[str, Any]) -> list[str]:
        """Run the Clipper for `source_path`; returns the output ids it inserted."""
        # an upload is moved to where the import wants it, not copied
        source_path = adopt_upload(self.paths, source_path)
        args = {
            "source_path": str(source_path),
            "params": {**params, "channel": params.get("channel") or ctl.channel},
        }
        try:
            done = self.child(ctl, "clip", args)
        finally:
            self._drop_download(source_path, source_id)
        seen = [str(i) for i in done.get("job_ids") or []]
        ctl.extras["studio_job_id"] = seen[0] if seen else None
        ctl.extras.pop("studio_job_ids", None)  # a clip job reports the single `studio_job_id`
        return [str(i) for i in done.get("output_ids") or []]

    def _drop_download(self, source_path: Path, source_id: str) -> None:
        """A linked video is deleted when its clip job ends, whatever the ending (done, failed, cancelled)."""
        folder = studio_bridge.download_dir(self.paths, source_id)
        if source_path.parent == folder:
            shutil.rmtree(folder, ignore_errors=True)

    def rerun(self, ctl: JobControl, studio_job_id: str, changes: dict[str, Any]) -> list[str]:
        """Run the later stages of `studio_job_id` again with other options; keeps the transcript."""
        done = self.child(ctl, "rerun", {"studio_job_id": studio_job_id, "changes": changes})
        ctl.extras["studio_job_id"] = studio_job_id
        ctl.extras.pop("studio_job_ids", None)
        return [str(i) for i in done.get("output_ids") or []]

    def fetch_url(self, ctl: JobControl, url: str, source_id: str) -> Path:
        """Download `url` with the pinned yt-dlp (`clipper.source.import_url`), shown as `Downloading N%`.

        yt-dlp runs inside the child. Cancel first creates a flag file that the child turns into
        `CancelToken.cancel()` (yt-dlp is killed and its folder removed by the Clipper itself, even in the
        middle of a download); the runner then kills the child process tree as the backstop, and the partly
        downloaded folder is removed here once more.
        """
        flag = tmp_dir(self.paths) / f"cancel-download-{source_id}"

        def signal_cancel() -> None:
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.touch()

        flag.unlink(missing_ok=True)
        try:
            done = self.child(
                ctl, "download", {"url": url, "source_id": source_id, "cancel_file": str(flag)}, signal_cancel
            )
        except BaseException:
            # the runner has killed the child's tree and reaped it; a file it held open may take a moment to
            # let go, so the folder is removed with retries and a failure is logged, never swallowed (D-9)
            studio_bridge.remove_download(self.paths, source_id)
            raise
        finally:
            flag.unlink(missing_ok=True)
        return Path(str(done["path"]))


class RealSeriesAdapter(StudioAdapter):
    """Produces a series part: the job `Workshop.produce` prepared, run with the `master_from_script` plan."""

    def produce(self, ctl: JobControl, params: dict[str, Any]) -> list[str]:
        """Run the part's job; a failed or cancelled part goes back to `approved` to be queued again."""
        try:
            done = self.child(ctl, "series", {"params": {**params, "job_id": ctl.job_id}})
            return [str(i) for i in done.get("output_ids") or []]
        except BaseException:
            studio_bridge.release_part({**params, "job_id": ctl.job_id})
            raise

    def release(self, params: dict[str, Any]) -> None:
        """A part left `producing` by a crash or a Quit goes back to `approved`."""
        studio_bridge.release_part(params)

    def sweep(
        self, active_job_ids: Collection[str], job_statuses: Mapping[str, str] | None = None
    ) -> int:
        """`producing` parts with no live job behind them (found at start): finished ones become `produced`,
        the others go back to `approved`."""
        return studio_bridge.sweep_parts(active_job_ids, job_statuses)

    def finish(self, params: dict[str, Any]) -> None:
        """A part whose job is done becomes `produced`, so the start sweep leaves it alone."""
        studio_bridge.finish_part(params)

    def listing(self) -> list[dict[str, Any]]:
        """Stories and parts read from the workshop files under `stories/`."""
        return studio_bridge.list_series()

    def queue_next(self, slug: str) -> list[dict[str, Any]]:
        """Queue the next approved part (`Workshop.produce`); workshop usage errors become 409/404 answers."""
        from ns_studio.series.store import WorkshopError

        try:
            return studio_bridge.queue_next_part(slug, self.fake)
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

    def available(self) -> None:
        """Load what `bundle` needs without running it."""
        lazy("ns_supervisor.doctor", "build_bundle")
        lazy("ns_supervisor.control", "app_path")

    def bundle(self) -> Path:
        """Create the diagnostics zip (logs, reports, settings without keys) and return its path."""
        build = lazy("ns_supervisor.doctor", "build_bundle")
        app_path = lazy("ns_supervisor.control", "app_path")
        return Path(build(self.paths, out_dir=self.out_dir, app=app_path(self.paths), **self.options))


class RealKeyringAdapter:
    """The stock-footage keys: Windows Credential Manager (service `Nightshift`) AND the DPAPI file.

    The nightly task may run in a session that cannot read the owner's Credential Manager (the studio then
    falls back to `config/secrets.dpapi`, `ns_common.secret_file`), so a key saved here must reach both places
    or the nightly run would keep an old key. `set` fails only when neither accepted the value, like the
    installer's `ResilientStore`; `get` asks the keyring first and the file second.
    """

    def __init__(
        self,
        paths: Paths | None = None,
        *,
        keyring_module: Any = None,
        protect: Callable[[bytes], bytes] | None = None,
        unprotect: Callable[[bytes], bytes] | None = None,
    ) -> None:
        self.paths = paths
        self._keyring = keyring_module
        self._protect = protect
        self._unprotect = unprotect

    def _module(self) -> Any:
        if self._keyring is not None:
            return self._keyring
        try:
            return importlib.import_module("keyring")
        except ImportError as e:
            raise AdapterUnavailable("the keyring package is not installed") from e

    def get(self, name: str) -> str | None:
        """Stored secret (Credential Manager, else the DPAPI file) or None."""
        value: str | None = None
        try:
            value = self._module().get_password(KEYRING_SERVICE, name)
        except AdapterUnavailable:
            if self.paths is None:  # nothing else to ask
                raise
        except Exception:  # a broken backend must not hide the file copy
            value = None
        if value or self.paths is None:
            return value
        from ns_common.secret_file import dpapi_unprotect, load_secret

        return load_secret(self.paths, name, unprotect=self._unprotect or dpapi_unprotect)

    def set(self, name: str, value: str) -> None:
        """Write Credential Manager and the DPAPI file; raise only when both refused."""
        failures: list[str] = []
        try:
            self._module().set_password(KEYRING_SERVICE, name, value)
        except Exception as e:  # recorded: the file copy may still work
            failures.append(f"keyring: {type(e).__name__}")
        if self.paths is not None:
            from ns_common.secret_file import dpapi_protect, store_secret

            try:
                store_secret(self.paths, name, value, protect=self._protect or dpapi_protect)
            except OSError as e:
                failures.append(f"file: {e}")
        if len(failures) == (2 if self.paths is not None else 1):
            raise AdapterUnavailable("the key could not be stored (" + "; ".join(failures) + ")")

    def delete(self, name: str) -> None:
        """Forget a key in Credential Manager and the DPAPI file (best effort each; the Settings page's
        "clear" and the uninstall; a stale key left behind made a later setup fail on a rejected key)."""
        try:
            self._module().delete_password(KEYRING_SERVICE, name)
        except Exception:  # noqa: BLE001 - absent, or a broken backend
            pass
        if self.paths is not None:
            from ns_common.secret_file import delete_secret
            try:
                delete_secret(self.paths, name)
            except OSError:
                pass


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
        runner: StudioRunner | None = None,
    ) -> None:
        self.paths = paths
        self.link = link
        self._acquirer = acquirer
        self._http = http
        self._transcribe = transcribe
        self._runner = runner or StudioRunner(paths)

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
            raise _missing_model(r)
        if r.status_code == 400:
            raise ApiError(400, "voice_rejected", _detail(r))
        if r.status_code >= 400:
            code = _service_code(r)
            if code is not None:  # the service named the condition (NS-E311, NS-E315, ...): the page words it
                raise NsError(code, _detail(r))
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
        """The words spoken in `wav`: faster-whisper runs in the studio venv while this holds the tenant."""
        if self._transcribe is not None:
            return self._transcribe(wav)
        with self._tenant("whisper"):
            done = self._runner.run("transcribe", {"wav": str(wav)})
        return str(done.get("text") or "").strip()

    def pending(self) -> dict[str, Any] | None:
        """The queued setup voice (name, mode, description, channel), or None when none waits or it exists."""
        return read_pending_voice(self.paths)

    def apply_pending(self, ctl: JobControl | None = None) -> str | None:
        """Create the setup voice in the studio venv (`voice_setup.apply_pending`), which holds `tts`.

        Called with the `JobControl` of an `apply_voice` job (Library > Voices > Create it now), it reports
        progress and honours Cancel: the child is stopped and the lease `studio:voice-<name>` it may still
        hold is released at once.
        """
        args = {"supervisor_url": self.link.base_url}
        if ctl is None:
            done = self._runner.run("apply_voice", args)
        else:
            want = self.pending()
            owner = f"studio:voice-{want['name']}" if want else None
            ctl.progress(0.1, "Making the voice: loading the voice engine")
            try:
                done = self._runner.run("apply_voice", args, check_cancel=ctl.check_cancel)
            except BaseException:
                if owner:
                    with contextlib.suppress(Exception):  # an engine that is down holds nothing
                        self.link.release_owner(owner)
                raise
            ctl.progress(1.0, "The voice is ready")
        name = done.get("name")
        return str(name) if name else None

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


CLONE_NOTE = "Cloned from your recording"


def read_pending_voice(paths: Paths) -> dict[str, Any] | None:
    """`config/pending_voice.json` as the page shows it; None when absent, unreadable or already made."""
    try:
        raw = json.loads(read_text_retry(paths.config / "pending_voice.json"))
    except (OSError, ValueError):
        return None
    name = raw.get("name") if isinstance(raw, dict) else None
    if not isinstance(name, str) or not name or (paths.models_tts_voices / name / "voice.json").is_file():
        return None
    mode = "clone" if raw.get("mode") == "clone" else "design"
    return {
        "name": name,
        "mode": mode,
        "description": str(raw.get("description") or "") if mode == "design" else CLONE_NOTE,
        "channel": raw.get("channel") if isinstance(raw.get("channel"), str) else None,
    }


def _service_code(r: httpx.Response) -> str | None:
    """The `NS-` code in a TTS service answer (`{"error": "NS-E315", ...}`) when it is a known one."""
    try:
        code = r.json().get("error")
    except (ValueError, AttributeError):
        return None
    if isinstance(code, str) and code.startswith("NS-"):
        try:
            NsError(code)
        except KeyError:
            return None
        return code
    return None


def _missing_model(r: httpx.Response) -> ApiError:
    """The 409 for an absent model file, naming the model the owner can actually get.

    Only the VoiceDesign talker has a download (`ns models get voicedesign`); a missing Base talker or
    tokenizer belongs to the base install, so offering the design download for it would not help.
    """
    try:
        missing = str(r.json().get("missing_model") or "")
    except (ValueError, AttributeError):
        missing = ""
    if "design" in missing.lower():
        return ApiError(
            409,
            "model_missing",
            "The voice design model is not downloaded yet.",
            hint="Get it with the button below (it is a large download), then try again.",
        )
    return ApiError(
        409,
        "base_model_missing",
        f"A file of the voice model is missing ({missing or 'unknown'}).",
        hint="Run the installer's model step again; this model belongs to the base download.",
    )


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
    """Re-registers the 'Nightshift Daily' task at the new time (`ns_supervisor.schedule.install`).

    A task the owner switched off in Task Scheduler gets the new time and stays switched off (D-28).
    A task registered with `ns schedule install --disabled` (record mode `disabled`) is registered enabled.
    """

    def __init__(self, paths: Paths) -> None:
        self.paths = paths

    def apply(self, run_time: str) -> dict[str, Any]:
        """Register the task at `run_time`; if Windows refuses, the time stays saved and the note says why."""
        from ns_supervisor.control import app_path
        from ns_supervisor.schedule import ScheduleError, install

        try:
            message = install(self.paths, run_time, app_path(self.paths), keep_state=True)
        except ScheduleError as e:
            return {"applied": False, "note": f"Not saved: Windows refused to change the nightly task ({e})"}
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
        keyring=RealKeyringAdapter(paths),
        tts=RealTtsAdapter(paths, link),
        folder=RealFolderOpener(),
        schedule=RealScheduleAdapter(paths),
        update=RealUpdateAdapter(paths),
        voice_maker=RealVoiceMaker(paths, link),
        series=RealSeriesAdapter(paths, link),
        backup=RealBackupAdapter(paths),
    )
