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


def bundle_urls(page: str) -> tuple[str, str]:
    js = page.split('src="')[1].split('"')[0]
    css = "./app.css" + page.split('href="./app.css')[1].split('"')[0]
    return js, css


def test_the_page_names_the_bundle_with_a_token_so_a_window_that_cached_the_old_bundle_fetches_the_new(env):
    """The first update to this fix: the browser holds app.js stored without a rule under `./app.js`."""
    client, static = env
    (static / "index.html").write_text(
        '<!doctype html><title>Nightshift</title><script type="module" src="./app.js"></script>'
        '<link rel="stylesheet" href="./app.css">',
        encoding="utf-8",
    )
    (static / "app.css").write_text("body{}", encoding="utf-8")
    first = client.get("/")
    js, css = bundle_urls(first.text)
    assert js.startswith("./app.js?v=") and css.startswith("./app.css?v="), first.text
    assert client.get(js[1:]).text == "// ui"  # the query string does not change what is served
    # An update changes the bundle: the page must point at URLs the old window has never cached.
    (static / "app.js").write_text("// ui of the new version, a bit longer", encoding="utf-8")
    second = client.get("/")
    assert bundle_urls(second.text)[0] != js and second.headers["etag"] != first.headers["etag"]
    # The window asks again with the validators of the old page: it gets the new page, not a 304.
    stale = client.get("/", headers={"If-None-Match": first.headers["etag"]})
    assert stale.status_code == 200 and bundle_urls(stale.text)[0] == bundle_urls(second.text)[0]
    # A validator the old server issued (Last-Modified of the file) never answers 304 either.
    old = client.get("/", headers={"If-Modified-Since": "Wed, 01 Jan 2031 00:00:00 GMT"})
    assert old.status_code == 200 and "?v=" in old.text
    current = client.get("/", headers={"If-None-Match": second.headers["etag"]})
    assert current.status_code == 304 and revalidates(current)


def test_a_file_that_only_looks_hashed_is_not_cached_for_a_year(env):
    client, static = env
    for name, body in (("foo-original.png", b"png"), ("notes-Abcdefg1.txt", b"txt")):
        (static / "assets" / name).write_bytes(body)
        r = client.get(f"/assets/{name}")
        assert r.status_code == 200 and "immutable" not in directives(r), name
    (static / "assets" / "app-Dk3Xz9aQ.js").write_text("1", encoding="utf-8")
    assert "immutable" in directives(client.get("/assets/app-Dk3Xz9aQ.js"))
