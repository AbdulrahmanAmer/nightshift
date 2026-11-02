"""The dashboard's calls into the other components resolve to real code (system-gaps.md #2, #3, #6 to #13).

`test_every_collaborator_name_exists` scans every module of `ns_dashboard` for the names it takes from
`ns_studio`, `ns_supervisor`, `ns_tts` and `ns_common` (imports, `lazy("module", "name")` and
`import_module("module")`) and resolves each one. An adapter that calls a function nobody wrote fails here,
not on the owner's first video. The tests below it run the adapters against the real entry points.
"""

from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from ns_common.db import Db
from ns_common.paths import Paths
from ns_dashboard.adapters import RealRunAdapter
from ns_dashboard.events import EventHub
from ns_dashboard.jobs import JobCancelled, JobControl, JobStore
from ns_dashboard.state import open_db
from ns_dashboard.supervisor_link import SupervisorLink
from ns_studio import jobs
from ns_studio.channels.crud import add_channel
from ns_studio.context import FAKEABLE
from ns_studio.orchestrator import FORMAT_STAGES
from ns_studio.stages import StageState, register
from tests.helpers import InProcessRunner, make_channel

DASHBOARD = Path(__file__).resolve().parents[1] / "ns_dashboard"
OWNED = ("ns_studio", "ns_supervisor", "ns_tts", "ns_common")

#: Calls that are known to point at nothing yet, with the gap that fixes them. Fixing a gap removes its entry
#: (the test is strict: an entry that now resolves fails, and so does an entry nobody calls any more).
KNOWN_UNWIRED: dict[tuple[str, str], str] = {
}


def _const(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _called(node: ast.Call) -> str:
    f = node.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def collaborator_names() -> list[tuple[str, str | None, str]]:
    """(module, name or None, where) for everything `ns_dashboard` takes from the other components."""
    found: list[tuple[str, str | None, str]] = []
    for f in sorted(DASHBOARD.rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            where = f"{f.name}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module.split(".")[0] in OWNED:
                    found.extend((node.module, a.name, where) for a in node.names)
            elif isinstance(node, ast.Import):
                found.extend((a.name, None, where) for a in node.names if a.name.split(".")[0] in OWNED)
            elif isinstance(node, ast.Call) and _called(node) == "lazy" and len(node.args) >= 2:
                mod, attr = _const(node.args[0]), _const(node.args[1])
                if mod and attr:
                    found.append((mod, attr, where))
            elif isinstance(node, ast.Call) and _called(node) == "import_module" and node.args:
                mod = _const(node.args[0])
                if mod and mod.split(".")[0] in OWNED:
                    found.append((mod, None, where))
    return found


NAMES = collaborator_names()
UNIQUE = sorted({(m, a) for m, a, _w in NAMES}, key=lambda t: (t[0], t[1] or ""))


def resolves(module: str, attr: str | None) -> bool:
    """True when `module` imports and has `attr` (a member or a submodule)."""
    try:
        mod = importlib.import_module(module)
    except ImportError:
        return False
    if attr is None or hasattr(mod, attr):
        return True
    try:
        importlib.import_module(f"{module}.{attr}")
    except ImportError:
        return False
    return True


def test_scan_finds_the_known_collaborators():
    assert ("ns_studio.policy.regen", "run_job_guarded") in UNIQUE
    assert ("ns_supervisor.verify", "run_verify") in UNIQUE
    assert len(UNIQUE) > 20


@pytest.mark.parametrize(("module", "attr"), UNIQUE, ids=[f"{m}.{a}" for m, a in UNIQUE])
def test_every_collaborator_name_exists(module: str, attr: str | None):
    known = KNOWN_UNWIRED.get((module, attr or ""))
    if known:
        assert not resolves(module, attr), f"{module}.{attr} now exists: remove it from KNOWN_UNWIRED"
        pytest.xfail(f"{module}.{attr} is not wired yet ({known})")
    assert resolves(module, attr), f"ns_dashboard uses {module}.{attr}, which does not exist"


def test_known_unwired_entries_are_still_called():
    called = {(m, a) for m, a, _w in NAMES}
    stale = [k for k in KNOWN_UNWIRED if k not in called]
    assert not stale, f"nobody calls these any more, remove them: {stale}"


# ---------------------------------------------------------------- gap 2: Run now -> the orchestrator


class WireA:
    name = "wire_a"
    tenant = "llama"
    inputs: tuple[str, ...] = ()
    outputs = ("a.json",)

    def run(self, ctx: Any) -> None:
        ctx.write_json("a.json", {"n": 1})


class WireB:
    name = "wire_b"
    tenant = None
    inputs = ("a.json",)
    outputs = ("b.json",)

    def run(self, ctx: Any) -> None:
        ctx.write_json("b.json", {"n": 2})
        ctx.db.execute(
            "insert or replace into outputs(output_id, job_id, channel, kind, k, title, path, status,"
            " created_at)"
            " values (?,?,?,?,?,?,?,?,?)",
            (f"{ctx.job_id}/s1", ctx.job_id, ctx.channel.slug, "short", 1, "A title", "x.mp4", "pending",
             "2026-11-02T00:00:00+00:00"),
        )


@pytest.fixture
def two_stage_plan(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    register(WireA())
    register(WireB())
    monkeypatch.setitem(FORMAT_STAGES, "shorts_only", ["wire_a", "wire_b"])
    monkeypatch.setattr(jobs, "STAGE_MODULES", ())
    monkeypatch.setattr("ns_studio.policy.gate.available", lambda: False)
    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    add_channel(paths, db, make_channel())
    return paths


def make_ctl(paths: Paths) -> tuple[JobStore, JobControl]:
    store = JobStore(open_db(paths))
    rec = store.create("run", "after-dark", {})
    return store, JobControl(rec, store, EventHub())


def adapter(paths: Paths) -> RealRunAdapter:
    return RealRunAdapter(
        paths,
        SupervisorLink("http://127.0.0.1:9", timeout_s=0.2),
        fake=FAKEABLE,
        runner=InProcessRunner(),
    )


def test_run_now_calls_the_orchestrator_and_reports_stages(two_stage_plan: Paths):
    paths = two_stage_plan
    store, ctl = make_ctl(paths)
    ids = adapter(paths).run(ctl, "after-dark", "shorts_only", 1)
    job_id = ctl.extras["studio_job_ids"][0]
    assert ids == [f"{job_id}/s1"]
    assert (paths.job_dir(job_id) / "b.json").exists()
    assert "wire_a" in StageState.load(paths.job_dir(job_id)).entries
    rec = store.get(ctl.job_id)
    assert rec is not None and rec.message == "wire_b (2/2)" and rec.progress == pytest.approx(0.5)


def test_run_now_count_spans_jobs_and_progress(two_stage_plan: Paths):
    paths = two_stage_plan
    store, ctl = make_ctl(paths)
    ids = adapter(paths).run(ctl, "after-dark", "shorts_only", 2)
    assert len(ids) == 2 and len(set(ctl.extras["studio_job_ids"])) == 2
    rec = store.get(ctl.job_id)
    assert rec is not None and rec.progress == pytest.approx(0.75)


def test_run_now_stops_at_a_stage_boundary_when_cancelled(two_stage_plan: Paths):
    paths = two_stage_plan
    store, ctl = make_ctl(paths)
    store.request_cancel(ctl.job_id)
    with pytest.raises(JobCancelled):
        adapter(paths).run(ctl, "after-dark", "shorts_only", 1)
    job_dirs = [d for d in paths.jobs_dir.iterdir() if d.is_dir()]
    assert job_dirs and not (job_dirs[0] / "a.json").exists()


def test_run_now_unknown_format_is_a_coded_error(two_stage_plan: Paths):
    from ns_common.errors import NsError

    _store, ctl = make_ctl(two_stage_plan)
    with pytest.raises(NsError) as err:
        adapter(two_stage_plan).run(ctl, "after-dark", "series_compilation", 1)
    assert err.value.code == "NS-E306"


# ----------------------------------------------------------- gap 3: Clipper -> the clip job and import_url


class ClipStub:
    """Stands in for the heavy clip stages: records the parameters it was given and files one clip."""

    name = "clip_stub"
    tenant = None
    inputs = ("clip_params.json",)
    outputs = ("done.json",)

    def run(self, ctx: Any) -> None:
        from ns_studio.clipper.models import ClipParams

        p = ClipParams.load(ctx.job_dir)
        ctx.write_json("done.json", {"source": p.source, "template": p.template, "max_clips": p.max_clips})
        ctx.db.execute(
            "insert or replace into outputs(output_id, job_id, channel, kind, k, title, path, status,"
            " created_at) values (?,?,?,?,?,?,?,?,?)",
            (f"{ctx.job_id}/c1", ctx.job_id, ctx.channel.slug, "clip", 1, "A clip", "c.mp4", "pending",
             "2026-11-02T00:00:00+00:00"),
        )


@pytest.fixture
def clip_plan(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    from ns_studio.clipper import stages_clip  # noqa: F401  (registers the real clip stages first)

    register(ClipStub())
    monkeypatch.setitem(FORMAT_STAGES, "clip", ["clip_stub"])
    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    add_channel(paths, db, make_channel())
    return paths


def clip_adapter(paths: Paths):
    from ns_dashboard.adapters import RealClipAdapter

    return RealClipAdapter(
        paths,
        SupervisorLink("http://127.0.0.1:9", timeout_s=0.2),
        fake=FAKEABLE,
        runner=InProcessRunner(),
    )


OPTIONS = {"channel": "after-dark", "template": "podcast_single", "max_clips": 3, "min_s": 20, "max_s": 45,
           "rights_confirmed": True}


def test_clipper_runs_the_clip_job_with_the_owners_options(clip_plan: Paths):
    paths = clip_plan
    video = paths.home / "talk.mp4"
    store, ctl = make_ctl(paths)
    ids = clip_adapter(paths).clip(ctl, video, "src-dash", dict(OPTIONS))
    job_id = ctl.extras["studio_job_id"]
    assert ids == [f"{job_id}/c1"]
    done = json.loads((paths.job_dir(job_id) / "done.json").read_text(encoding="utf-8"))
    assert done == {"source": str(video), "template": "podcast_single", "max_clips": 3}
    rec = store.get(ctl.job_id)
    assert rec is not None and rec.message == "clip_stub (1/1)"


def test_clipper_rerun_keeps_the_job_and_changes_the_template(clip_plan: Paths):
    paths = clip_plan
    _store, ctl = make_ctl(paths)
    adapter_ = clip_adapter(paths)
    first = adapter_.clip(ctl, paths.home / "talk.mp4", "src-dash", dict(OPTIONS))
    job_id = ctl.extras["studio_job_id"]
    _store2, ctl2 = make_ctl(paths)
    again = adapter_.rerun(ctl2, job_id, {"template": "talking_head", "max_clips": 2})
    assert again == first and ctl2.extras["studio_job_id"] == job_id
    done = json.loads((paths.job_dir(job_id) / "done.json").read_text(encoding="utf-8"))
    assert done["template"] == "talking_head" and done["max_clips"] == 2


def test_clipper_removes_the_download_after_the_import(clip_plan: Paths):
    paths = clip_plan
    from ns_dashboard import studio_bridge

    downloaded = studio_bridge.download_dir(paths, "src-link") / "source.mp4"
    downloaded.parent.mkdir(parents=True)
    downloaded.write_bytes(b"x")
    _store, ctl = make_ctl(paths)
    clip_adapter(paths).clip(ctl, downloaded, "src-link", dict(OPTIONS))
    assert not downloaded.parent.exists()


def test_clipper_link_download_reaches_import_url(tmp_home: Path):
    from ns_common.errors import NsError
    from ns_dashboard.adapters import RealClipAdapter

    paths = Paths.from_env()
    _store, ctl = make_ctl(paths)
    link = SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
    adapter_ = RealClipAdapter(paths, link, runner=InProcessRunner())
    with pytest.raises(NsError) as err:  # the real import_url runs and reports the missing yt-dlp (NS-E409)
        adapter_.fetch_url(ctl, "https://example.com/watch?v=abc", "src-link")
    assert err.value.code == "NS-E409" and "yt-dlp" in err.value.detail


# ---------------------------------------------------------------- gap 6: the worker runs `series_part` rows


def test_series_part_row_is_picked_up_and_produced(two_stage_plan: Paths, monkeypatch: pytest.MonkeyPatch):
    from dataclasses import replace

    from ns_common.logging import NsLogger
    from ns_dashboard.adapters import RealSeriesAdapter
    from ns_dashboard.fakes import fake_adapters
    from ns_dashboard.jobs import JobWorker
    from ns_dashboard.svc_jobs import build_handlers
    from ns_studio.series.workshop import ProduceRequest, make_db_enqueue

    for fmt in ("master_first", "master_from_script"):
        monkeypatch.setitem(FORMAT_STAGES, fmt, ["wire_a", "wire_b"])
    paths = two_stage_plan
    job_id = "after-dark-20261102-ab12"
    job_dir = paths.job_dir(job_id)
    job_dir.mkdir(parents=True)
    (job_dir / "script_source.json").write_text("{}", encoding="utf-8")  # what Workshop.produce prepares
    req = ProduceRequest(
        job_id=job_id, channel="after-dark", fmt="master_first", plan="master_from_script",
        story_slug="the-lift",
        part_n=2, parts_total=6, series_title="The Lift", series_slug="the-lift",
        title="The Lift - Part 02",
        short_titles=[], script="s.md", shorts=[], concept="c.md", design={}, end_card="Next",
        publish_not_before_days=0,
    )
    make_db_enqueue(Db(paths.db_path))(req)  # exactly the row the workshop writes
    state = open_db(paths)
    real = RealSeriesAdapter(
        paths, SupervisorLink("http://127.0.0.1:9", timeout_s=0.2), fake=FAKEABLE,
        runner=InProcessRunner(),
    )
    store = JobStore(state)
    worker = JobWorker(store, build_handlers(replace(fake_adapters(state, paths), series=real)), EventHub(),
                       NsLogger("dashboard", paths.logs))
    assert worker.run_next()
    rec = store.get(job_id)
    assert rec is not None and rec.status == "done", rec and rec.message
    assert rec.result is not None and rec.result["output_ids"] == [f"{job_id}/s1"]
    assert rec.result["story_slug"] == "the-lift" and rec.result["part_n"] == 2
    assert (job_dir / "job.json").exists() and (job_dir / "b.json").exists()


def test_failed_series_part_for_a_missing_story_still_fails_cleanly(two_stage_plan: Paths):
    from ns_dashboard import studio_bridge

    studio_bridge.release_part({"story_slug": "nope", "part_n": 1, "job_id": "x"})  # no story, no error
    assert studio_bridge.list_series() == []
