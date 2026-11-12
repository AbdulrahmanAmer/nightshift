"""Card footage-robust (rc7): a file download retries like the search, a dead clip gives its slot to the next
candidate, downloads stay within the slots plus a bound, a redo reuses the cache, black footage never reaches
the render, and a stage a resume skips acquires no service."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_common.models import VideoCfg
from ns_studio import orchestrator as orch
from ns_studio.media import clips as clips_mod
from ns_studio.media.clip_sources import ClipCandidate, Orientation, PixabaySource
from ns_studio.media.clips import (
    MAX_GIVE_UPS_IN_A_ROW,
    ClipCache,
    ClipsStage,
    Searcher,
    assign_clips,
    spare_bound,
)
from ns_studio.media.presets import visual_treatment
from ns_studio.media.qc import black_check
from ns_studio.media.render import render_video
from ns_studio.media.shotplan import Shot, ShotSlot
from ns_studio.stages import register
from tests.helpers import make_ctx

needs_ffmpeg = pytest.mark.skipif(not ff.available(), reason="ffmpeg required")


class FakeTime:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s


def _pixabay(tmp_path: Path, handler, t: FakeTime) -> PixabaySource:
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return PixabaySource("K", tmp_path, client=client, sleep=t.sleep, wall=t.clock)


def _cand(cid: str, *, dur: float = 8.0, source: str = "stub") -> ClipCandidate:
    return ClipCandidate(source, cid, f"https://x/{cid}", f"https://cdn.example/{cid}.mp4", 1080, 1920, dur,
                         (), "", "test")


# ---- F1: the file download retries a timeout, a reset connection and a 5xx ---------------------------------


def test_f1_a_read_timeout_on_the_first_try_then_the_file(tmp_path: Path):
    t, calls = FakeTime(), {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200, content=b"v" * 200_000)

    dest = tmp_path / "pixabay" / "1.mp4"
    assert _pixabay(tmp_path, handler, t).download(_cand("1", source="pixabay"), dest) == dest
    assert dest.read_bytes() == b"v" * 200_000
    assert calls["n"] == 2 and t.slept == [2.0]  # OUTAGE_BACKOFF_S[0]
    assert not list(tmp_path.rglob("*.part"))


def test_f1_a_reset_connection_and_a_503_are_tried_again(tmp_path: Path):
    t, calls = FakeTime(), {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadError("connection reset by peer", request=req)
        if calls["n"] == 2:
            return httpx.Response(503)
        return httpx.Response(200, content=b"ok")

    dest = tmp_path / "pixabay" / "2.mp4"
    _pixabay(tmp_path, handler, t).download(_cand("2", source="pixabay"), dest)
    assert dest.read_bytes() == b"ok" and calls["n"] == 3 and t.slept == [2.0, 5.0]


def test_f1_four_timeouts_give_up_with_ns_e503_and_leave_no_part(tmp_path: Path):
    t, calls = FakeTime(), {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(NsError) as ei:
        _pixabay(tmp_path, handler, t).download(_cand("3", source="pixabay"), tmp_path / "pixabay" / "3.mp4")
    assert ei.value.code == "NS-E503" and "Tried 4 times" in ei.value.detail
    assert calls["n"] == 4 and t.slept == [2.0, 5.0, 15.0]
    assert not list(tmp_path.rglob("*.part")) and not (tmp_path / "pixabay" / "3.mp4").exists()


def test_f1_a_404_is_not_asked_again(tmp_path: Path):
    t, calls = FakeTime(), {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    with pytest.raises(NsError) as ei:
        _pixabay(tmp_path, handler, t).download(_cand("4", source="pixabay"), tmp_path / "pixabay" / "4.mp4")
    assert ei.value.code == "NS-E505" and calls["n"] == 1 and t.slept == []


# ---- F1: a clip that cannot be fetched gives its slot to the next candidate --------------------------------


class StubSource:
    """Fixed candidates for every keyword; `download` gives up (NS-E503, as a stock site after its four tries)
    for the ids in `dead`, else writes a few bytes."""

    name = "stub"

    def __init__(self, ids: list[str], dead: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.ids = ids
        self.dead = set(dead)
        self.downloads: list[str] = []

    def search(self, keyword: str, *, orientation: Orientation, per_page: int = 80) -> list[ClipCandidate]:
        return [_cand(i) for i in self.ids] if orientation == "portrait" else []

    def download(self, c: ClipCandidate, dest: Path) -> Path:
        self.downloads.append(c.id)
        if c.id in self.dead:
            raise NsError("NS-E503", "Nightshift could not reach Stub (ReadTimeout). Tried 4 times.")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"clip")
        return dest


def _slots(*lengths: float) -> list[ShotSlot]:
    out, t = [], 0.0
    for ln in lengths:
        out.append(ShotSlot(0, t, t + ln))
        t += ln
    return out


def _assign(ctx, src, slots, **kw) -> list[Shot]:
    cache = ClipCache(ctx.db, ctx.paths, [src])
    beats = [{"keywords": ["hallway"], "mood": "calm"}]
    return assign_clips(beats=beats, slots=slots, searcher=Searcher([src]), cache=cache, blocked=set(),
                        seed=1, label="s1", **kw)


def test_f1_a_dead_clip_is_skipped_for_the_next_candidate(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    src = StubSource(["a", "b", "c"], dead={"a"})
    shots = _assign(ctx, src, _slots(2.0, 2.0))
    assert [s.clip_id for s in shots] == ["b", "c"]
    assert src.downloads == ["a", "b", "c"]  # the dead one once, then one file per slot


def test_f1_no_candidate_left_is_ns_e503_naming_the_slot(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    src = StubSource(["a", "b"], dead={"a", "b"})  # two dead clips: under MAX_GIVE_UPS_IN_A_ROW
    with pytest.raises(NsError) as ei:
        _assign(ctx, src, _slots(2.0))
    assert ei.value.code == "NS-E503"
    assert "No other clip was left for s1 shot 1 (0.0-2.0 s)" in ei.value.detail
    assert src.downloads == ["a", "b"]


def test_f1_an_outage_stops_after_three_dead_clips_in_a_row(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ids = [f"c{i}" for i in range(8)]
    src = StubSource(ids, dead=set(ids))
    with pytest.raises(NsError) as ei:
        _assign(ctx, src, _slots(2.0))
    assert ei.value.code == "NS-E503" and "in a row" in ei.value.detail and "s1 shot 1" in ei.value.detail
    assert len(src.downloads) == MAX_GIVE_UPS_IN_A_ROW  # not all eight candidates through the same outage


# ---- F2: no more files than slots plus the bound -----------------------------------------------------------


def test_f2_downloads_are_at_most_the_slots_plus_the_bound(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ids = [f"c{i}" for i in range(20)]
    black = {"c0", "c2"}
    src = StubSource(ids)

    def probe(path: Path, ss: float, length: float | None):
        return None if path.stem in black else []

    slots = _slots(*([2.0] * 6))
    shots = _assign(ctx, src, slots, probe=probe)
    assert len(shots) == 6 and not {s.clip_id for s in shots} & black
    assert len(src.downloads) == 6 + len(black) <= len(slots) + spare_bound(len(slots))


def test_f2_black_beyond_the_bound_stops_instead_of_downloading_on(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ids = [f"c{i}" for i in range(20)]
    src = StubSource(ids)
    slots = _slots(2.0, 2.0)
    with pytest.raises(NsError) as ei:
        _assign(ctx, src, slots, probe=lambda path, ss, length: None)  # every clip black
    assert ei.value.code == "NS-E402" and "spare downloads are spent" in ei.value.detail
    assert len(src.downloads) == spare_bound(len(slots)) + 1


def _prepare_job(ctx) -> None:
    ctx.write_json("parts.json", [{"n": 1, "text": "x", "hook": "", "word_count": 1, "est_seconds": 12.0}])
    ctx.write_json("captions/report.json", {"s1": {}})
    words = [
        {"w": "a", "start": 0.2, "end": 0.5, "sentence_idx": 0},
        {"w": "b", "start": 4.0, "end": 4.3, "sentence_idx": 1},
        {"w": "c", "start": 8.0, "end": 8.3, "sentence_idx": 2},
    ]
    ctx.write_json("captions/s1.json", {"k": 1, "audio_s": 12.0, "words": words})
    ctx.write_json("visual_plan.json", [
        {"part": 1, "beat": 0, "sentence_start": 0, "sentence_end": 1, "keywords": ["dark hallway"],
         "mood": "eerie", "need_vertical": True},
        {"part": 1, "beat": 1, "sentence_start": 2, "sentence_end": 2, "keywords": ["rain window"],
         "mood": "intimate", "need_vertical": True},
    ])


@needs_ffmpeg
def test_f2_f5_a_redo_after_qc_downloads_nothing_new_and_the_cost_is_recorded(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _prepare_job(ctx)
    src = ctx.services.clip_sources[0]
    ClipsStage().run(ctx)
    first = list(src.downloads)
    shots = ctx.read_json("clips/shots.json")["s1"]["shots"]
    assert len(first) == len(shots)  # one file per shot, no spare needed
    cost = ctx.read_json("clips/footage.json")
    assert cost["runs"][0]["files_downloaded"] == len(shots) and cost["bytes"] > 0
    # a QC redo whose cause is not the footage (black frames replace the clips under the black: the test
    # `test_f2_a_redo_for_black_frames_replaces_only_the_clip_under_the_black`)
    ctx.write_json("policy_attempts.json", {"regenerations": 1, "history": [
        {"code": "NS-E404", "detail": "s1: loudness", "rejected_to": None, "cause": "media"}]})
    ClipsStage().run(ctx)
    assert src.downloads == first  # the redo takes every clip from the cache
    again = ctx.read_json("clips/footage.json")
    assert len(again["runs"]) == 2 and again["runs"][1]["files_downloaded"] == 0
    assert again["runs"][1]["bytes"] == 0


@needs_ffmpeg
def test_f2_a_redo_after_the_policy_rejected_the_footage_still_blocks_it(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _prepare_job(ctx)
    ClipsStage().run(ctx)
    before = {s["clip_id"] for s in ctx.read_json("clips/shots.json")["s1"]["shots"]}
    ctx.write_json("policy_attempts.json", {"regenerations": 1, "history": [
        {"code": "NS-E613", "detail": "x", "rejected_to": None, "cause": "media"}]})
    ClipsStage().run(ctx)
    after = {s["clip_id"] for s in ctx.read_json("clips/shots.json")["s1"]["shots"]}
    assert not before & after


@needs_ffmpeg
def test_f2_a_redo_for_black_frames_replaces_only_the_clip_under_the_black(tmp_home: Path, monkeypatch):
    ctx = make_ctx(tmp_home)
    _prepare_job(ctx)
    src = ctx.services.clip_sources[0]
    ClipsStage().run(ctx)
    before = ctx.read_json("clips/shots.json")["s1"]["shots"]
    first = list(src.downloads)
    dark = before[1]
    seen: list[Path] = []

    def spans(video: Path):  # QC's intervals in the rejected render: inside shot 2 only
        seen.append(video)
        return [(dark["start"] + 0.1, dark["start"] + 0.5)]

    monkeypatch.setattr(clips_mod, "_black_spans", spans)
    rejected = tmp_home / "rejected" / "attempt1"
    ctx.write_json("policy_attempts.json", {"regenerations": 1, "history": [
        {"code": "NS-E404", "detail": "s1: black_frames", "rejected_to": str(rejected), "cause": "media"}]})
    ClipsStage().run(ctx)
    after = ctx.read_json("clips/shots.json")["s1"]["shots"]
    assert seen == [rejected / "shorts" / "s1" / "short.mp4"]
    assert dark["clip_id"] not in {s["clip_id"] for s in after}  # the black clip is not put back
    assert len(src.downloads) == len(first) + 1  # one new file, every other clip from the cache


@needs_ffmpeg
def test_f2_a_redo_for_black_frames_blocks_the_item_when_its_render_is_gone(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _prepare_job(ctx)
    ClipsStage().run(ctx)
    before = {s["clip_id"] for s in ctx.read_json("clips/shots.json")["s1"]["shots"]}
    ctx.write_json("policy_attempts.json", {"regenerations": 1, "history": [
        {"code": "NS-E404", "detail": "s1: black_frames", "rejected_to": str(tmp_home / "gone"),
         "cause": "media"}]})
    ClipsStage().run(ctx)
    after = {s["clip_id"] for s in ctx.read_json("clips/shots.json")["s1"]["shots"]}
    assert not before & after


@needs_ffmpeg
def test_f2_a_short_hook_redo_keeps_the_footage(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _prepare_job(ctx)
    src = ctx.services.clip_sources[0]
    ClipsStage().run(ctx)
    first = list(src.downloads)
    ctx.write_json("policy_attempts.json", {"regenerations": 1, "history": [
        {"code": "NS-E611", "detail": "short_hook: s1 does not open with its hook", "rejected_to": None,
         "cause": "media"}]})
    ClipsStage().run(ctx)
    assert src.downloads == first  # a hook redo downloads no new footage


@needs_ffmpeg
def test_qc_black_spans_of_a_black_video(tmp_path: Path):
    black = ff.make_test_clip(tmp_path / "b.mp4", width=64, height=64, seconds=1.0, kind="color",
                              color="0x000000")
    assert clips_mod._black_spans(black) and clips_mod._black_spans(tmp_path / "none.mp4") is None


# ---- F3: black footage never reaches the render (QC's own blackdetect) -------------------------------------


@needs_ffmpeg
def test_f3_a_black_clip_is_replaced_and_the_render_passes_qc_black_frames(tmp_home: Path, tmp_path: Path):
    ctx = make_ctx(tmp_home)
    _prepare_job(ctx)
    src = ctx.services.clip_sources[0]
    first_pick = src.search("dark hallway", orientation="portrait")[0].id
    src.searches.clear()
    src.black = {first_pick}
    ClipsStage().run(ctx)
    entry = ctx.read_json("clips/shots.json")["s1"]
    shots = [Shot.from_json(s) for s in entry["shots"]]
    assert first_pick in src.downloads and first_pick not in {s.clip_id for s in shots}
    audio = ff.make_silence(tmp_path / "a.wav", 12.0)
    treatment = visual_treatment(ctx.combo.visual_treatment)
    video = VideoCfg(encoder="libx264", preset="ultrafast")
    good = render_video(shots=shots, audio=audio, out=tmp_path / "good.mp4", size=(1080, 1920),
                        treatment=treatment, video=video, ass_path=None)
    assert black_check(good).passed
    # the same render with the black clip put back in shot 2 fails QC: the check above can fail
    black_path = str(ctx.paths.cache_clips / "fake" / f"{first_pick}.mp4")
    s = shots[1]
    shots[1] = Shot(s.beat, s.start, s.end, s.source, first_pick, black_path, 0.0, s.width, s.height, s.url,
                    s.license, s.keyword)
    bad = render_video(shots=shots, audio=audio, out=tmp_path / "bad.mp4", size=(1080, 1920),
                       treatment=treatment, video=video, ass_path=None)
    assert not black_check(bad).passed


# ---- F4: a stage a resume skips acquires nothing -----------------------------------------------------------


def _stage(name: str, tenant):
    class S:
        inputs = ()
        outputs = (f"{name}.json",)

        def run(self, ctx):
            ctx.write_json(f"{name}.json", {"ok": True})

    S.name, S.tenant = name, tenant  # type: ignore[attr-defined]
    return S()


def test_f4_a_skipped_llama_stage_acquires_nothing(tmp_home: Path, monkeypatch):
    monkeypatch.setitem(orch.FORMAT_STAGES, "fr_fmt", ["fr1", "fr2", "fr3"])
    for n, t in (("fr1", "llama"), ("fr2", "tts"), ("fr3", None)):
        register(_stage(n, t))
    ctx = make_ctx(tmp_home, fmt="fr_fmt")
    orch.run_job(ctx)
    events = ctx.services.acquirer.events  # type: ignore[attr-defined]
    assert [(e[0], e[1]) for e in events] == [("acquire", "llama"), ("release", "llama"),
                                              ("acquire", "tts"), ("release", "tts")]
    events.clear()
    ctx.file("fr3.json").unlink()  # the redo starts at the last stage, as a redo from `clips` does
    assert orch.run_job(ctx) == ["fr1", "fr2", "fr3"]
    assert events == []  # no story model, no voice service for stages that were skipped


def test_f4_the_tenant_is_acquired_before_the_first_stage_that_runs(tmp_home: Path, monkeypatch):
    monkeypatch.setitem(orch.FORMAT_STAGES, "fr_fmt2", ["fs1", "fs2"])
    register(_stage("fs1", "llama"))
    register(_stage("fs2", "llama"))
    ctx = make_ctx(tmp_home, fmt="fr_fmt2")
    orch.run_job(ctx)
    events = ctx.services.acquirer.events  # type: ignore[attr-defined]
    events.clear()
    ctx.file("fs2.json").unlink()
    orch.run_job(ctx)
    assert [(e[0], e[1]) for e in events] == [("acquire", "llama"), ("release", "llama")]
