"""The service layer behind every `/api` route, free of any web framework (component C9)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_dashboard import studio_bridge
from ns_dashboard.adapters import Adapters, real_adapters
from ns_dashboard.assistant.llm import LlmProvider, SupervisorLlmProvider
from ns_dashboard.assistant.loop import Assistant
from ns_dashboard.assistant.store import AssistantStore
from ns_dashboard.assistant.tools import build_registry
from ns_dashboard.channel_hold import make_channel_hold
from ns_dashboard.events import EventHub
from ns_dashboard.fakes import fake_adapters
from ns_dashboard.jobs import JOB_OWNER, JobRecord, JobStore, JobWorker, make_gpu_gate
from ns_dashboard.state import AppState, open_db
from ns_dashboard.supervisor_link import SupervisorLink, make_acquirer, supervisor_url
from ns_dashboard.svc_channels import ChannelsService
from ns_dashboard.svc_engine import EngineService
from ns_dashboard.svc_jobs import JobsService, build_handlers
from ns_dashboard.svc_library import LibraryService
from ns_dashboard.svc_outputs import OutputsService
from ns_dashboard.svc_settings import SettingsService
from ns_dashboard.svc_voices import VoicesService


class Dashboard:
    """Bundle of services: outputs, channels, jobs, engine, settings, library and the Assistant."""

    def __init__(
        self,
        state: AppState,
        *,
        clock: Callable[[], datetime] = datetime.now,
        heartbeat_s: float = 2.0,
        llm_provider: LlmProvider | None = None,
    ) -> None:
        self.state = state
        self.channels = ChannelsService(state)
        self.outputs = OutputsService(state)
        self.engine = EngineService(state, self.channels, self.outputs, clock=clock, heartbeat_s=heartbeat_s)
        self.jobs = JobsService(state, self.channels)
        self.settings = SettingsService(state, self.engine)
        self.library = LibraryService(state, self.channels)
        self.voices = VoicesService(state)
        self.registry = build_registry()
        self.assistant_store = AssistantStore(state.paths)
        acquirer = make_acquirer(state.link.base_url)
        provider = llm_provider or SupervisorLlmProvider(state.link.status, acquirer)
        self.assistant = Assistant(self, self.registry, self.assistant_store, provider)
        state.worker.on_idle = self.answer_saved_requests

    def answer_saved_requests(self) -> None:
        """Answer the Assistant requests saved while the engine was busy (called when a job ends)."""
        if self.assistant_store.queued():
            self.assistant.run_queued()

    def close(self) -> None:
        """Close the databases (needed on Windows before a temp folder can be deleted)."""
        self.assistant_store.close()
        self.state.db.close()


def build_state(
    paths: Paths | None = None,
    *,
    adapters: Adapters | None = None,
    link: SupervisorLink | None = None,
    fake_services: bool = False,
) -> AppState:
    """Wire DB, logger, event hub, supervisor link, adapters, job store and worker (thread not started)."""
    paths = paths or Paths.from_env()
    paths.ensure()
    db = open_db(paths)
    log = NsLogger("dashboard", paths.logs)
    hub = EventHub()
    link = link or SupervisorLink(supervisor_url(paths))
    adapters = adapters or (fake_adapters(db, paths) if fake_services else real_adapters(paths, link, log))
    def release_part_of(rec: JobRecord) -> None:
        adapters.series.release({**rec.params, "job_id": rec.job_id})

    def finish_part_of(rec: JobRecord) -> None:
        adapters.series.finish({**rec.params, "job_id": rec.job_id})

    store = JobStore(db)

    def sweep_at_start(active_ids: set[str], part_statuses: dict[str, str]) -> None:
        """Series parts without a live job settle; link-download folders without a live job are removed; the
        upload sheets still waiting to be posted are brought up to YouTube's rules (once per rules
        version)."""
        from ns_studio.media.outbox import sweep_sheets

        try:
            sweep_sheets(paths, db, log)
        except Exception as e:  # never stops the worker from starting; the next start tries again
            log.warn("sheet_sweep_failed", f"the upload sheets were not checked against YouTube's rules: {e}")
        try:
            live = {
                str(rec.params.get("source_id"))
                for status in ("queued", "running")
                for rec in store.list_jobs(status=status, kind="clip", limit=1000)
                if rec.params.get("source_id")
            }
            studio_bridge.sweep_downloads(paths, live, log)
        except Exception as e:  # a sweep that fails must never stop the worker from starting
            log.warn("download_sweep_failed", str(e))
        adapters.series.sweep(active_ids, part_statuses)

    def recover_studio_work() -> None:
        """Engine start: a run or job whose process is gone is closed as interrupted and offered (D-31)."""
        from ns_studio import ownership

        # Recovery says which stage an interrupted job had reached. It reads the job's own plan through
        # `orchestrator.plan_names`, which completes a bare stage table itself (Short combos, policy gates,
        # the Clipper's stages), so this process, which never runs a stage, needs no set-up call here.
        ownership.recover(paths, db, log=log)

    worker = JobWorker(
        store, build_handlers(adapters, paths), hub, log, gpu_gate=make_gpu_gate(link.status),
        recover_studio=recover_studio_work,
        pulse=None if fake_services else (lambda: link.heartbeat(JOB_OWNER)),
        on_recover={"series_part": release_part_of},
        on_end={"series_part": release_part_of},
        on_done={"series_part": finish_part_of},
        sweep=sweep_at_start,
        hold=make_channel_hold(paths, db),
    )
    return AppState(
        paths=paths, db=db, log=log, hub=hub, link=link, adapters=adapters, store=store, worker=worker,
        fake_services=fake_services,
    )


def default_static_dir() -> Path:
    """Folder served at `/` (the UI lives in `ns_dashboard/static`)."""
    return Path(__file__).resolve().parent / "static"
