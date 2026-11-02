"""Creating and managing dashboard jobs: run, clip, learn, verify; plus upload plumbing (component C9)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from ns_common.db import new_job_id
from ns_common.paths import Paths
from ns_dashboard.adapters import Adapters
from ns_dashboard.common import KNOWN_FORMATS, now_iso, offered_formats
from ns_dashboard.errors import ApiError
from ns_dashboard.jobs import Handler, JobControl
from ns_dashboard.state import AppState
from ns_dashboard.svc_channels import ChannelsService
from ns_dashboard.templates import load_templates
from ns_dashboard.uploads import ALLOWED_EXT, CHUNK, UploadInfo, UploadManager, find_source

CLIP_PARAM_KEYS = ("channel", "template", "max_clips", "min_s", "max_s", "caption_style", "rights_confirmed")
MAX_CSV_BYTES = 20 * 1024 * 1024


URL_RE = re.compile(r"^https?://[^\s/@:]+(?::\d+)?(?:[/?#]\S*)?$", re.IGNORECASE)
MAX_URL_CHARS = 2000


def clean_url(value: Any) -> str | None:
    """A validated http(s) link (no credentials, no spaces), or None when no link was given."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if len(text) > MAX_URL_CHARS or not URL_RE.match(text):
        raise ApiError(
            400, "bad_url", "Paste a full web link that starts with http:// or https://",
            hint="Links with a user name or password in them are not accepted.",
        )
    return text


def read_clip_layout(paths: Paths | None, studio_job_id: Any) -> dict[str, Any] | None:
    """What the Clipper chose for a clip job (`clip_layout.json`): the layout, per clip, and a face cam or not."""
    if paths is None or not isinstance(studio_job_id, str) or not studio_job_id:
        return None
    try:
        raw = json.loads((paths.job_dir(studio_job_id) / "clip_layout.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("template"), str):
        return None
    return {
        "template": raw["template"],
        "per_clip": raw.get("per_clip") or {},
        "notes": raw.get("notes") or {},
        "no_facecam": raw.get("facecam") in (None, {}),
    }


def build_handlers(adapters: Adapters, paths: Paths | None = None) -> dict[str, Handler]:
    """Job handlers that call the adapters; each returns the job's `result`.

    With `paths`, a clip result also says which layout the Clipper chose (`layout`).
    """

    def run_handler(ctl: JobControl) -> dict[str, Any]:
        p = ctl.params
        ids = adapters.run.run(ctl, str(p["channel"]), str(p["format"]), p.get("count"))
        return {"output_ids": ids, **ctl.extras}

    def clip_handler(ctl: JobControl) -> dict[str, Any]:
        p = ctl.params
        opts = {k: p.get(k) for k in CLIP_PARAM_KEYS}
        opts["rerun"] = bool(p.get("rerun"))
        if p.get("studio_job_id"):  # "Re-run with another template": the later stages of an earlier clip job
            ids = adapters.clip.rerun(ctl, str(p["studio_job_id"]), opts)
            done = {"output_ids": ids, "source_id": p.get("source_id"), "rerun_of": p.get("rerun_of"),
                    "layout": read_clip_layout(paths, p["studio_job_id"])}
            return {**done, **ctl.extras}
        if p.get("url"):  # download first (stage "download"), then run the usual pipeline on the file
            source = adapters.clip.fetch_url(ctl, str(p["url"]), str(p["source_id"]))
        else:
            source = Path(str(p["source_path"]))
        ids = adapters.clip.clip(ctl, source, str(p["source_id"]), opts)
        layout = read_clip_layout(paths, ctl.extras.get("studio_job_id"))
        return {"output_ids": ids, "source_id": p["source_id"], "layout": layout, **ctl.extras}

    def learn_handler(ctl: JobControl) -> dict[str, Any]:
        return adapters.learn.import_csv(ctl, Path(str(ctl.params["csv_path"])))

    def verify_handler(ctl: JobControl) -> dict[str, Any]:
        return adapters.verify.verify(ctl, bool(ctl.params.get("quick", True)))

    def series_part_handler(ctl: JobControl) -> dict[str, Any]:
        p = ctl.params
        ids = adapters.series.produce(ctl, p)
        return {"output_ids": ids, "story_slug": p.get("story_slug"), "part_n": p.get("part_n")}

    return {
        "run": run_handler,
        "clip": clip_handler,
        "learn": learn_handler,
        "verify": verify_handler,
        "series_part": series_part_handler,
    }


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, list | tuple | dict | set):
        return len(value)
    return 0


def learn_response(result: dict[str, Any], job_id: str | None = None) -> dict[str, Any]:
    """Shape an import result as `{matched, unmatched, summary: [str], changes: [{pool, option, delta}]}`."""
    matched = _count(result.get("matched"))
    unmatched_raw = result.get("unmatched", [])
    unmatched = _count(unmatched_raw)
    changes: list[dict[str, Any]] = []
    for c in result.get("changes") or result.get("weight_changes") or []:
        if isinstance(c, dict):
            delta = c.get("delta", c.get("change", 0))
            changes.append(
                {"pool": c.get("pool", ""), "option": c.get("option", ""), "delta": float(delta or 0)}
            )
    summary = result.get("summary")
    if not (isinstance(summary, list) and all(isinstance(s, str) for s in summary)):
        summary = [
            f"{matched} row(s) matched videos you uploaded.",
            f"{unmatched} row(s) had no matching upload yet.",
        ]
        for c in sorted(changes, key=lambda x: -abs(x["delta"]))[:3]:
            word = "up" if c["delta"] > 0 else "down"
            summary.append(f"'{c['option']}' ({c['pool']}) moved {word} by {abs(c['delta']):.2f}.")
    out: dict[str, Any] = {"matched": matched, "unmatched": unmatched, "summary": summary, "changes": changes}
    if isinstance(unmatched_raw, list):
        out["unmatched_rows"] = unmatched_raw
    if job_id:
        out["job_id"] = job_id
    return out


MAX_SCHEDULE_AHEAD = timedelta(days=31)


def parse_schedule_at(value: Any, now: datetime) -> datetime | None:
    """The start time of a job request as an aware local datetime, or None to start now.

    `null`, an empty value and a time that has already passed all mean "now". A time without a zone is the
    PC's local time (CR-C3-UI-5). Raises 422 for text that is not a date and time, or one over 31 days away.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        at = value
    elif isinstance(value, str):
        try:
            at = datetime.fromisoformat(value.strip())
        except ValueError as e:
            raise ApiError(
                422, "invalid_options", "'schedule_at' must be a date and time such as 2026-11-03T02:00."
            ) from e
    else:
        raise ApiError(422, "invalid_options", "'schedule_at' must be a date and time or null.")
    at = at.astimezone()
    if at - now > MAX_SCHEDULE_AHEAD:
        raise ApiError(422, "invalid_options", "'schedule_at' can be at most 31 days from now.")
    return at if at > now else None


def _int(body: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    raw = body.get(key)
    if raw in (None, ""):
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError) as e:
        raise ApiError(422, "invalid_options", f"'{key}' must be a whole number.") from e
    if not lo <= val <= hi:
        raise ApiError(422, "invalid_options", f"'{key}' must be between {lo} and {hi}.")
    return val


class JobsService:
    """Validates requests, creates `dashboard_jobs` rows and wakes the worker."""

    def __init__(self, state: AppState, channels: ChannelsService) -> None:
        self.s = state
        self.channels = channels
        self.uploads = UploadManager(state.paths)

    def _accepting(self) -> None:
        if self.s.draining:
            raise ApiError(503, "shutting_down", "Nightshift is shutting down and not accepting new jobs.")

    def _schedule(self, body: dict[str, Any]) -> datetime | None:
        return parse_schedule_at(body.get("schedule_at"), self.s.store.clock())

    def _enqueue(
        self, kind: str, channel: str | None, params: dict[str, Any], schedule_at: datetime | None = None
    ) -> dict[str, Any]:
        """Insert the job; with `schedule_at` it stays `queued` (message "Scheduled for ...") until then."""
        if schedule_at is not None:
            params = {**params, "schedule_at": schedule_at.isoformat(timespec="seconds")}
        rec = self.s.store.create(kind, channel, params)
        if schedule_at is not None:
            self.s.store.update_progress(rec.job_id, 0.0, f"Scheduled for {schedule_at:%a %d %b %H:%M}")
            rec = self.s.store.get(rec.job_id) or rec
        self.s.worker.enqueue(rec.job_id)
        self.s.hub.publish("dashboard_job", rec.to_dict())
        return rec.to_dict()

    # --- creation ---
    def create_run(self, body: dict[str, Any]) -> dict[str, Any]:
        """Queue `studio run` for one channel; `schedule_at` (null = now) holds it until that time."""
        self._accepting()
        slug = str(body.get("channel") or "")
        ch = self.channels.load(slug)
        if ch.status == "paused":
            raise ApiError(409, "channel_paused", f"'{ch.display_name}' is paused. Resume it first.")
        fmt = str(body.get("format") or ch.format)
        if fmt not in offered_formats():
            if fmt in KNOWN_FORMATS:
                raise ApiError(422, "invalid_options", f"'{fmt}' is not available yet.")
            raise ApiError(422, "invalid_options", f"Unknown video type '{fmt}'.")
        count = body.get("count")
        n = _int(body, "count", 1, 1, 20) if count else None
        params: dict[str, Any] = {"channel": slug, "format": fmt, "count": n}
        return self._enqueue("run", slug, params, self._schedule(body))

    def create_clip_rerun(self, body: dict[str, Any]) -> dict[str, Any]:
        """Run the later stages of a finished clip job again with another template (no new transcription)."""
        self._accepting()
        rec = self.s.store.get(str(body.get("job_id") or ""))
        if rec is None or rec.kind != "clip":
            raise ApiError(404, "not_found", "That clip job was not found.")
        if rec.status != "done":
            raise ApiError(409, "not_finished", "Wait until that clip job has finished.")
        template = str(body.get("template") or "")
        if template not in {t["id"] for t in load_templates(self.s.paths)}:
            raise ApiError(422, "invalid_options", f"Unknown layout '{template}'.")
        studio_job_id = self._studio_job_id(rec.result or {})
        if studio_job_id is None:
            raise ApiError(
                409, "cannot_rerun", "That clip job cannot be re-run.",
                hint="Start a new clip job from the Create page.",
            )
        params = {k: rec.params.get(k) for k in CLIP_PARAM_KEYS}
        params.update(
            {
                "template": template,
                "max_clips": _int(body, "max_clips", int(params.get("max_clips") or 6), 1, 10),
                "studio_job_id": studio_job_id,
                "rerun_of": rec.job_id,
                "source_id": rec.params.get("source_id"),
                "rights_confirmed": True,
            }
        )
        return self._enqueue("clip", rec.channel, params)

    def _studio_job_id(self, result: dict[str, Any]) -> str | None:
        """The studio job directory of a finished clip job: noted in its result, else from its outputs."""
        noted = result.get("studio_job_id")
        if isinstance(noted, str) and noted:
            return noted
        ids = [str(i) for i in result.get("output_ids") or []]
        if not ids:
            return None
        marks = ",".join("?" for _ in ids)
        row = self.s.db.one(f"SELECT job_id FROM outputs WHERE output_id IN ({marks}) LIMIT 1", ids)
        return str(row["job_id"]) if row else None

    def create_verify(self, body: dict[str, Any]) -> dict[str, Any]:
        """Queue `ns verify`."""
        self._accepting()
        return self._enqueue("verify", None, {"quick": bool(body.get("quick", True))})

    def _save_csv(self, filename: str, fh: BinaryIO) -> dict[str, Any]:
        """Save an analytics CSV under `imports/analytics` and create its (inline) learn job."""
        self._accepting()
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename or "analytics.csv").name) or "analytics.csv"
        if not name.lower().endswith(".csv"):
            raise ApiError(400, "bad_file_type", "Please choose the .csv file exported from YouTube Studio.")
        data = fh.read(MAX_CSV_BYTES + 1)
        if not data:
            raise ApiError(400, "empty_file", "That file is empty.")
        if len(data) > MAX_CSV_BYTES:
            raise ApiError(413, "file_too_large", "That CSV is larger than 20 MB.")
        dest_dir = self.s.paths.imports_analytics
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = now_iso().replace(":", "").replace("-", "")[:15]
        dest = dest_dir / f"{stamp}-{name}"
        dest.write_bytes(data)
        rec = self.s.store.create("learn", None, {"csv_path": str(dest), "filename": name})
        return rec.to_dict()

    def import_learn(self, filename: str, fh: BinaryIO) -> dict[str, Any]:
        """Import an analytics CSV now (no GPU needed) and answer with the plain-English result.

        The import still runs as a `learn` job so it shows up in the job history.
        """
        job = self._save_csv(filename, fh)
        done = self.s.worker.run_inline(job["job_id"])
        if done.status != "done":
            raise ApiError(500, "learn_failed", done.message or "The import failed.")
        return learn_response(done.result or {}, done.job_id)

    def import_stream(self, filename: str, fh: BinaryIO) -> UploadInfo:
        """Store a streamed multipart upload through the chunked-upload manager."""
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(0)
        info = self.uploads.start(filename, size)
        offset = 0
        while offset < size:
            block = fh.read(CHUNK)
            if not block:
                break
            info = self.uploads.append(info.upload_id, offset, block)
            offset += len(block)
        return self.uploads.complete(info.upload_id)

    def require_rights(self, body: dict[str, Any]) -> None:
        """Refuse to start without the owner's rights confirmation (spec 19.1); also blocks while draining."""
        self._accepting()
        if body.get("rights_confirmed") is not True:
            raise ApiError(
                400, "rights_required", "Please confirm that you own this video or are licensed to use it.",
                hint="Tick 'I own or have rights to this video' before starting.",
            )

    def create_clip(self, body: dict[str, Any], uploaded: UploadInfo | None = None) -> dict[str, Any]:
        """Queue a Clipper job. `uploaded` is a multipart file already stored via `import_stream`."""
        self.require_rights(body)
        schedule_at = self._schedule(body)
        slug = str(body.get("channel") or "")
        self.channels.load(slug)
        template = str(body.get("template") or "auto")
        if template not in {t["id"] for t in load_templates(self.s.paths)}:
            raise ApiError(422, "invalid_options", f"Unknown layout '{template}'.")
        min_s = _int(body, "min_s", 20, 5, 178)
        max_s = _int(body, "max_s", 60, 6, 179)
        if max_s <= min_s:
            raise ApiError(422, "invalid_options", "'max_s' must be larger than 'min_s'.")
        params: dict[str, Any] = {
            "channel": slug,
            "template": template,
            "max_clips": _int(body, "max_clips", 6, 1, 10),
            "min_s": min_s,
            "max_s": max_s,
            "caption_style": body.get("caption_style") or None,
            "rights_confirmed": True,
        }
        url = clean_url(body.get("url"))
        if url:
            if uploaded is not None or any(body.get(k) for k in ("path", "upload_id", "source_id")):
                raise ApiError(422, "invalid_options", "Give either a link, a file, or a path, not several.")
            source_id, source_path, rerun = new_job_id("src"), None, False
            params["url"] = url
        else:
            source_id, source_path, rerun = self._resolve_source(body, uploaded)
        params.update({"source_id": source_id, "source_path": str(source_path) if source_path else None,
                       "rerun": rerun})
        # the Clipper import creates the `sources` row; for a re-run it exists, so record the rights now
        self.s.db.execute("UPDATE sources SET rights_confirmed = 1 WHERE source_id = ?", (source_id,))
        return self._enqueue("clip", slug, params, schedule_at)

    def _registered_source(self, source_id: str) -> Path | None:
        """Path stored in the `sources` table (set by the Clipper import), if the file still exists."""
        row = self.s.db.one("SELECT path FROM sources WHERE source_id = ?", (source_id,))
        if row is None:
            return None
        p = Path(row["path"])
        return p if p.is_file() else None

    def _resolve_source(self, body: dict[str, Any], uploaded: UploadInfo | None) -> tuple[str, Path, bool]:
        if uploaded is not None and uploaded.path:
            return uploaded.source_id, Path(uploaded.path), False
        for key, rerun in (("upload_id", False), ("source_id", True)):
            sid = body.get(key)
            if sid:
                found = find_source(self.s.paths, str(sid)) or self._registered_source(str(sid))
                if found is None:
                    code = "upload_incomplete" if key == "upload_id" else "not_found"
                    status = 409 if key == "upload_id" else 404
                    raise ApiError(status, code, "That video has not finished uploading.")
                if key == "upload_id" and found.suffix.lower() not in ALLOWED_EXT:
                    raise ApiError(400, "bad_file_type", f"'{found.suffix}' is a voice recording.")
                return str(sid), found, rerun
        raw = body.get("path")
        if raw:
            p = Path(str(raw)).expanduser()
            if not p.is_file() or p.suffix.lower() not in ALLOWED_EXT:
                raise ApiError(400, "bad_path", f"'{raw}' is not a video file on this PC.")
            return new_job_id("src"), p, False
        raise ApiError(400, "no_source", "Choose a video: upload it, give a local path, or paste a link.")

    # --- queries / cancel ---
    def list_jobs(self, status: str | None, kind: str | None, limit: int) -> list[dict[str, Any]]:
        """Newest first."""
        return [j.to_dict() for j in self.s.store.list_jobs(status or None, kind or None, limit)]

    def get(self, job_id: str) -> dict[str, Any]:
        """One job or 404."""
        rec = self.s.store.get(job_id)
        if rec is None:
            raise ApiError(404, "not_found", "That job was not found.")
        return rec.to_dict()

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Cancel a queued or running job; 409 when it already ended."""
        rec = self.s.store.get(job_id)
        if rec is None:
            raise ApiError(404, "not_found", "That job was not found.")
        if rec.status not in ("queued", "running"):
            raise ApiError(409, "already_finished", f"That job already ended ({rec.status}).")
        self.s.worker.cancel(job_id)
        return self.get(job_id)
