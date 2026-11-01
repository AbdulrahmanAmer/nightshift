import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "ns_dashboard" / "static"
INDEX = STATIC / "index.html"

pytestmark = pytest.mark.skipif(not INDEX.exists(), reason="ns_dashboard/static is delivered by feat/c9-ui")

ATTR = re.compile(r"""\b(?:src|href)\s*=\s*["']([^"']*)["']""", re.IGNORECASE)


def test_index_references_only_local_assets():
    html = INDEX.read_text(encoding="utf-8")
    refs = ATTR.findall(html)
    assert refs, "index.html should load app.css and app.js"
    external = [r for r in refs if re.match(r"(?i)^(https?:)?//", r.strip())]
    assert not external, f"external src/href found: {external}"
    assert not re.search(r"""(?i)\b(?:src|href)\s*=\s*["']\s*https?://""", html)


def test_local_assets_exist():
    html = INDEX.read_text(encoding="utf-8")
    for ref in ATTR.findall(html):
        ref = ref.strip()
        if not ref or ref.startswith(("#", "data:", "mailto:", "javascript:")):
            continue
        target = STATIC / ref.split("?", 1)[0].split("#", 1)[0].lstrip("/")
        assert target.exists(), f"index.html references a missing file: {ref}"


def test_page_is_compatible_with_the_content_security_policy():
    """The server sends `script-src 'self'`: no inline script bodies and no inline event handlers."""
    html = INDEX.read_text(encoding="utf-8")
    inline = [m for m in re.finditer(r"(?is)<script\b([^>]*)>(.*?)</script>", html) if m.group(2).strip()]
    assert not inline, "inline <script> bodies are blocked by the CSP; move them to app.js"
    handlers = re.findall(r"""(?i)\son[a-z]+\s*=\s*["']""", html)
    assert not handlers, f"inline event handlers are blocked by the CSP: {handlers[:3]}"


CSS_URL = re.compile(r"""(?i)url\(\s*["']?(?:https?:)?//""")
CSS_IMPORT = re.compile(r"""(?i)@import\s+(?:url\()?\s*["']?(?:https?:)?//""")
JS_CALL = re.compile(r"""(?:fetch|EventSource|XMLHttpRequest|importScripts)\([^)]*["'`]https?://""")
JS_SRC = re.compile(r"""\.src\s*=\s*["'`]https?://""")


def test_js_and_css_load_nothing_from_the_network():
    css = STATIC / "app.css"
    if css.exists():
        text = css.read_text(encoding="utf-8")
        assert not CSS_URL.search(text) and not CSS_IMPORT.search(text), "app.css loads external resources"
    js = STATIC / "app.js"
    if js.exists():
        text = js.read_text(encoding="utf-8")
        assert not JS_CALL.search(text) and not JS_SRC.search(text), "app.js talks to the network"
