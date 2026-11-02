"""Engine state for the status strip, the `/api/status` object and engine commands (component C9)."""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import ns_common
from ns_common.config import load_install_state, load_runtime
from ns_common.errors import NsError
from ns_common.models import GpuStatus
from ns_dashboard.errors import ApiError
from ns_dashboard.events import iter_sse
from ns_dashboard.state import AppState, load_dashboard_settings
from ns_dashboard.svc_channels import ChannelsService
from ns_dashboard.svc_outputs import OutputsService

SERVICE_LABELS = {
    "llama": "the language model (LLM, 9 GB)",
    "tts": "the voice engine",
    "whisper": "speech recognition",
    "embed": "the search model",
}
FORMAT_LABELS = {
    "master_first": "a full video plus Shorts",
    "shorts_only": "Shorts",
    "series_compilation": "a series compilation",
}


def disk_info(home: Path) -> dict[str, Any]:
    """Free and total GB of the drive that holds `home` (nearest existing parent)."""
    probe = home
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return {"free_gb": None, "total_gb": None, "home": str(home)}
    return {
        "free_gb": round(usage.free / 1e9, 1),
        "total_gb": round(usage.total / 1e9, 1),
        "home": str(home),
    }


class EngineService:
    """Composes supervisor state, dashboard jobs and schedule into the objects the UI shows."""

    def __init__(
        self,
        state: AppState,
        channels: ChannelsService,
        outputs: OutputsService,
        *,
        clock: Callable[[], datetime] = datetime.now,
        monotonic: Callable[[], float] = time.monotonic,
        heartbeat_s: float = 2.0,
    ) -> None:
        self.s = state
        self.channels = channels
        self.outputs = outputs
        self.clock = clock
        self.monotonic = monotonic
        self.heartbeat_s = heartbeat_s

    # --- schedule ---
    def next_run(self) -> datetime | None:
        """Next occurrence of the nightly run time (local time), or None without active channels."""
        if not any(c.status == "active" for c in self.channels.load_all()):
            return None
        hh, mm = (int(x) for x in str(load_dashboard_settings(self.s.paths)["run_time"]).split(":"))
        now = self.clock()
        at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return at if at > now else at + timedelta(days=1)

    # --- engine object ---
    def engine(self, st: dict[str, Any] | None = None) -> dict[str, Any]:
        """`{state, detail, elapsed_s, vram_owner, reachable, ...}` for the status strip."""
        if st is None:
            st = self.s.link.status()
        reachable = st is not None
        services = (st or {}).get("services") or {}
        gpu_owner = (st or {}).get("gpu_owner")
        running = self.s.store.running()
        now = self.monotonic()
        starting = [n for n, v in services.items() if isinstance(v, dict) and v.get("state") == "starting"]
        for name in list(self.s.loading_since):
            if name not in starting:
                del self.s.loading_since[name]
        for name in starting:
            self.s.loading_since.setdefault(name, now)
        state, detail, elapsed = "idle", "Services idle", None
        loading_service = None
        if not reachable:
            detail = "Engine not running"
        elif "services" not in (st or {}):
            state, detail = "starting", "Engine starting"
        elif starting:
            loading_service = starting[0]
            state = "loading"
            detail = f"Loading {SERVICE_LABELS.get(loading_service, loading_service)}..."
            elapsed = int(now - self.s.loading_since[loading_service])
        elif running is not None:
            state, detail = "working", running.message or "Working"
        elif gpu_owner or any(isinstance(v, dict) and v.get("state") == "ready" for v in services.values()):
            state, detail = "ready", "Ready"
        if self.s.draining:
            detail = "Shutting down..."
        return {
            "state": state,
            "detail": detail,
            "elapsed_s": elapsed,
            "vram_owner": gpu_owner,
            "reachable": reachable,
            "service": loading_service,
            "draining": self.s.draining,
            "progress": round(running.progress * 100, 1) if running is not None else None,
        }

    # --- /api/status ---
    def status(self) -> dict[str, Any]:
        """The object behind `GET /api/status` and every `status` event."""
        st = self.s.link.status()
        engine = self.engine(st)
        cards = self.channels.load_all()
        counts = self.outputs.counts()
        running = self.s.store.running()
        nxt = self.next_run()
        runs = self.s.db.query("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1")
        incidents = self.s.db.query(
            "SELECT channel, ts, kind, note FROM incidents ORDER BY rowid DESC LIMIT 5"
        )
        recent = self.s.db.one(
            "SELECT * FROM dashboard_jobs WHERE status NOT IN ('queued', 'running')"
            " ORDER BY rowid DESC LIMIT 1"
        )
        last_error = None
        if recent is not None and recent["status"] == "failed":
            last_error = {"job_id": recent["job_id"], "message": recent["message"], "kind": recent["kind"]}
        settings = load_dashboard_settings(self.s.paths)
        pending = {r["channel"]: int(r["n"]) for r in self.s.db.query(
            "SELECT channel, COUNT(*) AS n FROM outputs WHERE status = 'pending' GROUP BY channel"
        )}
        return {
            "version": ns_common.__version__,
            "engine": engine,
            "next_run": nxt.isoformat(timespec="minutes") if nxt else None,
            "next_run_in_s": int((nxt - self.clock()).total_seconds()) if nxt else None,
            "schedule_time": settings["run_time"],
            "outbox": counts,
            "channels": [
                {
                    "slug": c.slug,
                    "display_name": c.display_name,
                    "status": c.status,
                    "niche": c.niche,
                    "voice": c.voice.name or c.voice.speaker or c.voice.mode,
                    "masters_per_week": c.masters_per_week,
                    "publish_window": c.publish_window,
                    "format": c.format,
                    "pending": pending.get(c.slug, 0),
                    "tonight": self._tonight(c.status, c.format, settings["run_time"]),
                }
                for c in cards
            ],
            "services": {
                n: {"state": v.get("state", "stopped"), "vram_mb": v.get("vram_mb")}
                for n, v in ((st or {}).get("services") or {}).items()
                if isinstance(v, dict)
            },
            "disk": disk_info(self.s.paths.home),
            "job": running.to_dict() if running else None,
            "queue": {**self.s.store.counts(), "scheduled": len(self.s.store.scheduled())},
            "last_run": self._run_row(runs[0]) if runs else None,
            "incidents": [dict(r) for r in incidents],
            "last_error": last_error,
            "gpu": self._gpu((st or {}).get("gpu")),
        }

    @staticmethod
    def _gpu(raw: Any) -> dict[str, Any] | None:
        """The supervisor's `gpu` block as `GpuStatus`, or None when it is absent or malformed."""
        if not isinstance(raw, dict):
            return None
        try:
            return GpuStatus.model_validate(raw).model_dump(mode="json")
        except ValueError:
            return None

    @staticmethod
    def _tonight(status: str, fmt: str, run_time: str) -> str:
        if status == "paused":
            return "Paused - nothing will be made"
        return f"Makes {FORMAT_LABELS.get(fmt, fmt)} at {run_time}"

    @staticmethod
    def _run_row(row: Any) -> dict[str, Any]:
        summary = None
        if row["summary_json"]:
            try:
                summary = json.loads(row["summary_json"])
            except ValueError:
                summary = None
        duration = None
        if row["finished_at"]:
            try:
                a = datetime.fromisoformat(row["started_at"])
                b = datetime.fromisoformat(row["finished_at"])
                duration = round((b - a).total_seconds(), 1)
            except ValueError:
                duration = None
        return {
            "run_id": row["run_id"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "mode": row["mode"],
            "duration_s": duration,
            "summary": summary if isinstance(summary, dict) else {},
        }

    def runs(self, limit: int = 30) -> list[dict[str, Any]]:
        """Recent runs, newest first."""
        n = max(1, min(limit, 200))
        rows = self.s.db.query("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (n,))
        return [self._run_row(r) for r in rows]

    # --- morning notes ---
    def notes(self) -> list[dict[str, Any]]:
        """Notes the nightly run left because no toast could be shown (`run/morning_note.json`), oldest first."""
        try:
            raw = json.loads((self.s.paths.home / "run" / "morning_note.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = raw.get("notes") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return []
        return [
            {"at": str(n.get("at") or ""), "title": str(n.get("title") or ""), "message": str(n.get("message") or "")}
            for n in items
            if isinstance(n, dict)
        ]

    def clear_notes(self) -> dict[str, Any]:
        """The owner has seen the notes: remove the file."""
        (self.s.paths.home / "run" / "morning_note.json").unlink(missing_ok=True)
        return {"ok": True}

    # --- commands ---
    def warmup(self, service: str | None) -> dict[str, Any]:
        """Start `service` (default `llama`) so the first job is fast."""
        name = service or "llama"
        if name not in SERVICE_LABELS:
            raise ApiError(422, "unknown_service", f"Unknown service '{name}'.")
        result = self.s.link.warmup(name)
        self.s.log.info("engine_warmup", service=name)
        return result

    def shutdown(self, grace_s: int | None) -> dict[str, Any]:
        """Quit Nightshift: cancel the running job at its next stage boundary, then stop the engine."""
        grace = int(grace_s) if grace_s is not None else 30
        if not 0 <= grace <= 300:
            raise ApiError(422, "invalid_options", "'grace_s' must be between 0 and 300.")
        self.s.link.shutdown(grace)
        self.s.draining = True
        cancelled = self.s.worker.cancel_running()
        self.s.log.info("engine_shutdown", grace_s=grace, cancelled_job=cancelled)
        self.s.hub.publish("job", {"draining": True})
        return {"ok": True, "cancelled_job": cancelled}

    def stop_all(self) -> dict[str, Any]:
        """Stop every model process and cancel the running job."""
        self.s.link.stop_all()
        cancelled = self.s.worker.cancel_running()
        self.s.log.info("engine_stop_all", cancelled_job=cancelled)
        return {"ok": True, "cancelled_job": cancelled}

    # --- events ---
    def events(self, limit: int | None = None) -> Iterator[str]:
        """SSE frames for `GET /api/events`."""
        return iter_sse(self.s.hub.subscribe(), self.status, heartbeat_s=self.heartbeat_s, limit=limit)

    def runtime_summary(self) -> dict[str, Any] | None:
        """`runtime.json` as a dict, or None when it is missing or invalid."""
        try:
            return load_runtime(self.s.paths).model_dump(mode="json")
        except (NsError, ValueError, OSError):
            return None

    def install_summary(self) -> dict[str, Any]:
        """Version, app path and profile from `install_state.json`."""
        st = load_install_state(self.s.paths)
        return {"version": st.version, "app_path": st.app_path, "profile": st.profile}
