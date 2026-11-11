"""Settings, API keys, diagnostics, templates, voices, logs and the learn summary (component C9)."""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from pydantic import ValidationError

from ns_common import devmode
from ns_common.atomic import write_bytes_atomic
from ns_common.commandline import render, typed
from ns_common.config import save_runtime
from ns_common.models import Runtime
from ns_common.secrets_names import KEY_NAMES, SHORT_NAMES
from ns_dashboard import logs_tail, runtime_flags
from ns_dashboard.common import now_iso
from ns_dashboard.errors import AdapterUnavailable, ApiError
from ns_dashboard.state import (
    AppState,
    load_dashboard_settings,
    load_ui_prefs,
    save_dashboard_settings,
    schedule_is_registered,
    status_schedule_mode,
)
from ns_dashboard.svc_engine import EngineService
from ns_dashboard.templates import load_templates
from ns_supervisor import kitnights

KeyCheck = Callable[[str, str], dict[str, Any]]


def live_key_check(service: str, key: str) -> dict[str, Any]:
    """One small search with the product User-Agent (the installer's checker; Cloudflare 403s urllib's)."""
    from installer.stage2.web.keys import check_key

    return check_key(service, key)


TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
KEY_ALIASES = {short: full for full, short in SHORT_NAMES.items()}
POOL_COLUMNS = {
    "hooks": "hook",
    "structures": "structure",
    "pov_tones": "pov_tone",
    "pacings": "pacing",
    "caption_styles": "caption_style",
    "visual_treatments": "visual_treatment",
    "title_formulas": "title_formula",
}
MAX_BACKUP_BYTES = 1024 * 1024 * 1024
#: How long an import waits for the job thread to finish an Assistant answer before it is refused.
WORKER_STOP_WAIT_S = 120.0
PREVIEW_TEXT = "This is how I will sound when I read your story."


class SettingsService:
    """Everything on the Settings, Learn and Runs & Logs pages that is not a job."""

    def __init__(self, state: AppState, engine: EngineService) -> None:
        self.s = state
        self.engine = engine
        #: the live key check (`installer.stage2.web.keys.check_key`): None skips it (fake services, tests)
        self.key_check: KeyCheck | None = None if state.fake_services else live_key_check

    # --- settings ---
    def _key_flags(self) -> dict[str, bool]:
        out: dict[str, bool] = {}
        for short, full in KEY_ALIASES.items():
            try:
                out[short] = bool(self.s.adapters.keyring.get(full))
            except AdapterUnavailable:
                out[short] = False
        return out

    def _key_problem(self, full: str) -> dict[str, str] | None:
        """What is wrong with the nightly-run copy of key `full` (`NS-E120` lock, `NS-E119`, disk), if anything.

        Only the real keyring adapter knows: the fakes have no file copy.
        """
        ask: Any = getattr(self.s.adapters.keyring, "file_problem", None)
        return ask(full) if ask is not None else None

    def _key_notice(self, full: str) -> dict[str, str] | None:
        """The one-time `NS-W116` the last write of key `full` gave: saved, on a drive without permissions."""
        ask: Any = getattr(self.s.adapters.keyring, "file_notice", None)
        return ask(full) if ask is not None else None

    def _key_warnings(self) -> dict[str, dict[str, str]]:
        """Short key name -> `{code, message, hint, detail}` for every key whose file copy is out of step, or
        whose last write gave the one-time `NS-W116`."""
        out: dict[str, dict[str, str]] = {}
        for short, full in KEY_ALIASES.items():
            problem = self._key_problem(full) or self._key_notice(full)
            if problem:
                out[short] = problem
        return out

    def get_settings(self) -> dict[str, Any]:
        """`{schedule_time, home, profile, flags, keys, key_warnings, notifications, ...}`."""
        refresh = getattr(self.s.adapters.schedule, "refresh", None)
        if refresh is not None:     # the real adapter looks at the Windows task; a fake has none
            refresh()
        stored = load_dashboard_settings(self.s.paths)
        runtime = self.engine.runtime_summary()
        flags = runtime_flags.flatten(runtime or {})
        nxt = self.engine.next_run()
        prefs = load_ui_prefs(self.s.paths)
        return {
            "schedule_time": stored["run_time"],
            # Automatic runs: True only while Windows has an enabled 'Nightshift Daily' task (the switch on
            # the page shows this, never the click). False is off: no task, one disabled in Task Scheduler,
            # or a dev install; `schedule_time` is then the time kept for switching on. `schedule_mode`
            # tells registered from interactive_only (NS-W205: only while signed in)
            "schedule_registered": schedule_is_registered(self.s.paths),
            "schedule_mode": status_schedule_mode(self.s.paths),
            # the command NS-W205's row names, as this install's owner types it in a new terminal (FN-3: the
            # plain words while they reach this install, else its own command file by full path)
            "schedule_install_command": typed(self.s.paths.home, "ns schedule install"),
            "home": str(self.s.paths.home),
            "profile": (runtime or {}).get("profile"),
            "dev_mode": bool((runtime or {}).get("dev_mode")),
            # engine test (docs/DEV_MODE.md): jobs use placeholder footage; not an editable engine flag
            "fake_footage": bool((runtime or {}).get("fake_footage")),
            # engine test (docs/DEV_MODE.md): the story text stages use a 1 to 2 GB model's thresholds
            "tiny_story": bool((runtime or {}).get("tiny_story")),
            # test install (H-10): the simulated graphics card, true only where it is in effect (a dev or
            # engine-test install); set by setup's answers, never by this page
            "fake_gpu": devmode.fake_gpu_in(runtime),
            "flags": flags,
            "keys": self._key_flags(),
            # a key whose copy for nightly runs (config/secrets.dpapi) could not be saved or locked, with the
            # code (NS-E120 / NS-E119) and its hint: the night run reads the file, not Credential Manager; or
            # the one-time NS-W116 (saved on a drive without permissions) the last save of that key gave
            "key_warnings": self._key_warnings(),
            "notifications": bool(stored["notifications"]),
            "runtime_locked": True,
            "install": self.engine.install_summary(),
            "owner_name": prefs["owner_name"],
            "ui_theme": prefs["theme"],
            "next_run": nxt.isoformat(timespec="minutes") if nxt else None,
        }

    @staticmethod
    def check_settings(body: dict[str, Any]) -> None:
        """Refuse a schedule time, switch or notifications value `put_settings` would refuse (also the Assistant's card gate)."""
        if "schedule_time" in body and not TIME_RE.match(str(body["schedule_time"])):
            raise ApiError(422, "bad_time", "Use a 24-hour time like 03:00.")
        if "automatic_runs" in body and not isinstance(body["automatic_runs"], bool):
            raise ApiError(422, "invalid_options", "'automatic_runs' must be true or false.")
        if "notifications" in body and not isinstance(body["notifications"], bool):
            raise ApiError(422, "invalid_options", "'notifications' must be true or false.")

    def put_settings(self, body: dict[str, Any], unlock: bool = False) -> dict[str, Any]:
        """Save Automatic runs (the switch and the time), notifications and (only when unlocked) runtime flags.

        `schedule_result` in the answer is what the page says about the switch or the time: `applied`, a
        `note` in words and the `code` (NS-W205, NS-E204, NS-E206) when there is one. `schedule_registered`,
        `schedule_time` and `next_run` beside it are read after the change, from the record of what Windows has.
        """
        unlocked = unlock or body.get("unlock") is True
        schedule_result: dict[str, Any] | None = None
        if body.get("automatic_runs") is True and (self.engine.runtime_summary() or {}).get("dev_mode"):
            raise ApiError(     # before anything is saved: a refusal changes nothing
                409, "dev_mode", "Automatic runs cannot be switched on in a dev install.",
                hint="A dev install's nightly task is registered disabled and never runs (docs/DEV_MODE.md).",
            )
        if "flags" in body or "profile" in body:
            if not unlocked:
                raise ApiError(
                    409, "runtime_locked", "Engine settings are locked.",
                    hint="Switch on 'Unlock editing' before changing them.",
                )
            self._save_runtime(body)
        updates: dict[str, Any] = {}
        self.check_settings(body)
        if "schedule_time" in body:
            updates["run_time"] = str(body["schedule_time"])
        if "notifications" in body:
            updates["notifications"] = body["notifications"]
        if "automatic_runs" in body:
            if body.get("by") != kitnights.BY_KIT:  # the owner's own click: theirs, never undone by the kit
                kitnights.owner_chose(self.s.paths, datetime.now().astimezone(), "Settings", self.s.log)
            # the switch (and the time with it): Windows first, and the answer is what Windows then has
            schedule_result = self.s.adapters.schedule.switch(body["automatic_runs"], updates.get("run_time"))
        elif "run_time" in updates:  # the task first: a time Windows refused must not be remembered as the schedule
            schedule_result = self.s.adapters.schedule.apply(updates["run_time"])
        if schedule_result is not None and schedule_result.get("applied") is False:
            updates.pop("run_time", None)
        if updates:
            save_dashboard_settings(self.s.paths, updates)
        out = self.get_settings()
        if schedule_result is not None:
            note = schedule_result.get("note")
            if isinstance(note, str):           # the commands it names, as this install's owner types them
                schedule_result = {**schedule_result, "note": render(note, self.s.paths.home)}
            out["schedule_result"] = schedule_result
        self.s.log.info("settings_saved", fields=sorted(body))
        return out

    def _save_runtime(self, body: dict[str, Any]) -> None:
        current = self.engine.runtime_summary()
        if current is None:
            raise ApiError(409, "no_runtime", "runtime.json does not exist yet; run the installer first.")
        try:
            merged = runtime_flags.apply_flags(current, body.get("flags") or {})
        except ValueError as e:
            raise ApiError(
                422,
                "test_switch",
                f"'{e}' belongs to an install made for testing; the dashboard cannot set it.",
                hint="It is an answer in answers.json at setup (docs/DEV_MODE.md).",
            ) from e
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
        if not secret:                                   # an empty value clears the key
            try:
                self.s.adapters.keyring.delete(full)
            except AdapterUnavailable as e:
                raise ApiError(501, "keyring_unavailable", str(e)) from e
            self.s.log.info("api_key_cleared", name=full)
            problem = self._key_problem(full)
            if problem:  # Credential Manager forgot it, the file still has it (and `get` reads it back)
                self.s.log.warn("api_key_clear_incomplete", problem["detail"], name=full, code=problem["code"])
                return {
                    "name": full, "set": True, "warning": problem,
                    "message": f"{problem['code']}: the key is still stored for nightly runs. {problem['hint']}",
                }
            notice = self._key_notice(full)
            if notice:  # cleared; the rest of the file was saved without a lock (once per home)
                return {
                    "name": full, "set": False, "warning": notice,
                    "message": f"Key cleared. {notice['code']}: {notice['message']} {notice['hint']}",
                }
            return {"name": full, "set": False}
        if len(secret) > 512:
            raise ApiError(400, "bad_key", "Paste the key without spaces (up to 512 characters).")
        short = SHORT_NAMES.get(full, full)
        service = short[0].upper() + short[1:]
        verdict = self.key_check(short, secret) if self.key_check is not None else None
        if verdict is not None and not verdict.get("ok") and verdict.get("code") != "NS-E503":
            # the service answered and did not accept the key: not stored, so a working key is not replaced
            raise ApiError(
                422, str(verdict.get("code") or "bad_key"),
                f"{service} did not accept this key. Paste it again from your {service} account page "
                "(no spaces); the key you had, if any, is kept.",
            )
        try:
            self.s.adapters.keyring.set(full, secret)
        except AdapterUnavailable as e:
            raise ApiError(501, "keyring_unavailable", str(e)) from e
        self.s.log.info("api_key_set", name=full, checked=bool(verdict and verdict.get("ok")))
        out: dict[str, Any] = {"name": full, "set": True}
        if verdict is not None:
            out["checked"] = bool(verdict.get("ok"))
            out["message"] = (
                f"{service} accepted the key" if verdict.get("ok")
                else f"Key saved, but {service} could not be reached to check it (no internet?)"
            )
        problem = self._key_problem(full)
        if problem:
            # saved in Credential Manager, not in the file the night run reads: the old key stays there
            out["warning"] = problem
            note = (
                f"{problem['code']}: the copy of the {service} key that nightly runs read when Credential "
                f"Manager is out of reach was not updated (it may still be an older key). {problem['hint']}"
            )
            out["message"] = f"{out['message']}. {note}" if out.get("message") else f"Key saved. {note}"
            return out
        notice = self._key_notice(full)
        if notice:
            # saved in both places, but on a drive that keeps no permissions: said once per home
            out["warning"] = notice
            note = f"{notice['code']}: {notice['message']} {notice['hint']}"
            out["message"] = f"{out['message']}. {note}" if out.get("message") else f"Key saved. {note}"
        return out

    # --- diagnostics ---
    @staticmethod
    def _diagnostics_unavailable(e: AdapterUnavailable) -> ApiError:
        return ApiError(
            501, "diagnostics_unavailable", str(e),
            hint="Use the 'Nightshift - Diagnostics' desktop shortcut instead.",
        )

    def check_diagnostics(self) -> None:
        """Refuse (501 `diagnostics_unavailable`) when the doctor that writes the zip cannot be loaded.

        The Assistant runs this before it shows its confirm card (D-27); nothing is written.
        """
        try:
            self.s.adapters.doctor.available()
        except AdapterUnavailable as e:
            raise self._diagnostics_unavailable(e) from e

    def diagnostics(self) -> dict[str, Any]:
        """Create the diagnostics zip (`ns doctor --bundle`)."""
        try:
            path = Path(self.s.adapters.doctor.bundle())
        except AdapterUnavailable as e:
            raise self._diagnostics_unavailable(e) from e
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
        write_bytes_atomic(cache, data)  # a second preview request must not be served half a wav
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
        return {**base, **self._results_page(last[0] if last else None)}

    def _results_page(self, last: Any) -> dict[str, Any]:
        """The fields the Results page draws: the last import, top and bottom videos, daily views, milestone."""
        from ns_dashboard import learn_view
        from ns_dashboard.svc_jobs import learn_response

        shown = learn_response(last.result or {}) if last is not None else {}
        top, bottom = learn_view.top_bottom(self.s.db)
        channels = [c.slug for c in self.engine.channels.load_all()]
        return {
            "imported_at": last.finished_at if last is not None else None,
            "matched": shown.get("matched"),
            "unmatched": shown.get("unmatched"),
            "changes": shown.get("changes", []),
            "top": top,
            "bottom": bottom,
            "daily": learn_view.daily(self.s.db),
            "milestone": learn_view.milestone(self.s.db, channels),
        }

    def _unmatched_uploads(self) -> list[str]:
        rows = self.s.db.query(
            "SELECT o.output_id FROM outputs o LEFT JOIN uploads u ON u.output_id = o.output_id"
            " WHERE o.status = 'approved' AND u.output_id IS NULL AND o.profile IS NULL ORDER BY o.created_at DESC LIMIT 50"
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

    # --- export / import / uninstall ---
    def export_settings(self) -> dict[str, Any]:
        """`ns backup`: a zip of settings, voices, the database and exemplars (never models or videos)."""
        path = Path(self.s.adapters.backup.export())
        self.s.last_backup = path
        size = path.stat().st_size if path.exists() else None
        self.s.log.info("settings_exported", path=str(path))
        out: dict[str, Any] = {
            "path": str(path), "name": path.name, "size": size, "download_url": "/api/settings/export/latest",
        }
        # what `ns backup` left out on purpose (the keys file, when the zip goes to a drive without permissions)
        notes = [str(n) for n in getattr(self.s.adapters.backup, "last_notes", None) or []]
        if notes:
            out["left_out"] = notes
            out["note"] = " ".join(notes)
        return out

    def latest_backup(self) -> Path:
        """The backup zip made most recently in this session."""
        path = self.s.last_backup
        if path is None or not path.is_file():
            raise ApiError(404, "not_found", "No settings export has been made yet.")
        return path

    def _restore_with_db_closed(self, zip_path: Path) -> list[str]:
        """Run the restore while the dashboard holds no connection to `studio.db`.

        On Windows the restore cannot replace a database whose `-wal` and `-shm` files are open (WinError 32),
        and overwriting one that live connections use risks corruption. The worker is stopped (no job runs: the
        caller checked), the connection is closed for the restore and opened again afterwards, and the worker
        starts again, whether the restore worked or not.
        """
        was_started = self.s.worker.started
        if was_started and not self.s.worker.stop(WORKER_STOP_WAIT_S):
            # the thread is inside an Assistant answer: carry on with it (no second worker) and ask again later
            self.s.worker.start()
            raise ApiError(
                409, "assistant_busy", "Nightshift is still answering a saved request.",
                hint="Wait a minute for the answer to finish, then import the settings again.",
            )
        try:
            with self.s.db.released():
                return self.s.adapters.backup.restore(zip_path)
        finally:
            if was_started:
                self.s.worker.start()

    def import_settings(self, filename: str, fh: BinaryIO) -> dict[str, Any]:
        """Save an uploaded backup zip and restore it with `ns restore`."""
        if self.s.store.running() is not None:
            raise ApiError(
                409, "job_running", "A job is running right now.",
                hint="Wait for it to finish (or cancel it), then import the settings again.",
            )
        if self.s.draining:
            raise ApiError(503, "shutting_down", "Nightshift is shutting down.")
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename or "backup.zip").name) or "backup.zip"
        if not name.lower().endswith(".zip"):
            raise ApiError(400, "bad_file_type", "Choose the nightshift-backup zip that Export settings made.")
        stamp = now_iso().replace(":", "").replace("-", "")[:15]
        dest = self.s.paths.home / "imports" / "restore" / f"{stamp}-{name}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with dest.open("wb") as out:
            while block := fh.read(1024 * 1024):
                size += len(block)
                if size > MAX_BACKUP_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise ApiError(413, "file_too_large", "That file is larger than 1 GB, so it is not a settings backup.")
                out.write(block)
        if size == 0:
            dest.unlink(missing_ok=True)
            raise ApiError(400, "empty_file", "That file is empty.")
        lines = self._restore_with_db_closed(dest)
        self.s.log.warn("settings_restored", "settings restored from an uploaded backup", file=name)
        # the keys file is the one file a restore may leave as it was (NS-W116 on a drive without permissions,
        # NS-E120 when its lock fails): the page shows that line, not only "restored"
        left_out = [ln for ln in lines if "secrets.dpapi" in ln and ln != "restored config/secrets.dpapi"]
        note = "Restart Nightshift to use the restored settings."
        if left_out:
            note = f"{note} {' '.join(left_out)}"
        return {"restored": True, "file": name, "lines": lines, "left_out": left_out, "note": note}

    def uninstall_info(self) -> dict[str, Any]:
        """Where UNINSTALL.cmd is (the page copies the path; Windows asks before deleting anything)."""
        path = Path(self.s.adapters.backup.uninstall_path())
        return {"path": str(path), "exists": path.is_file()}
