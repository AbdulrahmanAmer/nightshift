# ruff: noqa: E501
"""The Assistant's `fetch_footage`: stock clips are searched, downloaded into the cache and reported with their
paths (component C9). Stub clip sources stand in for Pexels and Pixabay, so nothing here touches the network."""
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_dashboard.assistant.llm import ScriptedToolLlm, StaticProvider
from ns_dashboard.assistant.loop import run_tool
from ns_dashboard.assistant.registry import ToolContext, render_confirm
from ns_dashboard.assistant.tools import build_registry
from ns_dashboard.errors import ApiError
from ns_dashboard.handlers import Dashboard, build_state
from ns_dashboard.supervisor_link import SupervisorLink
from ns_studio.media.clip_sources import ClipCandidate, Orientation

NO_KEY = "No stock footage key. Add a Pixabay key (free) in Settings > Stock footage keys"


class StubSource:
    """A keyed source that never uses the network: `n` candidates per search, files of a few bytes."""

    def __init__(self, name: str = "pixabay", n: int = 3, fail_download: set[str] | None = None) -> None:
        self.name = name
        self.n = n
        self.fail_download = fail_download or set()
        self.searches: list[tuple[str, str]] = []
        self.downloads: list[str] = []

    def search(self, keyword: str, *, orientation: Orientation, per_page: int = 80) -> list[ClipCandidate]:
        self.searches.append((keyword, orientation))
        w, h = (1920, 1080) if orientation == "landscape" else (1080, 1920)
        return [
            ClipCandidate(
                source=self.name, id=str(100 + i), page_url=f"https://{self.name}.example/v/{100 + i}",
                download_url=f"https://cdn.example/{100 + i}.mp4", width=w, height=h, duration=8.0 + i,
                tags=("fog", "forest"), title="fog forest", license=f"{self.name} licence",
            )
            for i in range(self.n)
        ]

    def download(self, cand: ClipCandidate, dest: Path) -> Path:
        if cand.id in self.fail_download:
            raise NsError("NS-E503", "stub: connection dropped")
        self.downloads.append(cand.id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"mp4:" + cand.id.encode())
        return dest


def make(tmp_home: Path, keys: dict[str, str] | None = None, sources: list[StubSource] | None = None):
    state = build_state(
        Paths.from_env(), fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
    )
    dash = Dashboard(state, llm_provider=StaticProvider(ScriptedToolLlm([])))
    for name, value in (keys or {}).items():
        state.adapters.keyring.set(name, value)
    seen: list[dict[str, str]] = []
    stubs = {s.name: s for s in (sources or [])}

    def factory(usable: dict[str, str], _paths: Paths) -> list[Any]:
        seen.append(dict(usable))
        return [stubs[n] for n in usable if n in stubs]

    dash.library.footage_sources = factory
    return dash, state, seen


def test_fetch_downloads_into_the_cache_and_records_rows(tmp_home: Path):
    stub = StubSource()
    dash, state, seen = make(tmp_home, {"pixabay_api_key": "pk"}, [stub])
    res = dash.library.fetch_footage("Foggy Forests", "landscape", 2)

    assert seen == [{"pixabay": "pk"}]  # only the key that exists is handed to the sources
    assert list(res)[0] == "paths" and len(res["paths"]) == 2 and res["downloaded"] == 2
    assert res["folder"] == str(state.paths.cache_clips) and res["requested"] == 2
    for p in res["paths"]:
        assert Path(p).is_absolute() and Path(p).is_file()
        assert Path(p).parent == state.paths.cache_clips / "pixabay"
    first = res["clips"][0]
    assert first["path"] == res["paths"][0] and first["source"] == "pixabay" and first["clip_id"] == "100"
    assert first["page_url"] == "https://pixabay.example/v/100" and first["license"] == "pixabay licence"
    assert first["size"] == "1920x1080" and first["duration"] == 8.0 and first["new"] is True
    assert first["keywords"] == ["foggy forest"]  # normalised like the studio's keywords

    rows = {r["clip_id"]: r for r in dash.library.footage()}  # what Library > Footage lists
    assert set(rows) == {"100", "101"}
    assert rows["100"]["local_path"] == first["path"] and rows["100"]["license"] == "pixabay licence"
    assert rows["100"]["keywords"] == ["foggy forest"] and rows["100"]["width"] == 1920
    assert json.loads(state.db.one("SELECT keywords FROM clips WHERE clip_id = '101'")["keywords"]) == ["foggy forest"]


def test_a_second_fetch_takes_new_clips_and_never_downloads_a_cached_one_again(tmp_home: Path):
    stub = StubSource(n=3)
    dash, _state, _ = make(tmp_home, {"pixabay_api_key": "pk"}, [stub])
    one = dash.library.fetch_footage("fog", "landscape", 2)
    two = dash.library.fetch_footage("fog", "landscape", 2)
    assert [c["clip_id"] for c in one["clips"]] == ["100", "101"]
    assert [c["clip_id"] for c in two["clips"]] == ["102", "100"]  # one new clip, then a cached one fills the count
    assert [c["new"] for c in two["clips"]] == [True, False] and two["downloaded"] == 1
    assert "already in the cache" in two["note"]
    assert stub.downloads == ["100", "101", "102"]
    three = dash.library.fetch_footage("fog", "landscape", 1)
    assert three["downloaded"] == 0 and three["paths"] == [one["paths"][0]] and stub.downloads == ["100", "101", "102"]


def test_no_key_is_a_coded_error_and_nothing_is_searched(tmp_home: Path):
    stub = StubSource()
    dash, _state, seen = make(tmp_home, {}, [stub])
    with pytest.raises(ApiError) as ei:
        dash.library.fetch_footage("fog")
    assert ei.value.code == "NS-E502" and ei.value.message == NO_KEY and ei.value.status == 409
    assert seen == [] and stub.searches == []
    # a Pexels key alone does not satisfy an explicit Pixabay request; an explicit Pexels request names Pexels
    dash2, _s2, _ = make(tmp_home, {"pexels_api_key": "xk"}, [StubSource("pexels")])
    with pytest.raises(ApiError) as e2:
        dash2.library.fetch_footage("fog", source="pixabay")
    assert e2.value.code == "NS-E502"
    dash3, _s3, _ = make(tmp_home, {"pixabay_api_key": "pk"}, [stub])
    with pytest.raises(ApiError) as e3:
        dash3.library.fetch_footage("fog", source="pexels")
    assert e3.value.code == "NS-E501" and "Pexels key" in e3.value.message


def test_count_is_capped_and_arguments_are_checked(tmp_home: Path):
    stub = StubSource(n=12)
    dash, _state, _ = make(tmp_home, {"pixabay_api_key": "pk"}, [stub])
    res = dash.library.fetch_footage("fog", "landscape", 99)
    assert res["requested"] == 5 and res["downloaded"] == 5 and len(stub.downloads) == 5
    assert dash.library.fetch_footage("rain", "landscape", 0)["requested"] == 1
    assert dash.library.fetch_footage("mist", "landscape", None)["requested"] == 1
    for bad in (
        {"q": "  "}, {"q": "x" * 101}, {"q": "fog", "orientation": "square"}, {"q": "fog", "source": "youtube"},
        {"q": "fog", "count": "many"},
    ):
        with pytest.raises(ApiError) as ei:
            dash.library.fetch_footage(**bad)
        assert ei.value.code == "invalid_options", bad
    assert len(stub.downloads) == 5 + 1 + 1  # the rejected calls fetched nothing


def test_orientation_is_searched_and_filtered(tmp_home: Path):
    stub = StubSource()
    dash, _state, _ = make(tmp_home, {"pixabay_api_key": "pk"}, [stub])
    res = dash.library.fetch_footage("fog", "portrait", 1)
    assert stub.searches == [("fog", "portrait")]
    assert res["orientation"] == "portrait" and res["clips"][0]["size"] == "1080x1920"


def test_auto_asks_every_keyed_source_and_a_dead_download_is_skipped(tmp_home: Path):
    pix = StubSource("pixabay", n=2, fail_download={"100"})
    pex = StubSource("pexels", n=2)
    dash, _state, seen = make(tmp_home, {"pixabay_api_key": "pk", "pexels_api_key": "xk"}, [pix, pex])
    res = dash.library.fetch_footage("fog", "landscape", 2)
    assert seen == [{"pixabay": "pk", "pexels": "xk"}]
    assert {(c["source"], c["clip_id"]) for c in res["clips"]} == {("pexels", "100"), ("pixabay", "101")}
    # pexels only
    dash2, _s2, seen2 = make(tmp_home, {"pixabay_api_key": "pk", "pexels_api_key": "xk"}, [pix, pex])
    dash2.library.fetch_footage("fog", "landscape", 1, "pexels")
    assert seen2 == [{"pexels": "xk"}]


def test_when_nothing_can_be_downloaded_the_source_error_comes_back(tmp_home: Path):
    pix = StubSource("pixabay", n=2, fail_download={"100", "101"})
    dash, _state, _ = make(tmp_home, {"pixabay_api_key": "pk"}, [pix])
    with pytest.raises(NsError) as ei:
        dash.library.fetch_footage("fog")
    assert ei.value.code == "NS-E503"
    assert dash.library.footage() == []  # no row for a clip that did not arrive


def test_nothing_found_is_an_empty_answer_with_a_note(tmp_home: Path):
    dash, _state, _ = make(tmp_home, {"pixabay_api_key": "pk"}, [StubSource(n=0)])
    res = dash.library.fetch_footage("zzzz")
    assert res["paths"] == [] and res["clips"] == [] and "No landscape clips found" in res["note"]


def test_route_posts_json_and_reports_the_coded_error(tmp_home: Path):
    from tests.test_dashboard_api import make_env

    client, state = make_env(tmp_home)
    body = {"q": "fog", "orientation": "landscape", "count": 1}
    r = client.post("/api/library/footage/fetch", json=body)
    assert r.status_code == 409 and r.json()["error"]["code"] == "NS-E502" and r.json()["error"]["message"] == NO_KEY
    state.adapters.keyring.set("pixabay_api_key", "pk")
    client.app.state.dashboard.library.footage_sources = lambda keys, paths: [StubSource()]
    ok = client.post("/api/library/footage/fetch", json={**body, "count": 2})
    assert ok.status_code == 200 and len(ok.json()["paths"]) == 2 and Path(ok.json()["paths"][0]).is_file()
    assert client.get("/api/library/footage").json()[0]["local_path"] in ok.json()["paths"]
    assert client.post("/api/library/footage/fetch", json={"orientation": "landscape"}).status_code == 422


def test_the_tool_is_registered_with_a_confirm_card_and_runs(tmp_home: Path):
    reg = build_registry()
    td = reg.get("fetch_footage")
    assert td is not None and td.route == ("POST", "/api/library/footage/fetch")
    spec = td.spec
    assert spec.side_effect is True and spec.topic == "library" and spec.args_schema["required"] == ["q"]
    assert render_confirm(spec, {"q": "foggy road", "orientation": "portrait", "count": 3, "source": "pexels"}) == (
        "Download up to 3 portrait stock clips for 'foggy road' from pexels into the footage cache."
    )
    assert render_confirm(spec, {"q": "foggy road"}) == (
        "Download up to 1 landscape stock clips for 'foggy road' from auto into the footage cache."
    )
    assert reg.validate("fetch_footage", {"q": "fog", "count": 6}) is not None  # above the cap
    assert reg.validate("fetch_footage", {"q": "fog", "source": "auto", "count": 5}) is None

    dash, _state, _ = make(tmp_home, {"pixabay_api_key": "pk"}, [StubSource()])
    ok, result = run_tool(td, ToolContext(dash), {"q": "fog", "count": 1})
    assert ok and len(result["paths"]) == 1 and Path(result["paths"][0]).is_file()

    nokey, _s, _ = make(tmp_home, {}, [StubSource()])
    ok2, err = run_tool(td, ToolContext(nokey), {"q": "fog"})
    assert ok2 is False and err["error"]["code"] == "NS-E502" and err["error"]["message"] == NO_KEY


def test_where_is_answers_the_footage_cache(tmp_home: Path):
    dash, state, _ = make(tmp_home)
    where = dash.registry.get("where_is").call
    for thing in ("footage cache", "stock footage", "Stock Footage", "footage", "clip cache"):
        res = where(ToolContext(dash), {"thing": thing})
        assert res == {"kind": "cache", "path": str(state.paths.cache_clips)}, thing
    assert Path(res["path"]).is_absolute()


def test_a_dev_install_fetches_from_the_fake_source_without_a_key(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    """docs/DEV_MODE.md: the Footage page and the Assistant's fetch_footage work on a dev install with no key."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not installed")
    dash, state, seen = make(tmp_home, {}, [])
    state.paths.runtime_file.parent.mkdir(parents=True, exist_ok=True)
    state.paths.runtime_file.write_text(json.dumps({"dev_mode": True}), encoding="utf-8")
    out = dash.library.fetch_footage("fog", count=2)
    assert len(out["clips"]) == 2 and all(Path(c["path"]).is_file() for c in out["clips"])
    assert all(c["source"] == "fake" for c in out["clips"]) and seen == []
    # a stored key still wins over the fake source
    stub = StubSource()
    dash2, state2, seen2 = make(tmp_home, {"pixabay_api_key": "pk"}, [stub])
    dash2.library.fetch_footage("fog")
    assert seen2 == [{"pixabay": "pk"}] and stub.downloads


def test_fake_footage_fetches_from_the_fake_source_without_a_key_or_dev_mode(tmp_home: Path):
    """The fake_footage engine test (docs/DEV_MODE.md): a real install with no key still gets
    placeholder clips."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not installed")
    dash, state, seen = make(tmp_home, {}, [])
    state.paths.runtime_file.parent.mkdir(parents=True, exist_ok=True)
    state.paths.runtime_file.write_text(json.dumps({"fake_footage": True}), encoding="utf-8")
    out = dash.library.fetch_footage("fog", count=2)
    assert len(out["clips"]) == 2 and all(c["source"] == "fake" for c in out["clips"]) and seen == []
    # without the setting the same install still refuses
    state.paths.runtime_file.write_text(json.dumps({"fake_footage": False}), encoding="utf-8")
    with pytest.raises(ApiError) as ei:
        dash.library.fetch_footage("fog")
    assert ei.value.code == "NS-E502"
