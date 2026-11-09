"""Channel CRUD, pause/resume and validation on top of `ns_common.config` (component C9)."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

import yaml
from pydantic import ValidationError

from ns_common.config import list_channels, load_channel, save_channel
from ns_common.models import Channel, Voice
from ns_dashboard.channel_stats import channel_stats
from ns_dashboard.channels import validate_profile
from ns_dashboard.common import now_iso, require_slug
from ns_dashboard.errors import ApiError
from ns_dashboard.state import AppState
from ns_studio.channels.crud import unusable_formats
from ns_studio.media.job_formats import formats_in_use


class ChannelsService:
    """Channel profiles live in `config/channels/<slug>.yaml`; the `channels` table mirrors status."""

    def __init__(self, state: AppState) -> None:
        self.s = state

    # --- reading ---
    def load_all(self) -> list[Channel]:
        """Every readable profile; broken files are skipped and logged."""
        out: list[Channel] = []
        for slug in list_channels(self.s.paths):
            try:
                out.append(load_channel(self.s.paths, slug))
            except (OSError, ValueError, ValidationError, yaml.YAMLError) as e:
                self.s.log.warn("channel_unreadable", str(e), slug=slug)
        return out

    def load(self, slug: str) -> Channel:
        """One profile or 404."""
        require_slug(slug)
        try:
            return load_channel(self.s.paths, slug)
        except FileNotFoundError as e:
            raise ApiError(404, "channel_not_found", f"There is no channel called '{slug}'.") from e
        except (OSError, ValueError, ValidationError, yaml.YAMLError) as e:
            raise ApiError(500, "channel_unreadable", f"The profile for '{slug}' cannot be read: {e}") from e

    def exists(self, slug: str) -> bool:
        """True when the profile file exists."""
        return self.s.paths.channel_file(slug).exists()

    def _card(
        self,
        ch: Channel,
        counts: dict[tuple[str, str], int],
        last: dict[str, str],
        stats: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "slug": ch.slug,
            "display_name": ch.display_name,
            "status": ch.status,
            "niche": ch.niche,
            "language": ch.language,
            "format": ch.format,
            "voice": ch.voice.name or ch.voice.speaker or ch.voice.mode,
            "pending": counts.get((ch.slug, "pending"), 0),
            "approved": counts.get((ch.slug, "approved"), 0),
            "last_output_at": last.get(ch.slug),
            "stats": stats,
            "masters_per_week": ch.masters_per_week,
            "publish_window": ch.publish_window,
            "caption_styles": list(ch.pools.caption_styles),
            "formats_allowed": list(ch.formats_allowed),
        }

    def cards(self) -> list[dict[str, Any]]:
        """Channel cards with output counts and the `stats` object (week, retention, QC pass rate)."""
        counts = {
            (r["channel"], r["status"]): int(r["n"])
            for r in self.s.db.query(
                "SELECT channel, status, COUNT(*) AS n FROM outputs GROUP BY channel, status"
            )
        }
        last = {
            r["channel"]: r["last"]
            for r in self.s.db.query("SELECT channel, MAX(created_at) AS last FROM outputs GROUP BY channel")
        }
        loaded = self.load_all()
        stats = channel_stats(self.s.paths, self.s.db, loaded)
        return [self._card(ch, counts, last, stats.get(ch.slug)) for ch in loaded]

    def get(self, slug: str) -> dict[str, Any]:
        """Profile plus warnings, plus the format each kind of video uses (`formats_in_use`, decision D1)."""
        ch = self.load(slug)
        check = validate_profile(ch.model_dump(mode="json"), self.load_all(), ch.voice)
        return {
            "profile": ch.model_dump(mode="json"),
            **_flat_warnings(check["warnings"]),
            "formats_in_use": formats_in_use(ch, self.s.paths),
        }

    def formats_in_use(self, slug: str) -> dict[str, Any]:
        """The format a new job of this channel draws each kind of video from, and why (Head decision D1).

        `{"short": {...}, "long": {...}}`; each entry has `id`, `name`, `why` ("first allowed of that kind",
        "series design", "default ..."), `source` (shipped, edited, yours) and `applied` (False for a kind
        whose renders do not draw from the format yet). A problem is reported in the entry, never raised.
        """
        return formats_in_use(self.load(slug), self.s.paths)

    # --- writing ---
    def _mirror(self, ch: Channel) -> None:
        self.s.db.execute(
            "INSERT INTO channels(slug, display_name, created_at, status) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(slug) DO UPDATE SET display_name = excluded.display_name, status = excluded.status",
            (ch.slug, ch.display_name, ch.created_at or now_iso(), ch.status),
        )

    def _format_problems(
        self, listed: Collection[str], saved: Collection[str]
    ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """([(id, why)] new in this save, [(id, why)] already saved) for the listed formats nothing can use.

        An id is no use when its file is missing or does not load, or the studio would refuse it (the check a
        series design and the Formats editor use: `ns_studio.media.formats.kind_problems`). The ids the saved
        profile already listed are kept: an old channel whose format was deleted must stay editable.
        """
        return unusable_formats(listed, saved, paths=self.s.paths)

    def _checked(
        self,
        body: dict[str, Any],
        slug: str | None = None,
        before: Voice | None = None,
        saved_formats: Collection[str] = (),
    ) -> tuple[Channel, list[dict[str, str]]]:
        others = [c for c in self.load_all() if c.slug != (slug or body.get("slug"))]
        check = validate_profile(body, others, before)
        if not check["ok"]:
            raise ApiError(
                422, "invalid_profile", "Some channel settings need fixing.", details=check["errors"]
            )
        ch = Channel.model_validate(body)
        new, old = self._format_problems(ch.formats_allowed, saved_formats)
        if new:
            raise ApiError(
                422,
                "NS-E320",
                "These allowed formats cannot be used: " + ", ".join(fid for fid, _why in new) + ".",
                hint="Fix them in Library > Formats, or take them out of the list.",
                details=[
                    {"field": "formats_allowed", "message": f"{fid}: {why}", "code": "NS-E320"}
                    for fid, why in new
                ],
            )
        warnings = list(check["warnings"])
        warnings += [
            {
                "field": "formats_allowed",
                "message": f"{fid} is still listed but cannot be used ({why}); jobs skip it.",
            }
            for fid, why in old
        ]
        return ch, warnings

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        """New channel; 409 when the slug is taken."""
        ch, warnings = self.check_create(body)
        ch = ch.model_copy(update={"created_at": ch.created_at or now_iso()})
        save_channel(self.s.paths, ch)
        self._mirror(ch)
        self.s.log.info("channel_created", slug=ch.slug)
        return {"profile": ch.model_dump(mode="json"), **_flat_warnings(warnings)}

    def check_create(self, body: dict[str, Any]) -> tuple[Channel, list[dict[str, str]]]:
        """The channel `create` would save, or the ApiError it would raise (nothing is written)."""
        slug = str(body.get("slug", ""))
        if slug and self.exists(slug):
            raise ApiError(409, "channel_exists", f"A channel called '{slug}' already exists.")
        return self._checked(body)

    def check_update(self, slug: str, body: dict[str, Any]) -> tuple[Channel, list[dict[str, str]]]:
        """The channel `update` would save, or the ApiError it would raise (nothing is written)."""
        old = self.load(slug)
        if body.get("slug", slug) != slug:
            raise ApiError(422, "invalid_profile", "The channel id cannot be changed.",
                           details=[{"field": "slug", "message": "The channel id cannot be changed."}])
        draft = {**body, "slug": slug, "created_at": body.get("created_at") or old.created_at}
        return self._checked(draft, slug, old.voice, old.formats_allowed)

    def update(self, slug: str, body: dict[str, Any]) -> dict[str, Any]:
        """Replace a profile; the slug cannot change."""
        ch, warnings = self.check_update(slug, body)
        save_channel(self.s.paths, ch)
        self._mirror(ch)
        self.s.log.info("channel_updated", slug=slug)
        return {"profile": ch.model_dump(mode="json"), **_flat_warnings(warnings)}

    def _set_status(self, slug: str, status: str, kind: str, note: str | None) -> dict[str, Any]:
        ch = self.load(slug).model_copy(update={"status": status})
        save_channel(self.s.paths, ch)
        with self.s.db.tx() as db:
            self._mirror(ch)
            db.execute(
                "INSERT INTO incidents(channel, ts, kind, note) VALUES (?, ?, ?, ?)",
                (slug, now_iso(), kind, (note or "").strip()[:500] or None),
            )
        self.s.log.info(f"channel_{kind}", slug=slug)
        return {"profile": ch.model_dump(mode="json")}

    def pause(self, slug: str, note: str | None = None) -> dict[str, Any]:
        """Stop producing for a channel (YouTube warning, strike, owner's choice)."""
        return self._set_status(slug, "paused", "pause", note)

    def resume(self, slug: str, note: str | None = None) -> dict[str, Any]:
        """Start producing again."""
        return self._set_status(slug, "active", "resume", note)

    def validate(self, slug: str, draft: dict[str, Any] | None) -> dict[str, Any]:
        """`{ok, errors, warnings}` for a draft, or for the saved profile when no draft is sent."""
        require_slug(slug)
        saved = self.load(slug) if (not draft or self.exists(slug)) else None  # a draft may be a new channel
        raw = draft or (saved.model_dump(mode="json") if saved else {})
        raw = {**raw, "slug": raw.get("slug") or slug}
        others = [c for c in self.load_all() if c.slug != raw["slug"]]
        check = validate_profile(raw, others, saved.voice if saved else None)
        errors, warnings = list(check["errors"]), list(check["warnings"])
        listed = [f for f in raw.get("formats_allowed") or [] if isinstance(f, str)]
        new, old = self._format_problems(listed, saved.formats_allowed if saved else ())
        errors += [{"field": "formats_allowed", "message": f"{fid}: {why}"} for fid, why in new]
        warnings += [
            {"field": "formats_allowed", "message": f"{fid} is still listed but cannot be used ({why})."}
            for fid, why in old
        ]
        return {
            "ok": check["ok"] and not new,
            "errors": [e["message"] for e in errors],
            "warnings": [w["message"] for w in warnings],
            "error_fields": errors,
            "warning_fields": warnings,
        }


def _flat_warnings(warnings: list[dict[str, str]]) -> dict[str, Any]:
    """`warnings` as plain strings plus the field-tagged originals under `warning_fields`."""
    return {"warnings": [w["message"] for w in warnings], "warning_fields": warnings}
