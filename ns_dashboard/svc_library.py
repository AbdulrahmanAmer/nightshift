"""Library page operations: idea bank, stories, footage cache and formats (component C9, spec 20.2)."""
# Dense descriptor tables and long signatures: wrapping them hurts readability.
# ruff: noqa: E501

from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from ns_common.errors import NsError
from ns_common.secrets_names import SHORT_NAMES
from ns_dashboard import formats, updater
from ns_dashboard.common import now_iso
from ns_dashboard.errors import AdapterUnavailable, ApiError
from ns_dashboard.state import AppState
from ns_dashboard.svc_channels import ChannelsService
from ns_studio.footage_keys import NO_FOOTAGE_KEY

#: The names `ns models get` takes: the VoiceDesign talker and the story LLM.
MODEL_NAMES = ("voicedesign", "story_llm")
IDEA_STATUSES = ("new", "approved", "used", "rejected")
IDEA_SOURCES = ("owner", "assistant", "generated")
STATUS_ALIASES = {"dropped": "rejected"}  # the UI says "Dropped"; the shared schema says "rejected"

if TYPE_CHECKING:
    from collections.abc import Callable

    from ns_common.paths import Paths
    from ns_studio.media.clip_sources import ClipCandidate, ClipSource, Orientation

FOOTAGE_SOURCES = ("auto", "pixabay", "pexels")
FOOTAGE_ORIENTATIONS = ("landscape", "portrait")
FOOTAGE_MAX = 5  # clips per request: a chat request must not become a bulk download
_FOOTAGE_API_ERRORS = ("NS-E501", "NS-E502", "NS-E503")  # the codes the clip sources use (clips._API_ERRORS)
_SAFE_CLIP_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")  # becomes a file name in the cache
#: The update package the release carries (`Nightshift-Update-0.1.8.zip`; releases up to 0.1.1 named it `nightshift-app-...`).
UPDATE_ZIP_RE = re.compile(r"^(?:Nightshift-Update|nightshift-app)-\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.]+)?\.zip$", re.IGNORECASE)
MAX_UPDATE_BYTES = 200 * 1024 * 1024  # the Update zip is a few MB; the bundle check allows 25 MB


def _save_upload(fh: BinaryIO, dest: Path, limit: int, too_big: str) -> int:
    """Write `fh` to `dest` in 1 MB blocks; more than `limit` bytes deletes the file and raises 413."""
    size = 0
    with dest.open("wb") as out:
        while block := fh.read(1024 * 1024):
            size += len(block)
            if size > limit:
                out.close()
                dest.unlink(missing_ok=True)
                raise ApiError(413, "file_too_large", too_big)
            out.write(block)
    if size == 0:
        dest.unlink(missing_ok=True)
    return size


def _real_footage_sources(keys: dict[str, str], paths: Paths) -> list[ClipSource]:
    """Keyed Pixabay/Pexels sources for the names in `keys`, in that order (same construction as `build_services`)."""
    from ns_studio.media.clip_sources import PexelsSource, PixabaySource

    out: list[ClipSource] = []
    if "pixabay" in keys:
        out.append(PixabaySource(keys["pixabay"], paths.cache_search))
    if "pexels" in keys:
        out.append(PexelsSource(keys["pexels"], paths.cache_search))
    return out


def _stored_keywords(raw: str | None, fallback: str) -> list[str]:
    """The keyword list of a `clips` row (JSON text), or `[fallback]` when it is empty or unreadable."""
    try:
        found = json.loads(raw) if raw else []
    except ValueError:
        found = []
    return [str(k) for k in found] if isinstance(found, list) and found else [fallback]


def _footage_item(cand: ClipCandidate, path: Path, keywords: list[str], *, new: bool) -> dict[str, Any]:
    """One clip as the Assistant and the API report it: the absolute path first."""
    return {
        "path": str(path.absolute()), "source": cand.source, "clip_id": cand.id, "page_url": cand.page_url,
        "license": cand.license, "size": f"{cand.width}x{cand.height}", "width": cand.width,
        "height": cand.height, "duration": cand.duration, "keywords": keywords, "new": new,
    }


def like(q: str) -> str:
    """`%q%` with the LIKE wildcards in `q` escaped with a backslash (use with ESCAPE)."""
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def _idea(row: sqlite3.Row) -> dict[str, Any]:
    status = row["status"] or "new"
    if status == "new" and row["used"]:
        status = "used"
    return {
        "idea_id": row["idea_id"],
        "channel": row["channel"],
        "title": row["title"] or row["premise"][:60],
        "premise": row["premise"],
        "twist": row["twist"],
        "status": status,
        "source": row["source"] or "generated",
        "job_id": row["job_id"],
        "created_at": row["created_at"],
    }


def story_status(statuses: list[str]) -> str:
    """One word for a story from the states of its videos: pending, approved, posted, rejected or none."""
    if not statuses:
        return "none"
    if "pending" in statuses:
        return "pending"
    if "uploaded" in statuses:
        return "posted"
    if "approved" in statuses:
        return "approved"
    return "rejected"


class LibraryService:
    """Reads and writes what Nightshift remembers."""

    def __init__(self, state: AppState, channels: ChannelsService) -> None:
        self.s = state
        self.channels = channels
        #: Builds the keyed clip sources from `{name: key}`; tests replace it with stubs (no network).
        self.footage_sources: Callable[[dict[str, str], Paths], list[ClipSource]] | None = None

    # --- ideas ---
    def list_ideas(self, channel: str | None = None, status: str | None = None, q: str | None = None,
                   limit: int = 200) -> list[dict[str, Any]]:
        """Idea bank entries, newest first."""
        status = STATUS_ALIASES.get(status or "", status)
        if status and status not in IDEA_STATUSES:
            raise ApiError(422, "bad_filter", f"Unknown idea status '{status}'.")
        where, args = [], []
        if channel:
            where.append("channel = ?")
            args.append(channel)
        if status == "used":
            where.append("(status = 'used' OR (status = 'new' AND used = 1))")
        elif status == "new":
            where.append("status = 'new' AND used = 0")
        elif status:
            where.append("status = ?")
            args.append(status)
        if q:
            where.append("(premise LIKE ? ESCAPE '\\' OR twist LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\')")
            args += [like(q)] * 3
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self.s.db.query(
            f"SELECT * FROM ideas{clause} ORDER BY idea_id DESC LIMIT ?", [*args, max(1, min(limit, 500))]
        )
        return [_idea(r) for r in rows]

    def add_idea(self, body: dict[str, Any]) -> dict[str, Any]:
        """Add an idea typed by the owner (or the Assistant) for a channel."""
        slug = str(body.get("channel") or "")
        ch = self.channels.load(slug)
        premise = str(body.get("premise") or "").strip()
        if len(premise) < 10 or len(premise) > 600:
            raise ApiError(422, "invalid_idea", "The premise should be one to three sentences (10 to 600 characters).")
        status = str(body.get("status") or "new")
        if status not in ("new", "approved"):
            raise ApiError(422, "invalid_idea", "A new idea starts as 'new' or 'approved'.")
        source = str(body.get("source") or "owner")
        if source not in IDEA_SOURCES:
            raise ApiError(422, "invalid_idea", f"'source' must be one of {', '.join(IDEA_SOURCES)}.")
        title = str(body.get("title") or "").strip()[:100] or premise[:60]
        now = now_iso()
        with self.s.db.tx() as db:
            db.execute(
                "INSERT OR IGNORE INTO channels(slug, display_name, created_at, status) VALUES (?, ?, ?, ?)",
                (ch.slug, ch.display_name, ch.created_at or now, ch.status),
            )
            db.execute(
                "INSERT INTO ideas(channel, premise, twist, created_at, used, status, title, source)"
                " VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                (slug, premise, str(body.get("twist") or "").strip(), now, status, title, source),
            )
            new_id = db.one("SELECT last_insert_rowid() AS id")
        assert new_id is not None
        self.s.log.info("idea_added", idea_id=new_id["id"], channel=slug, source=source)
        return self.get_idea(int(new_id["id"]))

    def get_idea(self, idea_id: int) -> dict[str, Any]:
        """One idea or 404."""
        row = self.s.db.one("SELECT * FROM ideas WHERE idea_id = ?", (idea_id,))
        if row is None:
            raise ApiError(404, "not_found", "That idea was not found.")
        return _idea(row)

    def set_idea_status(self, idea_id: int, status: str | None) -> dict[str, Any]:
        """new | approved | used | rejected (`dropped` is accepted); approved ideas are drawn first."""
        status = STATUS_ALIASES.get(status or "", status)
        if status not in IDEA_STATUSES:
            raise ApiError(422, "invalid_idea", f"Status must be one of {', '.join(IDEA_STATUSES)}.")
        self.get_idea(idea_id)
        self.s.db.execute(
            "UPDATE ideas SET status = ?, used = ? WHERE idea_id = ?", (status, 1 if status == "used" else 0, idea_id)
        )
        return self.get_idea(idea_id)

    # --- stories ---
    def stories(self, q: str | None = None, channel: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Written stories with their outputs and where they were posted."""
        where, args = [], []
        if q:
            where.append(
                "(working_title LIKE ? ESCAPE '\\' OR opening_line LIKE ? ESCAPE '\\' OR text LIKE ? ESCAPE '\\')"
            )
            args += [like(q)] * 3
        if channel:
            where.append("channel = ?")
            args.append(channel)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self.s.db.query(
            "SELECT job_id, channel, format, working_title, opening_line, word_count, critic_avg, revisions,"
            f" created_at FROM stories{clause} ORDER BY created_at DESC LIMIT ?",
            [*args, max(1, min(limit, 200))],
        )
        out = [dict(r) | {"outputs": []} for r in rows]
        by_job = {s["job_id"]: s for s in out}
        if by_job:
            marks = ",".join("?" for _ in by_job)
            outs = self.s.db.query(
                "SELECT o.job_id, o.output_id, o.kind, o.k, o.status, u.video_url FROM outputs o"
                f" LEFT JOIN uploads u ON u.output_id = o.output_id WHERE o.job_id IN ({marks})"
                " ORDER BY o.output_id",
                list(by_job),
            )
            for o in outs:
                by_job[o["job_id"]]["outputs"].append(
                    {k: o[k] for k in ("output_id", "kind", "k", "status", "video_url")}
                )
        for s in out:  # what the Stories tab shows: a title, a word count and one status for the whole story
            s["title"] = s["working_title"] or s["opening_line"]
            s["words"] = s["word_count"]
            s["status"] = story_status([o["status"] for o in s["outputs"]])
        return out

    # --- footage ---
    def footage(self, q: str | None = None, limit: int = 60) -> list[dict[str, Any]]:
        """Cached stock clips by keyword, licence and last use."""
        where, args = [], []
        if q:
            where.append(
                "(c.keywords LIKE ? ESCAPE '\\' OR c.clip_id LIKE ? ESCAPE '\\' OR c.source LIKE ? ESCAPE '\\')"
            )
            args += [like(q)] * 3
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self.s.db.query(
            "SELECT c.*, (SELECT MAX(used_at) FROM clip_usage u WHERE u.source = c.source"
            " AND u.clip_id = c.clip_id) AS last_used,"
            " (SELECT COUNT(*) FROM clip_usage n WHERE n.source = c.source AND n.clip_id = c.clip_id) AS uses"
            f" FROM clips c{clause} ORDER BY c.fetched_at DESC LIMIT ?",
            [*args, max(1, min(limit, 200))],
        )
        out = []
        for r in rows:
            try:
                kw = json.loads(r["keywords"]) if r["keywords"] else []
            except ValueError:
                kw = []
            out.append(
                {
                    "source": r["source"], "clip_id": r["clip_id"], "url": r["url"], "license": r["license"],
                    "width": r["width"], "height": r["height"], "duration": r["duration"],
                    "local_path": r["local_path"], "keywords": kw, "fetched_at": r["fetched_at"],
                    "last_used": r["last_used"], "uses": int(r["uses"]),
                }
            )
        return out

    def fetch_footage(
        self, q: str | None, orientation: str | None = "landscape", count: Any = 1, source: str | None = "auto"
    ) -> dict[str, Any]:
        """Download up to `count` (1 to 5) stock clips for `q` into the footage cache and say where they are.

        A network download, not GPU work: no lease, no studio job. The files and `clips` rows are the ones the
        studio's `ClipCache` writes, so later jobs reuse them and Library > Footage lists them. Clips that are
        already cached are not downloaded again (they only fill the answer when fewer new ones were found).
        """
        from ns_studio.media.clips import ClipCache, Searcher, normalize_keyword

        query = str(q or "").strip()
        if not query or len(query) > 100:
            raise ApiError(422, "invalid_options", "Give a search word or phrase for the footage (up to 100 characters).")
        orient = str(orientation or "landscape").lower()
        if orient not in FOOTAGE_ORIENTATIONS:
            raise ApiError(422, "invalid_options", "Orientation must be landscape or portrait.")
        which = str(source or "auto").lower()
        if which not in FOOTAGE_SOURCES:
            raise ApiError(422, "invalid_options", f"Source must be one of {', '.join(FOOTAGE_SOURCES)}.")
        try:
            want = int(count if count is not None else 1)
        except (TypeError, ValueError):
            raise ApiError(422, "invalid_options", "count must be a whole number from 1 to 5.") from None
        want = max(1, min(want, FOOTAGE_MAX))

        keys = self._footage_keys()
        order = ("pixabay", "pexels") if which == "auto" else (which,)
        usable = {name: keys[name] for name in order if keys.get(name)}
        if not usable:
            if which == "pexels":
                raise ApiError(409, "NS-E501", "No Pexels key. Add a Pexels key (free) in Settings > Stock footage keys",
                               hint="Keys are entered by the owner in Settings, not in chat.")
            raise ApiError(409, "NS-E502", NO_FOOTAGE_KEY,
                           hint="Keys are entered by the owner in Settings, not in chat. A Pexels key works too.")
        sources = (self.footage_sources or _real_footage_sources)(usable, self.s.paths)

        searcher = Searcher(sources, per_page=80, log=self.s.log)
        wanted: Orientation = "portrait" if orient == "portrait" else "landscape"
        found = [c for c in searcher.search(query, wanted) if c.orientation == wanted]
        if not found and searcher.errors:
            raise searcher.errors[0]
        found.sort(key=lambda c: min(c.width, c.height) < 1080)  # stable: Full HD first, search order otherwise
        keyword = normalize_keyword(query) or query.lower()
        cache = ClipCache(self.s.db.db, self.s.paths, sources, log=self.s.log)
        by_name = {s.name: s for s in sources}
        fresh: list[dict[str, Any]] = []
        reused: list[dict[str, Any]] = []
        errors: list[NsError] = []
        for cand in found:
            if len(fresh) >= want:
                break
            if not _SAFE_CLIP_ID.fullmatch(cand.id):
                continue
            dest = cache.path_for(cand.source, cand.id)
            row = self.s.db.one("SELECT local_path, keywords FROM clips WHERE source = ? AND clip_id = ?", (cand.source, cand.id))
            if row is not None and row["local_path"] and Path(row["local_path"]).is_file():
                if len(reused) < want:
                    reused.append(_footage_item(cand, Path(row["local_path"]), _stored_keywords(row["keywords"], keyword), new=False))
                continue
            try:
                by_name[cand.source].download(cand, dest)
            except NsError as e:
                if e.code not in _FOOTAGE_API_ERRORS:
                    raise
                errors.append(e)
                continue
            with self.s.db.lock:  # the row is a quick write; the download above ran without the lock
                cache.record(cand, dest, [keyword])
            fresh.append(_footage_item(cand, dest, [keyword], new=True))
        if not fresh and errors:
            raise errors[0]
        clips = [*fresh, *reused[: max(0, want - len(fresh))]]
        self.s.log.info("footage_fetched", q=query, orientation=orient, source=which, new=len(fresh), reused=len(clips) - len(fresh))
        out: dict[str, Any] = {
            "paths": [c["path"] for c in clips], "folder": str(self.s.paths.cache_clips), "query": query,
            "orientation": orient, "requested": want, "downloaded": len(fresh), "clips": clips,
        }
        if not clips:
            out["note"] = f"No {orient} clips found for '{query}'. Try other words."
        elif len(fresh) < want:
            out["note"] = f"Only {len(fresh)} new clip(s) found for '{query}'; the rest were already in the cache."
        return out

    def _footage_keys(self) -> dict[str, str]:
        """The stock-footage keys that are stored, by short name (`pexels`, `pixabay`); never logged."""
        out: dict[str, str] = {}
        for full, short in SHORT_NAMES.items():
            try:
                value = self.s.adapters.keyring.get(full)
            except AdapterUnavailable:
                value = None
            if value:
                out[short] = value
        return out

    def delete_footage(self, source: str, clip_id: str) -> dict[str, Any]:
        """Remove a cached stock clip: its rows and its file (only a file inside the Nightshift home)."""
        from ns_dashboard.security import is_within

        row = self.s.db.one("SELECT local_path FROM clips WHERE source = ? AND clip_id = ?", (source, clip_id))
        if row is None:
            raise ApiError(404, "not_found", "That clip is not in the cache.")
        with self.s.db.tx() as db:
            db.execute("DELETE FROM clip_usage WHERE source = ? AND clip_id = ?", (source, clip_id))
            db.execute("DELETE FROM clips WHERE source = ? AND clip_id = ?", (source, clip_id))
        removed = False
        if row["local_path"]:
            path = Path(row["local_path"])
            if is_within(self.s.paths.home, path) and path.is_file():
                path.unlink(missing_ok=True)
                removed = True
        self.s.log.info("footage_deleted", source=source, clip_id=clip_id, file_removed=removed)
        return {"ok": True, "source": source, "clip_id": clip_id, "file_removed": removed}

    # --- formats ---
    def list_formats(self) -> list[dict[str, Any]]:
        """Shipped and user formats, each with the channels that may use it (an empty `formats_allowed` is all)."""
        found = formats.list_formats(self.s.paths)
        chans = self.channels.load_all()
        for f in found:
            f["channels"] = [c.slug for c in chans if not c.formats_allowed or f["id"] in c.formats_allowed]
        return found

    def create_format(self, body: dict[str, Any]) -> dict[str, Any]:
        """New format or a duplicate of an existing one."""
        return formats.create_format(self.s.paths, body)

    def update_format(self, fid: str, body: dict[str, Any]) -> dict[str, Any]:
        """Replace or patch a format."""
        return formats.update_format(self.s.paths, fid, body)

    # --- update ---
    def update_check(self) -> dict[str, Any]:
        """Is a newer version on GitHub? (structured result of `ns update --check`)"""
        return self.s.adapters.update.check()

    def models_get(self, name: str | None) -> dict[str, Any]:
        """Start downloading a model that is fetched on demand (the voice design model, the story model).

        Answers 501 until `ns models get` exists, so the page can say so instead of pretending.
        """
        if name not in MODEL_NAMES:
            raise ApiError(422, "unknown_model", f"Choose one of: {', '.join(MODEL_NAMES)}.")
        if not self.s.adapters.update.models_available():
            raise ApiError(
                501, "models_unavailable", "Downloading models from here is not available yet.",
                hint="Run the installer's model step again with the option ticked.",
            )
        current = self.s.model_downloads.get(name)
        if current and current["state"] == "running":
            return {"name": name, **current}
        self.s.model_downloads[name] = {"state": "running", "message": "Downloading"}

        def work() -> None:
            try:
                def progress(line: str) -> None:
                    self.s.model_downloads[name] = {"state": "running", "message": line[-200:]}

                code, line = self.s.adapters.update.get_model(name, progress)
                self.s.model_downloads[name] = {"state": "done" if code == 0 else "failed", "message": line}
            except Exception as e:  # a failing download must not leave the page waiting forever
                self.s.model_downloads[name] = {"state": "failed", "message": str(e)}

        started = {"name": name, **self.s.model_downloads[name]}
        threading.Thread(target=work, name=f"ns-model-{name}", daemon=True).start()
        return started

    def models_state(self) -> dict[str, dict[str, str]]:
        """Downloads started in this session: name -> {state, message}."""
        return {k: dict(v) for k, v in self.s.model_downloads.items()}

    def update_state(self) -> dict[str, Any]:
        """Progress of the detached updater (`run/update_state.json`): idle, running, done or failed."""
        return updater.read_update_state(self.s.paths)

    def update_apply(self) -> dict[str, Any]:
        """Install the newest version (`ns update --github`, rolls back on failure)."""
        if self.s.draining:
            raise ApiError(503, "shutting_down", "Nightshift is shutting down.")
        self.s.log.warn("update_apply", "update started from the dashboard")
        res = self.s.adapters.update.apply()
        if res.get("state") == "unavailable":
            raise ApiError(501, "update_unavailable", str(res.get("message") or "Updating from GitHub is not available yet."))
        return res

    def update_apply_file(self, zip_name: str, zip_fh: BinaryIO, sha_fh: BinaryIO | None) -> dict[str, Any]:
        """Install an update from files the owner picked: the zip and its `.sha256`.

        Both are saved under `data/updates/` (the sidecar as `<zip name>.sha256`, where `ns update` looks for
        it) and `ns update <zip>` does the rest: it refuses a zip without a matching digest (`NS-E701`) before
        anything is stopped, then installs with a rollback.
        """
        if self.s.draining:
            raise ApiError(503, "shutting_down", "Nightshift is shutting down.")
        if self.s.store.running() is not None:
            raise ApiError(
                409, "job_running", "A job is running right now.",
                hint="Wait for it to finish (or cancel it), then install the update.",
            )
        name = Path(zip_name or "").name
        if not UPDATE_ZIP_RE.match(name):
            raise ApiError(
                400, "bad_file_type", "Choose the file named Nightshift-Update-<version>.zip.",
                hint="The Setup zip is for a first install. The Update zip is the other file in the release.",
            )
        if sha_fh is None:
            raise ApiError(
                400, "no_checksum", f"Choose {name}.sha256 as well.",
                hint="Pick both files at once. The update is refused without its checksum file.",
            )
        folder = self.s.paths.data / "updates"
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / name
        side = folder / f"{name}.sha256"
        size = _save_upload(zip_fh, dest, MAX_UPDATE_BYTES, "That file is larger than 200 MB, so it is not an update.")
        if size == 0:
            raise ApiError(400, "empty_file", "That file is empty.")
        text = sha_fh.read(4096).decode("utf-8", errors="replace").strip()
        if not text:
            dest.unlink(missing_ok=True)
            raise ApiError(400, "empty_file", f"{name}.sha256 is empty.")
        side.write_text(text.splitlines()[0] + "\n", encoding="utf-8")
        self.s.log.warn("update_apply_file", "update started from an uploaded file", file=name)
        res = self.s.adapters.update.apply_file(dest)
        if res.get("state") == "error":
            raise ApiError(409, "update_refused", str(res.get("message") or "The update was refused."))
        if res.get("state") == "unavailable":
            raise ApiError(501, "update_unavailable", str(res.get("message") or "Updating is not available yet."))
        return res

    # --- series workshop (read-only list plus "Produce next part") ---
    def series(self) -> list[dict[str, Any]]:
        """Workshop stories with per-part status, read from the story folders."""
        return self.s.adapters.series.listing()

    def produce_next_part(self, slug: str) -> dict[str, Any]:
        """Queue the next approved part of `slug` as a `series_part` job and wake the worker."""
        if self.s.draining:
            raise ApiError(503, "shutting_down", "Nightshift is shutting down and not accepting new jobs.")
        queued = self.s.adapters.series.queue_next(slug)
        for item in queued:
            self.s.worker.enqueue(str(item["job_id"]))
            rec = self.s.store.get(str(item["job_id"]))
            if rec is not None:
                self.s.hub.publish("dashboard_job", rec.to_dict())
        self.s.log.info("series_part_queued", slug=slug, parts=[q["part"] for q in queued])
        return {"slug": slug, "queued": queued}
