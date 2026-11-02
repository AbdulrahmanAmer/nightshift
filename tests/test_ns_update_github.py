"""Gaps 9 and 16: `ns update --check` / `--github` and the supervisor's weekly update check."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_supervisor import cli, releases, update, update_check
from tests.test_supervisor_core import make, running, until

runner = CliRunner()
ZIP_BYTES = b"PK-not-really-a-zip-but-fine-for-hashing"
DIGEST = hashlib.sha256(ZIP_BYTES).hexdigest()
BASE = "https://github.com/AbdulrahmanAmer/nightshift/releases/download"
NOW = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)


def release_json(version: str = "0.2.0", *, digest: bool = True, sidecar: bool = False) -> dict[str, Any]:
    def asset(name: str, **extra: Any) -> dict[str, Any]:
        return {"name": name, "browser_download_url": f"{BASE}/v{version}/{name}", **extra}

    assets = [
        asset(f"Nightshift-{version}.zip"),
        asset(f"nightshift-app-{version}.zip", **({"digest": f"sha256:{DIGEST}"} if digest else {})),
    ]
    if sidecar:
        assets.append(asset(f"Nightshift-{version}.zip.sha256"))
    page = f"https://github.com/x/y/releases/tag/v{version}"
    return {"tag_name": f"v{version}", "html_url": page, "assets": assets}


def client_for(rel: dict[str, Any]) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.github.com":
            return httpx.Response(200, json=rel)
        if str(req.url).endswith(".sha256"):
            return httpx.Response(200, text=f"{DIGEST}  Nightshift-0.2.0.zip\n")
        return httpx.Response(200, content=ZIP_BYTES)

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def pin_latest(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    rel = releases.parse_release(release_json(version))
    monkeypatch.setattr(releases, "fetch_latest", lambda client=None: rel)


# ---------------------------------------------------------------- versions and the API


def test_version_ordering():
    assert releases.is_newer("0.1.1", "0.1.0") and releases.is_newer("v1.0.0", "0.9.9")
    assert not releases.is_newer("0.1.0", "0.1.0") and not releases.is_newer("0.1.0", "0.2.0")
    assert releases.is_newer("0.2.0", "0.2.0-rc1") and not releases.is_newer("0.2.0-rc1", "0.2.0")
    assert not releases.is_newer("banana", "0.1.0")  # unreadable never triggers an update


def test_check_reports_a_newer_release():
    res = releases.check("0.1.0", client_for(release_json("0.2.0")))
    assert res == {
        "update_available": True,
        "current": "0.1.0",
        "latest": "0.2.0",
        "notes": "https://github.com/x/y/releases/tag/v0.2.0",
    }
    assert releases.check("0.2.0", client_for(release_json("0.2.0")))["update_available"] is False


def test_github_unreachable_is_ns_e105():
    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    with pytest.raises(NsError) as ei:
        releases.fetch_latest(httpx.Client(transport=httpx.MockTransport(refuse)))
    assert ei.value.code == "NS-E105"


def test_release_without_a_version_tag_is_refused():
    with pytest.raises(NsError) as ei:
        releases.parse_release({"tag_name": "nightly", "assets": []})
    assert ei.value.code == "NS-E701"


def test_only_the_app_zip_and_github_hosts_are_picked():
    rel = releases.parse_release(release_json("0.2.0", sidecar=True))
    assert rel.zip_name == "nightshift-app-0.2.0.zip" and rel.digest == DIGEST and rel.sidecar_url
    evil = release_json("0.2.0")
    evil["assets"][1]["browser_download_url"] = "https://evil.example/nightshift-app-0.2.0.zip"
    assert releases.parse_release(evil).zip_url is None
    assert releases.trusted("https://objects.githubusercontent.com/x")
    assert not releases.trusted("http://github.com/x")
    assert not releases.trusted("https://github.com.evil.io/x")


# ---------------------------------------------------------------- download and verification


def test_download_verifies_the_api_digest_and_writes_the_sidecar(tmp_path: Path):
    rel = releases.parse_release(release_json("0.2.0"))
    z = releases.download(rel, tmp_path, client_for(release_json("0.2.0")))
    assert z.read_bytes() == ZIP_BYTES
    assert z.with_name(z.name + ".sha256").read_text(encoding="utf-8").split()[0] == DIGEST
    assert update.read_sidecar(z) == DIGEST  # exactly what update_app checks next


def test_download_falls_back_to_the_attached_sha256_file(tmp_path: Path):
    data = release_json("0.2.0", digest=False, sidecar=True)
    z = releases.download(releases.parse_release(data), tmp_path, client_for(data))
    assert z.exists()


def test_a_mismatch_deletes_the_file_and_is_ns_e701(tmp_path: Path):
    data = release_json("0.2.0")
    data["assets"][1]["digest"] = "sha256:" + "0" * 64
    with pytest.raises(NsError) as ei:
        releases.download(releases.parse_release(data), tmp_path, client_for(data))
    assert ei.value.code == "NS-E701" and "mismatch" in ei.value.detail
    assert list(tmp_path.iterdir()) == []


def test_no_digest_anywhere_means_no_install(tmp_path: Path):
    data = release_json("0.2.0", digest=False)
    with pytest.raises(NsError) as ei:
        releases.download(releases.parse_release(data), tmp_path, client_for(data))
    assert ei.value.code == "NS-E701" and not list(tmp_path.iterdir())


# ---------------------------------------------------------------- the ns commands


def invoke(home: Path, *args: str):  # type: ignore[no-untyped-def]
    return runner.invoke(cli.app, list(args), env={"NIGHTSHIFT_HOME": str(home)})


def test_ns_update_check_prints_a_sentence_and_a_json_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pin_latest(monkeypatch, "99.0.0")
    r = invoke(tmp_path / "home", "update", "--check")
    assert r.exit_code == 0 and "99.0.0 is available" in r.output
    last = json.loads(r.output.strip().splitlines()[-1])
    assert last["update_available"] is True and last["latest"] == "99.0.0"  # what ns_dashboard.updater parses


def test_ns_update_check_when_current(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pin_latest(monkeypatch, "0.0.1")
    r = invoke(tmp_path / "home", "update", "--check")
    assert r.exit_code == 0 and "Up to date" in r.output
    assert json.loads(r.output.strip().splitlines()[-1])["update_available"] is False


def test_ns_update_check_offline_exits_one_with_the_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def offline(client=None):  # type: ignore[no-untyped-def]
        raise NsError("NS-E105", "github.com: offline")

    monkeypatch.setattr(releases, "fetch_latest", offline)
    r = invoke(tmp_path / "home", "update", "--check")
    assert r.exit_code == 1 and "NS-E105" in r.output


def test_ns_update_github_downloads_then_applies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    got: dict[str, Any] = {}
    pin_latest(monkeypatch, "99.0.0")

    def fake_download(rel, dest, client=None):  # type: ignore[no-untyped-def]
        got["dest"] = dest
        return dest / rel.zip_name

    monkeypatch.setattr(releases, "download", fake_download)

    def fake_update(zip_path, app, env, *, paths):  # type: ignore[no-untyped-def]
        got["zip"] = zip_path
        return update.UpdateResult("0.1.0", "99.0.0", ["ok"])

    monkeypatch.setattr(update, "update_app", fake_update)
    r = invoke(home, "update", "--github", "--worker")
    assert r.exit_code == 0 and "Now on 99.0.0" in r.output
    assert got["zip"].name == "nightshift-app-99.0.0.zip"
    assert got["dest"] == Paths(home).data / "updates"


def test_ns_update_github_when_already_current_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pin_latest(monkeypatch, "0.0.1")
    monkeypatch.setattr(update, "update_app", lambda *a, **k: pytest.fail("must not update"))
    r = invoke(tmp_path / "home", "update", "--github", "--worker")
    assert r.exit_code == 0 and "Already on the newest" in r.output


def test_ns_update_takes_exactly_one_action(tmp_path: Path):
    assert invoke(tmp_path / "h", "update", "--check", "--github").exit_code == 2
    assert invoke(tmp_path / "h", "update", "--check", "--rollback").exit_code == 2
    assert invoke(tmp_path / "h", "update").exit_code == 2


# ---------------------------------------------------------------- the weekly check


def newer(current: str) -> dict[str, Any]:
    return {"update_available": True, "current": current, "latest": "0.2.0", "notes": ""}


def current_is_latest(current: str) -> dict[str, Any]:
    return {"update_available": False, "current": current, "latest": current, "notes": ""}


class Toaster:
    def __init__(self) -> None:
        self.shown: list[tuple[str, str]] = []

    def __call__(self, title: str, message: str) -> bool:
        self.shown.append((title, message))
        return True


def make_paths(tmp_path: Path) -> Paths:
    paths = Paths(tmp_path / "ns")
    paths.ensure()
    return paths


def test_weekly_check_toasts_and_waits_a_week(tmp_path: Path):
    paths, toaster = make_paths(tmp_path), Toaster()

    def run(when: datetime):  # type: ignore[no-untyped-def]
        return update_check.run_check(paths, now=when, current="0.1.0", checker=newer, toaster=toaster)

    first = run(NOW)
    assert first is not None and first["latest"] == "0.2.0"
    assert toaster.shown == [
        ("Nightshift", "Version 0.2.0 is available (you have 0.1.0). Open Settings > Update.")
    ]
    assert run(NOW + timedelta(days=6)) is None  # not due yet
    assert run(NOW + timedelta(days=7, minutes=1)) is not None
    assert len(toaster.shown) == 2


def test_weekly_check_is_quiet_when_current(tmp_path: Path):
    paths, toaster = make_paths(tmp_path), Toaster()
    res = update_check.run_check(
        paths, now=NOW, current="0.1.0", checker=current_is_latest, toaster=toaster
    )
    assert res is not None and toaster.shown == []


def test_weekly_check_honours_the_notifications_toggle(tmp_path: Path):
    paths, toaster = make_paths(tmp_path), Toaster()
    (paths.config / "dashboard.json").write_text(json.dumps({"notifications": False}), encoding="utf-8")
    res = update_check.run_check(paths, now=NOW, current="0.1.0", checker=newer, toaster=toaster)
    assert res is not None and toaster.shown == []  # recorded, but silent
    assert update_check.load_state(paths)["latest"] == "0.2.0"


def test_a_failed_check_is_retried_not_recorded(tmp_path: Path):
    paths = make_paths(tmp_path)

    def offline(cur: str) -> dict[str, Any]:
        raise NsError("NS-E105", "offline")

    assert update_check.run_check(paths, now=NOW, current="0.1.0", checker=offline) is None
    assert update_check.load_state(paths) == {} and update_check.due(paths, NOW)


def test_supervisor_polls_the_weekly_check_and_publishes_the_result(tmp_path: Path) -> None:
    calls: list[int] = []

    def job() -> dict[str, Any]:
        calls.append(1)
        return newer("0.1.0")

    sup, _, _ = make(tmp_path, update_poll_s=3600)
    sup.update_check = job

    async def main() -> None:
        q = sup.subscribe()
        async with running(sup):
            assert await until(lambda: len(calls) >= 1, 10)
            await asyncio.sleep(0.4)
            assert calls == [1]  # asked again only after update_poll_s
            kinds = []
            while not q.empty():
                kinds.append(q.get_nowait()["kind"])
            assert "update" in kinds

    asyncio.run(main())


def test_a_crashing_update_check_never_hurts_the_engine(tmp_path: Path) -> None:
    def job() -> dict[str, Any]:
        raise RuntimeError("boom")

    sup, _, _ = make(tmp_path)
    sup.update_check = job

    async def main() -> None:
        async with running(sup):
            await asyncio.sleep(0.5)
            assert not sup.exit_requested.is_set()
            assert (await sup.acquire("llama", "A"))["endpoint"]

    asyncio.run(main())


def test_new_update_package_name_wins_and_the_old_one_still_works():
    new = {"tag_name": "v0.3.0", "html_url": "x", "assets": [
        {"name": "Nightshift-Setup-0.3.0.zip", "browser_download_url": f"{BASE}/v0.3.0/Nightshift-Setup-0.3.0.zip"},
        {"name": "nightshift-app-0.3.0.zip", "browser_download_url": f"{BASE}/v0.3.0/nightshift-app-0.3.0.zip"},
        {"name": "Nightshift-Update-0.3.0.zip", "browser_download_url": f"{BASE}/v0.3.0/Nightshift-Update-0.3.0.zip",
         "digest": f"sha256:{DIGEST}"},
        {"name": "Nightshift-Update-0.3.0.zip.sha256", "browser_download_url": f"{BASE}/v0.3.0/Nightshift-Update-0.3.0.zip.sha256"},
    ]}
    rel = releases.parse_release(new)
    assert rel.zip_name == "Nightshift-Update-0.3.0.zip" and rel.digest == DIGEST
    assert rel.sidecar_url and rel.sidecar_url.endswith("Nightshift-Update-0.3.0.zip.sha256")
    old = releases.parse_release(release_json("0.1.1", sidecar=True))
    assert old.zip_name == "nightshift-app-0.1.1.zip" and old.sidecar_url
