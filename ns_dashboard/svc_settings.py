"""Settings, API keys, diagnostics, templates, voices, logs and the learn summary (component C9)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ns_common.config import save_runtime
from ns_common.models import Runtime
from ns_dashboard import logs_tail
from ns_dashboard.adapters import KEY_NAMES
from ns_dashboard.errors import AdapterUnavailable, ApiError
from ns_dashboard.state import AppState, load_dashboard_settings, save_dashboard_settings
from ns_dashboard.svc_engine import EngineService
from ns_dashboard.templates import load_templates, render_preview

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
KEY_ALIASES = {"pexels": "pexels_api_key", "pixabay": "pixabay_api_key"}
POOL_COLUMNS = {
    "hooks": "hook",
    "structures": "structure",
    "pov_tones": "pov_tone",
    "pacings": "pacing",
    "caption_styles": "caption_style",
    "visual_treatments": "visual_treatment",
    "title_formulas": "title_formula",
}
PREVIEW_TEXT = "This is how I will sound when I read your story."


class SettingsService:
    """Everything on the Settings, Learn and Runs & Logs pages that is not a job."""

    def __init__(self, state: AppState, engine: EngineService) -> None:
        self.s = state
        self.engine = engine

    # --- settings ---
    def _key_flags(self) -> dict[str, bool]:
        out: dict[str, bool] = {}
        for short, full in KEY_ALIASES.items():
            try:
                out[short] = bool(self.s.adapters.keyring.get(full))
            except AdapterUnavailable:
                out[short] = False
        return out

    def get_settings(self) -> dict[str, Any]:
        """`{schedule_time, home, profile, flags, keys, notifications, ...}`."""
        stored = load_dashboard_settings(self.s.paths)
        runtime = self.engine.runtime_summary()
        flags = {k: v for k, v in (runtime or {}).items() if k not in ("profile", "supervisor_port")}
        nxt = self.engine.next_run()
        return {
            "schedule_time": stored["run_time"],
            "home": str(self.s.paths.home),
            "profile": (runtime or {}).get("profile"),
            "flags": flags,
            "keys": self._key_flags(),
            "notifications": bool(stored["notifications"]),
            "runtime_locked": True,
            "install": self.engine.install_summary(),
            "next_run": nxt.isoformat(timespec="minutes") if nxt else None,
        }

    def put_settings(self, body: dict[str, Any], unlock: bool = False) -> dict[str, Any]:
        """Save the schedule, notifications and (only when unlocked) runtime flags."""
        unlocked = unlock or body.get("unlock") is True
        schedule_result: dict[str, Any] | None = None
        if "flags" in body or "profile" in body:
            if not unlocked:
                raise ApiError(
                    409, "runtime_locked", "Engine settings are locked.",
                    hint="Switch on 'Unlock editing' before changing them.",
                )
            self._save_runtime(body)
        updates: dict[str, Any] = {}
        if "schedule_time" in body:
            value = str(body["schedule_time"])
            if not TIME_RE.match(value):
                raise ApiError(422, "bad_time", "Use a 24-hour time like 03:00.")
            updates["run_time"] = value
        if "notifications" in body:
            if not isinstance(body["notifications"], bool):
                raise ApiError(422, "invalid_options", "'notifications' must be true or false.")
            updates["notifications"] = body["notifications"]
        if updates:
            save_dashboard_settings(self.s.paths, updates)
        if "run_time" in updates:
            schedule_result = self.s.adapters.schedule.apply(updates["run_time"])
        out = self.get_settings()
        if schedule_result is not None:
            out["schedule_result"] = schedule_result
        self.s.log.info("settings_saved", fields=sorted(body))
        return out

    def _save_runtime(self, body: dict[str, Any]) -> None:
        current = self.engine.runtime_summary()
        if current is None:
            raise ApiError(409, "no_runtime", "runtime.json does not exist yet; run the installer first.")
        merged = {**current, **(body.get("flags") or {})}
        if body.get("profile"):
            merged["profile"] = body["profile"]
        try:
            runtime = Runtime.model_validate(merged)
        except ValidationError as e:
            details = [{"field": ".".join(str(p) for p in x["loc"]), "message": x["msg"]} for x in e.errors()]
            msg = "Those engine settings are not valid."
            raise ApiError(422, "invalid_runtime", msg, details=details) from e
        save_runtime(self.s.paths, runtime)
        self.s.log.warn("runtime_edited", "runtime.json changed from the dashboard")

    def set_key(self, name: str | None, value: str | None) -> dict[str, Any]:
        """Store an API key in the Windows keyring; the value is never logged or returned."""
        full = KEY_ALIASES.get(str(name), str(name))
        if full not in KEY_NAMES:
            raise ApiError(400, "unknown_key", "Choose pexels or pixabay.")
        secret = (value or "").strip()
        if not secret or len(secret) > 512:
            raise ApiError(400, "bad_key", "Paste the key without spaces (up to 512 characters).")
        try:
            self.s.adapters.keyring.set(full, secret)
        except AdapterUnavailable as e:
            raise ApiError(501, "keyring_unavailable", str(e)) from e
        self.s.log.info("api_key_set", name=full)
        return {"name": full, "set": True}

    # --- diagnostics ---
    def diagnostics(self) -> dict[str, Any]:
        """Create the diagnostics zip (`ns doctor --bundle`)."""
        try:
            path = Path(self.s.adapters.doctor.bundle())
        except AdapterUnavailable as e:
            raise ApiError(
                501, "diagnostics_unavailable", str(e),
                hint="Use the 'Nightshift - Diagnostics' desktop shortcut instead.",
            ) from e
        self.s.last_bundle = path
        size = path.stat().st_size if path.exists() else None
        return {"path": str(path), "name": path.name, "size": size, "download_url": "/api/diagnostics/latest"}

    def latest_bundle(self) -> Path:
        """The most recent bundle created in this session."""
        path = self.s.last_bundle
        if path is None or not path.is_file():
            raise ApiError(404, "not_found", "No diagnostics bundle has been created yet.")
        return path

    # --- templates / voices ---
    def templates(self) -> list[dict[str, Any]]:
        """Clip templates (bare list)."""
        return load_templates(self.s.paths)

    def template_preview(self, template_id: str) -> Path:
        """PNG drawn with Pillow."""
        try:
            return render_preview(self.s.paths, template_id)
        except KeyError as e:
            raise ApiError(404, "not_found", "Unknown layout.") from e
        except AdapterUnavailable as e:
            raise ApiError(503, "pillow_missing", str(e)) from e

    def voices(self) -> list[dict[str, Any]]:
        """Voices as `[{name, mode, source}]` (bare list)."""
        try:
            voices, _live = self.s.adapters.tts.voices()
        except AdapterUnavailable:
            return []
        return [{"name": v.get("name"), "mode": v.get("mode"), "source": v.get("source")} for v in voices]

    def voice_preview(self, body: dict[str, Any]) -> bytes:
        """WAV bytes of a short sample, cached on disk by (voice, speed, text)."""
        voice = str(body.get("voice") or "").strip()
        if not voice:
            raise ApiError(400, "no_voice", "Pick a voice first.")
        text = str(body.get("text") or PREVIEW_TEXT).strip()[:300]
        try:
            speed = float(body.get("speed") or 1.0)
        except (TypeError, ValueError) as e:
            raise ApiError(422, "invalid_options", "'speed' must be a number.") from e
        if not 0.7 <= speed <= 1.3:
            raise ApiError(422, "invalid_options", "'speed' must be between 0.7 and 1.3.")
        key = hashlib.sha1(f"{voice}|{speed}|{text}".encode()).hexdigest()[:16]
        cache = self.s.paths.home / "cache" / "dashboard" / "voice" / f"{key}.wav"
        if cache.exists():
            return cache.read_bytes()
        try:
            data = self.s.adapters.tts.preview(voice, text, speed)
        except AdapterUnavailable as e:
            raise ApiError(503, "tts_unavailable", str(e)) from e
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(data)
        return data

    # --- logs ---
    def tail(
        self, component: str | None, level: str | None, n: int, after: str | None
    ) -> list[dict[str, Any]]:
        """Log records, oldest first (bare list)."""
        out = logs_tail.tail(
            self.s.paths.logs, component=component or None, level=level or None, n=n, after=after
        )
        return out["lines"]

    def log_components(self) -> list[str]:
        """Names of the components that have log files."""
        return logs_tail.tail(self.s.paths.logs, n=1)["components"]

    # --- learn ---
    def learn_summary(self) -> dict[str, Any]:
        """What the weights learned; the adapter's summary when C7 provides one, else computed from the DB."""
        try:
            base = dict(self.s.adapters.learn.summary())
        except AdapterUnavailable:
            base = {}
        if not base:  # the real adapter has nothing extra: compute it from pool_weights
            base = self._db_summary()
        last = self.s.store.list_jobs(status="done", kind="learn", limit=1)
        base.setdefault("last_import", last[0].result if last else None)
        base.setdefault("unmatched_uploads", self._unmatched_uploads())
        base.setdefault("summary", self._sentences(base))
        return base

    def _unmatched_uploads(self) -> list[str]:
        rows = self.s.db.query(
            "SELECT o.output_id FROM outputs o LEFT JOIN uploads u ON u.output_id = o.output_id"
            " WHERE o.status = 'approved' AND u.output_id IS NULL ORDER BY o.created_at DESC LIMIT 50"
        )
        return [r["output_id"] for r in rows]

    def _db_summary(self) -> dict[str, Any]:
        weights = [
            dict(r)
            for r in self.s.db.query(
                "SELECT channel, pool, option, weight, updated_at FROM pool_weights"
                " ORDER BY pool, weight DESC"
            )
        ]
        pools: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for pool, column in POOL_COLUMNS.items():
            rows = [w for w in weights if w["pool"] == pool]
            if not rows:
                continue
            counts = {
                r["v"]: int(r["n"])
                for r in self.s.db.query(f"SELECT {column} AS v, COUNT(*) AS n FROM combos GROUP BY {column}")
            }
            items = [
                {"option": w["option"], "score": w["weight"], "n": counts.get(w["option"], 0)}
                for w in rows
            ]
            pools[pool] = {"top": items[:3], "bottom": list(reversed(items[-3:])) if len(items) > 3 else []}
        return {"pools": pools, "weights": weights}

    @staticmethod
    def _sentences(summary: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for pool, parts in (summary.get("pools") or {}).items():
            top = (parts or {}).get("top") or []
            if top:
                out.append(f"Best {pool.replace('_', ' ')}: {', '.join(str(t['option']) for t in top)}.")
        if not out:
            out.append("Nothing learned yet. Import your YouTube Analytics export after a week of uploads.")
        return out
