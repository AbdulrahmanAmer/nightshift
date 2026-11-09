"""F-C5: notes, logs and diagnostics routes, and what the page shows of them.

- GET /api/logs/components and /api/logs/tail (component, level, n, after) against the files in logs/.
- GET /api/diagnostics/latest (404 before a bundle), POST /api/diagnostics, then the download.
- A morning note: `ns notify` (the product leaves a note in run/morning_note.json when no toast can show),
  GET /api/notes, the note in the bell (Playwright), POST /api/notes/clear, the bell again.
- Settings > Make report: the click sends POST /api/diagnostics and the page names the zip.

The note part needs the product's own no-desktop branch (session 0, as the nightly task runs). In an
interactive session `ns notify` shows a toast instead and leaves no note. The proof never writes the note file
itself: every other check runs, and when the product made no note the proof ends as SKIPPED naming the part
(a session-0 primitive is what the runner would need, for example a scheduled-task S4U run). The 404-first
part of diagnostics/latest is checked on a freshly spawned dashboard app
(proofs/_fakes/dashboard_no_worker.py), so it does not depend on proof order or on what the live dashboard's
session did before. The zips the product writes to the Desktop are moved into scratch before any check on
them. Ported from runs/qa/croutes (F-C5),
runs/qa/fc5.py and the Settings click (evidence log 2026-11-09 08:50, 09:54, 10:20, 14:44).
"""

from __future__ import annotations

import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from proofs._fakes import dashboard_no_worker
from proofs._lib import short, take_off_desktop

PROOF = {
    "id": "f-c5-notes-logs-diagnostics",
    "boxes": ["F-C5"],
    "install": "any",
    "window": True,
    "needs": ["engine"],
    "timeout_s": 600,
    "fakes": [],
}

LEVELS_ABOVE_INFO = {"INFO", "WARN", "WARNING", "ERROR"}
LEVELS_ABOVE_WARN = {"WARN", "WARNING", "ERROR"}
BUNDLE_PREFIX = "nightshift-diagnostics-"
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
BUNDLE_RE = re.compile(r"[A-Za-z]:[\\/][^\n]*?nightshift-diagnostics-[0-9-]+\.zip")


def _download(ctx: Any, path: str) -> tuple[int, dict[str, str], bytes]:
    """GET `path` from the dashboard with no proxy; the zip's bytes (ctx.api decodes a body as text)."""
    req = urllib.request.Request(f"http://127.0.0.1:{ctx.ports['dashboard']}{path}", method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=120) as resp:
            status, headers, body = resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        status, headers, body = exc.code, dict(exc.headers), exc.read()
    ctx.step(f"download GET {path}", status=status, bytes=len(body), content_type=headers.get("Content-Type"))
    return status, headers, body


def _tail(ctx: Any, **query: Any) -> list[dict[str, Any]]:
    status, lines = ctx.api("GET", "/api/logs/tail?" + urllib.parse.urlencode(query))
    ctx.check(
        status == 200 and isinstance(lines, list), f"GET /api/logs/tail {query} answers a list", status=status
    )
    return lines


def _logs(ctx: Any) -> None:
    """Components and tail against the log files."""
    on_disk = sorted((ctx.home / "logs").glob("*.jsonl"))
    ctx.check(
        bool(on_disk), "the install has component logs (logs/*.jsonl)", files=[p.name for p in on_disk][:12]
    )
    status, comps = ctx.api("GET", "/api/logs/components")
    ctx.check(
        status == 200 and isinstance(comps, list) and comps and comps == sorted(set(comps)),
        "GET /api/logs/components: a sorted list of component names",
        status=status,
        components=comps,
    )
    stems = [p.stem for p in on_disk]
    unbacked = [c for c in comps if not any(s == c or s.startswith(c + "-") for s in stems)]
    unlisted = [s for s in stems if not any(s == c or s.startswith(c + "-") for c in comps)]
    ctx.check(
        not unbacked and not unlisted,
        "every listed component has a log file and every log file has a listed component",
        components=comps,
        unbacked=unbacked,
        unlisted=unlisted,
    )

    default = _tail(ctx, n=50, level="", component="")
    stamps = [str(r.get("ts")) for r in default]
    ctx.check(
        bool(default)
        and len(default) <= 50
        and stamps == sorted(stamps)
        and all(r.get("level") and r.get("component") and r.get("event") for r in default),
        "logs/tail (the page's query, n=50): records with ts, level, component, event; oldest first",
        returned=len(default),
        first=stamps[0] if stamps else None,
        last=stamps[-1] if stamps else None,
    )
    raw = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in on_disk)
    ctx.check(
        stamps[-1] in raw,
        "the newest record returned is a line in the log files",
        ts=stamps[-1],
    )
    ctx.check(len(_tail(ctx, n=3)) <= 3, "n limits the answer", n=3)
    again = _tail(ctx, n=5, after=stamps[-1])
    ctx.check(
        all(str(r["ts"]) > stamps[-1] for r in again),
        "after returns only records newer than the timestamp",
        after=stamps[-1],
        returned=len(again),
    )

    if "dashboard" in comps:
        dash = _tail(ctx, n=20, level="info", component="dashboard")
        ctx.check(
            bool(dash)
            and len(dash) <= 20
            and all(r.get("component") == "dashboard" for r in dash)
            and all(str(r.get("level")).upper() in LEVELS_ABOVE_INFO for r in dash),
            "component=dashboard level=info: only dashboard records at info or above",
            returned=len(dash),
            levels=sorted({str(r.get("level")) for r in dash}),
        )
    warn = _tail(ctx, n=50, level="warn")
    ctx.check(
        all(str(r.get("level")).upper() in LEVELS_ABOVE_WARN for r in warn),
        "level=warn returns nothing below warn",
        returned=len(warn),
        levels=sorted({str(r.get("level")) for r in warn}),
    )


def _get(url: str, timeout: float = 30) -> tuple[int, Any]:
    """GET with no proxy: (status, parsed JSON or text). Network errors are raised."""
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with _NO_PROXY.open(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    text = raw.decode("utf-8", errors="replace")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def _log_text(path: Path, limit: int = 1500) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def _fresh_dashboard(ctx: Any) -> str | None:
    """A freshly spawned dashboard app (no job worker, nothing made in its session yet); its base URL.

    None when the install has no supervisor venv to start it with.
    """
    python = ctx.home / "venvs" / "supervisor" / "Scripts" / "python.exe"
    if not python.is_file():
        ctx.step("no dashboard app can be started here", python=str(python), exists=False)
        return None
    port_file = ctx.scratch / "dashboard-fresh.port"
    proc = ctx.spawn(
        [
            str(python),
            str(Path(dashboard_no_worker.__file__).resolve()),
            str(port_file),
            str(ctx.scratch / "tmp"),
        ],
        name="dashboard-fresh",
    )

    def listening() -> str | None:
        if proc.poll() is not None:
            ctx.check(
                False,
                "the fresh dashboard app started",
                exit_code=proc.returncode,
                log=_log_text(ctx.scratch / "dashboard-fresh.log"),
            )
        if not port_file.is_file():
            return None
        base = f"http://127.0.0.1:{port_file.read_text(encoding='ascii').strip()}"
        try:
            status, _ = _get(base + "/api/health", timeout=5)
        except OSError:
            return None
        return base if status == 200 else None

    base = ctx.wait(listening, 120, every=1, what="the fresh dashboard app to answer /api/health")
    ctx.step("fresh dashboard app started (its session has made no bundle)", url=base)
    return base


# ---------------------------------------------------------------------------- the Desktop is not ours
def _desktop() -> Path:
    """Where the product writes bundles: the user's Desktop (the same lookup the product uses)."""
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "Desktop")
            if isinstance(value, str) and value:
                return Path(value)
        except OSError:
            pass
    return Path.home() / "Desktop"


def _free_minute(ctx: Any) -> None:
    """The bundle's name carries the minute; the product would overwrite an owner's zip of this minute.
    Wait for the next minute rather than touch it."""

    def free() -> bool:
        return not (_desktop() / f"{BUNDLE_PREFIX}{datetime.now():%Y-%m-%d-%H%M}.zip").exists()

    if not free():
        ctx.step("a diagnostics zip of this minute is on the Desktop (not the proof's): waiting for the next")
        ctx.wait(free, 75, every=5, what="the minute of an existing diagnostics zip to pass")


def _take(ctx: Any, src: Path, tag: str) -> Path | None:
    """Move the product's file off the Desktop into scratch (fails if it stays on C:); None if absent."""
    if not src.is_file():
        ctx.step("no file at the path the product reported", path=str(src))
        return None
    return take_off_desktop(ctx, src, tag)


def _latest_is_404_first(ctx: Any, skips: list[str]) -> None:
    """diagnostics/latest before any bundle: 404 not_found, on a dashboard app that has made none."""
    status, _ = ctx.api("GET", "/api/diagnostics/latest")
    ctx.step(
        "diagnostics/latest on the live dashboard (its session may already have made a bundle)", status=status
    )
    base = _fresh_dashboard(ctx)
    if base is None:
        skips.append(
            "diagnostics/latest as 404 before any bundle: no dashboard app could be started fresh "
            "(the runner needs the install's supervisor venv)"
        )
        return
    status, answer = _get(base + "/api/diagnostics/latest")
    err = answer.get("error") if isinstance(answer, dict) else None
    code = err.get("code") if isinstance(err, dict) else None
    ctx.check(
        status == 404 and code == "not_found",
        "diagnostics/latest before any bundle (fresh dashboard app): 404 not_found",
        status=status,
        code=code,
    )


def _bundle(ctx: Any, skips: list[str]) -> bool:
    """POST /api/diagnostics, the download. False when the doctor cannot run on this install."""
    _free_minute(ctx)
    status, made = ctx.api("POST", "/api/diagnostics", timeout=600)
    if status == 501:
        skips.append(f"POST /api/diagnostics answered 501 ({short(made)}): the doctor cannot run here")
        return False
    reported = made.get("path") if status == 200 and isinstance(made, dict) else None
    src = Path(str(reported)) if reported else None
    # the route serves the file from the Desktop, so the download comes first; then the zip leaves the Desktop
    # and only then is anything checked
    dl: tuple[int, dict[str, str], bytes] | None = None
    kept: Path | None = None
    try:
        if src is not None and src.is_file():
            dl = _download(ctx, "/api/diagnostics/latest")
    finally:
        if src is not None:
            kept = _take(ctx, src, "api")
    ctx.check(
        status == 200
        and isinstance(made, dict)
        and str(made.get("name", "")).startswith(BUNDLE_PREFIX)
        and made.get("download_url") == "/api/diagnostics/latest",
        "POST /api/diagnostics answers 200 with the zip's name, size and download URL",
        status=status,
        answer=short(made),
    )
    ctx.check(dl is not None and kept is not None, "the reported zip existed on disk", path=str(src))
    assert dl is not None and isinstance(made, dict)
    dl_status, headers, body = dl
    ctx.check(
        dl_status == 200 and body[:2] == b"PK" and len(body) == made.get("size"),
        "diagnostics/latest downloads a zip of the size POST /api/diagnostics reported",
        status=dl_status,
        content_type=headers.get("Content-Type"),
        downloaded=len(body),
        reported=made.get("size"),
    )
    names = zipfile.ZipFile(io.BytesIO(body)).namelist()
    ctx.check(
        {"VERSION", "bundle-info.json", "nvidia-smi.txt"} <= set(names)
        and any(n.startswith("logs/") for n in names),
        "the bundle has VERSION, bundle-info.json, nvidia-smi.txt and the logs",
        entries=len(names),
        first=names[:6],
    )
    return True


def _note_file(ctx: Any) -> Path:
    return ctx.home / "run" / "morning_note.json"


def _bell_text(page: Any, base: str, settle_ms: int = 0) -> str:
    """Open the Queue page and the bell; the text of the Notifications panel."""
    page.goto(f"{base}/#/queue", wait_until="load")
    bell = page.get_by_role("button", name=re.compile(r"^Notifications", re.I)).first
    bell.wait_for(timeout=30000)
    bell.click()
    dialog = page.get_by_role("dialog", name="Notifications")
    dialog.wait_for(timeout=15000)
    if settle_ms:
        page.wait_for_timeout(settle_ms)
    return str(dialog.inner_text())


def _notes_and_bell(ctx: Any, skips: list[str]) -> None:
    """`ns notify`, then the dashboard side (notes, bell, clear) on the note the product made.

    The proof never writes the note. When `ns notify` left none (this session has a desktop, so it showed a
    toast), the note part is added to `skips` and nothing of it is claimed.
    """
    base = f"http://127.0.0.1:{ctx.ports['dashboard']}"
    path = _note_file(ctx)
    prior = path.read_bytes() if path.is_file() else None
    stamp = int(time.time())
    title, message = f"proof-F-C5 note {stamp}", "Left by the F-C5 proof for the bell."
    ctx.step(
        "notes before the proof",
        file_exists=prior is not None,
        note="a note file that was there is put back at the end" if prior else "none waiting",
    )
    try:
        done = ctx.cli(["ns", "notify", title, message], 60)
        ctx.check(done.returncode == 0, "ns notify exits 0", exit_code=done.returncode)

        def found() -> bool:
            status, notes = ctx.api("GET", "/api/notes")
            return status == 200 and isinstance(notes, list) and any(n.get("title") == title for n in notes)

        try:
            product_made = bool(ctx.wait(found, 6, every=1, what="the morning note from ns notify"))
        except Exception:  # noqa: BLE001  (no note: this session had a desktop and the toast was shown)
            product_made = False
        status, notes = ctx.api("GET", "/api/notes")
        ctx.check(
            status == 200 and isinstance(notes, list),
            "GET /api/notes answers a list",
            status=status,
            notes=len(notes) if isinstance(notes, list) else None,
        )
        if not product_made:
            ctx.step(
                "ns notify left no note: this session has a desktop and the product showed a toast",
                note="the proof does not write the note itself",
            )
            skips.append(
                "morning note written by `ns notify` in session 0 (GET /api/notes, the bell and "
                "POST /api/notes/clear on that note): this session is interactive, so the product showed a "
                "toast and left no note; the runner needs a session-0 primitive, e.g. a scheduled-task S4U "
                "run"
            )
            return
        ctx.step("ns notify left a morning note (no desktop or no toast)", file=str(path))

        mine = [n for n in notes if n.get("title") == title]
        ctx.check(
            len(mine) == 1 and mine[0].get("message") == message,
            "GET /api/notes lists the note with its title and message",
            notes=short(notes),
        )
        ctx.check(
            all(isinstance(n.get(k), str) for n in mine for k in ("at", "title", "message"))
            and bool(mine[0]["at"]),
            "the note carries at, title and message",
            note=mine[0],
        )
        datetime.fromisoformat(mine[0]["at"])

        page = ctx.page()
        text = _bell_text(page, base)
        ctx.check(
            title in text and message in text,
            "the bell panel shows the note's title and message",
            panel=text.replace("\n", " / ")[:400],
        )

        status, cleared = ctx.api("POST", "/api/notes/clear")
        ctx.check(
            status == 200 and cleared == {"ok": True},
            "POST /api/notes/clear answers {ok: true}",
            status=status,
            answer=short(cleared),
        )
        status, notes = ctx.api("GET", "/api/notes")
        ctx.check(
            status == 200 and notes == [] and not path.exists(),
            "notes are empty after clear and run/morning_note.json is gone",
            status=status,
            notes=short(notes),
            file_exists=path.exists(),
        )
        page.reload(wait_until="load")
        text = _bell_text(page, base, settle_ms=2500)
        ctx.check(
            title not in text,
            "after a reload the bell no longer lists the note",
            panel=text.replace("\n", " / ")[:400],
        )
    finally:
        if prior is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(prior)
            ctx.step("the note file that was waiting before the proof is back", file=str(path))
        else:
            path.unlink(missing_ok=True)


def _make_report(ctx: Any) -> None:
    """Settings > Make report: the page sends POST /api/diagnostics and names the zip."""
    _free_minute(ctx)
    page = ctx.page()
    page.goto(f"http://127.0.0.1:{ctx.ports['dashboard']}/#/settings", wait_until="load")
    button = page.get_by_role("button", name="Make report")
    button.wait_for(timeout=30000)
    button.click()

    def named() -> str | None:
        m = BUNDLE_RE.search(page.inner_text("body"))
        return m.group(0) if m else None

    shown = ctx.wait(named, 120, every=2, what="the Settings page to name the diagnostics zip")
    ctx.step("the page names the zip", text=shown)
    zip_path = Path(shown)
    ctx.check(zip_path.is_file(), "the zip the page names exists", path=shown)
    kept = _take(ctx, zip_path, "ui")  # off the Desktop before anything else is checked on it
    ctx.check(kept is not None, "the named zip was taken into scratch", path=shown)
    assert kept is not None
    raw = kept.read_bytes()
    names = zipfile.ZipFile(io.BytesIO(raw)).namelist()
    ctx.check(
        "VERSION" in names and "bundle-info.json" in names and raw[:2] == b"PK",
        "it is a diagnostics bundle (VERSION, bundle-info.json)",
        bytes=len(raw),
        entries=len(names),
    )


def run(ctx: Any) -> None:
    skips: list[str] = []
    _logs(ctx)
    _latest_is_404_first(ctx, skips)
    made = _bundle(ctx, skips)
    _notes_and_bell(ctx, skips)
    if made:
        _make_report(ctx)
    if skips:
        ctx.skip("; ".join(skips) + ". Every other check of the box passed.")
