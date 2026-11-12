"""Block G, WP5 (rc8): footage follow-ups of the rc7 review (`refs/reviews/block-f-footage.md`, f4 to f8).

- a download cut half way continues with an HTTP Range request (f6), proven on a stand-in server (httpx
  `MockTransport`: no socket, no real wait);
- a black check that cannot run is not a black clip and spends no spare (f4);
- a pool the dead clips emptied is a shortage, NS-E402 (f5), and a dead clip is not tried again in the stage;
- the master path gets the black check (f8);
- the footage step's progress reaches the Dashboard's job (FR F5).
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_studio.media import clips as clips_mod
from ns_studio.media.clips import ClipCache, ClipsStage, Searcher, assign_clips, treatment_probe
from ns_studio.media.presets import visual_treatment
from ns_studio.media.shotplan import Shot
from tests.helpers import make_ctx
from tests.test_footage_robust import FakeTime, StubSource, _cand, _pixabay, _slots

needs_ffmpeg = pytest.mark.skipif(not ff.available(), reason="ffmpeg required")

DATA = bytes(range(256)) * 1200  # 307,200 bytes
CUT = 100_000


class Cut(httpx.SyncByteStream):
    """A body that delivers `data` up to `stop` bytes, then loses the connection (or just ends, `quiet`)."""

    def __init__(self, data: bytes, stop: int, quiet: bool = False) -> None:
        self.data, self.stop, self.quiet = data, stop, quiet

    def __iter__(self) -> Iterator[bytes]:
        yield self.data[: self.stop]
        if not self.quiet:
            raise httpx.ReadError("connection lost")


def _full_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    return {"content-length": str(len(DATA)), "accept-ranges": "bytes", "etag": '"v1"', **(extra or {})}


def _download(tmp_path: Path, handler: Any) -> tuple[Path, list[int], FakeTime]:
    t = FakeTime()
    src = _pixabay(tmp_path, handler, t)
    got: list[int] = []
    src.progress = got.append
    dest = tmp_path / "pixabay" / "9.mp4"
    assert src.download(_cand("9", source="pixabay"), dest) == dest
    return dest, got, t


# ---- f6: resume with a Range request --------------------------------------------------------------------


def test_f6_a_cut_download_continues_from_the_bytes_it_has(tmp_path: Path):
    seen: list[dict[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(dict(req.headers))
        if "range" not in req.headers:
            return httpx.Response(200, headers=_full_headers(), stream=Cut(DATA, CUT))
        assert req.headers["range"] == f"bytes={CUT}-" and req.headers["if-range"] == '"v1"'
        rest = DATA[CUT:]
        crange = f"bytes {CUT}-{len(DATA) - 1}/{len(DATA)}"
        return httpx.Response(206, headers={"content-range": crange, "content-length": str(len(rest))},
                              content=rest)

    dest, got, t = _download(tmp_path, handler)
    assert dest.read_bytes() == DATA
    assert len(seen) == 2 and t.slept == [2.0]
    assert sum(got) == len(DATA)  # every byte fetched once: the retry did not pay for the first 100,000 again
    assert not list(tmp_path.rglob("*.part"))


def test_f6_a_body_that_ends_short_of_its_announced_size_is_continued_not_kept(tmp_path: Path):
    def handler(req: httpx.Request) -> httpx.Response:
        if "range" not in req.headers:  # the connection "ends" cleanly after 100,000 of 307,200 bytes
            return httpx.Response(200, headers=_full_headers(), stream=Cut(DATA, CUT, quiet=True))
        crange = f"bytes {CUT}-{len(DATA) - 1}/{len(DATA)}"
        return httpx.Response(206, headers={"content-range": crange}, content=DATA[CUT:])

    dest, got, _t = _download(tmp_path, handler)
    assert dest.read_bytes() == DATA and sum(got) == len(DATA)


def test_f6_a_server_without_accept_ranges_starts_again_from_byte_zero(tmp_path: Path):
    seen: list[bool] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append("range" in req.headers)
        if len(seen) == 1:
            return httpx.Response(200, headers={"content-length": str(len(DATA))}, stream=Cut(DATA, CUT))
        return httpx.Response(200, headers={"content-length": str(len(DATA))}, content=DATA)

    dest, got, _t = _download(tmp_path, handler)
    assert seen == [False, False]  # no Range asked of a server that never said it takes one
    assert dest.read_bytes() == DATA and sum(got) == CUT + len(DATA)  # as before: the cost of the restart


def test_f6_a_server_that_ignores_the_range_gives_the_whole_file_and_it_is_not_doubled(tmp_path: Path):
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, headers=_full_headers(), stream=Cut(DATA, CUT))
        assert "range" in req.headers  # asked, and answered with 200 and the whole body
        return httpx.Response(200, headers=_full_headers(), content=DATA)

    dest, _got, _t = _download(tmp_path, handler)
    assert dest.read_bytes() == DATA and len(dest.read_bytes()) == len(DATA)


def test_f6_a_wrong_content_range_restarts_instead_of_joining_wrong_bytes(tmp_path: Path):
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, headers=_full_headers(), stream=Cut(DATA, CUT))
        if calls["n"] == 2:  # the resume is answered with another start than the part's end
            return httpx.Response(206, headers={"content-range": f"bytes 5-{len(DATA) - 1}/{len(DATA)}"},
                                  content=DATA[5:])
        assert "range" not in req.headers  # the third request is the restart
        return httpx.Response(200, headers=_full_headers(), content=DATA)

    dest, _got, _t = _download(tmp_path, handler)
    assert dest.read_bytes() == DATA and calls["n"] == 3


def test_f6_four_cuts_still_give_up_and_leave_no_part(tmp_path: Path):
    t, calls = FakeTime(), {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        start = 0 if "range" not in req.headers else int(req.headers["range"][6:-1])
        stop = start + 10_000
        headers = _full_headers() if start == 0 else {
            "content-range": f"bytes {start}-{len(DATA) - 1}/{len(DATA)}"}
        status = 200 if start == 0 else 206
        return httpx.Response(status, headers=headers, stream=Cut(DATA[start:], stop - start))

    with pytest.raises(NsError) as ei:
        _pixabay(tmp_path, handler, t).download(_cand("9", source="pixabay"), tmp_path / "pixabay" / "9.mp4")
    assert ei.value.code == "NS-E503" and calls["n"] == 4 and t.slept == [2.0, 5.0, 15.0]
    assert not list(tmp_path.rglob("*.part")) and not (tmp_path / "pixabay" / "9.mp4").exists()


# ---- f4: a check that cannot run is not a black clip -------------------------------------------------------


def _probe(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    return treatment_probe(visual_treatment(ctx.combo.visual_treatment), (1080, 1920)), ctx


def _ran(monkeypatch: pytest.MonkeyPatch, *, stderr: str = "", error: Exception | None = None) -> None:
    def run(args: list[str], **_kw: Any) -> Any:
        if error is not None:
            raise error
        return SimpleNamespace(stderr=stderr)

    monkeypatch.setattr(ff, "run", run)


def test_f4_a_clip_ffmpeg_refuses_is_unreadable(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    probe, _ctx = _probe(tmp_home)
    _ran(monkeypatch, error=NsError("NS-E401", "moov atom not found", cmd=["ffmpeg"]))
    assert probe(Path("x.mp4"), 0.0, 2.0) is None
    _ran(monkeypatch, stderr="frame=    0 fps=0.0 q=-0.0 Lsize=N/A")  # ran, decoded no frame
    assert probe(Path("x.mp4"), 0.0, 2.0) is None
    _ran(monkeypatch, stderr="frame=   48 fps=0.0")  # ran, decoded, nothing black
    assert probe(Path("x.mp4"), 0.0, 2.0) == []


@pytest.mark.parametrize("error", [
    NsError("NS-E401", "ffmpeg not found on PATH (set NS_FFMPEG)"),  # FFmpeg did not start
    NsError("NS-E401", "No such filter: 'blackdetect'", cmd=["ffmpeg"]),  # the check's own chain is wrong
    FileNotFoundError("ffmpeg.exe"),
    PermissionError("blocked"),
])
def test_f4_a_check_that_cannot_run_stops_with_a_clear_error_and_spends_no_spare(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
):
    probe, ctx = _probe(tmp_home)
    _ran(monkeypatch, error=error)
    src = StubSource([f"c{i}" for i in range(20)])
    cache = ClipCache(ctx.db, ctx.paths, [src])
    beats = [{"keywords": ["hallway"], "mood": "calm"}]
    with pytest.raises(NsError) as ei:
        assign_clips(beats=beats, slots=_slots(2.0, 2.0), searcher=Searcher([src]), cache=cache,
                     blocked=set(), seed=1, probe=probe, label="s1")
    assert ei.value.code == "NS-E401" and "black check could not run" in ei.value.detail
    assert len(src.downloads) == 1  # it stopped at the first clip; before, every spare was spent (NS-E402)


# ---- f5: a shortage after dead clips is NS-E402, a dead clip is not tried twice ----------------------------


def test_f5_a_pool_the_dead_clips_emptied_is_a_shortage_not_a_network_error(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    src = StubSource(["a", "b"], dead={"a", "b"})
    cache = ClipCache(ctx.db, ctx.paths, [src])
    with pytest.raises(NsError) as ei:
        assign_clips(beats=[{"keywords": ["hallway"], "mood": "calm"}], slots=_slots(2.0),
                     searcher=Searcher([src]), cache=cache, blocked=set(), seed=1, label="s1")
    assert ei.value.code == "NS-E402"
    assert "NS-E503" in ei.value.detail
    assert "No other clip was left for s1 shot 1 (0.0-2.0 s)" in ei.value.detail


def test_f5_a_dead_clip_is_not_tried_again_for_the_next_part_of_the_stage(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    src = StubSource(["a", "b", "c", "d"], dead={"a"})
    cache = ClipCache(ctx.db, ctx.paths, [src])
    beats = [{"keywords": ["hallway"], "mood": "calm"}]
    for part in ("s1", "s2"):  # one cache for the stage, as `ClipsStage` and `ShortsDeriveStage` hold it
        assign_clips(beats=beats, slots=_slots(2.0), searcher=Searcher([src]), cache=cache, blocked=set(),
                     seed=1, label=part, used=set())
    assert src.downloads.count("a") == 1


# ---- f8: the master path checks every shot for black -------------------------------------------------------


def _prepare_master(ctx: Any) -> None:
    ctx.write_json("story.json", {"chapters": [{"n": 1, "title": "One", "text": "a b c"}]})
    ctx.write_json("audio/manifest.json", {"c1": {"seconds": 24.0, "path": "x.wav"}})
    ctx.write_json("words/c1.json", [
        {"w": "a", "start": 0.2, "end": 0.5, "sentence_idx": 0},
        {"w": "b", "start": 8.0, "end": 8.3, "sentence_idx": 1},
        {"w": "c", "start": 16.0, "end": 16.3, "sentence_idx": 2},
    ])
    ctx.write_json("visual_plan.json", [
        {"chapter": 1, "beat": 0, "sentence_start": 0, "sentence_end": 2, "keywords": ["dark hallway"],
         "mood": "eerie", "need_vertical": False},
    ])


@needs_ffmpeg
def test_f8_the_master_path_replaces_a_black_clip_and_records_what_the_footage_cost(tmp_home: Path):
    ctx = make_ctx(tmp_home, fmt="master_first")
    _prepare_master(ctx)
    src = ctx.services.clip_sources[0]
    first_pick = src.search("dark hallway", orientation="landscape")[0].id
    src.black = {first_pick}
    ClipsStage().run(ctx)
    shots = [Shot.from_json(s) for s in ctx.read_json("clips/shots.json")["long"]["shots"]]
    assert shots and first_pick in src.downloads  # it was fetched, looked at, and put back
    assert first_pick not in {s.clip_id for s in shots}
    cost = ctx.read_json("clips/footage.json")  # the master path records its cost too (it recorded none)
    assert cost["runs"][0]["shots"] == len(shots) and cost["bytes"] > 0


@needs_ffmpeg
def test_f8_the_master_path_probe_sees_black_the_way_qc_does(tmp_path: Path):
    black = ff.make_test_clip(tmp_path / "b.mp4", width=128, height=72, seconds=2.0, kind="color",
                              color="0x000000")
    lit = ff.make_test_clip(tmp_path / "l.mp4", width=128, height=72, seconds=2.0, kind="testsrc2")
    probe = treatment_probe(visual_treatment("dark_slowzoom"), (1920, 1080))
    assert probe(black, 0.0, None) and probe(lit, 0.0, None) == []
    assert clips_mod.clear_in_point(probe, black, 1.0, 0.0, random.Random(1)) is None


# ---- FR F5: the Dashboard's job shows the footage step's progress ------------------------------------------


def test_fr_f5_a_stage_message_may_carry_a_note_and_still_parses():
    from ns_dashboard.jobs import _detail_of, _stage_of, _steps_of

    plain, noted = "clips (10/19)", "clips (10/19) - 12 of 85 clips, 40 MB downloaded"
    assert (_stage_of(plain), _steps_of(plain), _detail_of(plain)) == ("clips", (10, 19), None)
    assert (_stage_of(noted), _steps_of(noted), _detail_of(noted)) == (
        "clips", (10, 19), "12 of 85 clips, 40 MB downloaded")
    assert _stage_of("Downloading 42%") == "download" and _detail_of("Downloading 42%") is None


def test_fr_f5_the_bridge_logger_hands_the_clips_progress_line_on(tmp_path: Path):
    from ns_dashboard.studio_bridge import StageWatchLogger

    stages: list[str] = []
    notes: list[str] = []
    log = StageWatchLogger(NsLogger("t", tmp_path), stages.append, notes.append)
    log.info("stage_start", stage="clips")
    log.info("clips_progress", "3 of 9 clips, 12 MB downloaded", done=3, total=9)
    log.info("clip_cached", source="fake")
    assert stages == ["clips"] and notes == ["3 of 9 clips, 12 MB downloaded"]


def test_fr_f5_a_note_from_the_child_reaches_the_job_card(tmp_home: Path):
    from ns_dashboard.adapters import RealRunAdapter
    from ns_dashboard.jobs import JobCancelled
    from tests.test_dashboard_rereview import RecordingLink, make_ctl

    store, ctl = make_ctl(Paths.from_env())
    shown: list[dict[str, Any]] = []

    class Runner:
        def run(self, kind: str, args: dict[str, Any], *, on_event: Any, **_kw: Any) -> dict[str, Any]:
            on_event({"ev": "note", "text": "ignored: no stage yet"})
            on_event({"ev": "stage", "stage": "clips", "index": 9, "total": 19,
                      "job_index": 0, "job_total": 1})
            on_event({"ev": "note", "text": "12 of 85 clips, 40 MB downloaded"})
            rec = store.get(ctl.job_id)
            assert rec is not None
            shown.append(rec.to_dict())
            raise JobCancelled()

    adapter = RealRunAdapter(Paths.from_env(), RecordingLink(), runner=Runner())  # type: ignore[arg-type]
    with pytest.raises(JobCancelled):
        adapter.run(ctl, "after-dark", "shorts_only", 1)
    job = shown[0]
    assert (job["stage"], job["step"], job["steps"]) == ("clips", 10, 19)
    assert job["detail"] == "12 of 85 clips, 40 MB downloaded"
    assert job["progress"] == pytest.approx(round(100 * 9 / 19, 1))  # the note moved no progress


def test_fr_f5_the_child_sends_the_note_and_the_bridge_finds_it(capsys: pytest.CaptureFixture[str]):
    from ns_dashboard import studio_job

    studio_job.report.note("5 of 9 clips, 20 MB downloaded")  # type: ignore[attr-defined]
    assert '"ev": "note"' in capsys.readouterr().out
