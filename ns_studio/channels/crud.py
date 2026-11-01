"""Channel profile CRUD with the spec section 8 pool minimums and distinctness rules (component C7)."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ns_common.config import list_channels, load_channel, save_channel
from ns_common.db import Db
from ns_common.models import Channel, Pools
from ns_common.paths import Paths
from ns_studio.errors import StudioError

MAX_CHANNELS = 3
MIN_HOOKS = 3
MIN_STRUCTURES = 3
MIN_PAIRS = 9
MIN_CAPTION_STYLES = 2
MIN_VISUAL_TREATMENTS = 2
WARN_BELOW = 4

_POOL_FIELDS = (
    "hooks",
    "structures",
    "pov_tones",
    "pacings",
    "caption_styles",
    "visual_treatments",
    "title_formulas",
)


def pool_problems(pools: Pools) -> list[str]:
    """Hard violations of the pool minimums (spec section 8); empty list when the pools are valid."""
    problems: list[str] = []
    for name in _POOL_FIELDS:
        options: list[str] = getattr(pools, name)
        if not options:
            problems.append(f"pools.{name} is empty")
        if len(set(options)) != len(options):
            problems.append(f"pools.{name} has duplicate entries")
    if len(pools.hooks) < MIN_HOOKS:
        problems.append(f"need at least {MIN_HOOKS} hooks (have {len(pools.hooks)})")
    if len(pools.structures) < MIN_STRUCTURES:
        problems.append(f"need at least {MIN_STRUCTURES} structures (have {len(pools.structures)})")
    pairs = len(pools.hooks) * len(pools.structures)
    if pairs < MIN_PAIRS:
        problems.append(f"hook x structure pairs must be at least {MIN_PAIRS} (have {pairs})")
    if len(pools.caption_styles) < MIN_CAPTION_STYLES:
        problems.append(
            f"need at least {MIN_CAPTION_STYLES} caption styles (have {len(pools.caption_styles)})"
        )
    if len(pools.visual_treatments) < MIN_VISUAL_TREATMENTS:
        problems.append(
            f"need at least {MIN_VISUAL_TREATMENTS} visual treatments (have {len(pools.visual_treatments)})"
        )
    return problems


def pool_warnings(pools: Pools) -> list[str]:
    """Soft warnings: fewer than 4 hooks or 4 structures (spec section 8 'warn below 4x4')."""
    out: list[str] = []
    if len(pools.hooks) < WARN_BELOW:
        out.append(f"fewer than {WARN_BELOW} hooks: videos will look repetitive sooner")
    if len(pools.structures) < WARN_BELOW:
        out.append(f"fewer than {WARN_BELOW} structures: videos will look repetitive sooner")
    return out


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _voice_key(ch: Channel) -> tuple[str, str, str, str]:
    v = ch.voice
    return (v.mode, _norm(v.speaker or ""), _norm(v.name or ""), _norm(v.description or ""))


def distinct_problems(new: Channel, others: list[Channel]) -> list[str]:
    """Violations of cross-channel distinctness: same niche, voice, caption set and visual set."""
    out: list[str] = []
    for o in others:
        if o.slug == new.slug:
            out.append(f"slug {new.slug!r} already exists")
            continue
        same = (
            _norm(o.niche) == _norm(new.niche)
            and _voice_key(o) == _voice_key(new)
            and set(o.pools.caption_styles) == set(new.pools.caption_styles)
            and set(o.pools.visual_treatments) == set(new.pools.visual_treatments)
        )
        if same:
            out.append(
                f"channel {o.slug!r} has the same niche, voice, caption styles and visual treatments"
            )
    return out


def validate_channel(ch: Channel, others: list[Channel]) -> tuple[list[str], list[str]]:
    """Return `(problems, warnings)` for `ch` against the other configured channels."""
    return pool_problems(ch.pools) + distinct_problems(ch, others), pool_warnings(ch.pools)


def all_channels(paths: Paths) -> list[Channel]:
    """Load every channel profile found in the channels directory, sorted by slug."""
    out: list[Channel] = []
    for slug in list_channels(paths):
        try:
            out.append(load_channel(paths, slug))
        except (ValidationError, ValueError, OSError) as e:
            raise StudioError("NS-E320", f"{slug}.yaml: {e}") from e
    return out


def get_channel(paths: Paths, slug: str) -> Channel:
    """Load one channel; `NS-E322` when it does not exist."""
    if not paths.channel_file(slug).exists():
        raise StudioError("NS-E322", slug)
    try:
        return load_channel(paths, slug)
    except (ValidationError, ValueError) as e:
        raise StudioError("NS-E320", f"{slug}.yaml: {e}") from e


def sync_channel_row(db: Db, ch: Channel, now: datetime | None = None) -> None:
    """Upsert the `channels` table row (display name and status) for `ch`."""
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    db.execute(
        "insert into channels(slug, display_name, created_at, status) values (?,?,?,?) "
        "on conflict(slug) do update set display_name=excluded.display_name, status=excluded.status",
        (ch.slug, ch.display_name, ch.created_at or stamp, ch.status),
    )


def add_channel(paths: Paths, db: Db, ch: Channel, *, now: datetime | None = None) -> Path:
    """Validate and persist a new channel; raises StudioError NS-E320 or NS-E321."""
    now = now or datetime.now(UTC)
    others = all_channels(paths)
    if len(others) >= MAX_CHANNELS:
        raise StudioError("NS-E320", f"at most {MAX_CHANNELS} channels per install")
    problems = pool_problems(ch.pools)
    if problems:
        raise StudioError("NS-E320", "; ".join(problems))
    clash = distinct_problems(ch, others)
    if clash:
        code = "NS-E320" if any("already exists" in c for c in clash) else "NS-E321"
        raise StudioError(code, "; ".join(clash))
    stamped = ch.model_copy(update={"created_at": ch.created_at or now.isoformat(timespec="seconds")})
    f = save_channel(paths, stamped)
    sync_channel_row(db, stamped, now)
    return f


def _set_dotted(data: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    cur: Any = data
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            raise StudioError("NS-E320", f"unknown field {key!r}")
        cur = cur[p]
    if not isinstance(cur, dict) or parts[-1] not in cur:
        raise StudioError("NS-E320", f"unknown field {key!r}")
    cur[parts[-1]] = value


def edit_channel(paths: Paths, db: Db, slug: str, updates: dict[str, Any]) -> Channel:
    """Apply dotted-key `updates` (for example `pools.hooks`), validate, and save the profile."""
    current = get_channel(paths, slug)
    data = copy.deepcopy(current.model_dump(mode="json"))
    for key, value in updates.items():
        _set_dotted(data, key, value)
    try:
        edited = Channel.model_validate(data)
    except ValidationError as e:
        raise StudioError("NS-E320", str(e).replace("\n", " ")) from e
    if edited.slug != slug:
        raise StudioError("NS-E320", "the slug cannot be changed")
    problems = pool_problems(edited.pools) + distinct_problems(
        edited, [c for c in all_channels(paths) if c.slug != slug]
    )
    if problems:
        raise StudioError("NS-E320", "; ".join(problems))
    save_channel(paths, edited)
    sync_channel_row(db, edited)
    return edited


def load_channel_yaml(path: Path) -> Channel:
    """Parse a channel YAML file given on the command line (`channel add --from-yaml`)."""
    try:
        return Channel.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (ValidationError, OSError, yaml.YAMLError) as e:
        raise StudioError("NS-E320", f"{path}: {e}") from e
