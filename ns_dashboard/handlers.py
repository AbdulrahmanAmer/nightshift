"""The service layer behind every `/api` route, free of any web framework (component C9)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_dashboard.adapters import Adapters, real_adapters
from ns_dashboard.assistant.llm import LlmProvider, SupervisorLlmProvider
from ns_dashboard.assistant.loop import Assistant
from ns_dashboard.assistant.store import AssistantStore
from ns_dashboard.assistant.tools import build_registry
from ns_dashboard.events import EventHub
from ns_dashboard.fakes import fake_adapters
from ns_dashboard.jobs import JobRecord, JobStore, JobWorker, make_gpu_gate
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

    store = JobStore(db)
    worker = JobWorker(
        store, build_handlers(adapters, paths), hub, log, gpu_gate=make_gpu_gate(link.status),
        on_recover={"series_part": release_part_of},
        on_end={"series_part": release_part_of},
        sweep=adapters.series.sweep,
    )
    return AppState(
        paths=paths, db=db, log=log, hub=hub, link=link, adapters=adapters, store=store, worker=worker,
        fake_services=fake_services,
    )


def default_static_dir() -> Path:
    """Folder served at `/` (the UI lives in `ns_dashboard/static`)."""
    return Path(__file__).resolve().parent / "static"
