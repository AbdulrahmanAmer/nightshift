"""The one reading of what Pexels or Pixabay answered (card stock-truth).

Every caller that talks to a stock footage site passes the answer here: setup's key question (the window and
the wizard), Settings > Stock footage keys, the check before a job, `ns doctor`, the clip search and the clip
download.
Each answer is one of six kinds, with a sentence that is true of it, its way out and its error code:

| kind | what the site did | code |
|---|---|---|
| ok | answered with the documented JSON (or the file) | none |
| key wrong | Pexels 401; Pixabay 400 with "Invalid or missing API key" | NS-E501 / NS-E502 |
| rate limited | 429 (the wait from `Retry-After` or the reset header) | NS-E504 |
| provider failing | 5xx | NS-E503 |
| no connection | no answer: refused, reset, DNS, timeout | NS-E503 |
| not understood | any other status, or a 2xx without the documented list | NS-E505 |

Only "key wrong" ever names the key. The paths are the documented ones (read 2026-11-11):
https://www.pexels.com/api/documentation/ gives "GET https://api.pexels.com/v1/videos/search" and says the old
`https://api.pexels.com/videos/` endpoints "will be deprecated in the future"; https://pixabay.com/api/docs/
gives "GET https://pixabay.com/api/videos/". Standard library only: the setup window runs on the bare Python.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ns_common.errors import NsError

OK = "ok"
KEY_WRONG = "key_wrong"
RATE_LIMITED = "rate_limited"
FAILING = "provider_failing"
NO_CONNECTION = "no_connection"
UNEXPECTED = "unexpected"
KINDS = (OK, KEY_WRONG, RATE_LIMITED, FAILING, NO_CONNECTION, UNEXPECTED)
#: The kinds that say something about the key; the others say only that it could not be checked now.
CHECKED = (OK, KEY_WRONG)

PROVIDERS = ("pexels", "pixabay")
NAMES = {"pexels": "Pexels", "pixabay": "Pixabay"}
KEY_CODES = {"pexels": "NS-E501", "pixabay": "NS-E502"}
CODES = {FAILING: "NS-E503", NO_CONNECTION: "NS-E503", RATE_LIMITED: "NS-E504", UNEXPECTED: "NS-E505"}
#: Where the owner enters a key (the dashboard's Settings page, section title in `Settings.tsx`).
KEYS_PLACE = "Settings > Stock footage keys"
PEXELS_BASE = "https://api.pexels.com/v1"
PIXABAY_BASE = "https://pixabay.com/api/videos/"
#: The documented list a search answer carries; a 2xx without it is an answer not understood.
EXPECT = {"pexels": "videos", "pixabay": "hits"}
#: The limits the sites document (Pexels: "200 requests per hour"; Pixabay: "100 requests per 60 seconds").
LIMITS = {"pexels": "200 requests an hour", "pixabay": "100 requests a minute"}
#: Pixabay's own words for a wrong key, as its live answer of 2026-11-11 gave them:
#: "[ERROR 400] Invalid or missing API key (https://pixabay.com/api/docs/)."
PIXABAY_KEY_WORDS = "invalid or missing api key"
TIMEOUT_S = 15.0
DEFAULT_WAIT_S = 60.0
MAX_WAIT_S = 3600.0
MAX_BODY = 1 << 20
DEFAULT_USER_AGENT = "Nightshift (+https://github.com/AbdulrahmanAmer/nightshift)"

#: One request: `(status, headers, body)`; raises OSError when no answer came (refused, reset, DNS, timeout).
Fetch = Callable[[urllib.request.Request], tuple[int, Mapping[str, str], bytes]]


def search_url(provider: str, base: str | None = None) -> str:
    """The documented video search endpoint of `provider` (`base` replaces the site: a stand-in server)."""
    if provider == "pexels":
        return (base or PEXELS_BASE).rstrip("/") + "/videos/search"
    return base or PIXABAY_BASE


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    for k, v in (headers or {}).items():
        if k.lower() == name.lower():
            return str(v)
    return None


def wait_s(headers: Mapping[str, str] | None, wall_now: float) -> float:
    """Seconds to wait after a 429: `Retry-After`, else the reset header (an epoch or seconds); 1..3600."""
    ra, reset = _header(headers, "Retry-After"), _header(headers, "X-RateLimit-Reset")
    wait = DEFAULT_WAIT_S
    try:
        if ra is not None:
            wait = float(ra)
        elif reset is not None:
            v = float(reset)
            wait = v - wall_now if v > 1e9 else v
    except ValueError:
        wait = DEFAULT_WAIT_S
    return min(MAX_WAIT_S, max(1.0, wait))


def _seconds(s: float) -> str:
    n = int(round(s))
    if n >= 120:
        return f"{n // 60} minutes"
    return "1 second" if n == 1 else f"{n} seconds"


@dataclass(frozen=True)
class Answer:
    """One classified answer of a stock site. `data` is the parsed JSON of an ok search (never shown)."""

    provider: str
    kind: str
    why: str = ""
    status: int | None = None
    wait: float | None = None
    data: Any = field(default=None, repr=False, compare=False)

    @property
    def name(self) -> str:
        """The site's name as the owner knows it."""
        return NAMES.get(self.provider, self.provider)

    @property
    def ok(self) -> bool:
        """True when the site answered as documented."""
        return self.kind == OK

    @property
    def checked(self) -> bool:
        """True when the answer says something about the key (ok or wrong); False: not checkable now."""
        return self.kind in CHECKED

    @property
    def code(self) -> str | None:
        """The error code of this kind (None for ok)."""
        if self.kind == OK:
            return None
        if self.kind == KEY_WRONG:
            return KEY_CODES.get(self.provider, "NS-E502")
        return CODES[self.kind]

    @property
    def sentence(self) -> str:
        """What happened, true of this kind; the key is named only when the site refused it."""
        n, why = self.name, f" ({self.why})" if self.why else ""
        return {
            OK: f"{n} accepted the key.",
            KEY_WRONG: f"{n} did not accept the key{why}: it is wrong or was revoked.",
            RATE_LIMITED: f"{n} says its request limit is reached{why}; it allows "
            f"{LIMITS.get(self.provider, 'a set number of requests')}. The key is fine.",
            FAILING: f"{n} is not answering right now{why}. The key is fine.",
            NO_CONNECTION: f"Nightshift could not reach {n}{why}: this PC has no connection, or no answer "
            "came in time.",
            UNEXPECTED: f"{n} answered in a way Nightshift does not understand{why}. That does not mean the "
            "key is wrong.",
        }[self.kind]

    @property
    def way_out(self) -> str:
        """What the owner does about it (for a job; setup and Settings say their own for a wrong key)."""
        n = self.name
        if self.kind == KEY_WRONG:
            return f"Copy the key again from your {n} account page and paste it in {KEYS_PLACE} (no spaces)."
        if self.kind == RATE_LIMITED:
            wait = f" {_seconds(self.wait)}" if self.wait else ""
            return f"Nightshift waits{wait} and tries again; nothing to change."
        if self.kind == FAILING:
            return "Nightshift will try again; nothing to change on this PC."
        if self.kind == NO_CONNECTION:
            return "Check this PC's internet connection; Nightshift will try again."
        if self.kind == UNEXPECTED:
            return "Try again later; if it keeps happening, update Nightshift."
        return ""

    @property
    def way_out_given_up(self) -> str:
        """The way out once the asking has stopped (the retries spent): nothing will try again by itself."""
        if self.kind == RATE_LIMITED:
            return ("The limit resets by itself (Pexels counts per hour, Pixabay per minute); try again "
                    "then. Nothing to change.")
        if self.kind == FAILING:
            return "Try again later; nothing to change on this PC."
        if self.kind == NO_CONNECTION:
            return "Check this PC's internet connection, then try again."
        return self.way_out

    def error(self, after: str = "", *, gave_up: bool = False) -> NsError:
        """The coded error of this answer: the sentence, `after` (how often it was tried) and the way out
        (`gave_up`: the caller stops asking, so the way out does not promise another try)."""
        said = f"{self.sentence} {after}".rstrip()
        way = self.way_out_given_up if gave_up else self.way_out
        return NsError(self.code or "NS-E505", f"{said} {way}".strip())


def classify(
    provider: str,
    *,
    status: int | None = None,
    headers: Mapping[str, str] | None = None,
    body: bytes | str = b"",
    expect: str | None = None,
    key_sent: bool = True,
    no_answer: str | None = None,
    unreadable: str | None = None,
    wall_now: float = 0.0,
) -> Answer:
    """The kind of one answer. `no_answer` names why none came (a timeout, a refused connection); `unreadable`
    names an answer that could not be read at all. `expect` is the list a 2xx must carry (None for a file
    download). `key_sent` False (a CDN file link carries no key): no status can be read as a wrong key."""
    if no_answer is not None:
        return Answer(provider, NO_CONNECTION, no_answer)
    if unreadable is not None or status is None:
        return Answer(provider, UNEXPECTED, unreadable or "no status")
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    if status == 429:
        return Answer(provider, RATE_LIMITED, "HTTP 429", status, wait_s(headers, wall_now))
    if status >= 500:
        return Answer(provider, FAILING, f"HTTP {status}", status)
    if 200 <= status < 300:
        if expect is None:
            return Answer(provider, OK, status=status)
        try:
            data = json.loads(text)
        except ValueError:
            return Answer(provider, UNEXPECTED, f"HTTP {status}, not JSON", status)
        if not isinstance(data, dict) or not isinstance(data.get(expect), list):
            return Answer(provider, UNEXPECTED, f"HTTP {status} without its '{expect}' list", status)
        return Answer(provider, OK, status=status, data=data)
    if key_sent and provider == "pexels" and status == 401:
        return Answer(provider, KEY_WRONG, "HTTP 401", status)
    if key_sent and provider == "pixabay" and status == 400 and PIXABAY_KEY_WORDS in text.lower():
        return Answer(provider, KEY_WRONG, "HTTP 400, invalid or missing API key", status)
    return Answer(provider, UNEXPECTED, f"HTTP {status}", status)


def key_check_request(
    provider: str, key: str, *, base: str | None = None, user_agent: str | None = None
) -> urllib.request.Request:
    """The one small search that tells whether `key` works: one result, at the documented endpoint. Pexels
    takes the key as `Authorization`; Pixabay only in the query string (so no URL of it is ever written)."""
    headers = {"User-Agent": user_agent or DEFAULT_USER_AGENT}
    if provider == "pexels":
        query = urllib.parse.urlencode({"query": "nature", "per_page": 1})
        headers["Authorization"] = key
    else:
        query = urllib.parse.urlencode({"key": key, "q": "nature", "per_page": 3})
    return urllib.request.Request(f"{search_url(provider, base)}?{query}", headers=headers)


def urllib_fetch(
    req: urllib.request.Request, timeout: float = TIMEOUT_S
) -> tuple[int, Mapping[str, str], bytes]:
    """One request with `urllib`: an HTTP error status is an answer; no answer raises OSError (its type only:
    a message can carry the address, and the address of a Pixabay request carries the key)."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), dict(resp.headers.items()), resp.read(MAX_BODY)
    except urllib.error.HTTPError as e:
        body = e.read(MAX_BODY) if e.fp is not None else b""
        return int(e.code), dict(e.headers.items()) if e.headers is not None else {}, body
    except urllib.error.URLError as e:
        raise OSError(type(e.reason).__name__) from None
    except http.client.HTTPException as e:
        raise OSError(type(e).__name__) from None


def check_key(
    provider: str,
    key: str,
    *,
    fetch: Fetch | None = None,
    base: str | None = None,
    user_agent: str | None = None,
    timeout: float = TIMEOUT_S,
) -> Answer:
    """Ask `provider` once whether `key` works (one bounded request, no retry) and classify the answer."""
    req = key_check_request(provider, key.strip(), base=base, user_agent=user_agent)
    try:
        status, headers, body = fetch(req) if fetch is not None else urllib_fetch(req, timeout)
    except (OSError, ValueError) as e:
        return classify(provider, no_answer=str(e) if type(e) is OSError and str(e) else type(e).__name__)
    return classify(
        provider, status=status, headers=headers, body=body, expect=EXPECT.get(provider), wall_now=time.time()
    )
