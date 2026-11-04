"""Live checks of the Pexels and Pixabay keys with ``urllib`` (docs/design/installer-ui.md 1.4).

The same one-result queries as ``s01_questions.validate_pexels`` and ``validate_pixabay``: HTTP 429 counts as
a working key, any other non-200 answer is a rejected key (NS-E501 or NS-E502), a network problem is NS-E503.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from installer.stage2.web.appcopy import read_version
from installer.stage2.web.preflight import copy_for

TIMEOUT_S = 15.0
# Pixabay and Pexels sit behind Cloudflare, which answers HTTP 403 (error 1010) to the default
# "Python-urllib/3.x" browser signature, so every key looked rejected. A product User-Agent passes.
# (The web package runs on the bare Python, so the version comes from the VERSION file, not ns_common.)
_VERSION = read_version(Path(__file__).resolve().parents[3]) or "0"
USER_AGENT = f"Nightshift-Setup/{_VERSION} (+https://github.com/AbdulrahmanAmer/nightshift)"
REJECTED = {"pexels": "NS-E501", "pixabay": "NS-E502"}
Fetch = Callable[[urllib.request.Request], int]


def _http_status(req: urllib.request.Request) -> int:
    """Status code of a request; HTTP errors return their code, network errors raise OSError."""
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except urllib.error.URLError as e:
        raise OSError(str(e.reason)) from None


def check_key(service: str, key: str, fetch: Fetch | None = None) -> dict[str, Any]:
    """KeyCheck for one key: ``{ok, code, message}``."""
    send = fetch or _http_status
    key = (key or "").strip()
    if service not in REJECTED:
        return {"ok": False, "code": None, "message": f"Unknown service {service}"}
    if not key:
        return {"ok": False, "code": REJECTED[service], "message": copy_for(REJECTED[service])[1]}
    if service == "pexels":
        query = urllib.parse.urlencode({"query": "nature", "per_page": 1})
        url = f"https://api.pexels.com/v1/search?{query}"
        req = urllib.request.Request(url, headers={"Authorization": key, "User-Agent": USER_AGENT})
    else:
        query = urllib.parse.urlencode({"key": key, "q": "nature", "per_page": 3})
        req = urllib.request.Request(f"https://pixabay.com/api/?{query}", headers={"User-Agent": USER_AGENT})
    try:
        status = send(req)
    except (OSError, ValueError):
        return {"ok": False, "code": "NS-E503", "message": copy_for("NS-E503")[1]}
    if status in (200, 429):
        return {"ok": True, "code": None, "message": None}
    return {"ok": False, "code": REJECTED[service], "message": copy_for(REJECTED[service])[1]}
