"""The service layer behind every `/api` route, free of any web framework (component C9)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_dashboard.adapters import Adapters, real_adapters
from ns_dashboard.events import EventHub
from ns_dashboard.fakes import fake_adapters
from ns_dashboard.jobs import JobStore, JobWorker, make_gpu_gate
from ns_dashboard.state import AppState, open_db
from ns_dashboard.supervisor_link import SupervisorLink, supervisor_url
from ns_dashboard.svc_channels import ChannelsService
from ns_dashboard.svc_engine import EngineService
from ns_dashboard.svc_jobs import JobsService, build_handlers
from ns_dashboard.svc_outputs import OutputsService
from ns_dashboard.svc_settings import SettingsService


class Dashboard:
    """Bundle of services: `outputs`, `channels`, `jobs`, `engine` and `settings`."""

    def __init__(
        self,
        state: AppState,
        *,
        clock: Callable[[], datetime] = datetime.now,
        heartbeat_s: float = 2.0,
    ) -> None:
        self.state = state
        self.channels = ChannelsService(state)
        self.outputs = OutputsService(state)
        self.engine = EngineService(state, self.channels, self.outputs, clock=clock, heartbeat_s=heartbeat_s)
        self.jobs = JobsService(state, self.channels)
        self.settings = SettingsService(state, self.engine)


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
    store = JobStore(db)
    worker = JobWorker(store, build_handlers(adapters), hub, log, gpu_gate=make_gpu_gate(link.status))
    return AppState(
        paths=paths, db=db, log=log, hub=hub, link=link, adapters=adapters, store=store, worker=worker
    )


def default_static_dir() -> Path:
    """Folder served at `/` (the UI lives in `ns_dashboard/static`)."""
    return Path(__file__).resolve().parent / "static"
