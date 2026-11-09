"""F-C6: the Settings routes for the last export and the uninstall section.

GET /api/settings/export/latest answers 404 before an export in a dashboard session (checked on a freshly
spawned dashboard app, proofs/_fakes/dashboard_no_worker.py, so it does not depend on what the live dashboard
did before); after POST /api/settings/export on the live dashboard it downloads a zip of the size the export
reported (and that zip is a Nightshift backup); GET /api/settings/uninstall names app\\UNINSTALL.cmd and that
file exists. Ported from runs/qa/croutes/croutes.py box F-C6 (evidence log 2026-11-09 08:50, 0.1.12;
regression 14:44, 0.1.14).

The export writes Desktop\\nightshift-backup-YYYY-MM-DD.zip (ns_supervisor/backup.py). The proof skips at the
start when that file already exists: an owner's backup is never overwritten or deleted. The new zip is moved
into scratch before any check on it. It holds config/secrets.dpapi and studio.db, so after the manifest and
the entry names are read the scratch copy is deleted and the evidence keeps only names and sizes. A
DesktopGuard is open before the click, so a failed run still moves a backup the product wrote off the Desktop.

The export goes through the Settings page (Playwright): the click on Export in the "Export settings" row
sends POST /api/settings/export (the response is read), the row then says "Saved as <path>" and offers a
Download link to /api/settings/export/latest; the Uninstall row names the folder of
GET /api/settings/uninstall.
"""

from __future__ import annotations

import io
import json
import re
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from proofs._fakes import dashboard_no_worker
from proofs._lib import DesktopGuard, desktop, short

PROOF = {
    "id": "f-c6-settings-export-uninstall",
    "boxes": ["F-C6"],
    "install": "any",
    "window": True,
    "needs": ["engine"],
    "timeout_s": 300,
    "fakes": [],
}

# ns_supervisor/backup.py: what a backup holds
ALLOWED = ("config/", "models/tts/voices/", "exemplars/", "prompt_overrides/", "data/studio.db")
MANIFEST = "backup-manifest.json"
BACKUP_GLOB = "nightshift-backup-*.zip"
BLOCKED = (".gguf", ".safetensors", ".onnx", ".mp4", ".mkv", ".mov", ".webm", ".avi")
MAX_LISTED = 300  # entries named in the evidence
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(url: str, timeout: float = 120) -> tuple[int, dict[str, str], bytes]:
    """GET with no proxy: (status, headers, body bytes). Network errors are raised."""
    req = urllib.request.Request(url, method="GET")
    try:
        with _NO_PROXY.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _download(ctx: Any, path: str) -> tuple[int, dict[str, str], bytes]:
    """GET `path` from the live dashboard (ctx.api decodes a body as text, which spoils a zip)."""
    status, headers, body = _get(f"http://127.0.0.1:{ctx.ports['dashboard']}{path}")
    ctx.step(f"download GET {path}", status=status, bytes=len(body), content_type=headers.get("Content-Type"))
    return status, headers, body


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
            status, _, _ = _get(base + "/api/health", timeout=5)
        except OSError:
            return None
        return base if status == 200 else None

    base = ctx.wait(listening, 120, every=1, what="the fresh dashboard app to answer /api/health")
    ctx.step("fresh dashboard app started (its session has made no export)", url=base)
    return base


def _row(page: Any, title: str) -> Any:
    """The SettingRow whose title is `title`: the title <p> sits in a div inside the row div."""
    return page.locator("p").filter(has_text=re.compile(rf"^{re.escape(title)}$")).locator("xpath=../..")


def _flat(text: str) -> str:
    return " ".join(str(text).split())


def _row_says(row: Any, expected: str) -> str:
    """The row's whitespace-flattened text when it contains `expected`, else an empty string."""
    text = _flat(row.inner_text())
    return text if expected in text else ""


def _read_backup(raw: bytes) -> dict[str, Any] | None:
    """Names, sizes and manifest of a backup zip (never its contents); None when it is not a zip."""
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return None
    with z:
        names = z.namelist()
        manifest: Any = None
        if MANIFEST in names:
            try:
                manifest = json.loads(z.read(MANIFEST))
            except ValueError:
                manifest = None
        return {
            "names": names,
            "sizes": {i.filename: i.file_size for i in z.infolist()},
            "intact": z.testzip() is None,
            "manifest": manifest,
        }


def run(ctx: Any) -> None:
    target = desktop() / f"nightshift-backup-{datetime.now():%Y-%m-%d}.zip"
    if target.exists():
        ctx.skip(
            f"{target} already exists on the Desktop: the export would overwrite it and the proof never "
            "overwrites or deletes an owner's backup. Move it away and run again."
        )
    skips: list[str] = []

    # 1. before any export in a dashboard session: 404 not_found, on a dashboard app that has made none
    status, _, _ = _get(f"http://127.0.0.1:{ctx.ports['dashboard']}/api/settings/export/latest")
    ctx.step(
        "export/latest on the live dashboard (its session may already have made an export)", status=status
    )
    base = _fresh_dashboard(ctx)
    if base is None:
        skips.append(
            "export/latest as 404 before any export: no dashboard app could be started fresh "
            "(the runner needs the install's supervisor venv)"
        )
    else:
        status, _, body = _get(base + "/api/settings/export/latest")
        try:
            answer: Any = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            answer = None
        err = answer.get("error") if isinstance(answer, dict) else None
        code = err.get("code") if isinstance(err, dict) else None
        ctx.check(
            status == 404 and code == "not_found",
            "export/latest before any export (fresh dashboard app): 404 not_found",
            status=status,
            code=code,
        )

    # 2. export through the Settings page: the click sends POST /api/settings/export and the response is read
    page = ctx.page()
    page.goto(f"http://127.0.0.1:{ctx.ports['dashboard']}/#/settings", wait_until="load")
    export_row = _row(page, "Export settings")
    button = export_row.get_by_role("button", name="Export", exact=True)
    button.wait_for(timeout=30000)
    dl: tuple[int, dict[str, str], bytes] | None = None
    kept: Path | None = None
    raw = b""
    export: Any = None
    src: Path | None = None
    on_disk: int | None = None
    # the guard is open before the click: whatever the product writes to the Desktop leaves it on the way out,
    # pass or fail
    with DesktopGuard(ctx, BACKUP_GLOB, "f-c6") as guard:
        with page.expect_response("**/api/settings/export", timeout=600000) as seen:
            button.click()
        response = seen.value
        status = response.status
        try:
            export = response.json()
        except Exception:  # noqa: BLE001  (an error answer that is not JSON: the checks below name it)
            export = response.text()
        ctx.step("the Export click sent POST /api/settings/export", status=status, answer=short(export))
        reported = export.get("path") if status == 200 and isinstance(export, dict) else None
        src = Path(str(reported)) if reported else None
        on_disk = src.stat().st_size if src is not None and src.is_file() else None
        # the route serves the file from the Desktop, so the download comes first; then the zip leaves the
        # Desktop and only then is anything checked
        if on_disk is not None and src is not None:
            dl = _download(ctx, "/api/settings/export/latest")
            kept = guard.take(src)
            # the backup holds secrets.dpapi and studio.db: its scratch copy goes as soon as it is read
            try:
                raw = kept.read_bytes()
            finally:
                kept.unlink(missing_ok=True)
                ctx.step("scratch copy of the backup deleted", bytes_read=len(raw), still_there=kept.exists())
    parsed = _read_backup(raw)

    ctx.check(
        status == 200
        and isinstance(export, dict)
        and str(export.get("name", "")).startswith("nightshift-backup-")
        and str(export.get("name", "")).endswith(".zip")
        and export.get("download_url") == "/api/settings/export/latest",
        "POST /api/settings/export answers 200 with a nightshift-backup zip and its download URL",
        status=status,
        answer=short(export),
    )
    assert isinstance(export, dict)
    ctx.check(
        kept is not None and on_disk is not None and on_disk == export.get("size") == len(raw),
        "the reported zip existed on disk with the reported size",
        path=str(src),
        reported=export.get("size"),
        on_disk=on_disk,
    )

    # the row the click was in now names the file and offers the download
    saved = _flat(f"Saved as {export.get('path')}")
    row_text = ctx.wait(
        lambda: _row_says(export_row, saved),
        30,
        every=1,
        what="the Export settings row to say Saved as <path>",
    )
    ctx.check(
        saved in row_text,
        "the Export settings row says Saved as the reported path",
        row=row_text,
        expected=saved,
    )
    link = export_row.get_by_role("link", name="Download")
    link.wait_for(timeout=15000)
    href, download = link.get_attribute("href"), link.get_attribute("download")
    ctx.check(
        href == "/api/settings/export/latest" and download == export.get("name"),
        "the row has a Download link to /api/settings/export/latest named for the zip",
        href=href,
        download=download,
        zip_name=export.get("name"),
    )

    # 3. export/latest downloaded a zip of that size
    assert dl is not None
    dl_status, headers, body = dl
    ctx.check(
        dl_status == 200 and body[:2] == b"PK" and len(body) == export.get("size"),
        "export/latest downloads a zip (PK header) of the size the export reported",
        status=dl_status,
        content_type=headers.get("Content-Type"),
        downloaded=len(body),
        reported=export.get("size"),
    )
    ctx.check(
        "application/zip" in headers.get("Content-Type", ""),
        "export/latest is served as application/zip",
        content_type=headers.get("Content-Type"),
    )
    ctx.check(parsed is not None, "the backup is a zip", bytes=len(raw))
    assert parsed is not None
    names: list[str] = parsed["names"]
    manifest = parsed["manifest"]
    ctx.step(
        "backup entries (names and sizes only)",
        entries=len(names),
        listed=[[n, parsed["sizes"].get(n)] for n in names[:MAX_LISTED]],
    )
    ctx.check(
        parsed["intact"] and MANIFEST in names and isinstance(manifest, dict),
        "the download is a complete zip with its manifest",
        entries=len(names),
    )
    assert isinstance(manifest, dict)
    listed = manifest.get("entries")
    ctx.check(
        isinstance(listed, list)
        and manifest.get("version")
        and manifest.get("created")
        and sorted(listed) == sorted(n for n in names if n != MANIFEST),
        "the manifest lists exactly the files in the zip",
        version=manifest.get("version"),
        created=manifest.get("created"),
        listed=len(listed) if isinstance(listed, list) else None,
        in_zip=len(names) - 1,
    )
    stray = [n for n in names if n != MANIFEST and not n.startswith(ALLOWED)]
    blocked = [n for n in names if n.lower().endswith(BLOCKED)]
    ctx.check(
        not stray and not blocked,
        "the backup holds settings, voices, exemplars and the database only (no models, no videos)",
        stray=stray[:10],
        blocked=blocked[:10],
    )
    if (ctx.home / "data" / "studio.db").is_file():
        ctx.check("data/studio.db" in names, "the database is in the backup", entry="data/studio.db")

    # 4. the uninstall section: app\UNINSTALL.cmd, and it exists
    status, info = ctx.api("GET", "/api/settings/uninstall")
    ctx.check(
        status == 200 and isinstance(info, dict) and {"path", "exists"} <= set(info),
        "GET /api/settings/uninstall answers {path, exists}",
        status=status,
        answer=short(info),
    )
    uninstall = Path(str(info["path"]))
    ctx.check(
        uninstall.name == "UNINSTALL.cmd" and uninstall.parent.name.lower() == "app",
        "the path names app\\UNINSTALL.cmd",
        path=str(uninstall),
    )
    ctx.check(
        info["exists"] is True and uninstall.is_file(),
        "exists is true and the file is on disk",
        exists=info["exists"],
        is_file=uninstall.is_file(),
    )
    uninstall_dir = re.sub(r"[\/][^\/]*$", "", str(info["path"]))  # the page's own cut of the path
    expected_sub = _flat(f"Run UNINSTALL.cmd in {uninstall_dir}")
    uninstall_row = _row(page, "Uninstall")
    sub_text = ctx.wait(
        lambda: _row_says(uninstall_row, expected_sub),
        30,
        every=1,
        what="the Uninstall row to name the folder of UNINSTALL.cmd",
    )
    ctx.check(
        expected_sub in sub_text,
        "the Uninstall row says Run UNINSTALL.cmd in the folder of the path the route reports",
        row=sub_text,
        expected=expected_sub,
        path=str(uninstall),
    )
    try:
        state = json.loads((ctx.home / "config" / "install_state.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        state = {}
    if state.get("app_path"):
        ctx.check(
            str(uninstall).lower() == str(Path(state["app_path"]) / "UNINSTALL.cmd").lower(),
            "it is the install's own app folder (install_state.app_path)",
            app_path=state["app_path"],
            path=str(uninstall),
        )

    if skips:
        ctx.skip("; ".join(skips) + ". Every other check of the box passed.")
