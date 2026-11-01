"""Format presets as data (spec 17.1): `presets/formats/<id>.yaml` loaded into `ns_common.models.Format`.

Adding a format never touches Python: drop a YAML file next to the shipped ones. `validate_all` checks every
file (model, id = file name, regions inside the canvas, caption styles that exist); `formats_allowed` and
`ensure_allowed` apply a channel's list of permitted formats.
"""

from __future__ import annotations

from collections.abc import Collection
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import ValidationError

from ns_common.models import Channel, Format
from ns_common.paths import Paths
from ns_studio.errors import StudioError
from ns_studio.media.channel_opts import channel_setting
from ns_studio.media.presets import PRESETS_DIR, load_caption_styles

FORMATS_DIR = PRESETS_DIR / "formats"
# Kinds a series design may pick (clip layouts are chosen per clip by the clipper).
DESIGN_KINDS = ("short", "long", "square")


def _load_file(path: Path) -> Format:
    try:
        fmt = Format.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as e:
        raise StudioError("NS-E320", f"formats/{path.name}: {str(e)[:300]}") from e
    if fmt.id != path.stem:
        raise StudioError("NS-E320", f"formats/{path.name}: id {fmt.id!r} must equal the file name")
    return fmt


@lru_cache(maxsize=8)
def _load_dir(directory: str) -> dict[str, Format]:
    d = Path(directory)
    files = sorted(d.glob("*.yaml")) if d.is_dir() else []
    return {p.stem: _load_file(p) for p in files}


def load_formats(directory: Path | None = None) -> dict[str, Format]:
    """Every preset in `presets/formats/` (or `directory`) keyed by id; NS-E320 when a file is invalid."""
    return dict(_load_dir(str(directory or FORMATS_DIR)))


def get_format(format_id: str, directory: Path | None = None) -> Format:
    """One preset by id; NS-E320 naming the known ids when it does not exist."""
    formats = load_formats(directory)
    if format_id not in formats:
        raise StudioError("NS-E320", f"unknown format {format_id!r}; known formats: {sorted(formats)}")
    return formats[format_id]


def format_ids(*, kinds: Collection[str] | None = None, directory: Path | None = None) -> list[str]:
    """Sorted preset ids, optionally only those of the given `kind`s."""
    return sorted(i for i, f in load_formats(directory).items() if kinds is None or f.kind in kinds)


def design_formats(directory: Path | None = None) -> list[str]:
    """Ids a series design can choose from: short, long and square presets (not per-clip layouts)."""
    return format_ids(kinds=DESIGN_KINDS, directory=directory)


def format_problems(fmt: Format, known_styles: Collection[str]) -> list[str]:
    """Rule violations of one preset beyond what the model checks (empty list when fine)."""
    out: list[str] = []
    names = [r.name for r in fmt.layout.regions]
    if len(set(names)) != len(names):
        out.append("region names must be unique")
    for r in fmt.layout.regions:
        if r.x + r.w > fmt.canvas.w or r.y + r.h > fmt.canvas.h:
            out.append(
                f"region {r.name!r} ({r.x},{r.y} {r.w}x{r.h}) leaves the {fmt.canvas.w}x{fmt.canvas.h} canvas"
            )
    if fmt.duration.min_s > fmt.duration.max_s:
        out.append("duration.min_s is larger than duration.max_s")
    if fmt.shots is not None and fmt.shots.min_s > fmt.shots.max_s:
        out.append("shots.min_s is larger than shots.max_s")
    if fmt.safe_margins.top + fmt.safe_margins.bottom >= fmt.canvas.h:
        out.append("safe margins leave no room for captions")
    unknown = [s for s in fmt.captions.styles_allowed if s not in known_styles]
    if unknown:
        out.append(f"unknown caption styles {unknown}")
    if fmt.layout.seam_captions and len(fmt.layout.regions) < 2:
        out.append("seam_captions needs at least two regions")
    return out


def validate_all(directory: Path | None = None) -> list[str]:
    """Check every preset; returns the sorted ids, raises NS-E320 listing all problems otherwise."""
    d = directory or FORMATS_DIR
    styles = set(load_caption_styles())
    problems: list[str] = []
    ids: list[str] = []
    for path in sorted(d.glob("*.yaml")) if d.is_dir() else []:
        try:
            fmt = _load_file(path)
        except StudioError as e:
            problems.append(e.detail)
            continue
        ids.append(fmt.id)
        problems += [f"formats/{path.name}: {p}" for p in format_problems(fmt, styles)]
    if not ids and not problems:
        problems.append(f"no format presets found in {d}")
    if problems:
        raise StudioError("NS-E320", "; ".join(problems))
    return ids


# ---------- channel option `formats_allowed` ----------
def formats_allowed(paths: Paths, channel: Channel) -> list[str] | None:
    """The channel's `formats_allowed` list; None (every format allowed) when it has none or an empty one."""
    raw = channel_setting(paths, channel, "formats_allowed", None)
    if raw is None or raw == []:
        return None
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise StudioError("NS-E320", f"{channel.slug}: formats_allowed must be a list of format ids")
    return list(raw)


def allowed_ids(
    allowed: Collection[str] | None, *, kinds: Collection[str] | None = None, directory: Path | None = None
) -> list[str]:
    """Preset ids (optionally of the given kinds) that `allowed` permits; None permits all."""
    return [i for i in format_ids(kinds=kinds, directory=directory) if allowed is None or i in allowed]


def ensure_allowed(format_id: str, allowed: Collection[str] | None, *, channel: str = "") -> None:
    """NS-E320 with a plain message when `format_id` is not in the channel's `formats_allowed`."""
    if allowed is None or format_id in allowed:
        return
    who = f"channel {channel!r}" if channel else "this channel"
    raise StudioError(
        "NS-E320",
        f"format {format_id!r} is not allowed for {who}; allowed formats: {sorted(allowed) or 'none'}. "
        "Add it to formats_allowed in the channel profile or choose one of the allowed formats.",
    )
