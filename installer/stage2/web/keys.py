"""Live checks of the Pexels and Pixabay keys with ``urllib`` (docs/design/installer-ui.md 1.4).

One small search at the documented endpoint, read by the one classifier (`ns_common.stock_answer`): only an
answer that says the key is wrong refuses it (NS-E501 or NS-E502). A site that is busy (NS-E504), failing
or out of reach (NS-E503), or answers in a way Nightshift does not read (NS-E505) leaves the key unchecked:
setup and Settings keep it, say so, and the check before the first video asks again (card stock-truth).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import ns_common.stock_answer as sa
from installer.stage2.web.appcopy import read_version
from installer.stage2.web.preflight import copy_for

TIMEOUT_S = sa.TIMEOUT_S
# Pixabay and Pexels sit behind Cloudflare, which answers HTTP 403 (error 1010) to the default
# "Python-urllib/3.x" browser signature, so every key looked rejected. A product User-Agent passes.
# (The web package runs on the bare Python, so the version comes from the VERSION file, not ns_common.)
_VERSION = read_version(Path(__file__).resolve().parents[3]) or "0"
USER_AGENT = f"Nightshift-Setup/{_VERSION} (+https://github.com/AbdulrahmanAmer/nightshift)"
REJECTED = sa.KEY_CODES
#: Said after the sentence of a key that could not be checked now (setup keeps it, Settings saves it).
KEPT_UNCHECKED = "The key is kept; Nightshift checks it again before the next video."


def ask(service: str, key: str, fetch: sa.Fetch | None = None) -> sa.Answer:
    """The classified answer of one key check (setup, Settings and `ns doctor` all ask through here)."""
    return sa.check_key(service, key, fetch=fetch, user_agent=USER_AGENT)


def verdict(answer: sa.Answer) -> dict[str, Any]:
    """KeyCheck ``{ok, code, message}`` of an answer: ``ok`` False only for a key the site refused; a key that
    could not be checked now is ``ok`` with its code and the sentence that says why."""
    if answer.ok:
        return {"ok": True, "code": None, "message": None}
    if answer.kind == sa.KEY_WRONG:
        n = answer.name
        return {
            "ok": False, "code": answer.code,
            "message": f"{answer.sentence} Copy it again from your {n} account page (no spaces) and paste "
            "it.",
        }
    return {"ok": True, "code": answer.code, "message": f"{answer.sentence} {KEPT_UNCHECKED}"}


def check_key(service: str, key: str, fetch: sa.Fetch | None = None) -> dict[str, Any]:
    """KeyCheck for one key: ``{ok, code, message}``."""
    key = (key or "").strip()
    if service not in REJECTED:
        return {"ok": False, "code": None, "message": f"Unknown service {service}"}
    if not key:
        return {"ok": False, "code": REJECTED[service], "message": copy_for(REJECTED[service])[1]}
    return verdict(ask(service, key, fetch))
