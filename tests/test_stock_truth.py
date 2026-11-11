"""Card stock-truth: every answer of Pexels and Pixabay is told as what it is; a wrong key shows before a job.

A local stand-in server per site (a free loopback port) replays the bodies and status codes the real sites
gave on 2026-11-11 (`REAL` below, from runs/stock-check) and the documented
search answers (`pexels_search.json`, `pixabay_search.json`). No test reaches the network: an autouse guard
refuses every connection and name lookup that is not loopback, and the real host names go to the stand-ins.
"""

from __future__ import annotations

import contextlib
import json
import random
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from typer.testing import CliRunner

from installer.stage2.web import keys as setup_keys
from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_studio import daily, jobs
from ns_studio.media.clip_sources import ClipCandidate, PexelsSource, PixabaySource
from tests.test_cli import CALLS, stub_stages  # noqa: F401  (the fixture: two stub stages, no model)

FIX = Path(__file__).parent / "fixtures" / "clips"
#: The sites' answers as captured live on 2026-11-11 (runs/stock-check/live-output.txt, live-real-output.txt);
#: the two 429 bodies are the documented words (no live capture of a 429 exists):
#: Pexels "429 Too Many Requests" (https://www.pexels.com/api/documentation/); Pixabay "HTTP error 429 ("Too
#: Many Requests") with the message "API rate limit exceeded"" (https://pixabay.com/api/docs/).
REAL: dict[str, dict[str, Any]] = {
    "pexels": {
        "key_wrong": {"status": 401, "headers": {"content-type": "application/json; charset=utf-8"},
                      "body": '{"status":401,"code":"Unauthorized","message":"Invalid API key"}'},
        "rate_limited": {"status": 429, "headers": {"content-type": "text/plain"},
                         "body": "Too Many Requests"},
    },
    "pixabay": {
        "ok_headers": {"content-type": "application/json", "x-ratelimit-limit": "100",
                       "x-ratelimit-remaining": "99", "x-ratelimit-reset": "60"},
        "key_wrong": {"status": 400, "headers": {"content-type": "text/html; charset=utf-8"},
                      "body": "[ERROR 400] Invalid or missing API key (https://pixabay.com/api/docs/)."},
        "rate_limited": {"status": 429, "headers": {"content-type": "text/html; charset=utf-8",
                                                    "x-ratelimit-limit": "100", "x-ratelimit-remaining": "0",
                                                    "x-ratelimit-reset": "60"},
                         "body": "API rate limit exceeded"},
    },
}
NOW = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)
HOSTS = {"api.pexels.com": "pexels", "pixabay.com": "pixabay"}
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64
KEY = "stocktruth-KEY-7f3a9c41d2e8b605aa"  # distinctive: the leak test searches every file for it
MODES = ("ok", "key_wrong", "rate_limited", "failing", "no_connection", "unexpected")


# ------------------------------------------------------------------------------------------- no network
@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse any name lookup or connection that is not loopback (and any proxy the PC is set to use)."""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    real_getaddrinfo, real_connect = socket.getaddrinfo, socket.socket.connect

    def getaddrinfo(host: Any, *a: Any, **kw: Any) -> Any:
        if host not in ("127.0.0.1", "localhost", "::1", b"127.0.0.1"):
            raise OSError(f"the test refuses a name lookup of {host!r}")
        return real_getaddrinfo(host, *a, **kw)

    def connect(self: socket.socket, address: Any) -> Any:
        if not (isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1")):
            raise OSError(f"the test refuses a connection to {address!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)


# ------------------------------------------------------------------------------------------- stand-ins
class StandIn:
    """One site: answers every request as `mode` says, with the real bodies, and counts what it was asked.
    `no_connection`: the connection is closed with no answer at all (a reset, as when the line drops; a
    refused port costs Windows two seconds per try, which the retries would multiply)."""

    def __init__(self, provider: str) -> None:
        self.provider, self.mode, self.paths = provider, "ok", []
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - quiet
                return

            def do_GET(self) -> None:  # noqa: N802
                stand_in.paths.append(urlsplit(self.path).path)
                if stand_in.mode == "no_connection":
                    self.close_connection = True
                    return
                status, headers, body = stand_in.answer(self.path.split("?")[0].endswith(".mp4"))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def target(self) -> str:
        return self.url

    def base(self) -> str:
        """The base a source is built with (Pexels' documented `/v1`, Pixabay's video endpoint)."""
        return self.target() + ("/v1" if self.provider == "pexels" else "/api/videos/")

    @property
    def searches(self) -> int:
        return sum(1 for p in self.paths if not p.endswith(".mp4"))

    def answer(self, download: bool) -> tuple[int, dict[str, str], bytes]:
        real = REAL[self.provider]
        if self.mode == "ok":
            if download:
                return 200, {"content-type": "video/mp4"}, MP4
            headers = REAL["pixabay"]["ok_headers"] if self.provider == "pixabay" else {
                "content-type": "application/json; charset=utf-8"}
            return 200, headers, (FIX / f"{self.provider}_search.json").read_bytes()
        if self.mode in ("key_wrong", "rate_limited"):
            r = real[self.mode]
            return int(r["status"]), dict(r["headers"]), str(r["body"]).encode()
        if self.mode == "failing":
            return 503, {"content-type": "text/plain"}, b""
        # an answer not understood: Pixabay's own words, where a search list or a video file should be
        words = REAL["pixabay"]["key_wrong"]
        return (403 if download else 200), dict(words["headers"]), str(words["body"]).encode()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def sites(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, StandIn]]:
    """Both stand-ins; `urllib` requests to the real hosts (setup's and Settings' key check, `ns doctor`) go
    to them, as do sources built without a base (the studio's), through the module's documented bases."""
    from ns_studio.media import clip_sources

    out = {p: StandIn(p) for p in ("pexels", "pixabay")}
    real_urlopen = urllib.request.urlopen

    def urlopen(req: Any, *a: Any, **kw: Any) -> Any:
        url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
        parts = urlsplit(url)
        site = out.get(HOSTS.get(parts.hostname or "", ""))
        if site is None:
            raise urllib.error.URLError("the test refuses this host")
        query = f"?{parts.query}" if parts.query else ""
        headers = dict(req.header_items()) if isinstance(req, urllib.request.Request) else {}
        to = urllib.request.Request(site.target() + parts.path + query, headers=headers)
        return real_urlopen(to, *a, **kw)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(clip_sources, "PEXELS_URL", out["pexels"].url + "/v1", raising=False)
    monkeypatch.setattr(clip_sources, "PIXABAY_URL", out["pixabay"].url + "/api/videos/", raising=False)
    try:
        yield out
    finally:
        for s in out.values():
            s.close()


def _source(provider: str, site: StandIn, cache: Path, key: str = KEY) -> PexelsSource | PixabaySource:
    cls = PexelsSource if provider == "pexels" else PixabaySource
    return cls(key, cache, base_url=site.base(), sleep=lambda s: None, wall=lambda: 1_700_000_000.0)


def _candidate(provider: str, site: StandIn) -> ClipCandidate:
    at = site.target()
    return ClipCandidate(provider, "1", f"{at}/page", f"{at}/file/1.mp4", 1920, 1080, 10.0, ("fog",), "fog",
                         "l")


# ----------------------------------------------------------------------------- 1. every answer, every caller
EXPECTED_CODE = {"ok": None, "rate_limited": "NS-E504", "failing": "NS-E503", "no_connection": "NS-E503",
                 "unexpected": "NS-E505"}
KEY_CODE = {"pexels": "NS-E501", "pixabay": "NS-E502"}


def _expected(provider: str, mode: str, caller: str) -> tuple[str | None, bool, bool]:
    """(code, ok for the key check's verdict, whether the words say the key was not accepted)."""
    if mode == "key_wrong":
        if caller == "download":  # a file link carries no key: its 4xx is an answer not understood
            return "NS-E505", True, False
        return KEY_CODE[provider], caller != "key_check", True  # the key check's verdict: not ok
    return EXPECTED_CODE[mode], True, False


def _ask(provider: str, mode: str, caller: str, site: StandIn, tmp: Path) -> tuple[str | None, bool, bool]:
    site.mode = mode
    if caller == "key_check":
        v = setup_keys.check_key(provider, KEY)
        return v["code"], bool(v["ok"]), "did not accept" in str(v["message"] or "")
    src = _source(provider, site, tmp / f"{provider}-{mode}-{caller}")
    try:
        if caller == "search":
            assert src.search("fog", orientation="landscape")
        else:
            src.download(_candidate(provider, site), tmp / f"{provider}-{mode}.mp4")
    except NsError as e:
        return e.code, True, "did not accept" in e.detail
    return None, True, False


def test_each_answer_is_classified_for_each_caller(sites: dict[str, StandIn], tmp_path: Path):
    got, want = {}, {}
    for provider in ("pexels", "pixabay"):
        for mode in MODES:
            for caller in ("key_check", "search", "download"):
                want[(provider, mode, caller)] = _expected(provider, mode, caller)
                got[(provider, mode, caller)] = _ask(provider, mode, caller, sites[provider], tmp_path)
    wrong = {k: (got[k], want[k]) for k in want if got[k] != want[k]}
    assert wrong == {}, "\n".join(f"{k}: got {g}, want {w}" for k, (g, w) in wrong.items())


def test_the_hints_never_blame_the_key_for_a_limit_an_outage_or_an_odd_answer():
    from ns_common.errors import describe, load_errors

    assert {"NS-E504", "NS-E505"} <= set(load_errors()), "a limit and an odd answer have their own codes"
    for code in ("NS-E503", "NS-E504", "NS-E505"):
        hint = describe(code).hint.lower()
        assert "paste" not in hint and "check the pexels key" not in hint and "add or check" not in hint, code
    for code in ("NS-E501", "NS-E502"):  # the key codes no longer carry a 429 or an outage
        assert "limit" not in describe(code).hint.lower(), code


# ------------------------------------------------------------------------------- 2. the documented path
def test_pexels_is_asked_at_the_documented_path(monkeypatch: pytest.MonkeyPatch):
    """https://www.pexels.com/api/documentation/ (read 2026-11-11): "GET https://api.pexels.com/v1/videos/search";
    "The https://api.pexels.com/videos/ endpoints will be deprecated in the future"."""
    asked: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        asked.append(str(req.url).split("?")[0])
        return httpx.Response(200, content=(FIX / "pexels_search.json").read_bytes())

    def urlopen(req: Any, *a: Any, **kw: Any) -> Any:  # records the address; no answer (nothing is sent)
        asked.append(str(req.full_url).split("?")[0])
        raise urllib.error.URLError("recorded only")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    PexelsSource("K", Path("."), client=httpx.Client(transport=httpx.MockTransport(handler))).search(
        "fog", orientation="landscape")
    setup_keys.check_key("pexels", "K")  # setup's and Settings' key check
    assert asked == ["https://api.pexels.com/v1/videos/search"] * 2


# ------------------------------------------------------------------------------- 3, 4. setup and Settings
class Redirect(httpx.HTTPTransport):
    """The wizard's own client, sending the real host names to the stand-ins (a real HTTP exchange)."""

    def __init__(self, sites: dict[str, StandIn]) -> None:
        super().__init__()
        self.sites = sites

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        site = self.sites.get(HOSTS.get(request.url.host, ""))
        if site is None:
            raise httpx.ConnectError("the test refuses this host", request=request)
        to = httpx.URL(site.target())
        request.url = request.url.copy_with(scheme="http", host=to.host, port=to.port)
        return super().handle_request(request)


def test_setup_accepts_a_key_it_cannot_check_now(sites: dict[str, StandIn], tmp_path: Path):
    from installer.stage2.steps.s01_questions import S01Questions
    from installer.stage2.testing import FakeUi, make_test_context
    from tests.test_wizard_steps import provided

    sites["pixabay"].mode = "failing"
    v = setup_keys.check_key("pixabay", KEY)  # the setup window's /api/check-key
    window = (v["ok"], v["code"], "kept" in str(v["message"]), "not answering" in str(v["message"]))
    ui = FakeUi(interactive=False)
    ctx = make_test_context(tmp_path, raw_answers=provided(tmp_path, pixabay_api_key=KEY), simulate=False,
                            ui=ui, http=lambda: httpx.Client(transport=Redirect(sites)))
    raised = None
    try:
        S01Questions().run(ctx)  # the wizard step with the answers file's key
    except NsError as e:
        raised = e.code
    stored = ctx.store.get("pixabay_api_key") == KEY
    said = "Could not check the Pixabay key now" in ui.text and "not answering" in ui.text
    # a wrong key is still refused on both
    sites["pixabay"].mode = "key_wrong"
    bad = setup_keys.check_key("pixabay", KEY)
    ctx2 = make_test_context(tmp_path / "w", raw_answers=provided(tmp_path / "w", pixabay_api_key=KEY),
                             simulate=False, ui=FakeUi(interactive=False),
                             http=lambda: httpx.Client(transport=Redirect(sites)))
    with pytest.raises(NsError) as ei:
        S01Questions().run(ctx2)
    assert (window, raised, stored, said) == ((True, "NS-E503", True, True), None, True, True)
    assert (bad["ok"], bad["code"], ei.value.code) == (False, "NS-E502", "NS-E502")


def test_settings_saves_a_key_it_cannot_check_now(sites: dict[str, StandIn], tmp_home: Path):
    from ns_dashboard.errors import ApiError
    from ns_dashboard.handlers import Dashboard, build_state
    from ns_dashboard.supervisor_link import SupervisorLink
    from ns_dashboard.svc_settings import live_key_check

    state = build_state(Paths.from_env(), fake_services=True, link=SupervisorLink("http://127.0.0.1:9",
                                                                                     timeout_s=0.2))
    svc = Dashboard(state).settings
    svc.key_check = live_key_check
    out: dict[str, Any] = {}
    for mode in ("rate_limited", "failing", "no_connection", "unexpected"):
        sites["pexels"].mode = mode
        try:
            out[mode] = svc.set_key("pexels", f"{KEY}-{mode}")
        except ApiError as e:
            out[mode] = {"refused": e.code}
    for mode, answer in out.items():
        assert answer.get("set") is True and answer.get("checked") is False, (mode, answer)
        assert "could not be checked now" in answer["message"] and "did not accept" not in answer["message"]
    assert state.adapters.keyring.get("pexels_api_key") == f"{KEY}-unexpected"
    sites["pexels"].mode = "key_wrong"
    with pytest.raises(ApiError) as ei:
        svc.set_key("pexels", "wrong")
    assert ei.value.code == "NS-E501" and state.adapters.keyring.get("pexels_api_key") == f"{KEY}-unexpected"


# ------------------------------------------------------------------------------- 5 to 8. before a job
def _night_env(tmp_home: Path, sites: dict[str, StandIn], keys: dict[str, str], per_day: int = 3) -> Any:
    from tests.test_cadence import _channel, _env

    paths, db = _env(tmp_home)
    _channel(paths, db, 9, format="shorts_only", shorts_per_day_max=per_day)
    env = jobs.open_env(light=True)
    env.services.clip_sources = [_source(p, sites[p], paths.cache_search, k) for p, k in keys.items()]
    env.services.footage_key = True
    return env


def _night(env: Any) -> tuple[dict[str, Any], list[str]]:
    ran: list[str] = []
    report = daily.run_daily(env, now=NOW, rng=random.Random(1), awake=contextlib.nullcontext,
                             run_fn=lambda ctx: ran.append(ctx.job_id) or [])
    return report, ran


def _notes(paths: Paths) -> list[str]:
    from ns_supervisor import notify

    return [str(n["message"]) for n in notify.read_notes(paths)]


def test_a_wrong_key_stops_the_night_before_any_model_loads(sites: dict[str, StandIn], tmp_home: Path):
    sites["pixabay"].mode = "key_wrong"
    env = _night_env(tmp_home, sites, {"pixabay": KEY})
    report, ran = _night(env)
    assert ran == [] and report["jobs"] == []  # run_fn is what loads models: never called
    assert report["failures"] and {f["code"] for f in report["failures"]} == {"NS-E502"}
    said = report["failures"][0]["error"]
    assert "Pixabay did not accept the key" in said and "Settings > Stock footage keys" in said
    assert all(f["job_id"] == "(not created)" for f in report["failures"])
    assert not env.paths.jobs_dir.exists() or not list(env.paths.jobs_dir.iterdir())
    assert sites["pixabay"].searches == 1  # asked once for the whole night
    notes = [n for n in _notes(env.paths) if "Pixabay did not accept" in n]
    assert len(notes) == 1 and "while Automatic runs are on in Settings" in notes[0]


def test_studio_run_stops_on_a_wrong_key_before_any_stage(
    sites: dict[str, StandIn], tmp_home: Path, stub_stages: None, monkeypatch: pytest.MonkeyPatch  # noqa: F811
):
    from ns_studio.cli import app

    sites["pixabay"].mode = "key_wrong"
    monkeypatch.setattr(jobs, "load_keys", lambda: {"pixabay": KEY})
    res = CliRunner().invoke(app, ["run", "--fake-services=llm,tts", "--format", "shorts_only"])
    assert CALLS == [] and res.exit_code == 1, res.output
    assert "NS-E502" in res.output and "Settings > Stock footage keys" in res.output
    assert not Paths.from_env().jobs_dir.exists() or not list(Paths.from_env().jobs_dir.iterdir())


def test_a_night_of_three_jobs_asks_each_provider_once(sites: dict[str, StandIn], tmp_home: Path):
    env = _night_env(tmp_home, sites, {"pexels": KEY, "pixabay": KEY})
    report, ran = _night(env)
    assert len(ran) == 3 and not report["failures"]
    assert (sites["pexels"].searches, sites["pixabay"].searches) == (1, 1)


def test_one_wrong_key_beside_a_good_one_runs_the_job_and_says_so(sites: dict[str, StandIn], tmp_home: Path):
    sites["pexels"].mode = "key_wrong"
    env = _night_env(tmp_home, sites, {"pexels": KEY, "pixabay": KEY})
    report, ran = _night(env)
    assert len(ran) == 3 and not report["failures"]
    notes = [n for n in _notes(env.paths) if "Pexels did not accept" in n]
    assert len(notes) == 1 and "Pixabay footage only" in notes[0]
    assert "Settings > Stock footage keys" in notes[0]


@pytest.mark.parametrize("mode", ["rate_limited", "failing", "no_connection", "unexpected"])
def test_a_rate_limit_or_an_outage_does_not_stop_the_job(
    sites: dict[str, StandIn], tmp_home: Path, mode: str
):
    sites["pixabay"].mode = mode
    env = _night_env(tmp_home, sites, {"pixabay": KEY}, per_day=1)
    report, ran = _night(env)
    assert len(ran) == 1 and not report["failures"], (mode, report["failures"])
    assert sites["pixabay"].searches == 1  # one request for the night, no retry


def test_the_check_is_kept_for_its_stated_time_and_asked_again_after(
    sites: dict[str, StandIn], tmp_home: Path
):
    from ns_studio import footage_keys as fk

    env = _night_env(tmp_home, sites, {"pixabay": KEY})
    fk.precheck(env.services, env.paths, env.log, now=1000.0)
    fk.precheck(env.services, env.paths, env.log, now=1000.0 + fk.KNOWN_TTL_S - 1)
    assert sites["pixabay"].searches == 1                         # just inside: kept
    fk.precheck(env.services, env.paths, env.log, now=1000.0 + fk.KNOWN_TTL_S)
    assert sites["pixabay"].searches == 2                         # at the end: asked again
    sites["pixabay"].mode = "failing"
    fk.precheck(env.services, env.paths, env.log, now=1_000_000.0)
    fk.precheck(env.services, env.paths, env.log, now=1_000_000.0 + fk.UNSURE_TTL_S - 1)
    fk.precheck(env.services, env.paths, env.log, now=1_000_000.0 + fk.UNSURE_TTL_S)
    assert sites["pixabay"].searches == 4                         # unsure: kept 30 minutes, then asked again
    env.services.clip_sources = [_source("pixabay", sites["pixabay"], env.paths.cache_search, KEY + "x")]
    fk.precheck(env.services, env.paths, env.log, now=1_000_000.0 + fk.UNSURE_TTL_S)
    assert sites["pixabay"].searches == 5                         # a new key is asked about at once


def test_a_torn_cache_file_is_asked_again(sites: dict[str, StandIn], tmp_home: Path):
    from ns_studio import footage_keys as fk

    env = _night_env(tmp_home, sites, {"pixabay": KEY})
    (env.paths.cache_search / fk.CHECK_FILE).write_text('{"pixabay": {"key_id"', encoding="utf-8")
    assert [a.kind for a in fk.precheck(env.services, env.paths, env.log, now=5.0)] == ["ok"]
    assert sites["pixabay"].searches == 1
    kept = json.loads((env.paths.cache_search / fk.CHECK_FILE).read_text(encoding="utf-8"))
    assert kept["pixabay"]["kind"] == "ok"


# ------------------------------------------------------------------------------- 9. ns doctor
class Down:
    base_url = "http://127.0.0.1:1"

    def is_up(self) -> bool:
        return False


def test_doctor_has_a_stock_key_row_per_provider(
    sites: dict[str, StandIn], tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_supervisor import doctor

    def rows(stored: dict[str, str]) -> dict[str, tuple[bool, str]]:
        monkeypatch.setattr(jobs, "load_keys", lambda: stored)
        got = doctor.diagnose(Paths.from_env(), Down())  # type: ignore[arg-type]
        return {name: (ok, detail) for name, ok, detail in got if name.startswith("stock key")}

    sites["pexels"].mode, sites["pixabay"].mode = "key_wrong", "ok"
    both = rows({"pexels": KEY, "pixabay": KEY})
    sites["pixabay"].mode = "failing"
    unsure = rows({"pixabay": KEY})
    none = rows({})
    assert both.get("stock key pixabay") == (True, "Pixabay accepted the key")
    assert both.get("stock key pexels", (None, ""))[0] is False
    assert "NS-E501" in both["stock key pexels"][1]
    assert "Settings > Stock footage keys" in both["stock key pexels"][1]
    assert unsure.get("stock key pixabay", (None, ""))[0] is True
    assert "could not be checked now (NS-E503)" in unsure["stock key pixabay"][1]
    assert unsure["stock key pexels"] == (True, "no key stored (optional: the other site's key is enough)")
    assert none["stock key pixabay"][1].startswith("no key stored (a video needs one")
    assert KEY not in json.dumps([both, unsure, none])


# ----------------------------------------------------------------------------- 10. the key is written nowhere
def test_the_key_is_written_nowhere(sites: dict[str, StandIn], tmp_home: Path):
    from ns_common.logging import NsLogger
    from ns_studio.media.clips import Searcher

    for mode in ("key_wrong", "rate_limited", "failing", "no_connection", "unexpected"):
        sites["pixabay"].mode = sites["pexels"].mode = mode
        for p in ("pexels", "pixabay"):
            setup_keys.check_key(p, KEY)
            src = _source(p, sites[p], Paths.from_env().cache_search)
            src.log = NsLogger("studio", Paths.from_env().logs, echo=False)
            Searcher([src], log=src.log).search(f"fog {mode}", "landscape")
            with contextlib.suppress(NsError):
                src.download(_candidate(p, sites[p]), Paths.from_env().cache_clips / p / f"{mode}.mp4")
    sites["pixabay"].mode = "key_wrong"
    env = _night_env(tmp_home, sites, {"pixabay": KEY, "pexels": KEY})
    _night(env)
    leaks = [str(f) for f in tmp_home.rglob("*") if f.is_file() and KEY.encode() in f.read_bytes()]
    assert leaks == []
    assert any(tmp_home.rglob("*.jsonl")), "the failing requests were logged somewhere to search"


# ------------------------------------------------------------------------------- 11. the optional credit
def test_the_sheet_offers_an_optional_credit_line():
    from ns_common.models import Metadata
    from ns_studio.media import outbox
    from ns_studio.media import youtube_rules as yr

    base = {"channel": "after-dark", "job_id": "after-dark-20261111-0e02", "kind": "short", "k": 1,
            "title": "T", "description": "D", "tags": [], "hashtags": [], "publish_after": "2026-11-12T18:00",
            "ai_disclosure": "no", "made_for_kids": False, "age_restrict_suggested": False, "combo_id": "x",
            "prompt_versions": {}}
    clip = {"id": "1", "url": "u", "license": "l"}
    used = [{**clip, "source": "pixabay"}, {**clip, "source": "pexels"},
            {**clip, "source": "pixabay", "id": "2"}]
    stock = Metadata.model_validate({**base, "clips": used})
    fake = Metadata.model_validate({**base, "clips": [{**clip, "source": "fake"}]})
    with_credit, without = outbox.sheet_text(stock, "s1.mp4"), outbox.sheet_text(fake, "s1.mp4")
    lines = [ln for ln in with_credit.splitlines() if "Stock footage from" in ln]
    assert len(lines) == 1 and lines[0].endswith(": Stock footage from Pixabay and Pexels.")
    assert lines[0].startswith("OPTIONAL CREDIT (not required; paste it at the end of the description")
    assert "OPTIONAL CREDIT" not in without
    # a sheet an earlier release wrote (no credit line) is not taken for one the owner edited
    earlier = "\n".join(ln for ln in with_credit.splitlines() if not ln.startswith("OPTIONAL CREDIT")) + "\n"
    assert yr.bare_sheet(earlier) == yr.bare_sheet(with_credit)
