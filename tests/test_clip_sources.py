import json
from pathlib import Path

import httpx
import pytest

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_studio.media.clip_sources import (
    FakeClipSource,
    PexelsSource,
    PixabaySource,
    TokenBucket,
    parse_pexels,
    parse_pixabay,
    retry_wait_s,
)

FIX = Path(__file__).parent / "fixtures" / "clips"


def _load(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


class FakeTime:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s


def test_token_bucket_waits_when_empty():
    t = FakeTime()
    b = TokenBucket(2, 0.5, clock=t.clock, sleep=t.sleep)
    assert b.acquire() == 0 and b.acquire() == 0
    assert b.acquire() == pytest.approx(2.0)
    assert t.slept == [pytest.approx(2.0)]
    t.now += 100
    assert b.acquire() == 0


def test_token_bucket_refill_is_capped():
    t = FakeTime()
    b = TokenBucket(3, 1.0, clock=t.clock, sleep=t.sleep)
    t.now += 1000
    b.acquire(3)
    assert b.acquire() == pytest.approx(1.0)


def test_parse_pexels_picks_smallest_hd_mp4_and_skips_hls():
    cands = parse_pexels(_load("pexels_search.json"), "portrait")
    assert [c.id for c in cands] == ["3571264", "855564", "4990223"]
    first = cands[0]
    assert first.download_url.endswith("/hd.mp4") and (first.width, first.height) == (1080, 1920)
    assert first.tags == ("a", "dark", "hallway", "with", "flickering", "light")
    assert first.license == "Pexels License" and first.orientation == "portrait"
    assert cands[2].download_url.endswith("/sd.mp4")  # nothing HD: the largest available


def test_pexels_search_sends_auth_header_and_orientation():
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers["Authorization"]
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json=_load("pexels_search.json"))

    src = PexelsSource("KEY123", Path("."), client=_client(handler))
    out = src.search("dark hallway", orientation="portrait", per_page=200)
    assert len(out) == 3
    assert seen["auth"] == "KEY123"
    assert seen["params"]["orientation"] == "portrait"
    assert seen["params"]["query"] == "dark hallway"
    assert seen["params"]["per_page"] == "80"


def test_pexels_retries_after_429_and_honours_reset():
    t = FakeTime()
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"X-Ratelimit-Reset": str(int(t.now) + 30)})
        return httpx.Response(200, json=_load("pexels_search.json"))

    src = PexelsSource("K", Path("."), client=_client(handler), sleep=t.sleep, wall=t.clock)
    assert len(src.search("x", orientation="portrait")) == 3
    assert calls["n"] == 2 and t.slept[0] == pytest.approx(30.0, abs=1.0)


def test_pexels_gives_up_after_repeated_429_and_codes_errors():
    t = FakeTime()
    src = PexelsSource(
        "K", Path("."), client=_client(lambda r: httpx.Response(429, headers={"Retry-After": "2"})),
        sleep=t.sleep, wall=t.clock,
    )
    with pytest.raises(NsError) as ei:
        src.search("x", orientation="portrait")
    assert ei.value.code == "NS-E501" and len(t.slept) == 4

    bad = PexelsSource("K", Path("."), client=_client(lambda r: httpx.Response(401)))
    with pytest.raises(NsError) as ei2:
        bad.search("x", orientation="portrait")
    assert ei2.value.code == "NS-E501"

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(NsError) as ei3:
        PexelsSource("K", Path("."), client=_client(boom), sleep=t.sleep).search("x", orientation="portrait")
    assert ei3.value.code == "NS-E503"


def test_missing_key_is_a_coded_error():
    with pytest.raises(NsError) as ei:
        PixabaySource("", Path(".")).search("x", orientation="portrait")
    assert ei.value.code == "NS-E502"


def test_retry_wait_parsing():
    r = httpx.Response(429, headers={"Retry-After": "7"})
    assert retry_wait_s(r, 0) == 7
    assert retry_wait_s(httpx.Response(429), 0) == 60
    assert retry_wait_s(httpx.Response(429, headers={"Retry-After": "99999"}), 0) == 3600


def test_pexels_download_streams_to_dest(tmp_path: Path):
    def handler(req: httpx.Request) -> httpx.Response:
        assert "Authorization" not in req.headers
        return httpx.Response(200, content=b"x" * 100_000)

    src = PexelsSource("K", tmp_path, client=_client(handler))
    cand = parse_pexels(_load("pexels_search.json"), "portrait")[0]
    dest = src.download(cand, tmp_path / "pexels" / "3571264.mp4")
    assert dest.read_bytes() == b"x" * 100_000
    assert not list(tmp_path.rglob("*.part"))


def test_pixabay_rendition_is_the_smallest_that_still_covers_1080p():
    from ns_studio.media.clip_sources import pick_pixabay_rendition

    def r(w, h):
        return {"url": f"u{w}x{h}", "width": w, "height": h, "size": w * h}

    live_2026 = {"large": r(3840, 2160), "medium": r(2560, 1440), "small": r(1920, 1080),
                 "tiny": r(1280, 720)}
    assert pick_pixabay_rendition(live_2026)["url"] == "u1920x1080"          # not the 35 MB 4K file
    older = {"large": r(1920, 1080), "medium": r(1280, 720), "small": r(640, 360), "tiny": r(480, 270)}
    assert pick_pixabay_rendition(older)["url"] == "u1920x1080"
    portrait = {"large": {"url": "", "width": 0, "height": 0, "size": 0}, "medium": r(1080, 1920),
                "small": r(540, 960), "tiny": r(360, 640)}
    assert pick_pixabay_rendition(portrait)["url"] == "u1080x1920"           # empty large skipped
    small_only = {"medium": r(1280, 720), "tiny": r(640, 360)}
    assert pick_pixabay_rendition(small_only)["url"] == "u1280x720"          # best there is
    assert pick_pixabay_rendition({}) is None


def test_parse_pixabay_filters_by_ratio_and_skips_empty_renditions():
    portrait = parse_pixabay(_load("pixabay_search.json"), "portrait")
    assert [c.id for c in portrait] == ["126", "128"]
    assert portrait[0].download_url.endswith("126_medium.mp4")  # large is empty
    landscape = parse_pixabay(_load("pixabay_search.json"), "landscape")
    assert [c.id for c in landscape] == ["125"]  # 1080x1080 is neither
    assert landscape[0].tags == ("rain", "window", "night") and landscape[0].license.startswith("Pixabay")


def test_pixabay_caches_responses_for_24h(tmp_path: Path):
    t = FakeTime()
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert req.url.params["key"] == "PK" and req.url.params["q"] == "rain"
        return httpx.Response(200, json=_load("pixabay_search.json"))

    src = PixabaySource("PK", tmp_path, client=_client(handler), wall=t.clock, sleep=t.sleep)
    a = src.search("rain", orientation="landscape")
    b = src.search("rain", orientation="portrait")  # same query, other orientation: served from cache
    assert calls["n"] == 1 and src.api_calls == 1
    assert [c.id for c in a] == ["125"] and [c.id for c in b] == ["126", "128"]
    t.now += 23 * 3600
    src.search("rain", orientation="landscape")
    assert calls["n"] == 1
    t.now += 2 * 3600
    src.search("rain", orientation="landscape")
    assert calls["n"] == 2


def _log_events(logs_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for f in sorted(logs_dir.glob("*.jsonl"))
        for line in f.read_text(encoding="utf-8").splitlines()
    ]


def _counting(calls: dict, status: int):
    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status)

    return handler


def test_d5_pexels_503_503_200_succeeds_after_two_retries(tmp_path: Path):
    t = FakeTime()
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(503)
        return httpx.Response(200, json=_load("pexels_search.json"))

    log = NsLogger("studio", tmp_path / "logs", echo=False)
    src = PexelsSource("K", Path("."), client=_client(handler), sleep=t.sleep, wall=t.clock, log=log)
    assert len(src.search("x", orientation="portrait")) == 3
    assert calls["n"] == 3 and len(t.slept) == 2 and t.slept[0] < t.slept[1]  # backoff grows
    retries = [e for e in _log_events(tmp_path / "logs") if e["event"] == "footage_host_retry"]
    assert len(retries) == 2 and retries[0]["data"]["source"] == "pexels"
    assert retries[0]["data"]["reason"] == "HTTP 503"


def test_d5_repeated_503_is_an_outage_never_the_key_hint():
    t = FakeTime()
    for cls, host in ((PixabaySource, "Pixabay"), (PexelsSource, "Pexels")):
        calls = {"n": 0}
        src = cls("K", Path("."), client=_client(_counting(calls, 503)), sleep=t.sleep, wall=t.clock)
        with pytest.raises(NsError) as ei:
            src.search("x", orientation="portrait")
        assert ei.value.code == "NS-E503"
        assert f"{host} is not answering right now; Nightshift will try again" in ei.value.detail
        assert "key" not in ei.value.detail.lower() and "key" not in ei.value.definition.hint.lower()
        assert calls["n"] > 1


def test_d5_timeouts_and_resets_are_retried():
    t = FakeTime()
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow", request=req)
        if calls["n"] == 2:
            raise httpx.ReadError("connection reset", request=req)
        return httpx.Response(200, json=_load("pexels_search.json"))

    src = PexelsSource("K", Path("."), client=_client(handler), sleep=t.sleep, wall=t.clock)
    assert len(src.search("x", orientation="portrait")) == 3 and calls["n"] == 3


def test_d5_key_errors_stop_at_once_without_a_retry():
    t = FakeTime()
    for status, cls, code in (
        (401, PexelsSource, "NS-E501"),
        (403, PexelsSource, "NS-E501"),
        (401, PixabaySource, "NS-E502"),
        (400, PixabaySource, "NS-E502"),
    ):
        calls = {"n": 0}
        src = cls("K", Path("."), client=_client(_counting(calls, status)), sleep=t.sleep, wall=t.clock)
        with pytest.raises(NsError) as ei:
            src.search("x", orientation="portrait")
        assert ei.value.code == code and calls["n"] == 1 and t.slept == []


def test_d5_the_429_wait_is_logged(tmp_path: Path):
    t = FakeTime()
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "12"})
        return httpx.Response(200, json=_load("pexels_search.json"))

    log = NsLogger("studio", tmp_path / "logs", echo=False)
    src = PexelsSource("K", Path("."), client=_client(handler), sleep=t.sleep, wall=t.clock, log=log)
    src.search("x", orientation="portrait")
    waits = [e for e in _log_events(tmp_path / "logs") if e["event"] == "footage_rate_limited"]
    assert len(waits) == 1 and waits[0]["data"]["wait_s"] == 12.0 and waits[0]["data"]["source"] == "pexels"


def test_d5_the_searcher_hands_the_job_log_to_the_sources(tmp_path: Path):
    from ns_studio.media.clips import Searcher

    t = FakeTime()
    log = NsLogger("studio", tmp_path / "logs", echo=False)
    src = PixabaySource(
        "K", tmp_path, client=_client(lambda r: httpx.Response(503)), sleep=t.sleep, wall=t.clock
    )
    s = Searcher([src], log=log)
    assert s.search("rain", "portrait") == []
    assert s.first_error() is not None and s.first_error().code == "NS-E503"
    events = [e["event"] for e in _log_events(tmp_path / "logs")]
    assert events.count("footage_host_retry") == 3 and "footage_host_down" in events


def test_d5_a_download_503_is_an_outage_not_a_key_error(tmp_path: Path):
    src = PexelsSource("K", tmp_path, client=_client(lambda r: httpx.Response(503)))
    cand = parse_pexels(_load("pexels_search.json"), "portrait")[0]
    with pytest.raises(NsError) as ei:
        src.download(cand, tmp_path / "pexels" / "a.mp4")
    assert ei.value.code == "NS-E503" and "not answering right now" in ei.value.detail
    assert not list(tmp_path.rglob("*.part"))
    bad = PexelsSource("K", tmp_path, client=_client(lambda r: httpx.Response(403)))
    with pytest.raises(NsError) as ei2:
        bad.download(cand, tmp_path / "pexels" / "b.mp4")
    assert ei2.value.code == "NS-E501"


@pytest.mark.skipif(not ff.available(), reason="ffmpeg required")
def test_fake_source_search_and_download(tmp_path: Path):
    src = FakeClipSource(tmp_path)
    cands = src.search("Dark Hallway", orientation="portrait")
    assert len(cands) == 6 and len({c.id for c in cands}) == 6
    assert all(c.orientation == "portrait" and "dark" in c.tags for c in cands)
    assert src.search("Dark Hallway", orientation="portrait") == cands  # deterministic
    land = src.search("Dark Hallway", orientation="landscape")
    assert {c.id for c in land}.isdisjoint({c.id for c in cands})
    out = src.download(cands[0], tmp_path / "c0.mp4")
    w, h = ff.video_size(out)
    assert h > w and ff.duration_s(out) >= 4.5
    src.download(cands[1], tmp_path / "c1.mp4")
    assert ff.video_size(tmp_path / "c1.mp4")[1] > 0
