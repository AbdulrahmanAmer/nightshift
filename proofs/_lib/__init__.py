"""Shared helpers for the proof scripts in proofs/ (QA). Standard library only, except `ffmpeg_path`, which
asks the product (`ns_common.ffmpeg`) where it finds FFmpeg, so a proof cannot look somewhere narrower.

The runner (dev/prove.py) skips this folder when it discovers proofs. Every helper takes the proof's `ctx` and
records what it observed through `ctx.step` / `ctx.check`, so the evidence carries it.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess  # noqa: TID251  (proof helpers measure files with FFmpeg, like the tests and dev/ do)
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

_lookup_lock = threading.Lock()
FINAL = ("done", "failed", "cancelled")
# The voice every proof channel speaks with: a preset of the voice engine (ns_tts/voices.py PRESETS).
PRESET_VOICE = {"mode": "preset", "speaker": "Ryan", "description": None, "name": None, "speed": 1.0}


def db_rows(ctx: Any, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """`ctx.db` rows as plain tuples: comparable with `==` and readable in the evidence."""
    return [tuple(r) for r in ctx.db(sql, params)]


def wait_job(ctx: Any, job_id: str, timeout: float = 600, every: float = 3) -> dict[str, Any]:
    """Poll GET /api/jobs/{id} until the job ends; returns the last answer (recorded as a step)."""
    t0 = time.monotonic()

    def ended() -> dict[str, Any] | None:
        status, job = ctx.api("GET", f"/api/jobs/{job_id}")
        if status == 200 and isinstance(job, dict) and job.get("status") in FINAL:
            return job
        return None

    job = ctx.wait(ended, timeout, every=every, what=f"job {job_id} to end")
    ctx.step(
        "job ended",
        job_id=job_id,
        status=job.get("status"),
        message=job.get("message"),
        seconds=round(time.monotonic() - t0, 1),
    )
    return job


def api_no_deadline(
    ctx: Any, method: str, path: str, body: Any = None, timeout: float = 60
) -> tuple[int, Any]:
    """A dashboard call that is not bound by the proof's deadline: for cleanup in a finally, which must still
    run after the proof has used up its time (ctx.api refuses then)."""
    url = f"http://127.0.0.1:{ctx.ports['dashboard']}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as err:
        raw, status = err.read(), err.code
    try:
        return status, json.loads(raw or b"null")
    except ValueError:
        return status, raw.decode("utf-8", errors="replace")[:400]


_OPENED: dict[int, list[str]] = {}  # id(ctx) -> the proof channels this run made active


def pauses_proof_channels(run: Callable[[Any], None]) -> Callable[[Any], None]:
    """Decorate a proof's `run`: every proof channel it made active is paused again at the end, pass or fail,
    through calls with no deadline, so the nightly never makes videos on a proof channel."""

    @functools.wraps(run)
    def wrapper(ctx: Any) -> None:
        try:
            run(ctx)
        finally:
            for slug in _OPENED.pop(id(ctx), []):
                try:
                    status, answer = api_no_deadline(ctx, "POST", f"/api/channels/{slug}/pause")
                    ctx.step(
                        "cleanup: proof channel paused", channel=slug, status=status, answer=_short(answer)
                    )
                except Exception as exc:  # noqa: BLE001  (a cleanup error must not hide the proof's own result)
                    ctx.step("cleanup: proof channel pause failed", channel=slug, error=str(exc))

    return wrapper


def _activate(ctx: Any, slug: str, status_now: str | None) -> None:
    """Resume a paused proof channel (a manual run on a paused channel is refused with 409) and remember it,
    so `pauses_proof_channels` pauses it again."""
    opened = _OPENED.setdefault(id(ctx), [])
    if slug not in opened:
        opened.append(slug)
    if status_now == "paused":
        status, answer = ctx.api("POST", f"/api/channels/{slug}/resume")
        ctx.check(
            status == 200,
            "proof channel resumed for this run",
            channel=slug,
            status=status,
            answer=_short(answer),
        )


def proof_channel(ctx: Any, slug: str, **overrides: Any) -> str:
    """Make sure the proof's own channel exists (created through POST /api/channels if missing) and is active
    for this run. The proof's `run` must be decorated with `pauses_proof_channels`.

    The body is the one the dev install's own channels use, taken from an existing channel with the fields
    `overrides` changes. Returns the slug. The channel's name starts with `proof-` so it is plainly a test's.
    """
    assert slug.startswith("proof-"), slug
    status, existing = ctx.api("GET", f"/api/channels/{slug}")
    if status == 200 and isinstance(existing, dict):
        ctx.step("proof channel exists", channel=slug)
        profile = dict(existing.get("profile") or {})
        overrides = {"voice": dict(PRESET_VOICE), **overrides}
        if any(profile.get(k) != v for k, v in overrides.items()):
            profile.update(overrides)
            status, body = ctx.api("PUT", f"/api/channels/{slug}", profile)
            ctx.check(
                status == 200,
                "proof channel updated through PUT /api/channels/{slug}",
                channel=slug,
                changed=sorted(overrides),
                status=status,
                answer=_short(body),
            )
        _activate(ctx, slug, str(profile.get("status") or ""))
        return slug
    status, channels = ctx.api("GET", "/api/channels")
    ctx.check(
        status == 200 and isinstance(channels, list) and bool(channels),
        "the install has a channel to copy",
        status=status,
    )
    src = next(
        c.get("slug") for c in channels if isinstance(c, dict) and not str(c.get("slug")).startswith("proof-")
    )
    status, full = ctx.api("GET", f"/api/channels/{src}")
    ctx.check(
        status == 200 and isinstance(full, dict) and "profile" in full,
        "template channel read",
        channel=src,
        status=status,
    )
    body = {k: v for k, v in full["profile"].items() if k not in ("created_at", "updated_at")}
    # A preset voice, never the template's: the template may carry the setup's designed voice, which a proof
    # must not use or consume (it may still be pending, and F-E12 needs it).
    body.update(
        {
            "slug": slug,
            "display_name": slug.replace("-", " ").title(),
            "voice": dict(PRESET_VOICE),
            "status": "active",
        }
    )
    body.update(overrides)
    status, answer = ctx.api("POST", "/api/channels", body)
    ctx.check(
        status in (200, 201),
        "proof channel created through POST /api/channels",
        channel=slug,
        status=status,
        answer=_short(answer),
    )
    _activate(ctx, slug, "active")
    return slug


def run_short(ctx: Any, channel: str, timeout: float = 900, **extra: Any) -> dict[str, Any]:
    """Start one shorts_only job on `channel` through POST /api/jobs/run and wait for it to end."""
    body = {"channel": channel, "format": "shorts_only", "count": 1, **extra}
    status, job = ctx.api("POST", "/api/jobs/run", body)
    ctx.check(
        status in (200, 201, 202) and isinstance(job, dict) and job.get("job_id"),
        "job accepted by POST /api/jobs/run",
        status=status,
        answer=_short(job),
    )
    return wait_job(ctx, job["job_id"], timeout=timeout)


DIAG_PREFIX = (
    "nightshift-diagnostics-"  # ns_supervisor/doctor.py: Desktop\nightshift-diagnostics-<minute>.zip
)


def desktop() -> Path:
    """Where the product writes bundles and backups: the user's Desktop (the lookup the product uses)."""
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
            ) as key:
                value, _ = winreg.QueryValueEx(key, "Desktop")
            if isinstance(value, str) and value:
                return Path(value)
        except OSError:
            pass
    return Path.home() / "Desktop"


def free_diag_minute(ctx: Any) -> None:
    """A diagnostics zip is named by the minute; the product would overwrite an owner's zip of this minute.
    Wait for the next minute rather than touch it."""

    def free() -> bool:
        return not (desktop() / f"{DIAG_PREFIX}{datetime.now():%Y-%m-%d-%H%M}.zip").exists()

    if not free():
        ctx.step("a diagnostics zip of this minute is on the Desktop (not the proof's): waiting for the next")
        ctx.wait(free, 75, every=5, what="the minute of an existing diagnostics zip to pass")


def take_off_desktop(ctx: Any, src: Path, tag: str) -> Path:
    """Move a file the product just wrote to the Desktop (C:) into scratch, before any check on it.

    Fails the proof when the file cannot be moved away: a proof never passes with its file left on C:."""
    ctx.check(src.is_file(), "the product wrote the file it reported", path=str(src))
    kept = ctx.scratch / f"{tag}-{src.name}"
    error = ""
    for _ in range(5):
        try:
            shutil.move(str(src), str(kept))
        except OSError as exc:  # OneDrive or antivirus may hold a new file for a moment
            error = str(exc)
        if not src.exists():
            break
        time.sleep(1)
    ctx.check(
        not src.exists() and kept.is_file(),
        "the file moved off the Desktop into scratch",
        was=str(src),
        now=str(kept),
        error=error,
    )
    return kept


class DesktopGuard:
    """Around a POST or a click that makes the product write a zip to the Desktop (C:):

        with DesktopGuard(ctx, "nightshift-diagnostics-*.zip", "api") as guard:
            status, made = ctx.api("POST", "/api/diagnostics")
            kept = guard.take(Path(made["path"]))

    On the way out, pass or fail, every matching file that was not on the Desktop before moves into scratch,
    so no failed check, timeout or crash leaves a product file on C:. Files that were there before are never
    touched."""

    def __init__(self, ctx: Any, pattern: str, tag: str) -> None:
        self.ctx, self.pattern, self.tag = ctx, pattern, tag
        self.before: set[str] = set()
        self.taken: dict[str, Path] = {}

    def __enter__(self) -> DesktopGuard:
        self.before = {p.name for p in desktop().glob(self.pattern)}
        return self

    def take(self, src: Path) -> Path:
        if src.name in self.before:
            self.ctx.check(False, "the product's file is new, not an owner's file", path=str(src))
        if src.name not in self.taken:
            self.taken[src.name] = take_off_desktop(self.ctx, src, self.tag)
        return self.taken[src.name]

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        left = [
            p for p in desktop().glob(self.pattern) if p.name not in self.before and p.name not in self.taken
        ]
        problems = []
        for p in left:
            try:
                self.take(p)
            except Exception as err:  # noqa: BLE001  (a ProofFailed here must not hide the first error)
                problems.append(f"{p}: {err}")
        if left:
            self.ctx.step(
                "Desktop guard: product files moved off the Desktop on the way out",
                files=[str(p) for p in left],
                problems=problems,
            )
        if problems and exc_type is None:
            self.ctx.check(False, "every product file left the Desktop", problems=problems)
        return False


def ffmpeg_path(home: Path) -> str | None:
    """The FFmpeg the product would use on this install: `ns_common.ffmpeg`'s own lookup (the `NS_FFMPEG`
    override, then PATH, then `<home>/bin/ffmpeg`), with `home` as the install. The proof used `shutil.which`
    alone, which does not see an FFmpeg the product finds (D-43: the repo test venv's PATH has no FFmpeg)."""
    from ns_common import ffmpeg as product

    with _lookup_lock:  # the product reads the install from NIGHTSHIFT_HOME: set it for the lookup only
        before = os.environ.get("NIGHTSHIFT_HOME")
        os.environ["NIGHTSHIFT_HOME"] = str(home)
        try:
            return product._find("ffmpeg", "NS_FFMPEG")
        finally:
            if before is None:
                os.environ.pop("NIGHTSHIFT_HOME", None)
            else:
                os.environ["NIGHTSHIFT_HOME"] = before


def ebur128(ctx: Any, path: Path) -> dict[str, float | None]:
    """Integrated loudness, true peak and LRA of `path`, measured by FFmpeg's ebur128 filter (independent of
    the product's own report)."""
    exe = ffmpeg_path(ctx.home)
    if not exe:
        ctx.skip("FFmpeg not found (not on PATH, not named by NS_FFMPEG, not under <home>/bin/ffmpeg)")
    proc = subprocess.run(
        [
            str(exe),
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    tail = proc.stderr[proc.stderr.rfind("Summary:") :]
    i = re.search(r"I:\s+(-?[\d.]+) LUFS", tail)
    tp = re.search(r"Peak:\s+(-?[\d.]+) dBFS", tail[tail.find("True peak") :])
    lra = re.search(r"LRA:\s+(-?[\d.]+) LU", tail)
    out = {
        "I_LUFS": float(i.group(1)) if i else None,
        "TP_dBTP": float(tp.group(1)) if tp else None,
        "LRA": float(lra.group(1)) if lra else None,
    }
    ctx.step("ebur128 measured", file=str(path), **out)
    return out


def files_under(root: Path, contains: str = "") -> list[str]:
    """Relative paths of the files under `root` (optionally only those whose path contains `contains`)."""
    if not root.exists():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and contains in str(p))


def _short(value: Any, limit: int = 400) -> Any:
    """A JSON value cut down for the evidence."""
    text = json.dumps(value, default=str)
    return value if len(text) <= limit else text[:limit] + "..."


short = _short
