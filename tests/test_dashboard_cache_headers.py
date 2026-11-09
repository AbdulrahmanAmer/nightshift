"""D-29: after an update the browser must run the new dashboard, not a cached copy of the old one.

`GET /` and `GET /app.js` used to carry Last-Modified and ETag but no Cache-Control, so a browser (or the
WebView2 host window) could reuse them without asking for a tenth of their age. These tests state the
headers over HTTP and show the effect: an unchanged file is answered 304, a changed one is fetched.
"""

import os
from pathlib import Path

import pytest

from tests.test_dashboard_api import make_env

pytest.importorskip("fastapi")


def directives(resp) -> set[str]:
    return {d.strip().lower() for d in resp.headers.get("cache-control", "").split(",") if d.strip()}


def revalidates(resp) -> bool:
    """True when the browser has to ask the server before it reuses the file."""
    d = directives(resp)
    return "no-store" in d or "no-cache" in d


@pytest.fixture
def env(tmp_home: Path):
    client, state = make_env(tmp_home)
    static = tmp_home / "static"
    (static / "assets").mkdir()
    (static / "assets" / "geist-latin-wght-normal-BgDaEnEv.woff2").write_bytes(b"font")
    (static / "brand").mkdir()
    (static / "brand" / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return client, static


def test_the_html_entry_is_never_reused_without_asking_the_server(env):
    client, _ = env
    for path in ("/", "/index.html"):
        r = client.get(path)
        assert r.status_code == 200 and "Nightshift" in r.text, path
        assert revalidates(r), f"{path}: {r.headers.get('cache-control')!r}"
        assert "max-age" not in r.headers.get("cache-control", "") or "max-age=0" in directives(r)
        assert "default-src 'self'" in r.headers["content-security-policy"]  # the other headers stay


def test_unhashed_files_revalidate_hashed_assets_are_immutable_and_the_api_stays_no_store(env):
    client, _ = env
    for path in ("/app.js", "/brand/favicon.svg"):
        r = client.get(path)
        assert r.status_code == 200 and revalidates(r), f"{path}: {r.headers.get('cache-control')!r}"
    font = client.get("/assets/geist-latin-wght-normal-BgDaEnEv.woff2")
    assert font.status_code == 200
    assert {"immutable", "max-age=31536000"} <= directives(font)
    api = client.get("/api/status")
    assert api.headers["cache-control"] == "no-store" and "content-security-policy" not in api.headers
    missing = client.get("/not-there.js")
    assert missing.status_code == 404 and "immutable" not in directives(missing)


def test_an_unchanged_file_is_answered_304_when_the_browser_asks_with_its_etag(env):
    client, _ = env
    for path in ("/", "/app.js"):
        first = client.get(path)
        etag = first.headers["etag"]
        again = client.get(path, headers={"If-None-Match": etag})
        assert again.status_code == 304 and again.content == b"", path
        assert revalidates(again), path  # the 304 refreshes the rule, it does not drop it


def test_a_changed_file_is_fetched_after_the_update_even_with_the_old_etag(env):
    client, static = env
    old = client.get("/app.js")
    assert old.text == "// ui"
    old_etag = old.headers["etag"]
    old_page_etag = client.get("/").headers["etag"]
    # The update replaces the app folder: new bytes, new modification time.
    (static / "app.js").write_text("// ui of the new version, a bit longer", encoding="utf-8")
    stamp = (static / "app.js").stat().st_mtime + 60
    os.utime(static / "app.js", (stamp, stamp))
    (static / "index.html").write_text("<!doctype html><title>Nightshift 2</title>", encoding="utf-8")
    new = client.get("/app.js", headers={"If-None-Match": old_etag})
    assert new.status_code == 200
    assert new.text == "// ui of the new version, a bit longer"
    assert new.headers["etag"] != old_etag
    page = client.get("/", headers={"If-None-Match": old_page_etag})
    assert page.status_code == 200 and "Nightshift 2" in page.text
