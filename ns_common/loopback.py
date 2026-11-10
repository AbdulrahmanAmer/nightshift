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

The rules:

- `NO_BROWSER` (engine, voice service): `Host` must name 127.0.0.1, localhost or ::1, and the request must
  carry none of `Origin`, `Referer`, `Sec-Fetch-*`. No page is a caller of either server (the dashboard's page
  goes through the dashboard's own `/api/engine/...`), and none of our programs sends those headers
  (httpx, urllib, PowerShell 5.1 `Invoke-RestMethod`).
- `LOOPBACK_PAGES` (dashboard, whose own page is a browser): the same `Host` rule; an `Origin` must be a
  loopback one (any port: the Vite dev server); without an `Origin`, a request that comes from another site
  (the browser's `Sec-Fetch-Site: cross-site`, or another site's `Referer`) is answered only when it opens a
  page: a GET or HEAD outside `/api/` (a link to the dashboard must open). Nothing under `/api/` and nothing
  that is not a GET or HEAD is answered for another site, so no GET that does work (the update check, the
  status that syncs the schedule record) can be set off from a foreign page.

A refused request gets 403 with `NS-E207` and no route runs. The first refusal of a page is one WARN; a page
that keeps trying (3 refusals within 10 minutes) is told to the owner once per local day through `tell`
(the morning notes), off the request path. Standard library only.
"""

from __future__ import annotations

import json
import threading
import weakref
from collections import deque
from collections.abc import Callable, Iterable
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
#: sub-domains is many origins, and the notes file keeps its newest 20 for everything the owner must read).
PAGES_NAMED = 3
#: Pages counted one by one per local day; the rest of that day are counted together.
PAGES_TRACKED = 32
OTHER_PAGES = "(other pages)"
NAME_MAX = 80
#: Used when the error table cannot be read (the refusal must still be answered).
FALLBACK_MESSAGE = "Nightshift refused a request that did not come from a program of this PC."
SAFE_METHODS = frozenset({"GET", "HEAD"})


@dataclass(frozen=True)
class Rule:
    """Who may call a server. `pages`: a browser page on this PC is a caller (the dashboard)."""

    name: str
    pages: bool
    envelope: bool  # the dashboard's `{"error": {...}}` body instead of the engine's flat one
    data_prefix: str = ""  # paths that are never a page a link may open (the dashboard's `/api/`)


NO_BROWSER = Rule("no-browser", pages=False, envelope=False)
LOOPBACK_PAGES = Rule("loopback-pages", pages=True, envelope=True, data_prefix="/api/")


@dataclass(frozen=True)
class Refusal:
    """Why a request is refused (`why`: host, origin, fetch-mark or referer) and who sent it."""

    why: str
    host: str
    origin: str | None
    page: str     # what the owner is told: the page's origin when the browser gave one, else the name called
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
    """True when `Origin` is absent (non-browser caller) or a loopback origin; `null` is refused."""
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


def decide(
    headers: Iterable[tuple[bytes, bytes]], rule: Rule, method: str = "GET", path: str = "/"
) -> Refusal | None:
    """None when the request may reach the routes of a server under `rule`, otherwise the refusal.

    `headers` are the ASGI scope's (bytes). A header sent twice is judged on every value: whichever one a
    parser in front of us kept, a foreign one refuses.
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
    if origins:
        page = origin or "null"
    elif referers:
        page = _clean(_site_of(referers[-1]))
    else:
        page = host or "(no name)"

    def no(why: str) -> Refusal:
        return Refusal(why, host, origin, page, browser)

    if not hosts or not all(is_loopback_host(h) for h in hosts):
        return no("host")
    if not rule.pages:
        if origins is not None:
            return no("origin")
        if marks:
            return no("fetch-mark")
        if referers is not None:
            return no("referer")
        return None
    if origins:
        return None if all(is_loopback_origin(o) for o in origins) else no("origin")
    # no Origin: a plain GET, a form an old browser posted, a navigation. From another site only a page opens.
    page_path = not (rule.data_prefix and path.startswith(rule.data_prefix))
    may_open = method.upper() in SAFE_METHODS and page_path
    navigation = marks.get("sec-fetch-mode") == "navigate"
    if marks.get("sec-fetch-site") == "cross-site" and not (may_open and navigation):
        return no("fetch-mark")
    if referers and not all(is_loopback_origin(_site_of(r)) for r in referers) and not may_open:
        return no("referer")
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


MORE_PAGES = "Still more web pages or names tried to control Nightshift today"


def note_text(refusal: Refusal, count: int, at: datetime, *, more: bool = False) -> tuple[str, str]:
    """What the owner reads in the morning note, and the part of it that names the page (how a note that
    already waits is recognised). True for a page and for a program that used another name; `more`: the
    notes that name a page are used up for today, this is the one that says there were others."""
    end = "Every request was refused and nothing was changed."
    page, last = refusal.page, f"last at {at:%H:%M}"
    if more:
        text = (
            f"{CODE}: {MORE_PAGES} (the latest: {page}, {count} times, {last}). {end} If you do not know "
            "what sent them, close the web pages you have open."
        )
        return text, MORE_PAGES
    if refusal.browser:
        mark = f"(from {page}, "
        text = (
            f"{CODE}: A web page open in your browser tried {count} times to control Nightshift "
            f"{mark}{last}). {end} If you do not know that page, close it."
        )
        return text, mark
    mark = f"by the name {page} ("
    text = (
        f"{CODE}: Nightshift was called {count} times {mark}{last}). {end} Nightshift answers only to "
        "127.0.0.1 and localhost: a script of yours must use http://127.0.0.1 and the port. If you do not "
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


#: One note writer at a time in this process: the notes file is read, extended and written back, and two
#: writers at once would lose a note.
_NOTES = threading.Lock()
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

    `log`: an `NsLogger` (or None). `tell(message, at)`: leaves a morning note; `waiting()`: the notes that
    wait now, so a restarted server (or the other server of the same install) does not tell the owner twice
    about one page on one day. Both run on a thread of their own, never on the request path; with `tell`
    None the refusals are logged only.
    """

    def __init__(
        self,
        app: Any,
        *,
        rule: Rule,
        log: Any = None,
        tell: Callable[[str, datetime], None] | None = None,
        waiting: Callable[[], list[dict[str, Any]]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.app = app
        self.rule = rule
        self.log = log
        self.tell = tell
        self.waiting = waiting
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
        refusal = decide(scope.get("headers") or [], self.rule, method, path)
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
        start: tuple[str, str, str, bool, datetime] | None = None
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
                more = key == OTHER_PAGES or self._named >= PAGES_NAMED
                if more and self._more:
                    page.told = True   # today's notes are used up; the log keeps counting
                else:
                    page.writing, page.tried_at = True, now
                    if more:
                        self._more = True
                    else:
                        self._named += 1
                    start = (key, *note_text(refusal, page.count, now, more=more), more, now)
        for event, msg, data in lines:
            self._warn(event, msg, **data)
        if start is not None:
            thread = threading.Thread(target=self._write_note, args=start, name="ns-refusal", daemon=True)
            with self._lock:
                self._writers.append(thread)
            thread.start()

    def _write_note(self, key: str, message: str, mark: str, more: bool, at: datetime) -> None:
        """On its own thread: leave the note unless one about this page already waits from today."""
        told, problem = False, ""
        try:
            day = at.date().isoformat()
            with _NOTES:
                waits = self.waiting() if self.waiting is not None else []
                already = any(
                    str(n.get("message", "")).startswith(f"{CODE}:") and mark in str(n.get("message", ""))
                    and str(n.get("at", ""))[:10] == day
                    for n in waits
                )
                if not already and self.tell is not None:
                    self.tell(message, at)
            told = True
        except Exception as e:  # noqa: BLE001 - whatever the notes writer raises, the server goes on
            problem = f"{type(e).__name__}: {e}"
        with self._lock:
            page = self._pages.get(key) if self._day == at.date() else None
            if page is not None:
                page.writing = False
                page.told = told
                if not told and more:      # the note was not left: its place is free for the next try
                    self._more = False
                elif not told:
                    self._named -= 1
        if not told:
            self._warn("morning_note_failed", problem, code=CODE, page=key)
