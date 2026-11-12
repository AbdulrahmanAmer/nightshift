"""Fakes that write real files: adapters, a stdlib fake supervisor and seeding helpers (component C9).

Used by the tests, by `dev/gate_m3_dashboard.py` and by `python -m ns_dashboard.server --fake-services`.
"""
# Dense descriptor tables and long signatures: wrapping them hurts readability.
# ruff: noqa: E501

from __future__ import annotations

import itertools
import json
import shutil
import tempfile
import threading
import zipfile
from collections.abc import Callable, Collection, Mapping
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml

from ns_common import ffmpeg
from ns_common.paths import Paths
from ns_common.tts_client import FakeTts
from ns_dashboard.adapters import Adapters, MadeVoice, scan_voice_library
from ns_dashboard.jobs import JobControl
from ns_dashboard.state import LockedDb

DEFAULT_CLIP_STAGES = ["import", "audio", "transcribe", "select", "detect", "captions", "render", "seo", "qc"]
DEFAULT_RUN_STAGES = ["ideas", "story", "voice", "captions", "clips", "render", "qc"]

_FAKE_IDS = itertools.count(1)


def fake_job_id(slug: str, when: datetime | None = None) -> str:
    """An id of the product's form (`<slug>-<yyyymmdd>-<4 hex>`) that cannot repeat in one process.

    The product draws the four hex digits at random and takes the id by making the job's folder
    (`ns_common.db.claim_job_id`). The fakes make no folder to claim with, and a random draw gave two seeded
    outputs the same id about once in 300 runs of a 20-output test. A counter does not repeat (65535 ids per
    process, far more than any test seeds)."""
    when = when or datetime.now(UTC)
    return f"{slug}-{when:%Y%m%d}-{next(_FAKE_IDS) % 0x10000:04x}"


def clip_stage_names() -> list[str]:
    """`FORMAT_STAGES["clip"]` from the orchestrator when C8 registered it, else a default list."""
    try:
        from ns_studio.orchestrator import FORMAT_STAGES

        names = FORMAT_STAGES.get("clip")
        if names:
            return list(names)
    except ImportError:
        pass
    return list(DEFAULT_CLIP_STAGES)


def fake_outbox_path(paths: Paths, channel: str, job_id: str, kind: str, k: int, story_title: str) -> Path:
    """Where C6's `outbox_target` (spec 15.2a) puts a story video; `kind` is `long` or `short`.

    The story title comes from a `seo.json` in the job folder, exactly as in the real pipeline.
    """
    from ns_common.config import load_channel
    from ns_common.models import Channel
    from ns_studio.context import JobContext
    from ns_studio.media.outbox import outbox_target

    try:
        ch = load_channel(paths, channel)
    except (OSError, ValueError, yaml.YAMLError):
        ch = Channel(slug=channel, display_name=channel, niche="fake channel")
    job_dir = paths.job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    seo = job_dir / "seo.json"
    if not seo.exists():
        seo.write_text(json.dumps([{"title": story_title}]), encoding="utf-8")
    ctx = JobContext(
        job_id=job_id, channel=ch, combo=None, fmt="shorts_only", paths=paths,  # type: ignore[arg-type]
        job_dir=job_dir, db=None, runtime=None, services=None, log=None,  # type: ignore[arg-type]
    )
    return outbox_target(ctx, kind, k)


def fake_clip_path(paths: Paths, channel: str, source_name: str, k: int, title: str) -> Path:
    """`outbox/<channel>/clips/<source-name-slug>/clip-01 <title>.mp4` (spec 15.2a)."""
    from ns_studio.media.outbox import safe_slug

    folder = paths.outbox / channel / "clips" / (safe_slug(source_name) or "source")
    folder.mkdir(parents=True, exist_ok=True)
    prefix = f"clip-{k:02d}"
    return folder / f"{prefix} {safe_slug(title, 80 - len(prefix) - 1)}.mp4"


def write_fake_video(path: Path, source: Path | None = None) -> Path:
    """Write a playable MP4 when FFmpeg exists (or copy `source`), else placeholder bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if source is not None and source.exists():
        shutil.copyfile(source, path)
    elif ffmpeg.available():
        ffmpeg.make_test_clip(path, width=180, height=320, seconds=1.0)
    else:
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048)
    return path


def write_sheet(video: Path, *, title: str, channel: str, kind: str, job_id: str, k: int | None = None) -> None:
    """Write `<video>.json` and `<video>.txt` upload sheets next to the video, as the product writes them
    (spec 15.8 layout; `sheet_read.read_sheet_file` reads both as whole sheets)."""
    from ns_common.models import Metadata
    from ns_studio.clipper.models import ClipMetadata
    from ns_studio.clipper.stages_clip import clip_sheet_text
    from ns_studio.media.outbox import sheet_text

    sheet: dict[str, Any] = {
        "channel": channel, "job_id": job_id, "kind": kind, "k": None if kind == "long" else k or 1,
        "title": title, "description": f"{title}\n\nOriginal fiction.", "tags": ["story", "fiction"],
        "hashtags": ["#Shorts"], "ai_disclosure": "yes", "made_for_kids": False,
        "age_restrict_suggested": False, "combo_id": "fake", "prompt_versions": {},
        "publish_after": (datetime.now() + timedelta(days=1)).isoformat(timespec="minutes"),
    }
    if kind == "clip":
        clip = ClipMetadata.model_validate({**sheet, "source_video": "source.mp4", "template": "center_crop"})
        video.with_suffix(".json").write_text(clip.model_dump_json(indent=2) + "\n", encoding="utf-8")
        video.with_suffix(".txt").write_text(clip_sheet_text(clip, video.name), encoding="utf-8")
        return
    meta = Metadata.model_validate({**sheet, "clips": []})
    video.with_suffix(".json").write_text(meta.model_dump_json(indent=2) + "\n", encoding="utf-8")
    video.with_suffix(".txt").write_text(sheet_text(meta, video.name), encoding="utf-8")


class FakeRunAdapter:
    """Advances fake stages and inserts one `short` output per count."""

    def __init__(self, db: LockedDb, paths: Paths, stages: list[str] | None = None) -> None:
        self.db = db
        self.paths = paths
        self.stages = stages or list(DEFAULT_RUN_STAGES)
        self.calls: list[tuple[str, str, int | None]] = []
        self.resumed: list[str] = []
        self.scripts: list[tuple[str, str | None]] = []
        self.last_stages: list[str] = []

    def run(
        self, ctl: JobControl, channel: str, fmt: str, count: int | None, *, resume_job_id: str | None = None,
        script: str | None = None, script_title: str | None = None,
    ) -> list[str]:
        """Run the stages and register the outputs."""
        self.calls.append((channel, fmt, count))
        stages = self.stages
        if script:  # the owner's text replaces the stages that write the story
            self.scripts.append((script, script_title))
            stages = ["script_import", *[s for s in stages if s not in ("ideas", "story")]]
        if resume_job_id:
            self.resumed.append(resume_job_id)
        self.last_stages = list(stages)
        for _ in ctl.stages(stages):
            pass
        job_id = fake_job_id(channel)
        ids: list[str] = []
        for k in range(1, (count or 1) + 1):
            title = f"Fake short {k} for {channel}"
            target = fake_outbox_path(self.paths, channel, job_id, "short", k, f"Fake story for {channel}")
            video = write_fake_video(target)
            write_sheet(video, title=title, channel=channel, kind="short", job_id=job_id, k=k)
            oid = f"{job_id}/s{k}"
            self.db.execute(
                "INSERT INTO outputs(output_id, job_id, channel, kind, k, title, path, duration_s, status,"
                " created_at) VALUES (?, ?, ?, 'short', ?, ?, ?, 1.0, 'pending', ?)",
                (oid, job_id, channel, k, title, str(video), datetime.now(UTC).isoformat()),
            )
            ids.append(oid)
        return ids


class FakeClipAdapter:
    """Walks the clip stages, copies the source into the outbox and inserts `clip` outputs."""

    def __init__(self, db: LockedDb, paths: Paths, stages: list[str] | None = None) -> None:
        self.db = db
        self.paths = paths
        self.stages = stages
        self.calls: list[dict[str, Any]] = []
        self.urls: list[str] = []
        self.reruns: list[dict[str, Any]] = []
        self.fail_url_with: Exception | None = None

    def fetch_url(self, ctl: JobControl, url: str, source_id: str) -> Path:
        """Pretend to download: report 0, 25, 50, 75, 100 percent and write a small video file."""
        self.urls.append(url)
        if self.fail_url_with is not None:
            raise self.fail_url_with
        for pct in (0, 25, 50, 75, 100):
            ctl.download(pct)
        return write_fake_video(self.paths.home / "imports" / "clipper" / source_id / "source.mp4")

    def clip(self, ctl: JobControl, source_path: Path, source_id: str, params: dict[str, Any]) -> list[str]:
        """Produce `min(max_clips, 2)` clips."""
        self.calls.append({"source_id": source_id, "source": str(source_path), **params})
        for _ in ctl.stages(self.stages or clip_stage_names()):
            pass
        channel = str(params.get("channel") or ctl.channel or "clips")
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            "INSERT OR IGNORE INTO sources(source_id, channel, path, duration_s, imported_at, template, status,"
            " rights_confirmed, title) VALUES (?, ?, ?, ?, ?, ?, 'imported', ?, ?)",
            (source_id, channel, str(source_path), 1.0, now, params.get("template"),
             1 if params.get("rights_confirmed") else 0, source_path.stem),
        )
        job_id = fake_job_id(channel)
        ctl.extras["studio_job_id"] = job_id
        ids: list[str] = []
        for k in range(1, max(1, min(int(params.get("max_clips") or 2), 2)) + 1):
            title = f"Fake clip {k} from {source_path.stem}"
            video = write_fake_video(fake_clip_path(self.paths, channel, source_path.stem, k, title), source_path)
            write_sheet(video, title=title, channel=channel, kind="clip", job_id=job_id, k=k)
            oid = f"{job_id}/c{k}"
            self.db.execute(
                "INSERT INTO outputs(output_id, job_id, channel, kind, k, title, path, duration_s, status,"
                " source_id, created_at) VALUES (?, ?, ?, 'clip', ?, ?, ?, 1.0, 'pending', ?, ?)",
                (oid, job_id, channel, k, title, str(video), source_id, now),
            )
            ids.append(oid)
        return ids

    def rerun(self, ctl: JobControl, studio_job_id: str, changes: dict[str, Any]) -> list[str]:
        """Pretend to run the later stages again: retitle the job's clips and keep their ids."""
        self.reruns.append({"studio_job_id": studio_job_id, **changes})
        for _ in ctl.stages(["select", "layout", "captions", "render", "qc", "outbox"]):
            pass
        tag = str(changes.get("template") or "auto")
        rows = self.db.query("SELECT output_id FROM outputs WHERE job_id = ? ORDER BY k", (studio_job_id,))
        for r in rows:
            self.db.execute(
                "UPDATE outputs SET title = ? WHERE output_id = ?",
                (f"Fake clip ({tag}) {r['output_id']}", r["output_id"]),
            )
        ctl.extras["studio_job_id"] = studio_job_id
        return [str(r["output_id"]) for r in rows]


class FakeLearnAdapter:
    """Counts CSV rows; the summary is fixed."""

    def __init__(self) -> None:
        self.imported: list[Path] = []

    def import_csv(self, ctl: JobControl, csv_path: Path) -> dict[str, Any]:
        """Pretend to match every row."""
        self.imported.append(csv_path)
        for _ in ctl.stages(["read", "match", "reweight"]):
            pass
        rows = max(0, len(csv_path.read_text(encoding="utf-8").strip().splitlines()) - 1)
        return {"rows": rows, "matched": rows, "unmatched": [], "weight_changes": []}

    def summary(self) -> dict[str, Any]:
        """Fixed summary."""
        return {
            "pools": {"hooks": {"top": [{"option": "cold_open", "score": 0.8, "n": 3}], "bottom": []}},
            "weights": [],
            "last_import": None,
            "unmatched_uploads": [],
        }


class FakeVerifyAdapter:
    """Always passes."""

    def verify(self, ctl: JobControl, quick: bool) -> dict[str, Any]:
        """Return fixed checks."""
        for _ in ctl.stages(["encoder", "tts", "llm"]):
            pass
        return {"checks": [{"name": "fake", "passed": True}], "warnings": [], "quick": quick}


class FakeDoctorAdapter:
    """Writes a small zip under the temp home."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths

    def available(self) -> None:
        """The fake can always write its zip."""

    def bundle(self) -> Path:
        """Create `diagnostics/nightshift-diagnostics-<ts>.zip`."""
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        out = self.paths.home / "diagnostics" / f"nightshift-diagnostics-{stamp}.zip"
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("README.txt", "fake diagnostics bundle")
        return out


class FakeKeyring:
    """In-memory keyring."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        """Stored secret or None."""
        return self.store.get(name)

    def set(self, name: str, value: str) -> None:
        """Store a secret."""
        self.store[name] = value

    def delete(self, name: str) -> None:
        """Forget a secret."""
        self.store.pop(name, None)


class FakeTtsAdapter:
    """FakeTts previews; a fixed voice list."""

    def __init__(self, paths: Paths, live: bool = True) -> None:
        self.paths = paths
        self.live = live
        self.previews: list[tuple[str, str, float]] = []

    def voices(self) -> tuple[list[dict[str, Any]], bool]:
        """The presets and the stored voices, the same whether the service is `live` or not (G-39)."""
        return scan_voice_library(self.paths), self.live

    def preview(self, voice: str, text: str, speed: float) -> bytes:
        """WAV bytes from `FakeTts`."""
        self.previews.append((voice, text, speed))
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "p.wav"
            FakeTts().speak(text, voice, speed, out)
            return out.read_bytes()


class FakeVoiceMaker:
    """Designs and clones voices with `ns_tts.engine.FakeEngine` into the real voice library layout
    (`models/tts/voices/<name>/{voice.json, speaker.bin, sample.wav}`) and writes `preview.wav`."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.calls: list[tuple[str, str, str]] = []  # (kind, name, test_line)
        self.fail_with: Exception | None = None

    def _library(self) -> Any:
        from ns_tts.voices import VoiceLibrary

        return VoiceLibrary(self.paths.models_tts_voices)

    def _preview(self, name: str, test_line: str, engine: Any) -> MadeVoice:
        from ns_tts.audio import write_wav

        lib = self._library()
        out = self.paths.models_tts_voices / name / "preview.wav"
        write_wav(out, engine.synth(test_line, lib.get(name)), engine.sr)
        return MadeVoice(voice_id=name, preview_wav_path=str(out))

    def design(self, name: str, description: str, test_line: str) -> MadeVoice:
        """Fake VoiceDesign: deterministic embedding from the description."""
        from ns_tts.engine import FakeEngine

        self.calls.append(("design", name, test_line))
        if self.fail_with is not None:
            raise self.fail_with
        engine = FakeEngine()
        self._library().add_design(name, description, engine=engine)
        return self._preview(name, test_line, engine)

    def clone(self, name: str, sample_wav: Path, test_line: str) -> MadeVoice:
        """Fake Base clone: deterministic embedding from the sample bytes."""
        from ns_tts.engine import FakeEngine

        self.calls.append(("clone", name, test_line))
        if self.fail_with is not None:
            raise self.fail_with
        engine = FakeEngine()
        self._library().add_clone(name, str(sample_wav), "fake words of the sample", True, engine=engine)
        return self._preview(name, test_line, engine)

    def pending(self) -> dict[str, Any] | None:
        """The queued setup voice, as the real adapter reads it."""
        from ns_dashboard.adapters import read_pending_voice

        return read_pending_voice(self.paths)

    def apply_pending(self, ctl: JobControl | None = None) -> str | None:
        """Make the queued voice with the fake engine and forget the request."""
        want = self.pending()
        if ctl is not None:
            ctl.progress(0.1, "Making the voice: loading the voice engine")
            ctl.check_cancel()
        if self.fail_with is not None:
            raise self.fail_with
        if want is None:
            return None
        from ns_tts.engine import FakeEngine

        # a recording is not kept by the fake: a cloned setup voice is made from its description line
        self._library().add_design(want["name"], want["description"], engine=FakeEngine())
        self.calls.append(("apply_pending", want["name"], ""))
        (self.paths.config / "pending_voice.json").unlink(missing_ok=True)
        return str(want["name"])


class FakeFolderOpener:
    """Records the folders it was asked to open."""

    def __init__(self) -> None:
        self.opened: list[Path] = []

    def open(self, path: Path) -> None:
        """Record `path`."""
        self.opened.append(path)


class FakeScheduleAdapter:
    """Records applied run times and switch clicks. There is no Windows task behind it, so the switch is
    refused with the reason and Automatic runs stay off, as every page then says."""

    def __init__(self) -> None:
        self.applied: list[str] = []
        self.switched: list[bool] = []

    def apply(self, run_time: str) -> dict[str, Any]:
        """Record and report success."""
        self.applied.append(run_time)
        return {"applied": True, "note": "fake"}

    def refresh(self) -> None:
        """Nothing to look at: no test and no fake-services dashboard asks the real Task Scheduler."""

    def switch(self, on: bool, run_time: str | None = None) -> dict[str, Any]:
        """Record the click; nothing is registered."""
        self.switched.append(on)
        if not on:
            return {"applied": True, "note": "Automatic runs are off"}
        return {"applied": False, "note": "Not saved: this dashboard runs on fake services, which have no "
                                           "Windows task. Automatic runs are still off"}


class FakeUpdateAdapter:
    """Canned update results."""

    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.applied = 0
        self.missing = False  # True: the command does not take --check yet

    def check(self) -> dict[str, Any]:
        """Report a newer version (or none)."""
        from ns_dashboard.updater import classify_update

        if self.missing:
            return classify_update({"ok": False, "returncode": 2, "update_available": None, "output": "",
                                    "error": "Error: Got unexpected extra argument"})
        return classify_update({"ok": True, "returncode": 0, "update_available": self.available,
                                "current": "0.1.0", "latest": "0.2.0" if self.available else "0.1.0",
                                "output": "", "error": ""})

    def models_available(self) -> bool:
        """Tests set `models_command` to switch the command on."""
        return getattr(self, "models_command", False)

    def model_unavailable(self, name: str) -> str | None:
        """None (every model can be had) unless a test put a reason in `unavailable_models`."""
        return getattr(self, "unavailable_models", {}).get(name)

    def get_model(self, name: str, on_line: Callable[[str], None] = lambda _l: None) -> tuple[int, str]:
        """Pretend to download."""
        self.downloaded = [*getattr(self, "downloaded", []), name]
        on_line(f"fetching {name} 50%")
        return 0, f"{name} is ready"

    def apply_file(self, zip_path: Path) -> dict[str, Any]:
        """Pretend to install a zip from disk."""
        self.files = [*getattr(self, "files", []), zip_path]
        return {"ok": True, "returncode": 0, "started": True, "state": "started", "output": "", "error": "",
                "message": "The update is running. Nightshift restarts by itself when it is done."}

    def apply(self) -> dict[str, Any]:
        """Pretend to install."""
        if self.missing:
            from ns_dashboard.updater import classify_update

            return classify_update({"ok": False, "returncode": 2, "output": "", "error": "No such option: --github"})
        self.applied += 1
        return {"ok": True, "returncode": 0, "update_available": False, "current": "0.2.0", "latest": "0.2.0",
                "output": "installed", "error": ""}


class FakeSeriesAdapter:
    """Walks fake stages for a `series_part` job and files one long video."""

    def __init__(self, db: LockedDb, paths: Paths) -> None:
        self.db = db
        self.paths = paths
        self.produced: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None
        self.stories: list[dict[str, Any]] = []

    def listing(self) -> list[dict[str, Any]]:
        """The stories a test put in `stories`."""
        return [dict(s) for s in self.stories]

    def release(self, params: dict[str, Any]) -> None:
        """Put the part back to `approved` when it is still `producing`."""
        for story in self.stories:
            if story["slug"] != params.get("story_slug"):
                continue
            for part in story["parts"]:
                if part["n"] == params.get("part_n") and part["status"] == "producing":
                    part["status"] = "approved"

    def sweep(self, active_job_ids: Collection[str], job_statuses: Mapping[str, str] | None = None) -> int:
        """Settle every `producing` part with no queued or running job (`produced` when its job is done)."""
        freed = 0
        for story in self.stories:
            for part in story["parts"]:
                if part["status"] != "producing" or part.get("job_id") in set(active_job_ids):
                    continue
                done = job_statuses is not None and job_statuses.get(str(part.get("job_id"))) == "done"
                part["status"] = "produced" if done else "approved"
                freed += 1
        return freed

    def finish(self, params: dict[str, Any]) -> None:
        """Mark the part `produced` when it is still `producing`."""
        for story in self.stories:
            if story["slug"] != params.get("story_slug"):
                continue
            for part in story["parts"]:
                if part["n"] == params.get("part_n") and part["status"] == "producing":
                    part["status"] = "produced"

    def queue_next(self, slug: str) -> list[dict[str, Any]]:
        """Queue the first approved part of `slug` as a `series_part` row and mark it producing."""
        from ns_dashboard.errors import ApiError

        story = next((s for s in self.stories if s["slug"] == slug), None)
        if story is None:
            raise ApiError(404, "not_found", f"no story {slug!r}")
        part = next((p for p in story["parts"] if p["status"] == "approved"), None)
        if part is None:
            raise ApiError(409, "cannot_produce", f"story {slug!r} has no approved part waiting")
        # like `Workshop.produce` (D-33): NS-E104, nothing queued, when the home's volume cannot hold the
        # part; the fakes' own clip source is fake, so the estimate is the fake-footage one
        from ns_studio import diskroom

        diskroom.require_series_part_room(self.paths, None, fake=("clips",))
        job_id = fake_job_id(str(story["channel"]))
        title = f"{story['title']} - Part {part['n']:02d}"
        self.db.execute(
            "INSERT INTO dashboard_jobs(job_id, kind, channel, status, message, created_at, params_json)"
            " VALUES (?, 'series_part', ?, 'queued', ?, ?, ?)",
            (job_id, story["channel"], title, datetime.now(UTC).isoformat(),
             json.dumps({"job_id": job_id, "channel": story["channel"], "story_slug": slug,
                         "part_n": part["n"], "title": title})),
        )
        part["status"], part["job_id"] = "producing", job_id
        return [{"part": part["n"], "job_id": job_id, "title": title}]

    def produce(self, ctl: JobControl, params: dict[str, Any]) -> list[str]:
        """Run the stages, then insert a `long` output for the part."""
        self.produced.append(dict(params))
        for _ in ctl.stages(DEFAULT_RUN_STAGES):
            pass
        if self.fail_with is not None:
            raise self.fail_with
        channel = str(params.get("channel") or ctl.channel or "series")
        title = str(params.get("title") or "Series part")
        video = write_fake_video(fake_outbox_path(self.paths, channel, ctl.job_id, "long", 1, title))
        write_sheet(video, title=title, channel=channel, kind="long", job_id=ctl.job_id)
        oid = f"{ctl.job_id}/long"
        self.db.execute(
            "INSERT INTO outputs(output_id, job_id, channel, kind, k, title, path, duration_s, status, created_at)"
            " VALUES (?, ?, ?, 'long', NULL, ?, ?, 1.0, 'pending', ?)",
            (oid, ctl.job_id, channel, title, str(video), datetime.now(UTC).isoformat()),
        )
        return [oid]


class FakeBackupAdapter:
    """Writes a small zip as the 'backup' and records restores."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.restored: list[Path] = []
        self.fail_restore = False
        self.on_restore: Callable[[], None] | None = None  # tests look at the dashboard while the restore runs
        self.last_notes: list[str] = []  # what the last export left out on purpose (like `RealBackupAdapter`)
        self.leave_out: list[str] = []  # lines the next exports report as left out (a keys file, NS-W116)
        self.restore_lines: list[str] = []  # extra result lines of a restore, e.g. a skipped keys file

    def export(self) -> Path:
        """Create `exports/nightshift-backup-<ts>.zip`."""
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        out = self.paths.home / "exports" / f"nightshift-backup-{stamp}.zip"
        self.last_notes = list(self.leave_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("manifest.json", "{}")
        return out

    def restore(self, zip_path: Path) -> list[str]:
        """Remember the file; fail when asked to."""
        from ns_dashboard.errors import ApiError

        if self.on_restore is not None:
            self.on_restore()
        if self.fail_restore:
            raise ApiError(422, "restore_failed", "That file could not be restored.", hint="not a backup")
        self.restored.append(zip_path)
        return [f"restored {zip_path.name}", *self.restore_lines]

    def uninstall_path(self) -> Path:
        """A path under the temp home."""
        return self.paths.home / "app" / "UNINSTALL.cmd"


def fake_adapters(db: LockedDb, paths: Paths) -> Adapters:
    """All fakes wired together."""
    return Adapters(
        run=FakeRunAdapter(db, paths),
        clip=FakeClipAdapter(db, paths),
        learn=FakeLearnAdapter(),
        verify=FakeVerifyAdapter(),
        doctor=FakeDoctorAdapter(paths),
        keyring=FakeKeyring(),
        tts=FakeTtsAdapter(paths),
        folder=FakeFolderOpener(),
        schedule=FakeScheduleAdapter(),
        update=FakeUpdateAdapter(),
        voice_maker=FakeVoiceMaker(paths),
        series=FakeSeriesAdapter(db, paths),
        backup=FakeBackupAdapter(paths),
    )


# ---------------------------------------------------------------- seeding helpers


def seed_channel(db: LockedDb, paths: Paths, slug: str = "after-dark", **kw: Any) -> None:
    """Write a channel profile file (valid per `ns_common.models.Channel`) and its `channels` row."""
    from ns_common.config import save_channel
    from ns_common.models import Channel

    base: dict[str, Any] = {
        "slug": slug,
        "display_name": slug.replace("-", " ").title(),
        "niche": f"original short fiction for {slug}",
        "format": "shorts_only",
    }
    ch = Channel(**(base | kw))
    save_channel(paths, ch)
    db.execute(
        "INSERT OR IGNORE INTO channels(slug, display_name, created_at, status) VALUES (?, ?, ?, ?)",
        (ch.slug, ch.display_name, datetime.now(UTC).isoformat(), ch.status),
    )


def seed_output(
    db: LockedDb,
    paths: Paths,
    *,
    channel: str = "after-dark",
    kind: str = "short",
    k: int = 1,
    status: str = "pending",
    title: str | None = None,
    video: Path | None = None,
    job_id: str | None = None,
    source_id: str | None = None,
    with_files: bool = True,
) -> str:
    """Insert an `outputs` row (and a video plus sheet in the outbox); returns the output id."""
    job_id = job_id or fake_job_id(channel)
    title = title or f"Seeded {kind} {k}"
    if kind == "clip":
        path = fake_clip_path(paths, channel, f"source {job_id[-4:]}", k, title)
    else:
        path = fake_outbox_path(paths, channel, job_id, kind, k, title)
    if with_files:
        write_fake_video(path, video)
        write_sheet(path, title=title, channel=channel, kind=kind, job_id=job_id, k=k)
    name = {"long": "long", "short": f"s{k}", "clip": f"c{k}"}[kind]
    oid = f"{job_id}/{name}"
    db.execute(
        "INSERT INTO outputs(output_id, job_id, channel, kind, k, title, path, duration_s, status, source_id,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1.0, ?, ?, ?)",
        (
            oid,
            job_id,
            channel,
            kind,
            None if kind == "long" else k,
            title,
            str(path),
            status,
            source_id,
            datetime.now(UTC).isoformat(),
        ),
    )
    return oid


# ---------------------------------------------------------------- fake supervisor


class FakeSupervisor:
    """A real HTTP server on 127.0.0.1 speaking the supervisor API (spec 15.3) from canned state."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {"gpu_owner": None, "services": {}}
        self.events: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # silence request logging
                return

            def _send(self, code: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                outer.calls.append(("GET", self.path, {}))
                if self.path == "/status":
                    self._send(200, outer.state)
                elif self.path == "/events":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for ev in outer.events:
                        self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                    self.wfile.flush()
                else:
                    self._send(404, {"detail": "not found"})

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b"{}"
                body = json.loads(raw or b"{}")
                outer.calls.append(("POST", self.path, body))
                if self.path == "/acquire":
                    alias = body.get("model_alias")
                    self._send(200, {"endpoint": "http://127.0.0.1:9", "model_alias": alias})
                elif self.path == "/warmup":
                    self._send(200, {"state": "starting"})
                elif self.path in ("/release", "/stop_all", "/shutdown", "/heartbeat"):
                    self._send(200, {"ok": True})
                elif self.path == "/release_owner":
                    held = outer.state.get("gpu_owner") == body.get("owner")
                    if held and body.get("unless_alive") and outer.state.get("lease_alive"):
                        self._send(200, {"ok": False, "refused": "alive", "silent_s": 3.0})
                        return
                    if held:
                        outer.state["gpu_owner"] = None
                    self._send(200, {"ok": held})
                else:
                    self._send(404, {"detail": "not found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        """Base URL of the fake."""
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> FakeSupervisor:
        """Start serving."""
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop serving."""
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> FakeSupervisor:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
