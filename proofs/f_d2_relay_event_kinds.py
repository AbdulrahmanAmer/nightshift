"""F-D2: the dashboard relay keeps the supervisor's `update` and `run` event kinds, and the page shows them.

With the Queue page open and a second EventSource on the dashboard's own stream (/api/events, what the page
reads), the supervisor is sent the two events the page's toasts depend on:

- `run`: POST /run_finished on the supervisor, the call the studio makes at the end of a nightly run. The
  stream must carry a frame of kind `run` (not `service`) and the page must show "The nightly run has
  finished. See the Queue".
- `update`: POST /events {kind: update, data: {...the shape the weekly check publishes...}} on the supervisor.
  The stream must carry a frame of kind `update` and the page must show "Version X is available (you have Y).
  Open Settings, Updates".

The `update` event is published by the proof through the supervisor's own /events route, with the fields the
weekly check (ns_supervisor/update_check.py) publishes. That proves the relay and the page; it does not prove
that the weekly check itself finds a newer release, which needs a fake api.github.com and an engine started
behind it. That last part ends as skipped. Ported from runs/qa/d1toast.py and runs/qa/fupcheck.py (evidence
log 2026-11-09, 0.1.14).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

PROOF = {
    "id": "f-d2-relay-event-kinds",
    "boxes": ["F-D2"],
    "install": "any",
    "window": True,
    "needs": ["engine"],
    "timeout_s": 300,
    "fakes": [],
}

LATEST = "99.0.0"
# the toast stack is one aria-live container (shell/Shell.tsx `Toasts`) with one child div per toast: reading
# the children one by one keeps a toast from being matched through the text of the others
TOAST_SELECTOR = "div[aria-live=polite].fixed > div"
LISTEN = """() => {
  window.__frames = [];
  window.__es = new EventSource('/api/events');
  window.__es.onmessage = (e) => {
    try { window.__frames.push(JSON.parse(e.data)); } catch (_) { /* not a frame */ }
  };
}"""


def _supervisor(ctx: Any, path: str, body: dict[str, Any]) -> tuple[int, Any]:
    """POST a JSON body to the supervisor (no proxy): (status, parsed answer). An error status is returned."""
    url = f"http://127.0.0.1:{ctx.ports['supervisor']}{path}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=15) as resp:
            status, raw = resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read().decode("utf-8", errors="replace")
    try:
        return status, json.loads(raw or "null")
    except ValueError:
        return status, raw


def _toasts(page: Any) -> list[str]:
    return [t.strip() for t in page.locator(TOAST_SELECTOR).all_inner_texts() if t.strip()]


def _frames(page: Any, kind: str) -> list[Any]:
    return [f for f in page.evaluate("window.__frames") if isinstance(f, dict) and f.get("kind") == kind]


def _expect_toast(ctx: Any, page: Any, before: set[str], text: str) -> str:
    def seen() -> str | None:
        return next((t for t in _toasts(page) if t not in before and text in t), None)

    return ctx.wait(seen, 30, every=0.3, what=f"the page to show a toast with: {text}")


def _clear_run_finished(ctx: Any) -> None:
    """Undo what POST /run_finished left in the supervisor.

    `run_finished_at` arms the post-run auto-exit (ns_supervisor/core.py `_check_auto_exit`): the engine would
    leave 60 s after the dashboard is closed. The flag is not in /status and has no route of its own; it is
    cleared by the first line of `acquire` ("somebody is working again"). An acquire of a service that does
    not exist clears it and then answers NS-E202 without starting or leasing anything.
    """
    status, answer = _supervisor(
        ctx, "/acquire", {"service": "proof-none", "owner": "proof-f-d2", "wait": False, "timeout_s": 1}
    )
    ctx.step(
        "state restored: run_finished_at cleared by an acquire of an unknown service (NS-E202)",
        status=status,
        answer=answer,
    )


def run(ctx: Any) -> None:
    posted: list[bool] = []  # filled the moment POST /run_finished is about to be sent
    try:
        _show_events(ctx, posted)
    finally:
        if posted:
            try:
                _clear_run_finished(ctx)
            except Exception as exc:  # the first error (if any) stays the result
                ctx.step("run_finished_at could not be cleared", reason=str(exc))
    ctx.skip(
        "the supervisor's own weekly update check (the real publisher of `update`) was not exercised: it "
        "asks api.github.com once a week, so it needs the 'github' fake endpoint (a TLS proxy for "
        "api.github.com with the QA CA), an engine started with HTTPS_PROXY / SSL_CERT_FILE pointing at it, "
        "and config/update_check.json aged so the first poll is due. The runner would start the engine "
        "that way (nsup with those variables) and run this proof against it. The relay and the page, "
        "shown above, are the part the box asks for"
    )


def _show_events(ctx: Any, posted: list[bool]) -> None:
    """The two events and their toasts; `posted` tells the caller to restore what /run_finished leaves."""
    page = ctx.page()
    page.goto(f"http://127.0.0.1:{ctx.ports['dashboard']}/#/queue", wait_until="load")
    page.evaluate(LISTEN)
    ctx.wait(
        lambda: page.evaluate("window.__frames.length") > 0,
        30,
        every=0.5,
        what="the dashboard stream to deliver a frame",
    )
    ctx.step("Queue page open, second EventSource on /api/events attached")

    before = set(_toasts(page))
    posted.append(True)
    status, answer = _supervisor(ctx, "/run_finished", {})
    ctx.step(
        "state left: run_finished_at set (arms the post-run auto-exit; restored at the end)",
    )
    ctx.check(
        status == 200 and isinstance(answer, dict) and answer.get("exit_scheduled") is False,
        "the supervisor took POST /run_finished and does not leave (the dashboard is open)",
        status=status,
        answer=answer,
    )
    toast = _expect_toast(ctx, page, before, "The nightly run has finished. See the Queue")
    runs = _frames(page, "run")
    ctx.check(
        bool(runs) and "finished" in json.dumps(runs[-1].get("payload")),
        "the stream carries the event as kind `run`, not `service`",
        frames=len(runs),
        payload=runs[-1].get("payload") if runs else None,
    )
    ctx.check(
        toast.startswith("The nightly run has finished"),
        "the page shows 'The nightly run has finished. See the Queue'",
        toast=toast,
    )
    page.screenshot(path=str(ctx.scratch / "run-finished-toast.png"))

    before = set(_toasts(page))
    data = {"current": ctx.version, "latest": LATEST, "update_available": True}
    status, answer = _supervisor(ctx, "/events", {"kind": "update", "data": data})
    ctx.check(
        status == 200 and answer == {"ok": True},
        "the supervisor took the `update` event",
        status=status,
        answer=answer,
    )
    toast = _expect_toast(ctx, page, before, f"Version {LATEST} is available")
    updates = _frames(page, "update")
    ctx.check(
        bool(updates) and LATEST in json.dumps(updates[-1].get("payload")),
        "the stream carries the event as kind `update`, not `service`",
        frames=len(updates),
        payload=updates[-1].get("payload") if updates else None,
    )
    ctx.check(
        toast.startswith(f"Version {LATEST} is available (you have {ctx.version}). Open Settings, Updates"),
        "the page shows 'Version X is available (you have Y). Open Settings, Updates'",
        toast=toast,
    )
    page.screenshot(path=str(ctx.scratch / "update-available-toast.png"))
    page.evaluate("window.__es.close()")
