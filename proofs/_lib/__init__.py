"""Shared helpers for the proof scripts in proofs/ (QA). Standard library only; nothing from the product.

The runner (dev/prove.py) skips this folder when it discovers proofs. Every helper takes the proof's `ctx` and
records what it observed through `ctx.step` / `ctx.check`, so the evidence carries it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # noqa: TID251  (proof helpers measure files with FFmpeg, like the tests and dev/ do)
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

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


def proof_channel(ctx: Any, slug: str, **overrides: Any) -> str:
    """Make sure the proof's own channel exists (created through POST /api/channels when missing).

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
    body.update({"slug": slug, "display_name": slug.replace("-", " ").title(), "voice": dict(PRESET_VOICE)})
    body.update(overrides)
    status, answer = ctx.api("POST", "/api/channels", body)
    ctx.check(
        status in (200, 201),
        "proof channel created through POST /api/channels",
        channel=slug,
        status=status,
        answer=_short(answer),
    )
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


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def ebur128(ctx: Any, path: Path) -> dict[str, float | None]:
    """Integrated loudness, true peak and LRA of `path`, measured by FFmpeg's ebur128 filter (independent of
    the product's own report)."""
    exe = ffmpeg_path()
    if not exe:
        ctx.skip("FFmpeg is not on PATH (hold the laptop lock, or install it)")
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
