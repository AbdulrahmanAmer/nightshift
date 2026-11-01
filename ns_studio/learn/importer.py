"""YouTube Studio analytics CSV import (spec section 18.9, component C7).

Columns are matched through a normalised alias table; the owner can override any mapping with
`column_map={"viewed_pct": "My column"}`. Rows are matched to outputs by recorded URL first, then
by exact title.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from ns_common.db import Db
from ns_studio.errors import StudioError

# canonical field -> accepted normalised headers (lower case, letters and digits only)
ALIASES: dict[str, tuple[str, ...]] = {
    "video_id": ("content", "contentid", "videoid"),
    "url": ("videourl", "url", "link", "videolink"),
    "title": ("videotitle", "title", "videoname"),
    "views": ("views",),
    "viewed_pct": ("viewedvsswipedaway", "stayedtowatch", "viewedpct", "viewedpercentage"),
    "avg_pct_viewed": (
        "averagepercentageviewed",
        "avgpercentageviewed",
        "averagepercentviewed",
        "averageviewedpercentage",
    ),
    "avg_view_duration_s": ("averageviewduration", "avgviewduration"),
    "likes": ("likes",),
    "comments": ("comments", "commentsadded"),
    "shares": ("shares",),
    "subs_gained": ("subscribers", "subscribersgained", "subscriberschange"),
}
_VIDEO_ID = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})")
_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass
class Row:
    """One analytics row with canonical fields (None when the column is absent or empty)."""

    video_id: str = ""
    url: str = ""
    title: str = ""
    views: int | None = None
    viewed_pct: float | None = None
    avg_pct_viewed: float | None = None
    avg_view_duration_s: float | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    subs_gained: int | None = None


@dataclass
class ImportResult:
    """Outcome of an import: matched output ids (with channel) and the rows that matched nothing."""

    matched: dict[str, str] = field(default_factory=dict)  # output_id -> channel
    unmatched: list[Row] = field(default_factory=list)
    date: str = ""


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.lower())


def _number(raw: str) -> float | None:
    text = raw.strip().replace(",", "").replace("%", "")
    if not text or text in ("-", "--"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_duration(raw: str) -> float | None:
    """Seconds from `ss`, `m:ss` or `h:mm:ss`; None when empty or malformed."""
    text = raw.strip()
    if not text:
        return None
    if ":" not in text:
        return _number(text)
    try:
        parts = [float(p) for p in text.split(":")]
    except ValueError:
        return None
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def video_id_of(url_or_id: str) -> str:
    """The 11-character video id inside a YouTube URL (or the id itself); '' when none."""
    text = url_or_id.strip()
    m = _VIDEO_ID.search(text)
    if m:
        return m.group(1)
    return text if _BARE_ID.match(text) else ""


def resolve_columns(header: list[str], column_map: dict[str, str] | None) -> dict[str, str]:
    """Map canonical field -> actual header text using overrides first, then the alias table."""
    by_norm = {_norm(h): h for h in header}
    out: dict[str, str] = {}
    for fld, aliases in ALIASES.items():
        wanted = (column_map or {}).get(fld)
        if wanted is not None:
            if wanted not in header:
                raise StudioError("NS-E323", f"mapped column {wanted!r} for {fld} not in the CSV header")
            out[fld] = wanted
            continue
        for a in aliases:
            if a in by_norm:
                out[fld] = by_norm[a]
                break
    return out


def read_csv(path: Path, column_map: dict[str, str] | None = None) -> list[Row]:
    """Parse a YouTube Studio export; the 'Total' row is dropped."""
    try:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            header = list(reader.fieldnames or [])
            raw_rows = list(reader)
    except (OSError, csv.Error, UnicodeDecodeError) as e:
        raise StudioError("NS-E323", f"{path}: {e}") from e
    cols = resolve_columns(header, column_map)
    if not ({"video_id", "url", "title"} & cols.keys()):
        raise StudioError("NS-E323", "no title, URL or content (video id) column found")
    if not ({"views", "viewed_pct", "avg_pct_viewed", "avg_view_duration_s"} & cols.keys()):
        raise StudioError("NS-E323", "no views or retention column found")

    def cell(raw: dict[str, str], fld: str) -> str:
        return (raw.get(cols[fld]) or "").strip() if fld in cols else ""

    def num(raw: dict[str, str], fld: str) -> float | None:
        return _number(cell(raw, fld))

    def integer(raw: dict[str, str], fld: str) -> int | None:
        v = num(raw, fld)
        return None if v is None else int(v)

    rows: list[Row] = []
    for raw in raw_rows:
        ident = [cell(raw, "video_id"), cell(raw, "title")]
        if any(i.lower() == "total" for i in ident):
            continue
        row = Row(
            video_id=cell(raw, "video_id"),
            url=cell(raw, "url"),
            title=cell(raw, "title"),
            views=integer(raw, "views"),
            viewed_pct=num(raw, "viewed_pct"),
            avg_pct_viewed=num(raw, "avg_pct_viewed"),
            avg_view_duration_s=parse_duration(cell(raw, "avg_view_duration_s")),
            likes=integer(raw, "likes"),
            comments=integer(raw, "comments"),
            shares=integer(raw, "shares"),
            subs_gained=integer(raw, "subs_gained"),
        )
        if row.video_id or row.url or row.title:
            rows.append(row)
    return rows


def match_rows(db: Db, rows: list[Row]) -> tuple[dict[str, Row], list[Row]]:
    """Match rows to `outputs.output_id`: recorded URL / video id first, then exact title."""
    uploads = db.query("select output_id, video_url from uploads")
    by_id: dict[str, str] = {}
    by_url: dict[str, str] = {}
    for u in uploads:
        by_url[u["video_url"].strip()] = u["output_id"]
        vid = video_id_of(u["video_url"])
        if vid:
            by_id[vid] = u["output_id"]
    titles: dict[str, list[str]] = {}
    for o in db.query("select output_id, title from outputs"):
        titles.setdefault(o["title"].strip(), []).append(o["output_id"])
    matched: dict[str, Row] = {}
    unmatched: list[Row] = []
    for row in rows:
        out_id = None
        ids = {video_id_of(row.video_id), video_id_of(row.url)} - {""}
        for vid in ids:
            out_id = out_id or by_id.get(vid)
        out_id = out_id or by_url.get(row.url)
        if out_id is None and row.title and len(titles.get(row.title, [])) == 1:
            out_id = titles[row.title][0]
        if out_id is None:
            unmatched.append(row)
        else:
            matched[out_id] = row
    return matched, unmatched


def import_rows(db: Db, rows: list[Row], date: str) -> ImportResult:
    """Store matched rows in `analytics` (replacing the same output and date); list the rest."""
    matched, unmatched = match_rows(db, rows)
    result = ImportResult(unmatched=unmatched, date=date)
    for out_id, r in matched.items():
        db.execute(
            "insert or replace into analytics(output_id, date, views, viewed_pct, avg_pct_viewed, "
            "avg_view_duration_s, likes, comments, shares, subs_gained) values (?,?,?,?,?,?,?,?,?,?)",
            (
                out_id,
                date,
                r.views,
                r.viewed_pct,
                r.avg_pct_viewed,
                r.avg_view_duration_s,
                r.likes,
                r.comments,
                r.shares,
                r.subs_gained,
            ),
        )
        ch = db.one("select channel from outputs where output_id=?", (out_id,))
        result.matched[out_id] = ch["channel"] if ch else ""
    return result
