"""Small helpers shared by the service modules (component C9)."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from ns_common.models import SLUG_RE
from ns_dashboard.errors import ApiError
from ns_studio.orchestrator import FORMAT_STAGES

OUTPUT_STATUSES = ("pending", "approved", "rejected", "uploaded")
OUTPUT_KINDS = ("long", "short", "clip")
KNOWN_FORMATS = ("master_first", "shorts_only", "series_compilation")


def offered_formats() -> tuple[str, ...]:
    """Video types a job can be started for: those with stages. `series_compilation` has none yet (a run
    would stop with NS-E306), so it is hidden until the orchestrator gives it stages."""
    return tuple(f for f in KNOWN_FORMATS if FORMAT_STAGES.get(f))


FORMATS = offered_formats()
_SLUG = re.compile(SLUG_RE)


def now_iso() -> str:
    """Current UTC time, ISO 8601."""
    return datetime.now(UTC).isoformat()


def require_slug(slug: str) -> str:
    """Return `slug` or raise 404 when it cannot be a channel slug (also blocks path tricks)."""
    if not _SLUG.match(slug or ""):
        raise ApiError(404, "not_found", "Unknown channel.")
    return slug


def output_urls(output_id: str) -> tuple[str, str]:
    """(media_url, thumb_url) for an output id; the `/` inside the id stays a path separator."""
    q = quote(output_id, safe="/")
    return f"/api/media/{q}", f"/api/media/{q}/thumb.jpg"


def output_item(row: sqlite3.Row) -> dict[str, Any]:
    """API shape `O` of an `outputs` row."""
    media, thumb = output_urls(row["output_id"])
    return {
        "output_id": row["output_id"],
        "job_id": row["job_id"],
        "channel": row["channel"],
        "kind": row["kind"],
        "k": row["k"],
        "title": row["title"],
        "duration_s": row["duration_s"],
        "status": row["status"],
        "review_note": row["review_note"],
        "source_id": row["source_id"],
        "combo_id": row["combo_id"],
        "created_at": row["created_at"],
        "media_url": media,
        "thumb_url": thumb,
    }
