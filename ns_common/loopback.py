"""The one check that keeps web pages away from Nightshift's local servers (the engine, the voice service and
the dashboard all run it before any route).

Every server of ours listens on this PC only, but a browser on this PC can be made to call them: a page on any
site can post a form to `http://127.0.0.1:<port>`, and a page whose name was pointed at 127.0.0.1 (DNS
rebinding) can read the answers too. Two facts about browsers are what the check rests on, and a page can
change neither:

- `Host` is the name the page (or program) addressed. A rebinding page's requests carry its own name there.
- A browser says so when a page is behind a request: `Origin` on every cross-origin request that is not a
  plain GET, `Sec-Fetch-Site`/`Sec-Fetch-Mode`/`Sec-Fetch-Dest` on every request to 127.0.0.1 or localhost,
  and `Referer` (which a page can drop, so it only ever adds a refusal). A page's script cannot set or remove
  `Host`, `Origin` or `Sec-Fetch-*`.

The rule, one code path for all three servers (`decide`); a `Rule` only says whether the server has a page of
its own:

- `Host` must name 127.0.0.1, localhost or ::1 (any port, or none).
- A request that carries none of `Origin`, `Referer`, `Sec-Fetch-*` is a program's (httpx, urllib,
  PowerShell 5.1 `Invoke-RestMethod`): answered, unless it carries a body and that body is a form's (below).
- A request that carries any of them is a browser's, and it is answered only when it comes from the server's
  OWN origin: scheme, host and port exactly as this request addressed the server (`own_origin`: the scheme of
  the connection and the `Host` header, which the browser sets from the address it connects to and a page
  cannot change). `http://localhost:8100` and `http://127.0.0.1:8100` are two origins to a browser, and so are
  `http://127.0.0.1:8100` and `http://127.0.0.1:5173`: a page served by any other local server is a foreign
  page. Every `Origin` must equal it; `Sec-Fetch-Site` must be `same-origin`; every `Referer` must be on it.
  One exception: a top-level navigation (GET or HEAD, `Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest:
  document`). The owner's own (`Sec-Fetch-Site: none`: typed, a bookmark, a link another program opened; no
  page can make a browser say `none`) opens any path; a link on another page (`same-site`, `cross-site`) opens
  only a page outside `rule.data_prefix`, never an `/api/` route. It opens in the server's own origin: the
  linking page reads nothing of it (no `Access-Control-*` header is ever sent) and cannot frame it
  (`frame-ancestors 'none'`, and a frame is not `document`). A form a page posts is a navigation too: only a
  GET or HEAD ever opens this way.
- `NO_BROWSER` (engine, voice service) has no page: no origin is its own, so every browser request is
  refused. No page is a caller of either (the dashboard's page goes through its own `/api/engine/...`).
- `OWN_PAGE` (dashboard): its own page, the window host (WebView2) and the Vite dev server are browsers. The
  Vite dev server needs no switch: its proxy keeps the `Host` the page addressed (`changeOrigin` is off), so
  the page's `Origin` equals it; were the `Host` rewritten, the page would be refused (fails closed).

Two more rules cover the browsers that say nothing at all (Internet Explorer and Edge's IE mode, a Firefox
older than 90: no `Sec-Fetch-*`, no `Origin` on a form POST, and no `Referer` from an HTTPS page or when the
page asks for none). Such a request looks like a program's, so it is told apart by what a page cannot change:

- The old browser itself (`old-browser`): its `User-Agent`. A request that is not a GET or HEAD, carries no
  `Sec-Fetch-*`, names no page (no `Origin`, no `Referer`; one that is present was just found to be the
  server's own, which a page cannot forge) and whose `User-Agent` names `Trident/` (every Internet Explorer
  and IE-mode request) or `Firefox/` (a Firefox that sends no `Sec-Fetch-*` is older than 90) is refused,
  whatever its body: also with none, which is all it takes to reach `/stop_all`. `Mozilla` is NOT the mark:
  Windows PowerShell 5.1 and PowerShell 7 begin their User-Agent with `Mozilla/5.0`. No program of ours
  sends either token (`python-httpx/`, `Python-urllib/`, `... WindowsPowerShell/5.1`, `... PowerShell/7`,
  `curl/`; the window host is Chromium and sends `Sec-Fetch-*`), and a page's script cannot set its
  browser's `User-Agent` on such a request. A GET or HEAD is answered as before: our GET routes only read,
  and a page of another origin cannot read the answer (no `Access-Control-*` header is ever sent). The
  dashboard's own page in such a browser still works: its requests carry its own `Origin` or `Referer`.
- A form's body (`form`): all a page can make any browser send to another origin without a preflight is a
  plain HTML form, and a form has one of three content types. So a request that is not a GET or HEAD and
  whose body is a form's (`FORM_TYPES`, or a body with no content type at all) is refused unless it carries
  the server's own `Origin` (the dashboard's own page uploading a file; the engine and the voice service
  have no own origin, so never). Our programs send JSON, or no body.

D-50: a request with NO browser header and NO body (`Content-Length: 0` or none, no `Transfer-Encoding`; the
HTTP parser in front of us holds the request to that framing) is a program's whatever content type it
states: an empty body carries no form. Windows PowerShell 5.1's `Invoke-RestMethod -Method Post` without
`-Body` states `application/x-www-form-urlencoded` with `Content-Length: 0`. A body-less POST cannot be
refused by its body without refusing our own programs'; the browsers that could send one unseen are the
ones the `old-browser` rule names, and it refuses theirs. With any browser header the rule above is
unchanged.

A refused request gets 403 with `NS-E207` and no route runs. The first refusal of a page is one WARN; a page
that keeps trying (3 refusals within 10 minutes) is told to the owner once per local day through `tell`
(the morning notes), off the request path. The note is written once and never changed, so it counts "at
least" the refusals it had seen when it was written (`note_text`). Standard library only.
"""

from __future__ import annotations

import json
import threading
import weakref
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from ns_common.errors import describe

CODE = "NS-E207"
LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
#: The address every server of ours binds.
BIND_HOST = "127.0.0.1"
#: A page that is refused this often within `WINDOW` keeps trying: the owner is told.
NOTE_AFTER = 3
WINDOW = timedelta(minutes=10)
#: Notes that name a page, per local day; after them one last note says there were more (a site with many
#: sub-domains is many origins, and every one of them would be a note).
PAGES_NAMED = 3
#: Pages counted one by one per local day; the rest of that day are counted together.
PAGES_TRACKED = 32
OTHER_PAGES = "(other pages)"
NAME_MAX = 80
#: Used when the error table cannot be read (the refusal must still be answered).
FALLBACK_MESSAGE = "Nightshift refused a request that did not come from a program of this PC."
SAFE_METHODS = frozenset({"GET", "HEAD"})
#: The content types a plain HTML form can send to another origin without a preflight.
FORM_TYPES = frozenset({"application/x-www-form-urlencoded", "multipart/form-data", "text/plain"})
#: At most this many of our notes wait unread. The notes file's own bound is its owner's
#: (`ns_supervisor.notify`): a refusal note is routine there, so it never pushes out a note that is not.
NOTES_WAITING_MAX = 8
NULL_PAGE = "a page opened from a file or an embedded frame"
FORM_SENDER = "a web form without a sender"
#: D-50: a browser's request (Sec-Fetch-*) with neither Origin nor Referer names no page; the name it called
#: is ours, never the page's.
UNKNOWN_PAGE = "a web page whose address the browser did not give"
#: What a `User-Agent` names when the browser is one that sends no `Sec-Fetch-*` (case as browsers send it):
#: every Internet Explorer and IE-mode request, and Firefox (one that sends no `Sec-Fetch-*` is older than
#: version 90).
OLD_BROWSER_TOKENS = ("Trident/", "Firefox/")
#: Who an `old-browser` refusal is counted and told under: one name for the day, whatever page it was.
OLD_BROWSER = "an old web browser (Internet Explorer mode in Edge, or a Firefox older than version 90)"


@dataclass(frozen=True)
class Rule:
    """Who may call a server. `own_page`: the server serves a page of its own, and that page (its exact
    origin, `own_origin`) is a caller (the dashboard); without it no browser request is answered."""

    name: str
    own_page: bool
    envelope: bool  # the dashboard's `{"error": {...}}` body instead of the engine's flat one
    data_prefix: str = ""  # paths that are never a page a navigation may open (the dashboard's `/api/`)


NO_BROWSER = Rule("no-browser", own_page=False, envelope=False)
OWN_PAGE = Rule("own-page", own_page=True, envelope=True, data_prefix="/api/")
#: The web scheme of a connection's scope (a websocket's says ws or wss; its page's Origin, http or https).
WEB_SCHEMES = {"http": "http", "https": "https", "ws": "http", "wss": "https"}


@dataclass(frozen=True)
class Refusal:
    """Why a request is refused (`why`: host, origin, fetch-mark, referer, old-browser or form) and who sent
    it."""

    why: str
    host: str
    origin: str | None
    page: str     # what the owner is told: the page's origin (or Referer site) when the browser gave one;
    #               a browser's request without either: `UNKNOWN_PAGE` (D-50); a browser that says nothing at
    #               all: `OLD_BROWSER`; a program's: the name it called
    browser: bool  # a browser mark was on the request (a page), not only a foreign name


def wall_clock() -> datetime:
    """Local time now (the day of "once per day" is the owner's day)."""
    return datetime.now()


def _host_name(value: str) -> str | None:
    """The host of a `Host` value (or an Origin's authority) without its port, lower-cased; None when the
    value is not exactly `name`, `name:port`, `[v6]` or `[v6]:port`."""
    host = value.strip().lower()
    if not host or "@" in host or "/" in host:
        return None
    if host.startswith("["):
        end = host.find("]")
        rest = host[end + 1 :] if end != -1 else "x"
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return None
        return host[1:end]
    if host.count(":") == 1:
        name, port = host.split(":", 1)
        return name if port.isdigit() or port == "" else None
    return host


def is_loopback_host(host_header: str | None) -> bool:
    """True when a `Host` header names this machine (127.0.0.1, localhost, ::1), with or without port."""
    if not host_header:
        return False
    return _host_name(host_header) in LOOPBACK_NAMES


def is_loopback_origin(origin: str | None) -> bool:
    """True when `Origin` is absent or names this machine on ANY port; `null` is False.

    Not a rule of any server (D-45: a page served by another local server has a loopback origin too); `decide`
    compares a browser's origin with `own_origin`. Kept for `ns_dashboard.security`'s importers."""
    if origin is None or origin.strip() == "":
        return True
    if origin.strip() == "null":
        return False
    try:
        return is_loopback_host(urlsplit(origin.strip()).netloc)
    except ValueError:
        return False


def bind_host(configured: str | None) -> str:
    """The address a server of ours listens on: `configured` when it names this PC, otherwise 127.0.0.1.

    A settings file may name any address; no server of ours listens beyond this PC, because the check above
    stops browsers, not another computer (which can send any `Host` it likes).
    """
    name = _host_name(configured or "")
    return name if name in LOOPBACK_NAMES else BIND_HOST


def _clean(value: str) -> str:
    """A header value as it may be shown and logged: printable ASCII, at most `NAME_MAX` characters."""
    text = "".join(ch if " " < ch < "\x7f" else "?" for ch in value.strip())
    return text[:NAME_MAX] + ("..." if len(text) > NAME_MAX else "")


def _site_of(url: str) -> str:
    """`scheme://host[:port]` of a Referer."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else url


def own_origin(hosts: Sequence[str], scheme: str) -> str | None:
    """The origin this request addressed the server as: `<scheme>://<Host>`, lower-cased, exactly as a browser
    serialises a page's origin (`http://127.0.0.1:8100`, `http://localhost:8100`, `http://[::1]:8100`, no
    port when none was addressed). None when `Host` values disagree or the scheme is not a web one: then no
    browser request is the server's own."""
    names = {h.strip().lower() for h in hosts}
    web = WEB_SCHEMES.get(scheme.lower())
    if len(names) != 1 or web is None:
        return None
    return f"{web}://{names.pop()}"


def _opens_page(rule: Rule, method: str, path: str, marks: Mapping[str, str]) -> bool:
    """A top-level navigation the server answers whoever started it: a GET or HEAD the browser marks as the
    navigation of a document. The owner's own (`Sec-Fetch-Site: none`: typed, a bookmark, a link another
    program opened, such as the window host's "open in the browser") may open any path; a link on another
    page only a page outside `rule.data_prefix`. A browser that sends no marks: its GET of a page."""
    if not rule.own_page or method.upper() not in SAFE_METHODS:
        return False
    if marks:
        if marks.get("sec-fetch-mode") != "navigate" or marks.get("sec-fetch-dest", "document") != "document":
            return False
        if marks.get("sec-fetch-site") == "none":
            return True
    return not (rule.data_prefix and path.startswith(rule.data_prefix))


def _foreign_mark(
    origins: Sequence[str] | None, referers: Sequence[str] | None, marks: Mapping[str, str], own: str | None,
    opens_page: bool,
) -> str | None:
    """Which browser mark shows that a page other than the server's own sent this request (`origin`,
    `fetch-mark`, `referer`), or None. `own` None: the server has no page, every mark is foreign."""
    if origins is not None and (own is None or any(o.strip().lower() != own for o in origins)):
        return "origin"
    if marks and not opens_page and not (own is not None and marks.get("sec-fetch-site") == "same-origin"):
        return "fetch-mark"
    if referers is not None and not opens_page:
        if own is None or any(_site_of(r).strip().lower() != own for r in referers):
            return "referer"
    return None


def media_type(content_type: str) -> str:
    """`text/plain` of `Text/Plain; charset=utf-8`: the media type alone, lower-cased, without parameters."""
    return content_type.split(";", 1)[0].strip().lower()


def _has_body(seen: dict[str, list[str]]) -> bool:
    """The request announces a body: a `Content-Length` other than 0, or any `Transfer-Encoding`."""
    if "transfer-encoding" in seen:
        return True
    return any(length.strip() != "0" for length in seen.get("content-length", []))


def is_form_like(seen: dict[str, list[str]], method: str) -> bool:
    """A request that is not a GET or HEAD and whose body a plain HTML form can send: one of `FORM_TYPES`
    (media type only), or a body with no content type at all. (`decide` lets a program's form-typed request
    with NO body through: D-50.)"""
    if method.upper() in SAFE_METHODS:
        return False
    types = [media_type(v) for v in seen.get("content-type", [])]
    if any(kind in FORM_TYPES for kind in types):
        return True
    return not any(types) and _has_body(seen)


def names_old_browser(seen: Mapping[str, Sequence[str]]) -> bool:
    """A `User-Agent` of the request names a browser that sends no `Sec-Fetch-*` (`OLD_BROWSER_TOKENS`). Any
    of several values counts (whichever a parser in front of us kept); none, or a program's, is False."""
    return any(token in value for value in seen.get("user-agent", ()) for token in OLD_BROWSER_TOKENS)


def _page_of(
    origins: Sequence[str] | None, referers: Sequence[str] | None, marks: Mapping[str, str],
    hosts: Sequence[str], scheme: str,
) -> str:
    """Who sent the request, as the owner is told (`Refusal.page`): the Origin a browser stated, else the site
    of its Referer. A browser that stated neither (Sec-Fetch-* only: an image, a script, a no-cors fetch, a
    file page) gave no address: `UNKNOWN_PAGE`, never the name it called, which is ours (D-50). One exception
    names it: `Sec-Fetch-Site: same-origin` to a name that is not this PC's, where the page IS at that name (a
    rebinding page's own request). A program (no browser header): the name it called."""
    host = _clean(hosts[-1]) if hosts else ""
    if origins:
        return _clean(origins[-1]) or "null"
    if referers:
        return _clean(_site_of(referers[-1]))
    if marks:
        same = own_origin(hosts, scheme) if marks.get("sec-fetch-site") == "same-origin" else None
        if same is not None and hosts and not is_loopback_host(hosts[-1]):
            return _clean(same)
        return UNKNOWN_PAGE
    return host or "(no name)"


def decide(
    headers: Iterable[tuple[bytes, bytes]],
    rule: Rule,
    method: str = "GET",
    path: str = "/",
    scheme: str = "http",
) -> Refusal | None:
    """None when the request may reach the routes of a server under `rule`, otherwise the refusal.

    `headers` are the ASGI scope's (bytes), `scheme` the scope's (`http`, or `ws` for a websocket). A header
    sent twice is judged on every value: whichever one a parser in front of us kept, a foreign one refuses.
    """
    seen: dict[str, list[str]] = {}
    for raw_name, raw_value in headers:
        seen.setdefault(raw_name.decode("latin-1").lower(), []).append(raw_value.decode("latin-1"))
    hosts = seen.get("host", [])
    origins = seen.get("origin")
    referers = seen.get("referer")
    marks = {n: v[-1].strip().lower() for n, v in seen.items() if n.startswith("sec-fetch-")}
    host = _clean(hosts[-1]) if hosts else ""
    origin = _clean(origins[-1]) if origins else None
    browser = bool(origins or referers or marks)
    page = _page_of(origins, referers, marks, hosts, scheme)
    # D-50: a program's request with no body carries no form, whatever its content type says (Windows
    # PowerShell 5.1 posts `-Method Post` without `-Body` as an empty urlencoded form); a browser's is judged
    # by its content type as before
    form = is_form_like(seen, method) and (browser or _has_body(seen))

    def no(why: str) -> Refusal:
        sender = FORM_SENDER if why == "form" and not browser else page
        return Refusal(why, host, origin, sender, browser)

    if not hosts or not all(is_loopback_host(h) for h in hosts):
        return no("host")
    own = own_origin(hosts, scheme) if rule.own_page else None
    if browser:
        why = _foreign_mark(origins, referers, marks, own, _opens_page(rule, method, path, marks))
        if why is not None:
            return no(why)
    # a browser that says nothing at all (no Sec-Fetch-*) and named no page: nothing of it may change
    # anything, whatever its body. An Origin or Referer that IS here passed the check above (a request that
    # is not a GET or HEAD is never a navigation), so it is the server's own: the dashboard's page in such a
    # browser; the engine and the voice service have no page and refused it there.
    names_no_page = not marks and not origins and not referers
    if method.upper() not in SAFE_METHODS and names_no_page and names_old_browser(seen):
        return Refusal("old-browser", host, origin, OLD_BROWSER, True)
    # a form's body: only the server's own page may send one (it states its Origin: a file upload)
    if form and not origins:
        return no("form")
    return None


def refusal_body(refusal: Refusal, rule: Rule) -> bytes:
    """The JSON a refused request gets: the engine's error shape, or the dashboard's envelope."""
    try:
        definition = describe(CODE)
        message, hint = definition.message, definition.hint
    except (KeyError, OSError, ValueError):
        message, hint = FALLBACK_MESSAGE, None
    if rule.envelope:
        return json.dumps({"error": {"code": CODE, "message": message, "hint": hint}}).encode()
    return json.dumps({"code": CODE, "message": message, "detail": refusal.why}).encode()


#: The words of the day's last note (and how it is recognised; also in the notes of 0.1.15-rc4, which ended
#: it with "today").
MORE_PAGES = "Still more web pages or names tried to control Nightshift"
#: What a refusal note says of the requests it counts: Nightshift refused every one of them, so none ran.
#: True by construction of every request the note speaks of, whenever it is read: a request from that page
#: (its Origin, its Referer, or no address: `UNKNOWN_PAGE`), to that name, a form with a body and no sender,
#: or a request of an old browser that is not a GET or HEAD and names no page (`OLD_BROWSER`), is refused by
#: `decide` before any route, every time, counted or not (a page's top-level navigation that opens a page,
#: and an old browser's GET, are not attempts to control and are not counted).
REFUSED_WORDS = "Nightshift refused every one of those requests, so none of them ran."


def note_text(refusal: Refusal, count: int, at: datetime, *, more: bool = False) -> tuple[str, str]:
    """What the owner reads in the morning note, and the part of it that names the page (how a note that
    already waits is recognised). True for a page and for a program that used another name; `more`: the
    notes that name a page are used up for today, this is the one that says there were others.

    D-50: a note is written ONCE (at the refusal that makes the page one that keeps trying, or at a later one
    when it was held) and never changed, while the page may go on for hours, on both servers, after a
    restart. So `count` (the refusals this guard had counted for this page today when the note was written)
    is said as "at least", with the time it was counted by: both stay true whatever comes after. The day's
    last note names a page as one "among them" and gives no count (its count may be of several pages). It
    never says what other requests did, so no note may claim "every request" or "nothing was changed"."""
    end = REFUSED_WORDS
    page, by = refusal.page, f"by {at:%H:%M}"
    if more:
        text = (
            f"{CODE}: {MORE_PAGES} on {at:%Y-%m-%d} (among them {page}, {by}). {end} If you do not know "
            "what sent them, close the web pages you have open."
        )
        return text, MORE_PAGES
    if refusal.why == "old-browser":
        # one note a day for every such request, whatever page it was: the browser gave no address
        mark = f"{CODE}: An old web browser "
        text = (
            f"{mark}(Internet Explorer mode in Edge, or a Firefox older than version 90) tried at least "
            f"{count} times to control Nightshift ({by}). {end} Close the pages open in that browser; open "
            "Nightshift in Edge or Chrome."
        )
        return text, mark
    if refusal.why == "form" and not refusal.browser:
        mark = "sent Nightshift a web form "
        text = (
            f"{CODE}: Something on this PC {mark}at least {count} times ({by}). {end} No program of "
            "Nightshift does that; it is most likely a web page in an old browser or in Internet Explorer "
            "mode. Close the web pages you do not know. A script of yours must send JSON, or no body."
        )
        return text, mark
    if refusal.browser and page == UNKNOWN_PAGE:
        mark = f"{CODE}: A web page whose address the browser did not give tried "
        text = (
            f"{mark}at least {count} times to control Nightshift ({by}). {end} If you do not know which "
            "page that is, close the web pages you have open."
        )
        return text, mark
    if refusal.browser:
        mark = f"(from {NULL_PAGE if page == 'null' else page}, "
        text = (
            f"{CODE}: A web page open in your browser tried at least {count} times to control Nightshift "
            f"{mark}{by}). {end} If you do not know that page, close it."
        )
        return text, mark
    mark = f"by the name {page} ("
    text = (
        f"{CODE}: Nightshift was called at least {count} times {mark}{by}). {end} Nightshift answers only "
        "to 127.0.0.1 and localhost: a script of yours must use http://127.0.0.1 and the port. If you do not "
        "know what sent it, close the web pages you have open."
    )
    return text, mark


@dataclass
class _Page:
    """What one page (or foreign name) did today."""

    count: int = 0
    recent: deque[datetime] = field(default_factory=lambda: deque(maxlen=NOTE_AFTER))
    logged_at: datetime | None = None
    unlogged: int = 0
    owed: bool = False       # it kept trying: the owner must be told
    told: bool = False       # ... and was, today
    writing: bool = False    # a note writer is running
    tried_at: datetime | None = None


#: How a guard leaves a note: `tell(choose, at)` hands `choose` to the owner of the notes, which calls it
#: under the notes lock with the notes that wait and adds the message it answers (None: nothing); `tell`
#: returns the message when it was added, None when it was not (chosen none, or the notes are full of
#: notes that are not routine). The servers pass `ns_supervisor.notify.leave_chosen_note`.
Choose = Callable[[Sequence[Mapping[str, Any]]], str | None]
Tell = Callable[[Choose, datetime], str | None]
#: The live guards, so a test can wait for their note writers.
_GUARDS: weakref.WeakSet[LoopbackGuard] = weakref.WeakSet()


def settle(timeout_s: float = 10.0) -> bool:
    """Wait for every running note writer; False when one is still running after `timeout_s`."""
    done = True
    for guard in list(_GUARDS):
        for thread in guard.writers():
            thread.join(timeout_s)
            done = done and not thread.is_alive()
    return done


class LoopbackGuard:
    """Pure ASGI middleware: `decide`, then the app or a 403 (a websocket is closed with 1008).

    `log`: an `NsLogger` (or None). `tell(choose, at)` (`Tell`): leaves a morning note, counting what waits
    and writing in one step under the notes lock, so a restarted server (or the other server of the same
    install, at the same moment) does not tell the owner twice about one page on one day, nor pass the
    day's cap. It runs on a thread of its own, never on the request path; with `tell` None the refusals are
    logged only.
    """

    def __init__(
        self,
        app: Any,
        *,
        rule: Rule,
        log: Any = None,
        tell: Tell | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.app = app
        self.rule = rule
        self.log = log
        self.tell = tell
        self.clock = clock
        self._lock = threading.Lock()
        self._day: date | None = None
        self._pages: dict[str, _Page] = {}
        self._named = 0      # notes of today that name a page (written or being written)
        self._more = False   # today's one note that says there were more
        self._writers: list[threading.Thread] = []
        _GUARDS.add(self)

    # ------------------------------------------------------------------ the request
    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """Handle one ASGI connection."""
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        method, path = str(scope.get("method", "GET")), str(scope.get("path", "/"))
        scheme = str(scope.get("scheme") or ("ws" if scope["type"] == "websocket" else "http"))
        refusal = decide(scope.get("headers") or [], self.rule, method, path, scheme)
        if refusal is None:
            await self.answer(scope, receive, send)
            return
        try:
            self.record(refusal, method, path)
        except Exception as e:  # noqa: BLE001 - the books of a refusal must never turn a 403 into a 500
            self._warn("request_refused", f"refused; not counted: {e}", code=CODE, why=refusal.why)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = refusal_body(refusal, self.rule)
        head = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
        ]
        await send({"type": "http.response.start", "status": 403, "headers": head})
        await send({"type": "http.response.body", "body": body})

    async def answer(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """A request that passed: the app (a server adds its own response headers by overriding this)."""
        await self.app(scope, receive, send)

    # ------------------------------------------------------------------ the books
    def _now(self) -> datetime:
        return self.clock() if self.clock is not None else wall_clock()

    def _warn(self, event: str, msg: str, **data: Any) -> None:
        if self.log is None:
            return
        try:
            self.log.warn(event, msg, **data)
        except Exception:  # noqa: BLE001, S110 - a log that cannot be written must not change the answer
            pass

    def writers(self) -> list[threading.Thread]:
        """The note writers that may still run."""
        with self._lock:
            self._writers = [t for t in self._writers if t.is_alive()]
            return list(self._writers)

    def record(self, refusal: Refusal, method: str, path: str) -> None:
        """Count one refusal: the first of a page is logged, later ones in a count at most every `WINDOW`,
        and a page that keeps trying starts one note writer (at most one try per `WINDOW`)."""
        now = self._now()
        lines: list[tuple[str, str, dict[str, Any]]] = []
        start: tuple[str, Refusal, int, datetime] | None = None
        with self._lock:
            if self._day != now.date():   # a new day: yesterday's counts are logged and the books start again
                for name, old in self._pages.items():
                    if old.unlogged:
                        counts = {"code": CODE, "page": name, "count": old.unlogged, "total": old.count}
                        lines.append(("requests_refused", "refused again (until the day ended)", counts))
                self._day, self._pages, self._named, self._more = now.date(), {}, 0, False
            key = refusal.page
            if key not in self._pages and len(self._pages) >= PAGES_TRACKED:
                key = OTHER_PAGES
            page = self._pages.setdefault(key, _Page())
            page.count += 1
            page.recent.append(now)
            if page.logged_at is None:
                page.logged_at = now
                what = {"code": CODE, "why": refusal.why, "host": refusal.host, "origin": refusal.origin,
                        "method": method, "path": _clean(path)}
                lines.append(("request_refused", "refused: no program of this PC sent this request", what))
            else:
                page.unlogged += 1
                if not timedelta(0) <= now - page.logged_at < WINDOW:
                    counts = {"code": CODE, "page": key, "count": page.unlogged, "total": page.count}
                    lines.append(("requests_refused", "refused again", counts))
                    page.logged_at, page.unlogged = now, 0
            first = page.recent[0]
            if len(page.recent) == NOTE_AFTER and timedelta(0) <= now - first <= WINDOW:
                page.owed = True
            waited = page.tried_at is None or not timedelta(0) <= now - page.tried_at < WINDOW
            if self.tell is not None and page.owed and not page.told and not page.writing and waited:
                page.writing, page.tried_at = True, now
                start = (key, refusal, page.count, now)
        for event, msg, data in lines:
            self._warn(event, msg, **data)
        if start is not None:
            thread = threading.Thread(target=self._write_note, args=start, name="ns-refusal", daemon=True)
            with self._lock:
                self._writers.append(thread)
            thread.start()

    def _write_note(self, key: str, refusal: Refusal, count: int, at: datetime) -> None:
        """On its own thread: leave today's note about this page, within the day's bounds.

        The bounds are counted in the notes that wait (which every server of the install and every restart
        shares) by `choose`, which the owner of the notes calls under its lock and then writes what it chose:
        the count and the write are one step, so two servers refusing at the same moment cannot both pass
        it. Nothing when a note about this page already waits from today; at most `PAGES_NAMED` notes that
        name a page per day and then one that says there were more; nothing while `NOTES_WAITING_MAX` of ours
        wait unread (the note stays owed and is tried again at a later refusal). A refusal note is routine in
        the owner's rule: it never pushes out a note that is not, and when the notes are full of those it is
        not added and stays owed.
        """
        told, problem, held = False, "", ""
        chosen: list[bool] = []              # the note `choose` chose: is it today's "more" note
        day = at.date().isoformat()
        with self._lock:
            same_day = self._day == at.date()
            named_here, more_here = (self._named, self._more) if same_day else (0, False)

        def choose(waits: Sequence[Mapping[str, Any]]) -> str | None:
            nonlocal held
            ours = [n for n in waits if str(n.get("message", "")).startswith(f"{CODE}:")]
            today = [str(n.get("message", "")) for n in ours if str(n.get("at", ""))[:10] == day]
            named = max(named_here, sum(1 for m in today if MORE_PAGES not in m))
            more = more_here or any(MORE_PAGES in m for m in today)
            text, mark = note_text(refusal, count, at)
            as_more = key == OTHER_PAGES or named >= PAGES_NAMED
            if any(mark in m for m in today) or (as_more and more):
                return None   # the owner has today's note about this page, or today's notes are used up
            if len(ours) >= NOTES_WAITING_MAX:
                held = f"{len(ours)} notes about refused requests already wait unread"
                return None
            chosen.append(as_more)
            return note_text(refusal, count, at, more=True)[0] if as_more else text

        try:
            if self.tell is not None:
                written = self.tell(choose, at)
                if chosen and written is None:
                    held = "the notes are full of notes that are not routine"
                elif chosen:
                    with self._lock:
                        if self._day == at.date() and chosen[0]:
                            self._more = True
                        elif self._day == at.date():
                            self._named += 1
            told = not held
        except Exception as e:  # noqa: BLE001 - whatever the notes writer raises, the server goes on
            problem = f"{type(e).__name__}: {e}"
        with self._lock:
            page = self._pages.get(key) if self._day == at.date() else None
            if page is not None:
                page.writing = False
                page.told = told
        if held:
            self._warn("morning_note_held", f"not written now: {held}", code=CODE, page=key)
        elif not told:
            self._warn("morning_note_failed", problem, code=CODE, page=key)
