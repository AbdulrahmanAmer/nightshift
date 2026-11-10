"""H-15.9: the test-only release source (`NS_RELEASE_SOURCE`, `ns_supervisor/releases.py:release_source`).

A dev or engine-test install may take its update from a fake GitHub on the loopback address; every other
install refuses the variable with `NS-E704` and asks no server at all. No test here reaches the network: the
`Net` below answers every request in process (the fake GitHub on `ORIGIN`, a stand-in for the real GitHub that
offers ANOTHER version, so an answer from the wrong side shows), and the two tests of
`proofs/_fakes/github.py` talk to a server on 127.0.0.1 that they start and stop themselves.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pytest import MonkeyPatch as MP
from typer.testing import CliRunner

from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_supervisor import childenv, cli, releases, update, update_check

runner = CliRunner()

VAR = "NS_RELEASE_SOURCE"
ORIGIN = "http://127.0.0.1:8765"  # nothing listens there: `Net` answers in process
SOURCE = f"{ORIGIN}/repos/x/releases/latest"
GITHUB_LATEST = "https://api.github.com/repos/AbdulrahmanAmer/nightshift/releases/latest"
GITHUB_ASSETS = "https://github.com/AbdulrahmanAmer/nightshift/releases/download"
FAKE_V, GITHUB_V = "99.1.0", "99.9.9"
ZIP, GITHUB_ZIP = b"PK-the-candidate-the-fake-serves", b"PK-the-release-on-github"
NOW = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)

DEV: dict[str, Any] = {"dev_mode": True}
NORMAL: dict[str, Any] = {
    "dev_mode": False,
    "fake_footage": False,
    "tiny_story": False,
    "tts": {"engine": "qwentts", "device": "cuda"},
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def release_json(version: str, base: str, *, sidecar: bool = True, **extra: Any) -> dict[str, Any]:
    name = f"Nightshift-Update-{version}.zip"
    assets: list[dict[str, Any]] = [{"name": name, "browser_download_url": f"{base}/{name}"}]
    if sidecar:
        assets.append({"name": f"{name}.sha256", "browser_download_url": f"{base}/{name}.sha256"})
    return {"tag_name": f"v{version}", "html_url": f"{base}/v{version}.html", "assets": assets, **extra}


class Net:
    """Every request of a test, answered in process and remembered (`seen`, in order)."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.fake_json: dict[str, Any] = release_json(FAKE_V, f"{ORIGIN}/download")
        self.zip = ZIP
        self.sha_text = f"{sha(ZIP)}  Nightshift-Update-{FAKE_V}.zip\n"
        self.down = False  # the fake GitHub does not answer
        self.zip_answer: Callable[[], httpx.Response] | None = None  # e.g. a redirect
        self.sha_answer: Callable[[], httpx.Response] | None = None

    def handle(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        self.seen.append(url)
        if url.startswith(ORIGIN + "/"):
            if self.down:
                raise httpx.ConnectError("connection refused", request=req)
            if url == SOURCE:
                return httpx.Response(200, json=self.fake_json)
            if url.endswith(".sha256"):
                return self.sha_answer() if self.sha_answer else httpx.Response(200, text=self.sha_text)
            if url.endswith(".zip"):
                return self.zip_answer() if self.zip_answer else httpx.Response(200, content=self.zip)
            return httpx.Response(404, json={"message": "Not Found"})
        if req.url.host == "127.0.0.1":  # another port of this PC (the engine's, say): nothing listens there
            raise httpx.ConnectError("connection refused", request=req)
        if req.url.host == "api.github.com":
            return httpx.Response(200, json=release_json(GITHUB_V, f"{GITHUB_ASSETS}/v{GITHUB_V}"))
        if url.endswith(".sha256"):
            return httpx.Response(200, text=f"{sha(GITHUB_ZIP)}  Nightshift-Update-{GITHUB_V}.zip\n")
        return httpx.Response(200, content=GITHUB_ZIP)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle), follow_redirects=True)

    def everywhere(self, monkeypatch: MP) -> list[dict[str, Any]]:
        """Also answer the clients the product builds itself (the CLI, the weekly check, the git channel).

        Returns the keyword arguments each `httpx.Client` was made with."""
        made: list[dict[str, Any]] = []
        real_init = httpx.Client.__init__

        def init(client: httpx.Client, *args: Any, **kwargs: Any) -> None:
            made.append(kwargs)
            real_init(client, *args, **kwargs)

        monkeypatch.setattr(httpx.Client, "__init__", init)
        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda _t, request: self.handle(request))
        return made

    def off_origin(self) -> list[str]:
        return [u for u in self.seen if not u.startswith(ORIGIN + "/")]

    def left_this_pc(self) -> list[str]:
        return [u for u in self.seen if httpx.URL(u).host != "127.0.0.1"]

    def at_origin(self) -> list[str]:
        return [u for u in self.seen if u.startswith(ORIGIN + "/")]


def install(
    tmp_path: Path,
    monkeypatch: MP,
    runtime: Any = DEV,
    source: str | None = SOURCE,
    name: str = "home",
) -> Paths:
    """An install home with this `runtime.json` (a str is written as it is, None writes no file), the process
    aimed at it, and the variable set (or removed when `source` is None)."""
    paths = Paths(tmp_path / name)
    paths.ensure()
    if isinstance(runtime, str):
        paths.runtime_file.write_text(runtime, encoding="utf-8")
    elif runtime is not None:
        paths.runtime_file.write_text(json.dumps(runtime), encoding="utf-8")
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(paths.home))
    if source is None:
        monkeypatch.delenv(VAR, raising=False)
    else:
        monkeypatch.setenv(VAR, source)
    return paths


def refusal(call: Callable[[], Any]) -> str | None:
    """The code of the `NsError` the call raised; None when it went through."""
    try:
        call()
    except NsError as e:
        return e.code
    return None


def zips(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.rglob("*") if p.is_file())


class Toaster:
    def __init__(self) -> None:
        self.shown: list[tuple[str, str]] = []

    def __call__(self, title: str, text: str) -> bool:
        self.shown.append((title, text))
        return True


# ---------------------------------------------------------------- allowed: dev and engine-test installs


def test_the_test_source_is_used_on_a_dev_install(tmp_path: Path, monkeypatch: MP) -> None:
    paths = install(tmp_path, monkeypatch, DEV)
    net = Net()
    rel = releases.fetch_latest(net.client())
    assert net.seen == [SOURCE]  # that URL, not LATEST_URL
    assert rel.version == FAKE_V
    name = f"Nightshift-Update-{FAKE_V}.zip"
    assert rel.zip_url == f"{ORIGIN}/download/{name}"
    assert rel.sidecar_url == f"{ORIGIN}/download/{name}.sha256"
    z = releases.download(rel, tmp_path / "dest", net.client())
    assert z.read_bytes() == ZIP
    assert (tmp_path / "dest" / f"{name}.sha256").read_text(encoding="utf-8") == f"{sha(ZIP)}  {name}\n"
    # the digest and the zip came from the same host and port; nothing was asked of anyone else
    assert net.seen[1:] == [f"{ORIGIN}/download/{name}.sha256", f"{ORIGIN}/download/{name}"]
    assert net.off_origin() == []
    # the install's own log says where the answer came from
    logs = "".join(p.read_text(encoding="utf-8") for p in paths.logs.glob("update-*.jsonl"))
    assert "test_release_source" in logs and SOURCE in logs


def test_an_engine_test_install_counts_as_a_test_install(tmp_path: Path, monkeypatch: MP) -> None:
    allowed = {
        "dev": {"dev_mode": True},
        "tiny": {**NORMAL, "tiny_story": True},
        "footage": {**NORMAL, "fake_footage": True},
        "voice": {**NORMAL, "tts": {"engine": "fake", "device": "cpu"}},
    }
    for name, runtime in allowed.items():
        install(tmp_path, monkeypatch, runtime, name=name)
        net = Net()
        assert releases.fetch_latest(net.client()).version == FAKE_V, name
        assert net.seen == [SOURCE], name
    refused = {
        "normal": NORMAL,
        "empty": {},
        "words": {**NORMAL, "tiny_story": "true", "dev_mode": 1, "fake_footage": "yes"},  # only a JSON true
        "torn": '{"dev_mode": tr',  # an unreadable runtime.json is not a test install
        "list": "[true]",
        "missing": None,
    }
    for name, runtime in refused.items():
        install(tmp_path, monkeypatch, runtime, name=name)
        net = Net()
        assert refusal(lambda net=net: releases.fetch_latest(net.client())) == "NS-E704", name
        assert net.seen == [], name
    # Windows: the variable of the process and the one of the user arrive the same way (the environment); an
    # empty or blank one is no setting at all, and an explicit environment is judged like the process's own
    home = install(tmp_path, monkeypatch, NORMAL, source="   ", name="blank")
    assert releases.release_source().test is False
    env = {VAR: SOURCE, "NIGHTSHIFT_HOME": str(tmp_path / "dev")}
    assert releases.release_source(env).latest_url == SOURCE
    assert refusal(lambda: releases.release_source(env, home)) == "NS-E704"  # the install named wins


# ---------------------------------------------------------------- refused: a normal install, a wrong address


def test_a_normal_install_refuses_the_setting_with_ns_e704(tmp_path: Path, monkeypatch: MP) -> None:
    paths = install(tmp_path, monkeypatch, NORMAL)
    net = Net()
    made = net.everywhere(monkeypatch)
    by_hand = releases.Release(
        FAKE_V, f"v{FAKE_V}", "", "Nightshift-Update-9.zip", f"{ORIGIN}/download/a.zip", sha(ZIP), None
    )
    calls: dict[str, Callable[[], Any]] = {
        "fetch_latest": lambda: releases.fetch_latest(net.client()),
        "check": lambda: releases.check("0.1.0", net.client()),
        "download": lambda: releases.download(by_hand, tmp_path / "dest", net.client()),
        "parse_release": lambda: releases.parse_release(release_json(FAKE_V, f"{ORIGIN}/download")),
        "git channel": update._latest_release_tag,
    }
    codes = {name: refusal(call) for name, call in calls.items()}
    assert codes == dict.fromkeys(calls, "NS-E704")
    assert net.seen == []  # no request was sent anywhere: not to the fake, not to GitHub
    assert not (tmp_path / "dest").exists()
    # the CLI says so with the code and exits 1; the weekly check records nothing
    r = runner.invoke(cli.app, ["update", "--check"])
    assert r.exit_code == 1 and "NS-E704" in r.output and VAR in r.output
    toaster = Toaster()
    assert update_check.run_check(paths, now=NOW, current="0.1.0", toaster=toaster) is None
    assert "last_checked" not in update_check.load_state(paths)  # no check is recorded
    assert len(toaster.shown) == 1 and "NS-E704" in toaster.shown[0][1]  # ... and the owner is told
    log = "".join(p.read_text(encoding="utf-8") for p in paths.logs.glob("supervisor-*.jsonl"))
    assert "update_check_failed" in log and "NS-E704" in log
    assert net.seen == []
    # failed, then retried: the same refusal, still nothing sent; with the variable removed it is GitHub again
    assert refusal(calls["fetch_latest"]) == "NS-E704" and net.seen == []
    monkeypatch.delenv(VAR)
    assert releases.fetch_latest(net.client()).version == GITHUB_V and net.seen == [GITHUB_LATEST]
    assert all(k.get("trust_env", True) for k in made)  # GitHub clients keep the proxy settings of the PC


def test_the_source_must_be_loopback_and_its_assets_must_stay_there(
    tmp_path: Path, monkeypatch: MP
) -> None:
    bad_sources = (
        "http://example.com/repos/x/releases/latest",
        "http://127.0.0.1.example.com/latest",
        "http://127.0.0.1@example.com/latest",
        "http://user:pw@127.0.0.1:8765/latest",
        "ftp://127.0.0.1/latest",
        "127.0.0.1:8765/latest",
        "http://127.0.0.1:99999/latest",
        "http://127.0.0.1:0x50/latest",
        "http://192.168.1.20:8765/latest",
        "http:///latest",
    )
    for bad in bad_sources:
        install(tmp_path, monkeypatch, DEV, source=bad)
        net = Net()
        assert refusal(lambda net=net: releases.fetch_latest(net.client())) == "NS-E704", bad
        assert net.seen == [], bad
    for good in ("http://localhost:8765/latest", "https://[::1]:8765/latest", "HTTP://127.0.0.1/latest"):
        install(tmp_path, monkeypatch, DEV, source=good)
        assert releases.release_source().test is True, good

    install(tmp_path, monkeypatch, DEV)
    name = f"Nightshift-Update-{FAKE_V}.zip"
    dest = tmp_path / "dest"
    # an asset on another host, another port or another scheme is not this release's package
    for base in (f"{GITHUB_ASSETS}/v{FAKE_V}", "http://127.0.0.1:9999/download", "https://127.0.0.1:8765/download"):
        net = Net()
        net.fake_json = release_json(FAKE_V, base)
        rel = releases.fetch_latest(net.client())
        assert rel.zip_url is None and rel.sidecar_url is None, base
        assert refusal(lambda rel=rel, net=net: releases.download(rel, dest, net.client())) == "NS-E701", base
        assert net.seen == [SOURCE], base
    # one bad file among good ones: the zip is on the source, its checksum file is somewhere else
    net = Net()
    net.fake_json = release_json(FAKE_V, f"{ORIGIN}/download", sidecar=False)
    net.fake_json["assets"].append(
        {"name": f"{name}.sha256", "browser_download_url": f"http://example.com/{name}.sha256"}
    )
    rel = releases.fetch_latest(net.client())
    assert rel.zip_url is not None and rel.sidecar_url is None
    assert refusal(lambda: releases.download(rel, dest, net.client())) == "NS-E701"
    assert net.off_origin() == []
    # a redirect of the download to another host: refused, nothing kept
    net = Net()
    net.zip_answer = lambda: httpx.Response(302, headers={"Location": "http://example.com/evil.zip"})
    rel = releases.fetch_latest(net.client())
    with pytest.raises(NsError) as ei:
        releases.download(rel, dest, net.client())
    assert ei.value.code == "NS-E701" and "example.com" in ei.value.detail
    # ... of its checksum file (the zip is not asked for then)
    net = Net()
    net.sha_answer = lambda: httpx.Response(302, headers={"Location": "http://example.com/evil.sha256"})
    rel = releases.fetch_latest(net.client())
    assert refusal(lambda: releases.download(rel, dest, net.client())) == "NS-E701"
    assert not [u for u in net.seen if u.endswith(".zip")]
    # ... and a redirect of the releases answer itself
    net = Net()
    monkeypatch.setenv(VAR, f"{ORIGIN}/moved")
    real = net.handle

    def moved(req: httpx.Request) -> httpx.Response:
        if str(req.url) == f"{ORIGIN}/moved":
            net.seen.append(str(req.url))
            return httpx.Response(302, headers={"Location": GITHUB_LATEST})
        return real(req)

    client = httpx.Client(transport=httpx.MockTransport(moved), follow_redirects=True)
    assert refusal(lambda: releases.fetch_latest(client)) == "NS-E701"
    assert not dest.exists() or zips(dest) == []


def test_a_source_that_changes_between_check_and_download_is_refused(
    tmp_path: Path, monkeypatch: MP
) -> None:
    dest = tmp_path / "dest"
    paths = install(tmp_path, monkeypatch, DEV)
    net = Net()
    rel = releases.fetch_latest(net.client())
    assert net.seen == [SOURCE]

    def download() -> str | None:
        return refusal(lambda: releases.download(rel, dest, net.client()))

    # the variable is gone when the download starts: the loopback package is not GitHub's
    monkeypatch.delenv(VAR)
    assert download() == "NS-E701"
    # it names another port now
    monkeypatch.setenv(VAR, "http://127.0.0.1:9999/repos/x/releases/latest")
    assert download() == "NS-E701"
    # the install stopped being a test install
    monkeypatch.setenv(VAR, SOURCE)
    paths.runtime_file.write_text(json.dumps(NORMAL), encoding="utf-8")
    assert download() == "NS-E704"
    assert net.seen == [SOURCE] and not dest.exists()  # none of the three sent a request or made a folder
    # the other way round: a release read from GitHub, the variable set before the download
    install(tmp_path, monkeypatch, DEV, source=None)
    from_github = releases.fetch_latest(net.client())
    assert from_github.version == GITHUB_V
    monkeypatch.setenv(VAR, SOURCE)
    before = list(net.seen)
    assert refusal(lambda: releases.download(from_github, dest, net.client())) == "NS-E701"
    assert net.seen == before and not dest.exists()
    # the fake swaps the zip after it named its digest: the mismatch is caught, nothing is saved
    net = Net()
    net.fake_json = release_json(FAKE_V, f"{ORIGIN}/download", sidecar=False)
    net.fake_json["assets"][0]["digest"] = f"sha256:{sha(ZIP)}"
    rel = releases.fetch_latest(net.client())
    net.zip = b"PK-another-zip-under-the-same-name"
    assert download() == "NS-E701" and zips(dest) == []


def test_a_wrong_hash_from_the_test_source_saves_nothing(tmp_path: Path, monkeypatch: MP) -> None:
    install(tmp_path, monkeypatch, DEV)
    net = Net()
    net.sha_text = f"{'0' * 64}  Nightshift-Update-{FAKE_V}.zip\n"
    rel = releases.fetch_latest(net.client())
    dest = tmp_path / "dest"
    assert refusal(lambda: releases.download(rel, dest, net.client())) == "NS-E701"
    assert zips(dest) == [] and net.off_origin() == [] and len(net.seen) == 3
    # the retry with the right checksum goes through (nothing of the refused download is in its way)
    net.sha_text = f"{sha(ZIP)}  Nightshift-Update-{FAKE_V}.zip\n"
    assert releases.download(rel, dest, net.client()).read_bytes() == ZIP


def test_an_unreachable_test_source_is_an_error_not_github(tmp_path: Path, monkeypatch: MP) -> None:
    paths = install(tmp_path, monkeypatch, DEV)
    net = Net()
    net.everywhere(monkeypatch)
    net.down = True
    assert refusal(lambda: releases.fetch_latest(net.client())) == "NS-E105"
    net.seen.clear()
    with pytest.raises(NsError) as ei:
        releases.fetch_latest(net.client())
    assert ei.value.code == "NS-E105"
    assert "127.0.0.1:8765" in ei.value.detail and "github.com" not in ei.value.detail
    assert net.seen == [SOURCE]  # no second try at GitHub
    # the weekly check records nothing, so the next poll asks again; and again only the test source
    assert update_check.run_check(paths, now=NOW, current="0.1.0", toaster=Toaster()) is None
    assert not update_check.state_file(paths).exists()
    net.down = False
    again = update_check.run_check(paths, now=NOW + timedelta(minutes=10), current="0.1.0", toaster=Toaster())
    assert again is not None and again["latest"] == FAKE_V
    assert net.off_origin() == []
    # an answer with an error status names the test source too
    net.fake_json = {}
    monkeypatch.setenv(VAR, f"{ORIGIN}/nothing-here")
    with pytest.raises(NsError) as ei:
        releases.fetch_latest(net.client())
    assert ei.value.code == "NS-E118" and "127.0.0.1:8765" in ei.value.detail


# ---------------------------------------------------------------- a refusal that lasts is told to the owner


def test_a_refused_weekly_check_tells_the_owner_once_and_then_weekly(tmp_path: Path, monkeypatch: MP) -> None:
    paths = install(tmp_path, monkeypatch, NORMAL)
    net = Net()
    net.everywhere(monkeypatch)
    toaster = Toaster()

    def poll(after: timedelta) -> dict[str, Any] | None:
        return update_check.run_check(paths, now=NOW + after, current="0.1.0", toaster=toaster)

    # first hit: one toast with the code, its message and what to do
    assert poll(timedelta()) is None
    assert len(toaster.shown) == 1
    title, text = toaster.shown[0]
    d = NsError("NS-E704").definition
    assert title == "Nightshift" and "NS-E704" in text and d.message in text and d.hint in text
    assert update_check.load_state(paths)["refused"]["code"] == "NS-E704"
    # repeated hits (the check is asked every hour): nothing more within the week, then once again
    for hours in (1, 2, 24, 7 * 24 - 1):
        assert poll(timedelta(hours=hours)) is None
    assert len(toaster.shown) == 1
    assert poll(timedelta(days=7, minutes=1)) is None and len(toaster.shown) == 2
    assert poll(timedelta(days=7, hours=2)) is None and len(toaster.shown) == 2
    assert net.seen == []  # and no poll asked any server
    # the variable removed and Nightshift restarted: the check works and the refusal is forgotten
    monkeypatch.delenv(VAR)
    res = poll(timedelta(days=7, hours=3))
    assert res is not None and res["latest"] == GITHUB_V and "refused" not in update_check.load_state(paths)
    assert len(toaster.shown) == 3 and GITHUB_V in toaster.shown[2][1]  # the ordinary "is available" toast
    # ... so the variable coming back is told at once, not a week later
    monkeypatch.setenv(VAR, SOURCE)
    assert poll(timedelta(days=15)) is None and len(toaster.shown) == 4 and "NS-E704" in toaster.shown[3][1]
    # a telling dated in the future (the clock was set back) or unreadable does not silence it
    for told in ("2099-01-01T00:00:00+00:00", "yesterday"):
        state = json.dumps({"refused": {"code": "NS-E704", "told": told}})
        update_check.state_file(paths).write_text(state, encoding="utf-8")
        before = len(toaster.shown)
        assert poll(timedelta(days=16)) is None and len(toaster.shown) == before + 1, told


def test_a_refusal_reaches_the_owner_without_a_toast_and_is_retried_when_it_cannot(
    tmp_path: Path, monkeypatch: MP
) -> None:
    net = Net()
    net.everywhere(monkeypatch)
    words = getattr(update_check, "refusal_message", lambda _e: "(no such message before this change)")
    refused = words(NsError("NS-E704"))

    def notes(paths: Paths) -> list[str]:
        return [n["message"] for n in update_check.notify.read_notes(paths)]

    # notifications off (Settings): no toast, a note the dashboard shows; once
    paths = install(tmp_path, monkeypatch, NORMAL, name="quiet")
    (paths.config / "dashboard.json").write_text(json.dumps({"notifications": False}), encoding="utf-8")
    toaster = Toaster()
    for minutes in (0, 60, 120):
        assert update_check.run_check(paths, now=NOW + timedelta(minutes=minutes), toaster=toaster) is None
    assert toaster.shown == [] and notes(paths) == [refused]
    # no desktop or a toast that fails (the toaster says False): the note, once, and not twice with the
    # product's own toaster, which leaves the note itself
    paths = install(tmp_path, monkeypatch, NORMAL, name="session0")
    assert update_check.run_check(paths, now=NOW, toaster=lambda _t, _m: False) is None
    assert notes(paths) == [refused]
    paths = install(tmp_path, monkeypatch, NORMAL, name="own")
    monkeypatch.setattr(update_check.notify, "toast", lambda _t, _m: False)  # the real toaster, no toast
    job = update_check.make_job(paths)
    assert job() is None and job() is None
    assert notes(paths) == [refused]
    # the telling itself fails (no toast, the note cannot be written): nothing is remembered, the check
    # still answers, and the next poll tells
    paths = install(tmp_path, monkeypatch, NORMAL, name="broken")
    real_note = update_check.notify.leave_note

    def no_note(*a: Any, **k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(update_check.notify, "leave_note", no_note)
    assert update_check.run_check(paths, now=NOW, toaster=lambda _t, _m: False) is None
    assert "refused" not in update_check.load_state(paths) and notes(paths) == []
    log = "".join(p.read_text(encoding="utf-8") for p in paths.logs.glob("supervisor-*.jsonl"))
    assert "update_refusal_not_told" in log

    def boom(_t: str, _m: str) -> bool:
        raise RuntimeError("toast host crashed")

    assert update_check.run_check(paths, now=NOW + timedelta(hours=1), toaster=boom) is None
    monkeypatch.setattr(update_check.notify, "leave_note", real_note)
    toaster = Toaster()
    assert update_check.run_check(paths, now=NOW + timedelta(hours=2), toaster=toaster) is None
    assert len(toaster.shown) == 1 and update_check.load_state(paths)["refused"]["code"] == "NS-E704"
    # an unreachable source is not a lasting refusal: nobody is told (it clears by itself)
    paths = install(tmp_path, monkeypatch, DEV, name="offline")
    net.down = True
    toaster = Toaster()
    assert update_check.run_check(paths, now=NOW, toaster=toaster) is None
    assert toaster.shown == [] and notes(paths) == []


def test_the_ns_e704_hint_says_where_the_variable_can_be_and_to_restart() -> None:
    hint = NsError("NS-E704").definition.hint
    for words in ("NS_RELEASE_SOURCE", "user and system", "scheduled task", "restart Nightshift", "Stop All"):
        assert words in hint, words
    assert hint.index("restart Nightshift") < hint.index("check for updates again")


# ---------------------------------------------------------------- controls (pass before this change too)


def test_without_the_setting_nothing_changes(tmp_path: Path, monkeypatch: MP) -> None:
    assert releases.LATEST_URL == GITHUB_LATEST
    assert releases.TRUSTED_HOSTS == ("github.com", "githubusercontent.com")
    # a home as 0.1.14 left it (no test switch in runtime.json, a saved weekly check), and no variable
    old_runtime = {"profile": "full", "tts": {"engine": "qwentts"}}
    for name, runtime, source in (("dev", DEV, None), ("normal", old_runtime, None), ("blank", NORMAL, "  ")):
        paths = install(tmp_path, monkeypatch, runtime, source=source, name=name)
        update_check.state_file(paths).write_text(
            json.dumps({"last_checked": "2026-11-01T03:00:00+00:00", "latest": "0.1.14"}), encoding="utf-8"
        )
        net = Net()
        made = net.everywhere(monkeypatch)
        rel = releases.fetch_latest(net.client())
        assert net.seen == [GITHUB_LATEST] and rel.version == GITHUB_V, name
        z = releases.download(rel, tmp_path / name / "dest", net.client())
        assert z.read_bytes() == GITHUB_ZIP and net.off_origin() == net.seen, name
        assert releases.trusted(f"{GITHUB_ASSETS}/a.zip") and releases.trusted("https://objects.githubusercontent.com/a")
        assert not releases.trusted(f"{ORIGIN}/download/a.zip") and not releases.trusted("http://github.com/a.zip")
        # nothing to do: the saved check is not due; when it is, the default checker asks GitHub
        toaster = Toaster()
        assert update_check.run_check(paths, now=datetime(2026, 11, 2, tzinfo=UTC), toaster=toaster) is None
        res = update_check.run_check(paths, now=NOW, current="0.1.14", toaster=toaster)
        assert res is not None and res["latest"] == GITHUB_V and net.seen[-1] == GITHUB_LATEST, name
        assert all(k.get("trust_env", True) for k in made), name
        assert not list(paths.logs.glob("update-*.jsonl")), name  # no test-source note on GitHub
    # a release whose package is not on GitHub is still not picked
    net = Net()
    rel = releases.parse_release(release_json(FAKE_V, f"{ORIGIN}/download"))
    assert rel.zip_url is None
    assert refusal(lambda: releases.download(rel, tmp_path / "none", net.client())) == "NS-E701"
    assert net.seen == []


def test_a_prerelease_is_still_refused_from_the_test_source(tmp_path: Path, monkeypatch: MP) -> None:
    install(tmp_path, monkeypatch, DEV)
    for flag in ("prerelease", "draft"):
        candidate = release_json("99.1.0-rc1", f"{ORIGIN}/download", **{flag: True})
        seen: list[str] = []

        def answer(req: httpx.Request, body: Any = candidate, seen: list[str] = seen) -> httpx.Response:
            seen.append(str(req.url))
            return httpx.Response(200, json=body)  # whoever is asked says the same

        client = httpx.Client(transport=httpx.MockTransport(answer), follow_redirects=True)
        assert refusal(lambda client=client: releases.fetch_latest(client)) == "NS-E701", flag
        assert refusal(lambda client=client: releases.check("0.1.0", client)) == "NS-E701", flag
        assert len(seen) == 2, flag  # the answer was read and refused; no package was asked for


# ---------------------------------------------------------------- every route into the code


def test_ns_update_check_and_github_use_the_test_source(tmp_path: Path, monkeypatch: MP) -> None:
    paths = install(tmp_path, monkeypatch, DEV)
    net = Net()
    made = net.everywhere(monkeypatch)
    handed: list[Path] = []

    def recorder(zip_path: Path, app: Path, env: Any, *, paths: Paths) -> update.UpdateResult:
        handed.append(zip_path)
        assert zip_path.read_bytes() == ZIP  # the verified zip is on disk when the updater gets it
        return update.UpdateResult("0.1.0", FAKE_V, ["ok"])

    monkeypatch.setattr(update, "update_app", recorder)
    r = runner.invoke(cli.app, ["update", "--check"])
    assert r.exit_code == 0, r.output
    assert f"Version {FAKE_V} is available" in r.output  # the fake's version, not GitHub's
    assert json.loads(r.output.strip().splitlines()[-1])["latest"] == FAKE_V
    assert net.seen == [SOURCE]

    name = f"Nightshift-Update-{FAKE_V}.zip"
    for run in (1, 2):  # the second run downloads again over the zip the first one left
        r = runner.invoke(cli.app, ["update", "--github", "--worker"])
        assert r.exit_code == 0 and f"Now on {FAKE_V}" in r.output, r.output
        assert handed == [paths.data / "updates" / name] * run
        assert zips(paths.data / "updates") == [name, f"{name}.sha256"]
    # (the worker also looks for its own engine on this PC; nothing else was asked, and nothing left the PC)
    fetch = [SOURCE, f"{ORIGIN}/download/{name}.sha256", f"{ORIGIN}/download/{name}"]
    assert net.at_origin() == [SOURCE, *fetch, *fetch] and net.left_this_pc() == []
    # the release clients (the ones that follow redirects) put no proxy between themselves and the loopback
    release_clients = [k for k in made if k.get("follow_redirects")]
    assert len(release_clients) == 5 and all(k.get("trust_env") is False for k in release_clients)
    # the dashboard and the update worker start `ns update` as a child: the variable reaches it
    assert childenv.child_env()[VAR] == SOURCE


def test_the_weekly_check_uses_the_test_source(tmp_path: Path, monkeypatch: MP) -> None:
    paths = install(tmp_path, monkeypatch, DEV)
    net = Net()
    net.everywhere(monkeypatch)
    toaster = Toaster()
    res = update_check.run_check(paths, now=NOW, current="0.1.0", toaster=toaster)  # the default checker
    assert res is not None and res["latest"] == FAKE_V and res["update_available"] is True
    assert net.seen == [SOURCE]
    assert json.loads(update_check.state_file(paths).read_text(encoding="utf-8"))["latest"] == FAKE_V
    words = f"Version {FAKE_V} is available (you have 0.1.0). Open Settings > Update."
    assert toaster.shown == [("Nightshift", words)]
    # the job the supervisor polls is the same call
    update_check.state_file(paths).unlink()
    job = update_check.make_job(paths)
    monkeypatch.setattr(update_check.notify, "toast_or_note", toaster)
    polled = job()
    assert polled is not None and polled["latest"] == FAKE_V and net.off_origin() == []


def test_the_git_channel_reads_its_tag_from_the_test_source(tmp_path: Path, monkeypatch: MP) -> None:
    install(tmp_path, monkeypatch, DEV)
    net = Net()
    net.everywhere(monkeypatch)
    assert update._latest_release_tag() == f"v{FAKE_V}"
    assert net.seen == [SOURCE]


# ---------------------------------------------------------------- the fake GitHub of the proofs


def loopback_only(monkeypatch: MP) -> list[str]:
    """Let the real client talk to 127.0.0.1 only; a request to any other host fails the test, unsent."""
    elsewhere: list[str] = []
    real = httpx.HTTPTransport.handle_request

    def guarded(transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        if request.url.host != "127.0.0.1":
            elsewhere.append(str(request.url))
            raise httpx.ConnectError("a test may not leave the loopback address", request=request)
        return real(transport, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", guarded)
    return elsewhere


def test_the_fake_github_serves_a_folder_to_the_real_client(tmp_path: Path, monkeypatch: MP) -> None:
    try:
        from proofs._fakes import github
    except ImportError:  # before this change there is no fake: say so at an assertion
        github = None
    assert github is not None, "proofs/_fakes/github.py is missing"
    elsewhere = loopback_only(monkeypatch)
    folder = tmp_path / "served"
    folder.mkdir()
    name = f"Nightshift-Update-{FAKE_V}.zip"
    (folder / "Nightshift-Update-0.1.14.zip").write_bytes(b"PK-older")
    (folder / "Nightshift-Update-99.1.0-rc3.zip").write_bytes(b"PK-its-own-candidate")
    (folder / name).write_bytes(ZIP)
    srv = github.serve(folder)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        port = srv.server_address[1]
        source = f"http://127.0.0.1:{port}/repos/AbdulrahmanAmer/nightshift/releases/latest"
        install(tmp_path, monkeypatch, DEV, source=source)
        rel = releases.fetch_latest()  # the product's own client, over a real socket
        assert rel.version == FAKE_V and rel.digest == sha(ZIP)  # the newest zip of the folder, final over rc
        assert rel.zip_url == f"http://127.0.0.1:{port}/download/{name}"
        z = releases.download(rel, tmp_path / "dest")
        assert z.read_bytes() == ZIP
        # with a .sha256 beside the zip the fake publishes that file instead of a digest of its own
        (folder / f"{name}.sha256").write_text(f"{'0' * 64}  {name}\n", encoding="ascii")
        rel = releases.fetch_latest()
        assert rel.digest is None and rel.sidecar_url == f"http://127.0.0.1:{port}/download/{name}.sha256"
        assert refusal(lambda: releases.download(rel, tmp_path / "bad")) == "NS-E701"
        assert zips(tmp_path / "bad") == []
        # a file that is not a package of the folder is not served; an empty folder has no release
        for path in ("/download/..%2F..%2Fsecret.txt", "/download/runtime.json", "/elsewhere"):
            with pytest.raises(urllib.error.HTTPError) as he:
                urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10)
            assert he.value.code == 404, path
        for f in folder.iterdir():
            f.unlink()
        assert refusal(releases.fetch_latest) == "NS-E118"
        paths_seen = [p for _m, p in srv.seen]
        assert paths_seen[0] == "/repos/AbdulrahmanAmer/nightshift/releases/latest"
        assert f"/download/{name}" in paths_seen and f"/download/{name}.sha256" in paths_seen
        # on a normal install the same variable sends nothing to the fake
        install(tmp_path, monkeypatch, NORMAL, source=source, name="normal")
        before = len(srv.seen)
        assert refusal(releases.fetch_latest) == "NS-E704" and len(srv.seen) == before
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=10)
    assert elsewhere == []


def test_the_fake_github_starts_from_the_command_line_and_writes_ready_json(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "proofs" / "_fakes" / "github.py"
    assert script.is_file()
    folder = tmp_path / "served"
    folder.mkdir()
    (folder / f"Nightshift-Update-{FAKE_V}.zip").write_bytes(ZIP)
    proc = subprocess.Popen(
        [sys.executable, str(script), "--dir", str(folder), "--prerelease"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid: int | None = None
    try:
        ready = folder / "ready.json"
        deadline = time.monotonic() + 30
        while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        info = json.loads(ready.read_text(encoding="ascii"))
        pid = int(info["pid"])  # the server's own pid (a venv's python.exe starts the real one as its child)
        assert info["url"].startswith(f"http://127.0.0.1:{info['port']}/repos/")
        with urllib.request.urlopen(info["url"], timeout=10) as r:
            data = json.load(r)
        assert data["tag_name"] == f"v{FAKE_V}" and data["prerelease"] is True
        assert data["assets"][0]["digest"] == f"sha256:{sha(ZIP)}"
    finally:
        if pid is not None:  # the process this test started, by the pid it recorded
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
        proc.kill()
        proc.wait(timeout=10)
    lines = (folder / "requests.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["path"].endswith("/releases/latest")
